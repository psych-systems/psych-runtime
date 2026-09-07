"""An answer split from the work behind it, over real Runs.

The point of the split is that a person asking "where is order A1"
reads one sentence rather than four turns of tool calls. The point of deriving
it, rather than asking the model to classify its own question, is that the
answer cannot drift from what the Run actually did.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.records import TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import publish as make_version
from psych_runtime.model.prompt import system_prompt
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    tools = ToolRegistry()

    @tools.register(annotations={"read-only"})
    async def lookup_order(order_id: str) -> dict[str, str]:
        """Look up an order."""
        return {"order_id": order_id, "status": "shipped"}

    @tools.register(annotations={"read-only"})
    async def track(order_id: str) -> dict[str, str]:
        """Track a shipment."""
        return {"order_id": order_id, "eta": "Thursday"}

    yield tools


def _spec(**overrides: object) -> psych_runtime.AgentSpec:
    base: dict[str, object] = {
        "name": "support",
        "instructions": "Help the customer.",
        "model": psych_runtime.ModelRef(model="fake-standard"),
        "tools": (
            psych_runtime.CodeTool(name="lookup_order"),
            psych_runtime.CodeTool(name="track"),
        ),
        "limits": psych_runtime.Limits(max_turns=8, deadline_seconds=60),
    }
    base.update(overrides)
    return psych_runtime.AgentSpec(**base)  # type: ignore[arg-type]


async def _run(
    spec: psych_runtime.AgentSpec, model: FakeModel, tools: ToolRegistry
) -> tuple[InMemoryStore, psych_runtime.Dispatched]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    runtime = Runtime(store=store, model=model, registry=tools)
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "where is A1?"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return store, run


class TestTheSplit:
    async def test_the_work_collapses_and_the_answer_stands_alone(
        self, registry: ToolRegistry
    ) -> None:
        model = (
            FakeModel()
            .turn(text="Let me check.", tool_calls=[("lookup_order", {"order_id": "A1"})])
            .turn(tool_calls=[("track", {"order_id": "A1"})])
            .turn(text="A1 shipped and arrives Thursday.")
        )
        store, run = await _run(_spec(), model, registry)

        view = await psych_runtime.answer(store, run.run_id)

        assert view.text == "A1 shipped and arrives Thursday."
        assert view.finished is True
        # Two working turns, and the answer is not one of them.
        assert len(view.work) == 2
        assert all("Thursday." not in turn.text for turn in view.work)
        # The narration the model gave on the way is kept: when it is there it
        # is the most useful line in the collapsed section.
        assert view.work[0].text == "Let me check."
        assert view.tool_call_count == 2
        assert view.summary() == "2 turns, 2 tool calls"

        # And the work carries what a reader opening it needs.
        call = view.work[0].tool_calls[0]
        assert call.tool == "lookup_order"
        assert call.arguments == {"order_id": "A1"}
        assert call.result == {"order_id": "A1", "status": "shipped"}
        assert call.outcome is not None
        assert call.outcome.value == "ok"
        assert call.duration_seconds is not None

    async def test_a_one_turn_run_has_no_work_at_all(self, registry: ToolRegistry) -> None:
        """ "hi" is the case the model-classifies-it design gets wrong. Here
        there are no earlier turns, so there is nothing to collapse and no
        empty box where a section would be."""
        model = FakeModel().turn(text="Hello. How can I help?")
        store, run = await _run(_spec(), model, registry)

        view = await psych_runtime.answer(store, run.run_id)

        assert view.text == "Hello. How can I help?"
        assert view.work == ()
        assert view.summary() == ""
        assert view.finished is True

    async def test_a_run_with_no_answer_yet_says_so_rather_than_inventing_one(
        self, registry: ToolRegistry
    ) -> None:
        """A Run that never reached a finishing turn has no answer. Showing an
        empty string as though it were one would be a lie; ``finished`` is how
        a caller tells the two apart and knows to show the failure instead."""
        model = FakeModel()
        for _ in range(9):
            model = model.turn(tool_calls=[("lookup_order", {"order_id": "A1"})])
        spec = _spec(limits=psych_runtime.Limits(max_turns=3, deadline_seconds=60))
        store, run = await _run(spec, model, registry)

        state = await psych_runtime.status(store, run.run_id)
        assert state.terminal_state is TerminalState.FAILED

        view = await psych_runtime.answer(store, run.run_id)
        assert view.finished is False
        assert view.text == ""
        assert view.work, "the turns it did take are still the work it did"
        # The reason lives on the status, already written for a person.
        assert state.failure_message

    async def test_the_flat_transcript_is_unchanged(self, registry: ToolRegistry) -> None:
        """Both views project the same log, so a consumer who wants the whole
        conversation in order still gets exactly what they always did."""
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup_order", {"order_id": "A1"})])
            .turn(text="A1 shipped.")
        )
        store, run = await _run(_spec(), model, registry)

        view = await psych_runtime.answer(store, run.run_id)
        conversation = await psych_runtime.thread(store, run.run_id)

        assert view.text == "A1 shipped."
        roles = [message.role for message in conversation.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert conversation.messages[-1].content == "A1 shipped."


class TestAnswerStyle:
    """The half that changes the prompt, and therefore the Version."""

    def test_an_unset_style_assembles_byte_identically(self) -> None:
        """The guarantee that nothing already published changes behaviour."""
        spec = _spec()
        assert spec.answer_style is None
        assert system_prompt(spec).render() == "Help the customer."

    def test_setting_a_style_adds_the_instruction(self) -> None:
        rendered = system_prompt(_spec(answer_style="concise")).render()
        assert "Lead with the answer" in rendered
        # Asks for structure when it helps, never "always use a table": an
        # agent that formats "yes" as a table is worse than one that does not
        # format at all.
        assert "when you are presenting more than" in rendered
        assert "always" not in rendered.lower()

    def test_the_style_sits_in_the_cacheable_prefix(self) -> None:
        """It comes from the Spec and cannot change while a Run is in flight,
        so it belongs above everything that can (DESIGN.md §19)."""
        sections = system_prompt(_spec(answer_style="concise"))
        assert "Lead with the answer" in sections.stable_prefix

    def test_the_style_moves_the_version_hash(self) -> None:
        """Deliberate, and the opposite call from an MCP server description.

        A response style is part of what this agent is and changes only when
        someone changes the agent. A server description is a fact about an
        external system that moves on its own schedule, which is why that one
        is kept out of the Spec.
        """
        plain = make_version(_spec()).hash
        concise = make_version(_spec(answer_style="concise")).hash
        assert plain != concise

    async def test_the_instruction_reaches_the_model(self, registry: ToolRegistry) -> None:
        model = FakeModel().turn(text="A1 shipped.")
        store, run = await _run(_spec(answer_style="concise"), model, registry)

        sent = model.requests[0].messages[0]
        assert "Lead with the answer" in sent.content
        # And it is in the log, so a trace can prove what was asked for.
        report = await psych_runtime.report(store, run.run_id)
        assert "Lead with the answer" in report.system_prompt
