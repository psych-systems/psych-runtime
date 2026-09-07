"""End to end: a real Run from dispatch through the agent loop to the log.

DESIGN.md §22 calls the e2e suite the regression gate. Every assertion here goes
through the log rather than through internal state, because the log is what a
report, a stream and a crash recovery all read, and a feature that works only in
memory is a feature that breaks the moment a Worker dies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from psych_runtime.core.ids import RunId, new_run_id
from psych_runtime.core.records import (
    QueueKind,
    TerminalState,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutcome,
)
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef
from psych_runtime.core.version import publish
from psych_runtime.model.pricing import ModelPrice, StaticPriceTable
from psych_runtime.runtime.agent import AgentLoop, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel
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
    """Admit a Run the way dispatch will, so the log starts correctly."""
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
    return AgentLoop(
        journal,
        spec,
        model,
        ToolResolver(registry),
        ToolExecutor(registry),
        prices=StaticPriceTable(
            {
                "fake-standard": ModelPrice(
                    input=Decimal("1"),
                    output=Decimal("2"),
                    cache_read=Decimal("0.1"),
                    cache_write=Decimal("1.25"),
                )
            }
        ),
    )


class TestASimpleRun:
    async def test_a_run_with_one_tool_call_completes_and_the_log_tells_the_story(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        registry = build_registry()
        model = (
            FakeModel()
            .turn(text="Let me check.", tool_calls=[("lookup", {"order_id": "A1"})])
            .turn(text="Your order A1 has shipped.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, registry)

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        assert outcome.output == {"text": "Your order A1 has shipped."}

        state = reduce(await store.read(run_id))
        assert state.turn == 2
        assert state.model_calls == 2
        assert not state.has_dangling_tool_calls
        assert [result.tool for result in state.tool_results] == ["lookup"]
        assert state.tool_results[0].outcome is ToolOutcome.OK

    async def test_the_tool_call_is_recorded_before_it_runs(self) -> None:
        """DESIGN.md §8.4. This ordering is why force-settlement is safe: an
        orphaned call is visibly incomplete rather than ambiguous."""
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        records = await store.read(run_id)
        started = next(r for r in records if isinstance(r, ToolCallStarted))
        finished = next(r for r in records if isinstance(r, ToolCallFinished))
        assert started.seq < finished.seq

    async def test_usage_and_cost_are_recorded_per_call(self) -> None:
        from psych_runtime.core.usage import Usage

        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(
            text="done", usage=Usage(input=1_000_000, output=500_000, cache_read=2_000_000)
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        state = reduce(await store.read(run_id))
        assert state.usage.input == 1_000_000
        assert state.usage.cache_read == 2_000_000
        assert state.cost is not None
        # 1M at 1, 0.5M at 2, 2M at 0.1
        assert state.cost.amount == Decimal("2.20000000")

    async def test_an_unpriced_model_records_no_cost_rather_than_zero(self) -> None:
        store = InMemoryStore()
        spec = build_spec(model=ModelRef(model="model-nobody-priced"))
        model = FakeModel().turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        state = reduce(await store.read(run_id))
        assert state.cost is None
        assert state.unpriced_model_calls == 1


class TestToolFailuresAreData:
    async def test_a_raising_tool_becomes_an_error_result_not_a_dead_turn(self) -> None:
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
        state = reduce(await store.read(run_id))
        result = state.tool_results[0]
        assert result.outcome is ToolOutcome.ERROR
        assert result.failure is not None
        assert "no such order" in result.failure.message
        assert result.failure.traceback is not None

    async def test_a_malformed_tool_call_is_handed_back_as_data(self) -> None:
        """The model's mistake, and the model can fix it. Raising would end a
        turn over a bad JSON fragment."""
        from psych_runtime.testing.fake_model import ToolCallScript

        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(tool_calls=[ToolCallScript(name="lookup", raw_arguments='{"order_id": ')])
            .turn(text="Sorry, let me try again properly.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        state = reduce(await store.read(run_id))
        assert state.tool_results[0].failure is not None
        assert state.tool_results[0].failure.kind == "malformed_arguments"

    async def test_calling_a_tool_that_was_not_offered_is_reported_with_the_real_list(
        self,
    ) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("delete_everything", {})]).turn(text="Understood.")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        state = reduce(await store.read(run_id))
        failure = state.tool_results[0].failure
        assert failure is not None
        assert failure.kind == "unknown_tool"
        assert "lookup" in failure.message

    async def test_invalid_arguments_are_rejected_before_the_function_runs(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup", {"wrong_field": "A1"})])
            .turn(text="Let me correct that.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        state = reduce(await store.read(run_id))
        failure = state.tool_results[0].failure
        assert failure is not None
        assert failure.kind == "invalid_arguments"


def build_dynamic_registry() -> ToolRegistry:
    """The same ``lookup`` tool ``build_registry`` registers, built here from a
    runtime schema through ``register_dynamic`` instead of the decorator, so a
    Run can be driven through either registry and compared."""
    from pydantic import ConfigDict, create_model

    registry = ToolRegistry()
    # Deliberately NOT "lookup_Arguments", which is what the decorator path
    # synthesises. Naming it to match would make the equivalence assertion
    # below pass by construction: Pydantic puts the model's name in its error
    # text, so a matching name hides any leak of it into what the model reads.
    # A consumer writing create_model("OrderLookupArgs", ...) is the realistic
    # case, and it is the one that has to produce identical text.
    arguments_model = create_model(
        "OrderLookupArgs",
        __config__=ConfigDict(extra="forbid"),
        order_id=(str, ...),
    )

    def lookup(order_id: str) -> dict[str, str]:
        if order_id == "MISSING":
            raise ValueError("no such order")
        return {"order_id": order_id, "status": "shipped"}

    registry.register_dynamic(
        "lookup", lookup, arguments_model, description="Look up an order by id."
    )
    return registry


class TestDynamicallyRegisteredTools:
    """A tool registered through ``register_dynamic``, a schema and a callable
    with no Python signature, behaves identically to one registered through
    the decorator, all the way through a real Run."""

    async def test_a_dynamically_registered_tool_completes_a_run(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(text="Let me check.", tool_calls=[("lookup", {"order_id": "A1"})])
            .turn(text="Your order A1 has shipped.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_dynamic_registry())

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        state = reduce(await store.read(run_id))
        assert state.tool_results[0].outcome is ToolOutcome.OK
        assert state.tool_results[0].result == {"order_id": "A1", "status": "shipped"}

    async def test_a_hallucinated_argument_fails_the_same_way_with_the_same_guidance(
        self,
    ) -> None:
        """The equivalence proven directly rather than left implied:
        the identical malformed call against a derived tool and a dynamic tool
        produces byte-identical failure text reaching the model."""

        def script() -> FakeModel:
            return (
                FakeModel()
                .turn(tool_calls=[("lookup", {"order_id": "A1", "invented": True})])
                .turn(text="Let me correct that.")
            )

        spec = build_spec()

        derived_store = InMemoryStore()
        derived_run_id = await start_run(derived_store, spec)
        derived_loop = await make_loop(
            derived_store, derived_run_id, spec, script(), build_registry()
        )
        await derived_loop.run()

        dynamic_store = InMemoryStore()
        dynamic_run_id = await start_run(dynamic_store, spec)
        dynamic_loop = await make_loop(
            dynamic_store, dynamic_run_id, spec, script(), build_dynamic_registry()
        )
        await dynamic_loop.run()

        derived_failure = reduce(await derived_store.read(derived_run_id)).tool_results[0].failure
        dynamic_failure = reduce(await dynamic_store.read(dynamic_run_id)).tool_results[0].failure
        assert derived_failure is not None
        assert dynamic_failure is not None
        assert derived_failure.kind == "invalid_arguments"
        assert dynamic_failure.kind == "invalid_arguments"
        assert derived_failure.message == dynamic_failure.message

        # And identical because neither leaks its arguments model's name, not
        # because the two models happen to share one. Pydantic's own rendering
        # also carries diagnostics that restate the sentence before them and a
        # link inviting the model to go and fetch a URL, neither of which
        # belongs in a tool-argument error.
        for message in (derived_failure.message, dynamic_failure.message):
            assert "OrderLookupArgs" not in message
            assert "lookup_Arguments" not in message
            assert "errors.pydantic.dev" not in message
            assert "input_value=" not in message
            assert "invented" in message, "the offending field must still be named"


class TestFailureStreak:
    async def test_three_consecutive_failures_stop_the_model_repeating_the_tool(self) -> None:
        """DESIGN.md §23, item ten."""
        store = InMemoryStore()
        spec = build_spec(limits=Limits(failure_streak_threshold=3, failure_streak_hard_stop=6))
        model = FakeModel()
        for _ in range(4):
            model.turn(tool_calls=[("lookup", {"order_id": "MISSING"})])
        model.turn(text="I give up on that tool.")

        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        # The fourth turn must not have been offered the tool any more.
        fourth = model.requests[3]
        assert "lookup" not in {tool.name for tool in fourth.tools}
        assert any("failed 3 times in a row" in m.content for m in fourth.messages)

    async def test_the_hard_stop_fails_the_run(self) -> None:
        store = InMemoryStore()
        spec = build_spec(limits=Limits(failure_streak_threshold=2, failure_streak_hard_stop=3))
        model = FakeModel()
        for _ in range(6):
            model.turn(tool_calls=[("lookup", {"order_id": "MISSING"})])

        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.FAILED
        assert outcome.failure is not None
        assert outcome.failure.kind == "failure_streak"
        # The message is now built by psych_runtime.tools.guidance.failure_guidance
        # rather than an ad-hoc string, so it carries the call-specific detail
        # ("lookup" and the streak count) and the guidance module's own added
        # advice for this kind, not just whichever of the two a hand-written
        # message happened to include.
        assert "'lookup' failed 3 consecutive times" in outcome.failure.message
        assert "already ended" in outcome.failure.message.lower()


class TestPerTurnToolResolution:
    async def test_the_tool_set_is_rebuilt_every_turn(self) -> None:
        """DESIGN.md §10.2: nothing about the tool set is fixed at process start."""
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        assert len(model.requests) == 2
        for request in model.requests:
            assert {tool.name for tool in request.tools} == {"lookup"}

    async def test_the_system_prompt_leads_with_the_instructions(self) -> None:
        """Cache-deliberate order (DESIGN.md §19): the stable part comes first."""
        store = InMemoryStore()
        spec = build_spec(instructions="Help the customer.")
        model = FakeModel().turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        system = model.last_request.messages[0]
        assert system.content.startswith("Help the customer.")

    async def test_the_recorded_tool_names_match_what_was_offered(self) -> None:
        """A report six months later should say what the model was actually
        offered, not what the Spec granted."""
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(text="done")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        records = await store.read(run_id)
        started = next(r for r in records if r.type == "model_call_started")
        assert started.tool_names == ("lookup",)


class TestLargeResults:
    async def test_a_large_result_is_whole_in_the_log_and_elided_for_the_model(self) -> None:
        """DESIGN.md §10.8. The log always holds the whole thing; only the
        model's view is trimmed."""
        store = InMemoryStore()
        spec = build_spec(tools=(CodeTool(name="big"),), limits=Limits(large_result_bytes=1024))
        model = FakeModel().turn(tool_calls=[("big", {"rows": 5000})]).turn(text="That was a lot.")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        records = await store.read(run_id)
        finished = next(r for r in records if isinstance(r, ToolCallFinished))
        assert finished.result_bytes == 5000
        assert len(finished.result) == 5000  # whole, in the log
        assert finished.result_handle is not None
        assert finished.preview is not None

        # The model saw the handle, not the 5000 bytes.
        second_turn = model.requests[1]
        tool_message = next(m for m in second_turn.messages if m.role == "tool")
        assert finished.result_handle in tool_message.content
        assert len(tool_message.content) < 5000


class TestInterrupts:
    async def test_an_abort_stops_the_run_and_is_visible_in_the_log(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().turn(text="done")
        run_id = await start_run(store, spec)
        journal = await Journal.open(store, run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await journal.append(type="abort_requested", reason="the user pressed stop")

        loop = AgentLoop(
            journal,
            spec,
            model,
            ToolResolver(build_registry()),
            ToolExecutor(build_registry()),
        )
        outcome = await loop.run()

        assert outcome.state is TerminalState.ABORTED
        assert model.requests == []  # the model was never called

    async def test_a_message_sent_after_an_abort_lands_in_the_next_run_queue(self) -> None:
        """DESIGN.md §9: "stop mid-response and immediately send another request"
        is a modelled transition, not a race."""
        store = InMemoryStore()
        spec = build_spec()
        run_id = await start_run(store, spec)
        journal = await Journal.open(store, run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await journal.append(type="abort_requested", reason="stop")
        await journal.append(
            type="queue_enqueued",
            queue=QueueKind.NEXT_RUN,
            entry_id="entry-1",
            payload={"message": "actually, check A2"},
        )

        state = reduce(await store.read(run_id))
        assert state.aborted
        assert [e.payload["message"] for e in state.pending_next_run] == ["actually, check A2"]


class TestCrashRecovery:
    async def test_a_dangling_call_is_settled_before_the_loop_continues(self) -> None:
        """DESIGN.md §9: a model must never receive an assistant message with
        tool calls whose results are missing."""
        store = InMemoryStore()
        spec = build_spec()
        run_id = await start_run(store, spec)

        # First Worker: starts a tool call and dies.
        first = await Journal.open(store, run_id, SCOPE)
        await first.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await first.append(type="turn_started", turn=1)
        await first.append(type="model_call_started", turn=1, model="fake-standard")
        await first.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 1},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await first.append(
            type="tool_call_started",
            call_id="call-1",
            tool="lookup",
            arguments={"order_id": "A1"},
            turn=1,
        )

        # Second Worker reclaims and continues.
        model = FakeModel().turn(text="I could not confirm that.")
        second = await Journal.open(store, run_id, SCOPE)
        assert second.state.has_dangling_tool_calls
        await second.append(
            type="attempt_started",
            worker_id="wrk_2",
            attempt_number=2,
            reclaimed_expired_lease=True,
        )
        loop = AgentLoop(
            second, spec, model, ToolResolver(build_registry()), ToolExecutor(build_registry())
        )
        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        final = reduce(await store.read(run_id))
        assert not final.has_dangling_tool_calls
        assert final.tool_results[0].outcome is ToolOutcome.UNKNOWN

    async def test_the_unknown_outcome_reaches_the_model_honestly(self) -> None:
        """Guessing "it failed" would let the model retry a refund that already
        went out."""
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

        model = FakeModel().turn(text="ok")
        second = await Journal.open(store, run_id, SCOPE)
        loop = AgentLoop(
            second, spec, model, ToolResolver(build_registry()), ToolExecutor(build_registry())
        )
        await loop.run()

        tool_message = next(m for m in model.last_request.messages if m.role == "tool")
        assert "unknown" in tool_message.content.lower()
        assert "side effect" in tool_message.content


class TestStreamFailures:
    async def test_a_stream_that_aborts_mid_token_is_retried_not_accepted(self) -> None:
        """A stream ending without a completion event must not be mistaken for a
        short answer: that silently truncates a reply to a customer."""
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().aborts_mid_token(text="Your refund of ").turn(text="Full answer.")
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        assert outcome.output == {"text": "Full answer."}
        state = reduce(await store.read(run_id))
        assert state.failed_model_calls == 1
        assert state.transient_retries_used == 1

    async def test_a_permanent_failure_is_not_retried(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = FakeModel().raises_permanent(status_code=400)
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.FAILED
        assert len(model.requests) == 1

    async def test_the_transient_budget_is_per_run_and_bounded(self) -> None:
        """DESIGN.md §8.6: bounding per call lets a Run retry forever by
        spreading failures across steps."""
        store = InMemoryStore()
        spec = build_spec(limits=Limits(transient_retry_budget=2))
        model = FakeModel()
        for _ in range(6):
            model.raises_transient(status_code=503)

        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        outcome = await loop.run()

        assert outcome.state is TerminalState.FAILED
        assert len(model.requests) == 3  # the first, plus two retries
