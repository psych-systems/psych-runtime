"""A Run that stops to ask, and a person who answers it.

`SuspendReason.QUESTION` and `SuspensionPolicy.question_expires_seconds`
have existed since the suspension machinery was built, and nothing could reach
either: only `APPROVAL` was ever raised. The reducer's own `resume_payloads`
docstring says it holds "a human's answer to an `ask_user` question", which is
the shape this completes.

What these assert is the round trip: the Run parks, the question is readable
while it waits, an answer delivered through `psych_runtime.resume(payload=...)` comes
back as that tool call's result, and the model reads it like any other result
on its next turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.records import SuspendReason, TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import SuspensionPolicy
from psych_runtime.core.version import publish as make_version
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    yield ToolRegistry()


def _spec(*, may_ask: bool = True) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        suspension=SuspensionPolicy(may_ask_questions=may_ask),
        limits=psych_runtime.Limits(max_turns=6, deadline_seconds=60),
    )


class _Session:
    """A store and a Worker that can be started and stopped repeatedly.

    A suspended Run needs the Worker to stop, an answer to arrive, and a Worker
    to pick it up again. Reusing one store across that is the whole point: the
    answer reaches a different Attempt from the one that asked, which is the
    property `resume_payloads` exists to give.
    """

    def __init__(self, model: FakeModel, registry: ToolRegistry) -> None:
        self.store = InMemoryStore()
        self.runtime = Runtime(store=self.store, model=model, registry=registry)

    async def drain(self) -> None:
        worker = psych_runtime.Worker(
            self.store, self.runtime, poll_interval=0.01, supervisor_interval=0.05
        )
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.35)
        worker.stop()
        await asyncio.wait_for(task, timeout=10)


class TestAskingAndAnswering:
    async def test_the_run_parks_and_the_question_is_readable(self, registry: ToolRegistry) -> None:
        model = FakeModel().turn(
            tool_calls=[
                (
                    "ask_question",
                    {
                        "questions": [
                            {
                                "question": "Which order number?",
                                "header": "Order",
                                "options": [
                                    {"label": "A1", "description": "The one from Tuesday"},
                                    {"label": "A2", "description": "The one from Friday"},
                                ],
                            }
                        ]
                    },
                )
            ]
        )
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec())
        run = await psych_runtime.dispatch(
            session.store, version.hash, SCOPE, input={"message": "refund"}
        )

        await session.drain()

        state = await psych_runtime.status(session.store, run.run_id)
        assert state.lifecycle == "waiting"
        assert state.suspend_reason is SuspendReason.QUESTION
        # The question is the model's own words, not a wrapper's.
        # Both shapes: the one-line rendering a notification would use, and
        # the structure a console renders as clickable options.
        assert state.pending_question is not None
        assert state.pending_question.summary == "Which order number?"
        asked = state.pending_question.questions[0]
        assert asked.header == "Order"
        assert [option.label for option in asked.options] == ["A1", "A2"]
        assert asked.options[0].description == "The one from Tuesday"
        assert state.pending_approval is None, "a question is not an approval"
        assert state.terminal_state is None

    async def test_the_answer_becomes_the_tool_result(self, registry: ToolRegistry) -> None:
        """The round trip. The answer arrives on a different Attempt from the
        one that asked, and the model reads it as an ordinary tool result."""
        model = (
            FakeModel()
            .turn(
                tool_calls=[("ask_question", {"questions": [{"question": "Which order number?"}]})]
            )
            .turn(text="Thanks. Refunding A1 now.")
        )
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec())
        run = await psych_runtime.dispatch(
            session.store, version.hash, SCOPE, input={"message": "refund"}
        )
        await session.drain()

        await psych_runtime.resume(
            session.store, run.run_id, payload={"answer": "A1"}, by="mrutyunjay"
        )
        await session.drain()

        report = await psych_runtime.report(session.store, run.run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        asked = next(call for call in report.tool_calls if call.tool == "ask_question")
        assert asked.outcome is not None
        assert asked.outcome.value == "ok"
        assert asked.result == "A1"

        # And the model actually saw it, in the conversation it was sent.
        second_turn = model.requests[1]
        assert any("A1" in message.content for message in second_turn.messages)

    async def test_an_empty_answer_says_so_rather_than_returning_nothing(
        self, registry: ToolRegistry
    ) -> None:
        """A model handed `""` reads it as "they said nothing", which is a
        different thing from "nobody was asked" and leads it to invent one.
        Saying it in words lets the model decide what to do next."""
        model = (
            FakeModel()
            .turn(tool_calls=[("ask_question", {"questions": [{"question": "Which order?"}]})])
            .turn(text="I could not proceed without an order number.")
        )
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec())
        run = await psych_runtime.dispatch(
            session.store, version.hash, SCOPE, input={"message": "refund"}
        )
        await session.drain()

        await psych_runtime.resume(session.store, run.run_id, payload={"answer": "   "})
        await session.drain()

        report = await psych_runtime.report(session.store, run.run_id)
        asked = next(call for call in report.tool_calls if call.tool == "ask_question")
        assert asked.result is not None
        assert "did not give an answer" in str(asked.result)
        assert report.terminal_state is TerminalState.COMPLETED

    async def test_asking_nothing_is_a_tool_error_and_interrupts_nobody(
        self, registry: ToolRegistry
    ) -> None:
        """A malformed call must not park a Run: there is nothing for a person
        to answer, and a conversation stopped for a question nobody was asked
        is the worst of both."""
        model = (
            FakeModel()
            .turn(tool_calls=[("ask_question", {"questions": []})])
            .turn(text="Sorry, what is your order number?")
        )
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec())
        run = await psych_runtime.dispatch(
            session.store, version.hash, SCOPE, input={"message": "refund"}
        )

        await session.drain()

        report = await psych_runtime.report(session.store, run.run_id)
        assert report.terminal_state is TerminalState.COMPLETED, "it never suspended"
        asked = next(call for call in report.tool_calls if call.tool == "ask_question")
        assert asked.outcome is not None
        assert asked.outcome.value == "error"

    async def test_a_reclaimed_run_does_not_ask_twice(self, registry: ToolRegistry) -> None:
        """A Run answered and then crashed before recording the result replays
        into the branch that reads the answer, not the one that asks. Draining
        twice after one answer is that replay."""
        model = (
            FakeModel()
            .turn(tool_calls=[("ask_question", {"questions": [{"question": "Which order?"}]})])
            .turn(text="Refunding A1.")
        )
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec())
        run = await psych_runtime.dispatch(
            session.store, version.hash, SCOPE, input={"message": "refund"}
        )
        await session.drain()
        await psych_runtime.resume(session.store, run.run_id, payload={"answer": "A1"})
        await session.drain()
        await session.drain()

        report = await psych_runtime.report(session.store, run.run_id)
        asked = [call for call in report.tool_calls if call.tool == "ask_question"]
        assert len(asked) == 1
        assert len(report.suspensions) == 1


class TestWhoIsOfferedIt:
    async def test_an_agent_that_did_not_ask_for_it_is_not_offered_it(
        self, registry: ToolRegistry
    ) -> None:
        """Off by default. An agent running unattended must not be able to park
        a Run for a day because nobody said it could."""
        model = FakeModel().turn(text="Done.")
        session = _Session(model, registry)
        version = await psych_runtime.publish(session.store, _spec(may_ask=False))
        await psych_runtime.dispatch(session.store, version.hash, SCOPE, input={"message": "hi"})

        await session.drain()

        offered = {tool.name for tool in model.requests[0].tools}
        assert "ask_question" not in offered

    async def test_allowing_questions_moves_the_version_hash(self) -> None:
        """Whether an agent may stop and wait is part of what that agent is, so
        it is pinned with everything else about it (DESIGN.md §4)."""
        quiet = make_version(_spec(may_ask=False)).hash
        asking = make_version(_spec(may_ask=True)).hash

        assert quiet != asking
