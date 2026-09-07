"""End to end: the guidance in ``psych_runtime.tools.guidance`` actually reaches the
model, through a real ``AgentLoop`` run and the real conversation projection.

Every assertion here reads ``model.requests[n].messages``, the same
way ``tests/e2e/test_agent_run.py`` does, rather than calling
``psych_runtime.core.conversation._result_text`` or ``psych_runtime.tools.guidance`` directly:
the point of an e2e case is that the whole pipeline (``AgentLoop`` records the
outcome, ``psych_runtime.core.conversation`` projects it, ``psych_runtime.model.prompt``
assembles it into a request) produces the guided text, not just that the text
function itself returns something reasonable in isolation.

``psych_runtime.runtime.agent`` is unmodified in this change (see this ticket's final
reply for the edit it needs to fully adopt ``psych_runtime.tools.guidance``), so every
scenario here is one the *current* ``AgentLoop`` already produces correctly:
a raised exception, a malformed call, an unknown tool, a large result, and a
crash-recovered dangling call. What each test proves is what
``psych_runtime.core.conversation``'s new dispatch does with what ``AgentLoop``
already writes, and, where relevant, that the wording matches
``psych_runtime.tools.guidance``'s canonical copy exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from psych_runtime.core.ids import RunId, new_run_id
from psych_runtime.core.records import TerminalState, ToolCallFinished, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef
from psych_runtime.core.version import publish
from psych_runtime.runtime.agent import AgentLoop, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel, ToolCallScript
from psych_runtime.tools.guidance import (
    ABORTED_GUIDANCE,
    UNKNOWN_OUTCOME_GUIDANCE,
    elided_result_guidance,
)
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


def build_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> dict[str, str]:
        """Look up an order by id."""
        if order_id == "MISSING":
            raise ValueError("no such order")
        return {"order_id": order_id, "status": "shipped"}

    @registry.register
    def big(rows: int) -> str:
        """Return a lot of text."""
        return "x" * rows

    return registry


async def start_run(store: InMemoryStore, spec: AgentSpec, message: str = "where is A1?") -> RunId:
    version = publish(spec)
    await store.put_version(version)
    run_id = new_run_id()
    now = datetime.now(UTC)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=SCOPE,
            version_hash=version.hash,
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=now + timedelta(seconds=spec.limits.deadline_seconds),
        )
    )
    await store.append(
        run_id,
        1,
        _record(
            "run_admitted",
            run_id=run_id,
            seq=1,
            version_hash=version.hash,
            input={"message": message},
            deadline_at=now + timedelta(seconds=spec.limits.deadline_seconds),
        ),
    )
    return run_id


def _record(kind: str, **fields: Any) -> Any:
    from psych_runtime.core.records import RECORD_ADAPTER

    return RECORD_ADAPTER.validate_python(
        {"type": kind, "at": datetime.now(UTC), "scope": SCOPE, **fields}
    )


async def make_loop(
    store: InMemoryStore, run_id: RunId, spec: AgentSpec, model: FakeModel, registry: ToolRegistry
) -> AgentLoop:
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    return AgentLoop(journal, spec, model, ToolResolver(registry), ToolExecutor(registry))


def _tool_message_content(model: FakeModel, request_index: int) -> str:
    request = model.requests[request_index]
    tool_message = next(m for m in request.messages if m.role == "tool")
    return tool_message.content


class TestErrorGuidanceReachesTheModel:
    """A raised exception (``kind`` is the exception's own class name, which
    is not a bespoke :class:`~psych_runtime.tools.guidance.FailureKind`) still reaches
    the model as a clean message plus traceback, with none of the old
    ``f"{kind}: {message}"`` noise ``_result_text`` used to prepend."""

    async def test_the_kind_is_not_prefixed_onto_the_message_any_more(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup", {"order_id": "MISSING"})])
            .turn(text="I could not find that order.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        content = _tool_message_content(model, 1)
        assert "no such order" in content
        assert not content.startswith("ValueError:")

    async def test_a_host_tools_traceback_is_recorded_but_not_shown_to_the_model(self) -> None:
        """DESIGN.md §18 asks for a *sandboxed program's* traceback to reach
        the model, because the model wrote that program. A host tool's
        traceback is the consumer's own code: their paths, their module names,
        and whatever the exception message embedded (an asyncpg error carries
        the DSN, a botocore error the bucket). It belongs in the log, where an
        operator reads it, not in the prompt, the provider's logs, and one
        prompt injection away from a user."""
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "MISSING"})]).turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        content = _tool_message_content(model, 1)
        state = reduce(await store.read(run_id))
        failure = state.tool_results[0].failure
        assert failure is not None
        # Recorded in full...
        assert failure.traceback is not None
        assert "Traceback" in failure.traceback
        # ...and absent from what the model was sent.
        assert failure.traceback not in content
        assert "Traceback (most recent call last)" not in content

    async def test_the_guidance_advice_is_actually_present(self) -> None:
        """``AgentLoop`` routes ``ToolFailure.message`` through
        ``failure_guidance`` itself, rather than the ad-hoc ``str(err)`` this
        file's other tests were written against before that wiring landed.

        The double-traceback regression this also used to guard cannot happen
        now: ``psych_runtime.core.conversation`` appends a traceback only when the
        writer marked it as the model's to read, and a host tool's never is
        (see the test above).
        """
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "MISSING"})]).turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        content = _tool_message_content(model, 1)
        state = reduce(await store.read(run_id))
        failure = state.tool_results[0].failure
        assert failure is not None
        assert failure.traceback is not None

        # The generic fallback's non-transient advice, proving the message was
        # actually built by failure_guidance and not left as bare str(err).
        assert "no such order" in content
        assert "unlikely to succeed" in content.lower()

        # And the consumer's traceback is not in it at all.
        assert content.count(failure.traceback) == 0

    async def test_a_malformed_call_still_tells_the_model_what_to_send(self) -> None:
        """malformed_arguments is a pass-through FailureKind: agent.py's own
        message already carries the full guidance, so it must reach the model
        unchanged rather than a generic wrapper diluting it."""
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(tool_calls=[ToolCallScript(name="lookup", raw_arguments='{"order_id": ')])
            .turn(text="Let me try again.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        content = _tool_message_content(model, 1)
        assert "valid JSON object" in content

    async def test_an_unknown_tool_still_names_the_ones_that_exist(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("delete_everything", {})]).turn(text="Understood.")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        content = _tool_message_content(model, 1)
        assert "lookup" in content


class TestUnknownOutcomeGuidanceReachesTheModel:
    """A dangling call from a dead Attempt, settled by a reclaiming Worker's
    crash recovery -- a genuine ``ToolOutcome.UNKNOWN``, not a seeded one."""

    async def test_the_model_sees_exactly_guidances_canonical_wording(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        run_id = await start_run(store, spec)

        dead = await Journal.open(store, run_id, SCOPE)
        await dead.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await dead.append(type="turn_started", turn=1)
        await dead.append(type="model_call_started", turn=1, model="fake-standard")
        await dead.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 1},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await dead.append(
            type="tool_call_started",
            call_id="call-1",
            tool="lookup",
            arguments={"order_id": "A1"},
            turn=1,
        )

        model = FakeModel().turn(text="I could not confirm that.")
        reclaimer = await Journal.open(store, run_id, SCOPE)
        assert reclaimer.state.has_dangling_tool_calls
        await reclaimer.append(
            type="attempt_started",
            worker_id="wrk_2",
            attempt_number=2,
            reclaimed_expired_lease=True,
        )
        loop = AgentLoop(
            reclaimer, spec, model, ToolResolver(build_registry()), ToolExecutor(build_registry())
        )
        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        state = reduce(await store.read(run_id))
        assert state.tool_results[0].outcome is ToolOutcome.UNKNOWN

        content = _tool_message_content(model, 0)
        assert content == UNKNOWN_OUTCOME_GUIDANCE


class TestAbortedOutcomeGuidanceReachesTheModel:
    """An interrupt cancelling an in-flight call. Seeded directly on the log
    rather than produced by a live cancellation: exercising the interrupt
    plumbing itself is outside this file's scope, which is what the model is
    told rather than how a call gets cancelled, and DESIGN.md §9 already has
    its own e2e coverage in ``tests/e2e/test_durability.py`` for
    the interrupt mechanism. What this proves is the projection, the same as
    the crash-recovery test above proves it for UNKNOWN."""

    async def test_the_model_sees_exactly_guidances_canonical_wording(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        run_id = await start_run(store, spec)

        journal = await Journal.open(store, run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await journal.append(type="turn_started", turn=1)
        await journal.append(type="model_call_started", turn=1, model="fake-standard")
        await journal.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 1},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await journal.append(
            type="tool_call_started",
            call_id="call-1",
            tool="lookup",
            arguments={"order_id": "A1"},
            turn=1,
        )
        await journal.append(
            type="tool_call_finished", call_id="call-1", outcome=ToolOutcome.ABORTED
        )

        model = FakeModel().turn(text="Understood, stopping there.")
        loop = AgentLoop(
            journal, spec, model, ToolResolver(build_registry()), ToolExecutor(build_registry())
        )
        await loop.run()

        content = _tool_message_content(model, 0)
        assert content == ABORTED_GUIDANCE


class TestElidedResultGuidanceReachesTheModel:
    """A real large result, elided by ``AgentLoop._record_success`` and
    projected by ``psych_runtime.core.conversation``, matching
    ``psych_runtime.tools.guidance.elided_result_guidance``'s wording exactly even
    though the two are separate copies (see both modules' docstrings)."""

    async def test_the_model_sees_exactly_guidances_canonical_wording(self) -> None:
        store = InMemoryStore()
        spec = build_spec(tools=(CodeTool(name="big"),), limits=Limits(large_result_bytes=1024))
        model = FakeModel().turn(tool_calls=[("big", {"rows": 5000})]).turn(text="That was a lot.")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        records = await store.read(run_id)
        finished = next(r for r in records if isinstance(r, ToolCallFinished))
        assert finished.result_handle is not None
        assert finished.preview is not None

        content = _tool_message_content(model, 1)
        expected = elided_result_guidance(
            finished.result_bytes, finished.result_handle, finished.preview
        )
        assert content == expected
