"""The Telemetry conformance suite.

Two groups, importable so a consumer can run either against their own
``Telemetry`` implementation:

1. ``TelemetryAdapterConformanceSuite``: span lifecycle, status, attribute
   merging, parentage, and passivity to malformed input. Adapter-shape tests,
   true of any implementation.
2. ``SchemaConformanceSuite``: whether a span's attributes and parent match the
   shape ``psych_runtime.telemetry.schema`` declares for it. A statically typed host
   language would reject that mismatch before the code ran. Python will not, so
   the check has to happen at run time or not at all.

Both groups need to see what an implementation actually recorded, which the
``Telemetry`` protocol itself does not expose (a real backend like OTel does
not hand attributes back out). So both take a ``captured_spans`` fixture
returning a snapshot function alongside the ``telemetry`` instance under
test: ``RecordingTelemetry`` below is the simplest one, its ``captured_spans``
fixture being just ``lambda: recorder.spans``, and
``tests/functional/test_telemetry.py`` supplies the OpenTelemetry-backed
equivalent by draining an ``InMemorySpanExporter``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import pytest

from psych_runtime.core.errors import PsychError
from psych_runtime.telemetry.port import (
    NOOP_TELEMETRY,
    SpanAttributes,
    SpanAttributeValue,
    SpanStatus,
    Telemetry,
    TelemetrySpan,
)
from psych_runtime.telemetry.schema import (
    PSYCH_SCHEMA,
    AnyParent,
    AttributeType,
    NamedParents,
    RootOrExternalParent,
    SpanDefinition,
    TelemetrySchema,
)

__all__ = [
    "CapturedSpan",
    "RecordingTelemetry",
    "SchemaConformanceError",
    "SchemaConformanceSuite",
    "SchemaViolation",
    "SpanEvent",
    "TelemetryAdapterConformanceSuite",
    "assert_schema_conformant",
    "check_schema_conformance",
]


# ---------------------------------------------------------------------------
# What a captured span looks like, independent of which adapter produced it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanEvent:
    name: str
    attributes: dict[str, SpanAttributeValue]


@dataclass(frozen=True, slots=True)
class CapturedSpan:
    """One settled span, as recorded by any adapter.

    Attributes:
        name: the span's name.
        parent: the recording parent span's name, or ``None`` for a root (or
            a span whose parent was outside what the recorder tracks).
        attributes: the final merged attribute set at settlement.
        status: ``OK`` unless the span's body raised and no explicit status
            was set, or an explicit ``ERROR`` was set.
        status_description: the description passed to the status that won.
        events: in the order they were added.
        end_sequence: monotonic order of settlement across the whole
            recording session, not creation order. A child that settles
            before its sibling has a lower ``end_sequence`` even if it was
            opened second.
        had_exception: whether the span's body raised, regardless of what
            status won.
    """

    name: str
    parent: str | None
    attributes: dict[str, SpanAttributeValue]
    status: SpanStatus
    status_description: str
    events: tuple[SpanEvent, ...]
    end_sequence: int
    had_exception: bool


# ---------------------------------------------------------------------------
# The recording adapter
# ---------------------------------------------------------------------------


class _LiveRecordingSpan:
    """A span in progress. Every mutator is passive to hostile input, and every
    mutator is a no-op once the span has settled. Telemetry that raises, or that
    rewrites a settled span, turns an observability bug into a Run failure."""

    def __init__(self, recorder: RecordingTelemetry, name: str, parent: str | None) -> None:
        self._recorder = recorder
        self.name = name
        self.parent = parent
        self._attributes: dict[str, SpanAttributeValue] = {}
        self._events: list[SpanEvent] = []
        self._status = SpanStatus.OK
        self._status_description = ""
        self._explicit_status = False
        self._had_exception = False
        self._settled = False

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        if self._settled:
            # Still admits and runs its own callback; produces no span of its
            # own, because a parent that no longer exists cannot record one.
            return NOOP_TELEMETRY.start_span(name, attributes=attributes)
        return self._recorder._span_cm(name, parent=self.name, attributes=attributes)

    def set_attributes(self, attributes: SpanAttributes) -> None:
        if self._settled:
            return
        try:
            items = list(attributes.items())
        except Exception:
            return  # atomic: a call that fails to read is discarded whole
        for key, value in items:
            if value is None:
                self._attributes.pop(key, None)
            else:
                self._attributes[key] = value

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        if self._settled:
            return
        payload: dict[str, SpanAttributeValue] = {}
        if attributes is not None:
            try:
                payload = {k: v for k, v in attributes.items() if v is not None}
            except Exception:
                return
        self._events.append(SpanEvent(name=name, attributes=payload))

    def set_status(self, status: SpanStatus, description: str = "") -> None:
        if self._settled:
            return
        try:
            described = str(description)
        except Exception:
            return
        self._status = status
        self._status_description = described
        self._explicit_status = True

    def record_exception(self, exc: BaseException) -> None:
        if self._settled:
            return
        self._had_exception = True
        try:
            message = str(exc)
        except Exception:
            message = "<exception message could not be read>"
        self.add_event(
            "exception",
            {"exception.type": type(exc).__name__, "exception.message": message},
        )

    def _settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        if not self._explicit_status and self._had_exception:
            self._status = SpanStatus.ERROR
        self._recorder._commit(self)


@asynccontextmanager
async def _recording_span_cm(
    recorder: RecordingTelemetry,
    name: str,
    parent: str | None,
    attributes: SpanAttributes | None,
) -> AsyncIterator[TelemetrySpan]:
    live = _LiveRecordingSpan(recorder, name, parent)
    if attributes is not None:
        live.set_attributes(attributes)
    try:
        yield live
    except BaseException as exc:
        live.record_exception(exc)
        live._settle()
        raise
    else:
        live._settle()


class RecordingTelemetry:
    """A ``Telemetry`` that captures every span for a test to inspect.

    The reference adapter both conformance groups are written against, and
    the standard vehicle for schema conformance: exercise real code with a
    ``RecordingTelemetry`` in place of a consumer's real one, then check
    ``.spans`` against ``psych_runtime.telemetry.schema.PSYCH_SCHEMA``.
    """

    def __init__(self) -> None:
        self._spans: list[CapturedSpan] = []
        self._next_sequence = 0

    @property
    def spans(self) -> tuple[CapturedSpan, ...]:
        return tuple(self._spans)

    def clear(self) -> None:
        self._spans.clear()
        self._next_sequence = 0

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        return self._span_cm(name, parent=None, attributes=attributes)

    def _span_cm(
        self, name: str, *, parent: str | None, attributes: SpanAttributes | None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        return _recording_span_cm(self, name, parent, attributes)

    def _commit(self, live: _LiveRecordingSpan) -> None:
        seq = self._next_sequence
        self._next_sequence += 1
        self._spans.append(
            CapturedSpan(
                name=live.name,
                parent=live.parent,
                attributes=dict(live._attributes),
                status=live._status,
                status_description=live._status_description,
                events=tuple(live._events),
                end_sequence=seq,
                had_exception=live._had_exception,
            )
        )


# ---------------------------------------------------------------------------
# Schema conformance: does the output match what schema.py declares
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaViolation:
    span_name: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.span_name}: {self.detail}"


class SchemaConformanceError(PsychError):
    def __init__(self, violations: Sequence[SchemaViolation]) -> None:
        self.violations = tuple(violations)
        listed = "\n  ".join(str(v) for v in self.violations)
        count = len(self.violations)
        noun = "span" if count == 1 else "spans"
        super().__init__(f"{count} recorded {noun} violate the schema:\n  {listed}")


def _value_matches_type(value: SpanAttributeValue, attr_type: AttributeType) -> bool:
    if attr_type is AttributeType.STRING:
        return isinstance(value, str)
    if attr_type is AttributeType.INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if attr_type is AttributeType.FLOAT:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if attr_type is AttributeType.BOOL:
        return isinstance(value, bool)
    if attr_type is AttributeType.STRING_ARRAY:
        return isinstance(value, tuple) and all(isinstance(v, str) for v in value)
    raise AssertionError(f"unhandled AttributeType {attr_type!r}")  # pragma: no cover


def _check_parent(
    definition: SpanDefinition, parent: str | None, schema: TelemetrySchema
) -> SchemaViolation | None:
    constraint = definition.parent
    if isinstance(constraint, AnyParent):
        return None
    if isinstance(constraint, RootOrExternalParent):
        if parent is None or parent not in schema.spans:
            return None
        return SchemaViolation(
            definition.name,
            "illegal_parent",
            f"parent {parent!r} is a span this schema declares, but "
            f"{definition.name!r} requires root_or_external (no in-schema parent)",
        )
    if isinstance(constraint, NamedParents):
        if parent in constraint.spans:
            return None
        return SchemaViolation(
            definition.name,
            "illegal_parent",
            f"parent {parent!r} is not among the legal parents {constraint.spans!r}",
        )
    raise AssertionError(f"unhandled ParentConstraint {constraint!r}")  # pragma: no cover


def check_schema_conformance(
    schema: TelemetrySchema, spans: Sequence[CapturedSpan]
) -> tuple[SchemaViolation, ...]:
    """Check every captured span against its declared shape in ``schema``.

    ``schema.py`` is data, not types, so a renamed attribute or a span moved
    under the wrong parent breaks nothing until someone reads the trace and
    finds the field gone. This function is what turns that into a failure.
    Checked per span:
    the span is declared, every required start attribute is present in the
    final attribute set, every present attribute is declared with a matching
    type (and, where the schema closes the value set, a legal value), and the
    parent satisfies the span's ``ParentConstraint``.
    """
    violations: list[SchemaViolation] = []
    for span in spans:
        definition = schema.spans.get(span.name)
        if definition is None:
            violations.append(
                SchemaViolation(
                    span.name, "unknown_span", f"{span.name!r} is not declared in the schema"
                )
            )
            continue

        declared = {**definition.start_attributes, **definition.end_attributes}

        for attr_name, attr_def in definition.start_attributes.items():
            if attr_def.required and attr_name not in span.attributes:
                violations.append(
                    SchemaViolation(
                        span.name, "missing_required", f"missing required attribute {attr_name!r}"
                    )
                )

        for attr_name, value in span.attributes.items():
            declared_def = declared.get(attr_name)
            if declared_def is None:
                violations.append(
                    SchemaViolation(
                        span.name,
                        "undeclared_attribute",
                        f"attribute {attr_name!r} is not declared for span {span.name!r}",
                    )
                )
                continue
            if not _value_matches_type(value, declared_def.type):
                violations.append(
                    SchemaViolation(
                        span.name,
                        "wrong_type",
                        f"attribute {attr_name!r}={value!r} does not match declared type "
                        f"{declared_def.type.value}",
                    )
                )
                continue
            if declared_def.values is not None:
                candidates = value if isinstance(value, tuple) else (value,)
                bad = [v for v in candidates if v not in declared_def.values]
                if bad:
                    violations.append(
                        SchemaViolation(
                            span.name,
                            "invalid_enum_value",
                            f"attribute {attr_name!r} has value(s) {bad!r} outside the closed "
                            f"set {declared_def.values!r}",
                        )
                    )

        parent_violation = _check_parent(definition, span.parent, schema)
        if parent_violation is not None:
            violations.append(parent_violation)

    return tuple(violations)


def assert_schema_conformant(schema: TelemetrySchema, spans: Sequence[CapturedSpan]) -> None:
    violations = check_schema_conformance(schema, spans)
    if violations:
        raise SchemaConformanceError(violations)


# ---------------------------------------------------------------------------
# Group 1: the adapter contract
# ---------------------------------------------------------------------------


class _Unreadable(Mapping[str, SpanAttributeValue]):
    """A mapping whose every access raises. An adapter that iterates attributes
    without guarding gets to prove it survives one."""

    def __getitem__(self, key: str) -> SpanAttributeValue:
        raise RuntimeError("this attribute payload is unreadable")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("this attribute payload is unreadable")

    def __len__(self) -> int:
        raise RuntimeError("this attribute payload is unreadable")


class _HostileError(Exception):
    """An exception whose own message cannot be read. Recording a failure must
    not itself fail."""

    def __str__(self) -> str:
        raise RuntimeError("this exception's message is unreadable")


@dataclass(frozen=True, slots=True)
class _Sentinel:
    """A distinctive non-Exception-shaped payload, to check that a thrown
    value passes through a span unmodified rather than being coerced."""

    tag: str


class TelemetryAdapterConformanceSuite:
    """Subclass this and provide ``telemetry`` and ``captured_spans``
    fixtures. Every test method is a coroutine; ``pytest-asyncio`` runs in
    ``auto`` mode project-wide, so no marker is needed.

    ``captured_spans`` returns a zero-argument callable rather than a static
    sequence because several tests need a fresh read after each ``async
    with`` block settles.
    """

    @pytest.fixture
    def telemetry(self) -> Telemetry:
        raise NotImplementedError(
            "subclasses of TelemetryAdapterConformanceSuite must override the "
            "`telemetry` fixture to return the implementation under test"
        )

    @pytest.fixture
    def captured_spans(self) -> Callable[[], Sequence[CapturedSpan]]:
        raise NotImplementedError(
            "subclasses of TelemetryAdapterConformanceSuite must override the "
            "`captured_spans` fixture to return a snapshot of what was recorded"
        )

    # -- callback lifecycle ---------------------------------------------

    async def test_callback_runs_exactly_once(self, telemetry: Telemetry) -> None:
        calls = 0
        async with telemetry.start_span("s"):
            calls += 1
        assert calls == 1

    async def test_return_value_passes_through_the_caller_unmodified(
        self, telemetry: Telemetry
    ) -> None:
        sentinel = _Sentinel("ok")
        async with telemetry.start_span("s"):
            result = sentinel
        assert result is sentinel

    async def test_thrown_exception_propagates_as_the_same_instance(
        self, telemetry: Telemetry
    ) -> None:
        exc = ValueError("boom")
        with pytest.raises(ValueError, match="boom") as excinfo:
            async with telemetry.start_span("s"):
                raise exc
        assert excinfo.value is exc

    async def test_unreadable_thrown_exception_still_propagates(self, telemetry: Telemetry) -> None:
        with pytest.raises(_HostileError):
            async with telemetry.start_span("s"):
                raise _HostileError("unreadable")

    # -- status ------------------------------------------------------------

    async def test_explicit_ok_then_throw_stays_ok(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        with contextlib.suppress(ValueError):
            async with telemetry.start_span("s") as span:
                span.set_status(SpanStatus.OK)
                raise ValueError("boom")
        recorded = [s for s in captured_spans() if s.name == "s"]
        assert recorded
        assert recorded[-1].status is SpanStatus.OK

    async def test_explicit_error_keeps_its_own_message_after_a_throw(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        with contextlib.suppress(ValueError):
            async with telemetry.start_span("s") as span:
                span.set_status(SpanStatus.ERROR, "explicit reason")
                raise ValueError("a different reason")
        recorded = [s for s in captured_spans() if s.name == "s"]
        assert recorded
        assert recorded[-1].status is SpanStatus.ERROR
        assert recorded[-1].status_description == "explicit reason"

    async def test_unhandled_exception_sets_error_automatically(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        with contextlib.suppress(ValueError):
            async with telemetry.start_span("s"):
                raise ValueError("boom")
        recorded = [s for s in captured_spans() if s.name == "s"]
        assert recorded
        assert recorded[-1].status is SpanStatus.ERROR

    async def test_clean_exit_defaults_to_ok(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s"):
            pass
        recorded = [s for s in captured_spans() if s.name == "s"]
        assert recorded
        assert recorded[-1].status is SpanStatus.OK

    # -- recording -----------------------------------------------------

    async def test_set_attributes_merges_across_calls(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s", attributes={"a": "1"}) as span:
            span.set_attributes({"b": "2"})
        recorded = [s for s in captured_spans() if s.name == "s"][-1]
        assert recorded.attributes["a"] == "1"
        assert recorded.attributes["b"] == "2"

    async def test_none_value_deletes_a_previously_set_key(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s", attributes={"a": "1"}) as span:
            span.set_attributes({"a": None})
        recorded = [s for s in captured_spans() if s.name == "s"][-1]
        assert "a" not in recorded.attributes

    async def test_hostile_attributes_call_is_discarded_atomically(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s", attributes={"kept": "yes"}) as span:
            span.set_attributes(_Unreadable())
        recorded = [s for s in captured_spans() if s.name == "s"][-1]
        assert recorded.attributes == {"kept": "yes"}

    async def test_events_are_recorded_in_order(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s") as span:
            span.add_event("first")
            span.add_event("second")
        recorded = [s for s in captured_spans() if s.name == "s"][-1]
        assert [e.name for e in recorded.events] == ["first", "second"]

    async def test_span_methods_are_inert_after_settlement(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("s") as span:
            pass
        # None of these may raise, and none may change what was already recorded.
        span.set_attributes({"late": "value"})
        span.add_event("late-event")
        span.set_status(SpanStatus.ERROR, "too late")
        recorded = [s for s in captured_spans() if s.name == "s"][-1]
        assert "late" not in recorded.attributes
        assert recorded.status is SpanStatus.OK

    async def test_start_span_after_settlement_still_runs_its_own_callback(
        self, telemetry: Telemetry
    ) -> None:
        async with telemetry.start_span("s") as span:
            pass
        ran = False
        async with span.start_span("child-of-a-settled-span"):
            ran = True
        assert ran

    # -- parentage -----------------------------------------------------

    async def test_nested_span_records_the_correct_parent(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async with telemetry.start_span("parent") as parent, parent.start_span("child"):
            pass
        spans = {s.name: s for s in captured_spans()}
        assert spans["child"].parent == "parent"

    async def test_concurrent_children_both_attach_to_the_same_parent(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async def child(parent: TelemetrySpan, name: str, delay: float) -> None:
            async with parent.start_span(name):
                await asyncio.sleep(delay)

        async with telemetry.start_span("parent") as parent:
            await asyncio.gather(
                child(parent, "child-a", 0.02),
                child(parent, "child-b", 0.0),
            )
        spans = {s.name: s for s in captured_spans()}
        assert spans["child-a"].parent == "parent"
        assert spans["child-b"].parent == "parent"

    async def test_end_sequence_reflects_settlement_order_not_creation_order(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        async def child(parent: TelemetrySpan, name: str, delay: float) -> None:
            async with parent.start_span(name):
                await asyncio.sleep(delay)

        async with telemetry.start_span("parent") as parent:
            # child-slow is opened first but settles second.
            await asyncio.gather(
                child(parent, "child-slow", 0.03),
                child(parent, "child-fast", 0.0),
            )
        spans = {s.name: s for s in captured_spans()}
        assert spans["child-fast"].end_sequence < spans["child-slow"].end_sequence

    # -- passivity -------------------------------------------------------

    async def test_unreadable_start_attributes_do_not_block_the_callback(
        self, telemetry: Telemetry
    ) -> None:
        ran = False
        async with telemetry.start_span("s", attributes=_Unreadable()):
            ran = True
        assert ran

    async def test_unreadable_attributes_passed_mid_span_do_not_raise(
        self, telemetry: Telemetry
    ) -> None:
        async with telemetry.start_span("s") as span:
            span.set_attributes(_Unreadable())  # must not raise

    async def test_unreadable_event_attributes_do_not_raise(self, telemetry: Telemetry) -> None:
        async with telemetry.start_span("s") as span:
            span.add_event("e", _Unreadable())  # must not raise


# ---------------------------------------------------------------------------
# Group 2: schema conformance, Psych-original
# ---------------------------------------------------------------------------


class SchemaConformanceSuite:
    """Subclass this and provide ``telemetry`` and ``captured_spans``
    fixtures, exactly as for ``TelemetryAdapterConformanceSuite``.

    Where that suite checks adapter *behaviour*, this one checks adapter
    *output* against ``schema.py``: every span this implementation is asked
    to emit for each declared span in the schema must come back with its
    required attributes present, no undeclared attributes, and a legal parent.
    """

    @pytest.fixture
    def telemetry(self) -> Telemetry:
        raise NotImplementedError(
            "subclasses of SchemaConformanceSuite must override the `telemetry` fixture"
        )

    @pytest.fixture
    def captured_spans(self) -> Callable[[], Sequence[CapturedSpan]]:
        raise NotImplementedError(
            "subclasses of SchemaConformanceSuite must override the `captured_spans` fixture"
        )

    async def test_every_declared_span_emitted_correctly_is_conformant(
        self, telemetry: Telemetry, captured_spans: Callable[[], Sequence[CapturedSpan]]
    ) -> None:
        await _emit_psych_schema_span_tree(telemetry)
        violations = check_schema_conformance(PSYCH_SCHEMA, captured_spans())
        assert violations == ()


async def _emit_psych_schema_span_tree(telemetry: Telemetry) -> None:
    """Emit one instance of every span ``PSYCH_SCHEMA`` declares, nested the
    way real Psych code nests them: a run holding an attempt, which holds
    both a bare turn (with its model call and tool call), a compaction beside
    it, and a workflow step (with a subagent delegation). Attribute values are
    the first legal value
    for an enum attribute and a type-appropriate placeholder otherwise.

    Hand-nested rather than built generically from the schema's parent
    constraints, because ``PSYCH_SCHEMA`` is a fixed, small, known shape and a
    literal tree here is easier to verify by reading than a graph walker
    would be.
    """
    run_def = PSYCH_SCHEMA.spans["psych.run"]
    async with telemetry.start_span(
        "psych.run", attributes=_placeholder_attributes(run_def)
    ) as run:
        run.set_attributes(_placeholder_attributes(run_def, end=True))

        attempt_def = PSYCH_SCHEMA.spans["psych.attempt"]
        async with run.start_span(
            "psych.attempt", attributes=_placeholder_attributes(attempt_def)
        ) as attempt:
            attempt.set_attributes(_placeholder_attributes(attempt_def, end=True))

            turn_def = PSYCH_SCHEMA.spans["psych.turn"]
            async with attempt.start_span(
                "psych.turn", attributes=_placeholder_attributes(turn_def)
            ) as turn:
                turn.set_attributes(_placeholder_attributes(turn_def, end=True))

                model_call_def = PSYCH_SCHEMA.spans["psych.model_call"]
                async with turn.start_span(
                    "psych.model_call", attributes=_placeholder_attributes(model_call_def)
                ) as model_call:
                    model_call.set_attributes(_placeholder_attributes(model_call_def, end=True))

                tool_call_def = PSYCH_SCHEMA.spans["psych.tool_call"]
                async with turn.start_span(
                    "psych.tool_call", attributes=_placeholder_attributes(tool_call_def)
                ) as tool_call:
                    tool_call.set_attributes(_placeholder_attributes(tool_call_def, end=True))

            # Beside the turn rather than inside it, which is where the
            # runtime opens it: summarising is not a turn, so a trace that
            # nested it under one would say a turn made two model calls.
            compaction_def = PSYCH_SCHEMA.spans["psych.compaction"]
            async with attempt.start_span(
                "psych.compaction", attributes=_placeholder_attributes(compaction_def)
            ) as compaction:
                compaction.set_attributes(_placeholder_attributes(compaction_def, end=True))

            step_def = PSYCH_SCHEMA.spans["psych.step"]
            async with attempt.start_span(
                "psych.step", attributes=_placeholder_attributes(step_def)
            ) as step:
                step.set_attributes(_placeholder_attributes(step_def, end=True))

                delegation_def = PSYCH_SCHEMA.spans["psych.subagent_delegation"]
                async with step.start_span(
                    "psych.subagent_delegation", attributes=_placeholder_attributes(delegation_def)
                ) as delegation:
                    delegation.set_attributes(_placeholder_attributes(delegation_def, end=True))


def _placeholder_attributes(
    definition: SpanDefinition, *, end: bool = False
) -> dict[str, SpanAttributeValue]:
    attrs = definition.end_attributes if end else definition.start_attributes
    values: dict[str, SpanAttributeValue] = {}
    for attr_name, attr_def in attrs.items():
        if attr_def.values is not None:
            values[attr_name] = attr_def.values[0]
        elif attr_def.type is AttributeType.STRING:
            values[attr_name] = "x"
        elif attr_def.type is AttributeType.INT:
            values[attr_name] = 1
        elif attr_def.type is AttributeType.FLOAT:
            values[attr_name] = 1.0
        elif attr_def.type is AttributeType.BOOL:
            values[attr_name] = True
        elif attr_def.type is AttributeType.STRING_ARRAY:
            values[attr_name] = ("x",)
    return values
