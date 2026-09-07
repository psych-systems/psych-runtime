"""The spans the runtime actually emits, checked against the declared schema.

DESIGN.md §13.5 wants spans that "cannot drift from their contract as the code
changes". A schema nobody validates against real emissions is a document, not a
contract, so this runs a real agent and checks what came out.

Nothing checks the schema at import time: it is data, so a renamed attribute or
a re-parented span breaks silently. This test and
``psych_runtime.telemetry.conformance``'s schema group are what make it break loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, CompactionPolicy, ModelRef
from psych_runtime.core.usage import Usage
from psych_runtime.model.pricing import StaticPriceTable
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.telemetry.conformance import RecordingTelemetry, check_schema_conformance
from psych_runtime.telemetry.schema import PSYCH_SCHEMA
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme", principal="user-1")


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> str:
        """Look up an order."""
        if order_id == "BOOM":
            raise ValueError("no such order")
        return f"{order_id} shipped"

    return registry


def build_spec(*, compaction: CompactionPolicy | None = None) -> AgentSpec:
    return AgentSpec(
        name="support",
        instructions="Help.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="lookup"),),
        compaction=compaction,
    )


async def run_with(
    telemetry: RecordingTelemetry, model: FakeModel, *, spec: AgentSpec | None = None
) -> Any:
    store = InMemoryStore()
    spec = spec if spec is not None else build_spec()
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
    journal = await Journal.open(store, dispatched.run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(
        store=store,
        model=model,
        registry=build_registry(),
        telemetry=telemetry,
        prices=StaticPriceTable({}),
    )
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched


class TestEmittedSpansConform:
    async def test_a_run_with_a_tool_call_emits_only_schema_conformant_spans(self) -> None:
        telemetry = RecordingTelemetry()
        model = (
            FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 shipped.")
        )
        await run_with(telemetry, model)

        violations = check_schema_conformance(PSYCH_SCHEMA, telemetry.spans)
        assert not violations, f"emitted spans violate the schema: {violations}"

    async def test_a_failing_tool_still_emits_conformant_spans(self) -> None:
        """The error path is where attributes usually get forgotten."""
        telemetry = RecordingTelemetry()
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup", {"order_id": "BOOM"})])
            .turn(text="I could not find it.")
        )
        await run_with(telemetry, model)
        assert not check_schema_conformance(PSYCH_SCHEMA, telemetry.spans)

    async def test_a_failing_model_call_still_emits_conformant_spans(self) -> None:
        telemetry = RecordingTelemetry()
        model = FakeModel().raises_permanent(status_code=400)
        await run_with(telemetry, model)
        assert not check_schema_conformance(PSYCH_SCHEMA, telemetry.spans)

    async def test_the_expected_spans_are_actually_emitted(self) -> None:
        """A conformance check passes trivially if nothing was emitted, so the
        suite has to assert something came out too."""
        telemetry = RecordingTelemetry()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="done")
        await run_with(telemetry, model)

        names = [span.name for span in telemetry.spans]
        assert names.count("psych.turn") == 2
        assert names.count("psych.model_call") == 2
        assert names.count("psych.tool_call") == 1

    async def test_a_compaction_emits_a_conformant_span_of_its_own(self) -> None:
        """The span that was emitted for weeks and never declared.

        A model call the schema does not know about is one no consumer's
        pipeline expects and nothing checks, and this one spends real tokens:
        a trace showing the turns but not the calls between them cannot be
        reconciled against a provider's bill. Declaring it was half the fix;
        this is the half that keeps it declared.

        Beside the turns rather than inside one, because summarising is not a
        turn. A trace that nested it would say a turn made two model calls.
        """
        telemetry = RecordingTelemetry()
        # Tool calls rather than text on the first two turns: text ends the
        # loop, and the trigger is read from the *last finished* call, so a Run
        # that stops after one turn never gets to compare anything.
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup", {"order_id": "A1"})], usage=Usage(input=900, output=10))
            .turn(tool_calls=[("lookup", {"order_id": "A2"})], usage=Usage(input=950, output=10))
            .turn(text="Earlier: two orders were looked up.", usage=Usage(input=400, output=20))
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )
        spec = build_spec(compaction=CompactionPolicy(trigger_tokens=800, keep_recent_turns=1))
        await run_with(telemetry, model, spec=spec)

        violations = check_schema_conformance(PSYCH_SCHEMA, telemetry.spans)
        assert not violations, f"emitted spans violate the schema: {violations}"

        compactions = [span for span in telemetry.spans if span.name == "psych.compaction"]
        assert compactions, "the premise: the trigger was crossed and a summary was written"
        assert compactions[0].parent != "psych.turn"

    async def test_a_model_call_span_carries_its_usage(self) -> None:
        from psych_runtime.core.usage import Usage

        telemetry = RecordingTelemetry()
        model = FakeModel().turn(text="done", usage=Usage(input=120, output=34, cache_read=7))
        await run_with(telemetry, model)

        call = next(s for s in telemetry.spans if s.name == "psych.model_call")
        assert call.attributes["gen_ai.usage.input_tokens"] == 120
        assert call.attributes["gen_ai.usage.output_tokens"] == 34
        assert call.attributes["psych.usage.cache_read_tokens"] == 7

    async def test_a_tool_call_span_nests_inside_its_turn(self) -> None:
        """Parentage is part of the schema, and a flat span tree is useless for
        working out where a slow turn went."""
        telemetry = RecordingTelemetry()
        model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="done")
        await run_with(telemetry, model)

        tool_span = next(s for s in telemetry.spans if s.name == "psych.tool_call")
        assert tool_span.parent == "psych.turn"

        turn_span = next(s for s in telemetry.spans if s.name == "psych.turn")
        assert turn_span.parent == "psych.attempt"


class TestTelemetryCannotBreakARun:
    async def test_a_raising_telemetry_does_not_fail_the_run(self) -> None:
        """DESIGN.md §13.5: a consumer's broken exporter is never the reason a
        Run failed."""

        class Broken:
            def start_span(self, name: str, **kwargs: Any) -> Any:
                raise RuntimeError("the exporter is on fire")

            def set_attributes(self, attributes: Any) -> None:
                raise RuntimeError("the exporter is on fire")

            def add_event(self, name: str, attributes: Any = None) -> None:
                raise RuntimeError("the exporter is on fire")

            def set_status(self, status: Any, description: str = "") -> None:
                raise RuntimeError("the exporter is on fire")

            def record_exception(self, exc: BaseException) -> None:
                raise RuntimeError("the exporter is on fire")

        store = InMemoryStore()
        spec = build_spec()
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        runtime = Runtime(
            store=store,
            model=FakeModel().turn(text="done"),
            registry=build_registry(),
            telemetry=Broken(),
        )
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())

        state = await psych_runtime.state(store, dispatched.run_id)
        assert state.settled
        assert state.terminal_state is not None
        assert state.terminal_state.value == "completed"
