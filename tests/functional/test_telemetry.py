"""The Telemetry port and its two adapters, against real components.

DESIGN.md §22: functional tests run a component against a real adapter, never
a mock. ``OtelTelemetry`` here runs against a real ``opentelemetry-sdk``
``TracerProvider``, draining a real (in-process, no network)
``InMemorySpanExporter`` rather than asserting against internal state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from psych_runtime.telemetry.conformance import (
    CapturedSpan,
    RecordingTelemetry,
    SchemaConformanceError,
    SchemaConformanceSuite,
    SpanEvent,
    TelemetryAdapterConformanceSuite,
    assert_schema_conformant,
    check_schema_conformance,
)
from psych_runtime.telemetry.otel import OtelTelemetry
from psych_runtime.telemetry.port import (
    NOOP_TELEMETRY,
    GuardedTelemetry,
    SpanAttributeValue,
    SpanStatus,
    Telemetry,
    TelemetrySpan,
)
from psych_runtime.telemetry.schema import PSYCH_SCHEMA

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# The OTel adapter's own captured-spans reader, for the conformance suites
# ---------------------------------------------------------------------------


def _otel_attributes(attributes: Mapping[str, object] | None) -> dict[str, SpanAttributeValue]:
    """OpenTelemetry's own attribute value union (scalar or homogeneous
    ``Sequence``) is a runtime-safe subset of ``SpanAttributeValue`` (scalar
    or ``tuple[str, ...]``): every sequence OTel hands back here is a tuple
    already, since that is what ``OtelTelemetry`` ever writes to a real span.
    Cast per value because mypy has no way to know that from OTel's wider
    declared type."""
    return {key: cast(SpanAttributeValue, value) for key, value in (attributes or {}).items()}


def _captured_spans_from_otel(exporter: InMemorySpanExporter) -> tuple[CapturedSpan, ...]:
    """Turn what ``InMemorySpanExporter`` actually exported into the same
    ``CapturedSpan`` shape ``RecordingTelemetry`` produces, so both
    conformance suites can run unmodified against either adapter."""
    finished = exporter.get_finished_spans()
    id_by_span_id = {s.context.span_id: s.name for s in finished if s.context is not None}
    result: list[CapturedSpan] = []
    for seq, span in enumerate(finished):
        parent_name = id_by_span_id.get(span.parent.span_id) if span.parent is not None else None
        attributes = _otel_attributes(span.attributes)
        events = tuple(
            SpanEvent(name=event.name, attributes=_otel_attributes(event.attributes))
            for event in span.events
        )
        status = SpanStatus.ERROR if span.status.status_code is StatusCode.ERROR else SpanStatus.OK
        result.append(
            CapturedSpan(
                name=span.name,
                parent=parent_name,
                attributes=attributes,
                status=status,
                status_description=span.status.description or "",
                events=events,
                end_sequence=seq,
                had_exception=any(event.name == "exception" for event in span.events),
            )
        )
    return tuple(result)


@pytest.fixture
def otel_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def otel_telemetry(otel_exporter: InMemorySpanExporter) -> OtelTelemetry:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(otel_exporter))
    return OtelTelemetry(tracer=provider.get_tracer("psych-tests"))


# ---------------------------------------------------------------------------
# The no-op default really does nothing
# ---------------------------------------------------------------------------


class TestNoOpTelemetry:
    async def test_the_callback_runs_and_its_result_passes_through(self) -> None:
        async with NOOP_TELEMETRY.start_span("s") as span:
            result = 42
            async with span.start_span("nested") as nested:
                nested.set_attributes({"a": "b"})
                nested.add_event("e")
                nested.set_status(SpanStatus.ERROR, "irrelevant")
        assert result == 42

    async def test_an_exception_inside_still_propagates(self) -> None:
        with pytest.raises(ValueError, match="boom"):
            async with NOOP_TELEMETRY.start_span("s"):
                raise ValueError("boom")

    async def test_every_span_method_is_callable_and_does_nothing(self) -> None:
        # None of these may raise; a no-op span is inert, not merely quiet.
        async with NOOP_TELEMETRY.start_span("s", attributes={"x": "y"}) as span:
            span.set_attributes({"a": "b"})
            span.add_event("e", {"k": "v"})
            span.set_status(SpanStatus.OK)
            span.record_exception(ValueError("x"))


# ---------------------------------------------------------------------------
# A raising Telemetry cannot break a caller
# ---------------------------------------------------------------------------


class _ExplodingSpan:
    def start_span(
        self, name: str, *, attributes: object = None
    ) -> TelemetrySpan:  # pragma: no cover - never reached, start_span itself explodes first
        raise RuntimeError("exploding span: start_span")

    def set_attributes(self, attributes: object) -> None:
        raise RuntimeError("exploding span: set_attributes")

    def add_event(self, name: str, attributes: object = None) -> None:
        raise RuntimeError("exploding span: add_event")

    def set_status(self, status: object, description: str = "") -> None:
        raise RuntimeError("exploding span: set_status")

    def record_exception(self, exc: BaseException) -> None:
        raise RuntimeError("exploding span: record_exception")


class _ExplodingTelemetry:
    """Every method raises. Stands in for a broken consumer-supplied
    ``Telemetry`` (a misconfigured exporter, a typo'd custom adapter)."""

    def start_span(self, name: str, *, attributes: object = None) -> object:
        raise RuntimeError("exploding telemetry: start_span")


@asynccontextmanager
async def _exploding_span_cm() -> AsyncIterator[_ExplodingSpan]:
    yield _ExplodingSpan()


class _WorksAtStartThenExplodes:
    """``start_span`` itself succeeds; every method on the span it hands back
    raises. Distinguishes "the guard survives a broken ``start_span``" from
    "the guard survives a broken span"."""

    def start_span(
        self, name: str, *, attributes: object = None
    ) -> AbstractAsyncContextManager[_ExplodingSpan]:
        return _exploding_span_cm()


class TestGuardedTelemetryIsPassiveToARaisingImplementation:
    async def test_start_span_raising_does_not_break_the_caller(self) -> None:
        guarded: Telemetry = GuardedTelemetry(_ExplodingTelemetry())  # type: ignore[arg-type]
        ran = False
        async with guarded.start_span("s") as span:
            ran = True
            # Every method on the degraded span must also be inert.
            span.set_attributes({"a": "b"})
            span.add_event("e")
            span.set_status(SpanStatus.ERROR)
            span.record_exception(ValueError("x"))
        assert ran

    async def test_application_exceptions_still_propagate_through_the_guard(self) -> None:
        guarded: Telemetry = GuardedTelemetry(_ExplodingTelemetry())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="real failure"):
            async with guarded.start_span("s"):
                raise ValueError("real failure")

    async def test_a_span_whose_every_method_raises_does_not_break_the_caller(self) -> None:
        guarded: Telemetry = GuardedTelemetry(_WorksAtStartThenExplodes())  # type: ignore[arg-type]
        ran = False
        async with guarded.start_span("s") as span:
            ran = True
            span.set_attributes({"a": "b"})  # would raise on the real span; must not raise here
            span.add_event("e")
            span.set_status(SpanStatus.OK)
            span.record_exception(ValueError("caught"))
        assert ran


# ---------------------------------------------------------------------------
# The OTel adapter emits what the schema declares
# ---------------------------------------------------------------------------


class TestOtelTelemetryEmitsWhatTheSchemaDeclares:
    async def test_a_run_and_a_nested_attempt_produce_correctly_shaped_spans(
        self, otel_telemetry: OtelTelemetry, otel_exporter: InMemorySpanExporter
    ) -> None:
        async with otel_telemetry.start_span(
            "psych.run",
            attributes={
                "psych.run.id": "run_1",
                "psych.scope.tenant": "acme",
                "psych.version.hash": "sha256:" + "0" * 64,
            },
        ) as run:
            run.set_attributes({"psych.run.terminal_state": "completed"})
            async with run.start_span(
                "psych.attempt",
                attributes={
                    "psych.worker.id": "wrk_1",
                    "psych.attempt.number": 1,
                    "psych.attempt.reclaimed_expired_lease": False,
                },
            ):
                pass

        captured = _captured_spans_from_otel(otel_exporter)
        by_name = {s.name: s for s in captured}
        assert by_name["psych.run"].attributes["psych.run.id"] == "run_1"
        assert by_name["psych.run"].attributes["psych.run.terminal_state"] == "completed"
        assert by_name["psych.attempt"].parent == "psych.run"
        assert by_name["psych.attempt"].attributes["psych.attempt.number"] == 1
        assert check_schema_conformance(PSYCH_SCHEMA, captured) == ()

    async def test_an_unhandled_exception_produces_an_error_status_and_an_event(
        self, otel_telemetry: OtelTelemetry, otel_exporter: InMemorySpanExporter
    ) -> None:
        with pytest.raises(ValueError, match="boom"):
            async with otel_telemetry.start_span(
                "psych.tool_call",
                attributes={
                    "psych.tool.name": "t",
                    "psych.tool.call_id": "call_1",
                    "psych.tool.interruptible": True,
                },
            ):
                raise ValueError("boom")
        captured = _captured_spans_from_otel(otel_exporter)
        assert captured[0].status is SpanStatus.ERROR
        assert captured[0].had_exception

    async def test_a_hostile_start_attributes_mapping_does_not_prevent_the_call(
        self, otel_telemetry: OtelTelemetry
    ) -> None:
        class _Hostile:
            def items(self) -> object:
                raise RuntimeError("unreadable")

        ran = False
        async with otel_telemetry.start_span("psych.run", attributes=_Hostile()):  # type: ignore[arg-type]
            ran = True
        assert ran


# ---------------------------------------------------------------------------
# Both conformance groups, against both adapters
# ---------------------------------------------------------------------------


class TestRecordingTelemetryAdapterContract(TelemetryAdapterConformanceSuite):
    @pytest.fixture
    def telemetry(self) -> Telemetry:
        return RecordingTelemetry()

    @pytest.fixture
    def captured_spans(self, telemetry: Telemetry) -> Callable[[], Sequence[CapturedSpan]]:
        recorder = telemetry
        assert isinstance(recorder, RecordingTelemetry)
        return lambda: recorder.spans


class TestRecordingTelemetrySchemaConformance(SchemaConformanceSuite):
    @pytest.fixture
    def telemetry(self) -> Telemetry:
        return RecordingTelemetry()

    @pytest.fixture
    def captured_spans(self, telemetry: Telemetry) -> Callable[[], Sequence[CapturedSpan]]:
        recorder = telemetry
        assert isinstance(recorder, RecordingTelemetry)
        return lambda: recorder.spans


class TestOtelTelemetryAdapterContract(TelemetryAdapterConformanceSuite):
    @pytest.fixture
    def telemetry(self, otel_telemetry: OtelTelemetry) -> Telemetry:
        return otel_telemetry

    @pytest.fixture
    def captured_spans(
        self, otel_exporter: InMemorySpanExporter
    ) -> Callable[[], Sequence[CapturedSpan]]:
        return lambda: _captured_spans_from_otel(otel_exporter)


class TestOtelTelemetrySchemaConformance(SchemaConformanceSuite):
    @pytest.fixture
    def telemetry(self, otel_telemetry: OtelTelemetry) -> Telemetry:
        return otel_telemetry

    @pytest.fixture
    def captured_spans(
        self, otel_exporter: InMemorySpanExporter
    ) -> Callable[[], Sequence[CapturedSpan]]:
        return lambda: _captured_spans_from_otel(otel_exporter)


# ---------------------------------------------------------------------------
# assert_schema_conformant: the raising convenience wrapper
# ---------------------------------------------------------------------------


class TestAssertSchemaConformant:
    async def test_raises_naming_every_violation_it_found(self) -> None:
        recorder = RecordingTelemetry()
        async with recorder.start_span("psych.run"):  # missing every required attribute
            pass
        with pytest.raises(SchemaConformanceError, match=r"psych\.run"):
            assert_schema_conformant(PSYCH_SCHEMA, recorder.spans)

    async def test_does_not_raise_when_conformant(self) -> None:
        recorder = RecordingTelemetry()
        async with recorder.start_span(
            "psych.run",
            attributes={
                "psych.run.id": "r",
                "psych.scope.tenant": "t",
                "psych.version.hash": "h",
            },
        ):
            pass
        assert_schema_conformant(PSYCH_SCHEMA, recorder.spans)  # must not raise
