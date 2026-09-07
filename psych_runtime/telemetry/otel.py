"""The OpenTelemetry adapter.

DESIGN.md §13.5. Backed by ``opentelemetry-api``/``opentelemetry-sdk``,
installed under the ``otel`` extra. This module imports them at module scope,
so importing ``psych_runtime.telemetry.otel`` without the extra installed raises the
usual ``ModuleNotFoundError`` right there, exactly as ``psych_runtime.store.postgres``
does for ``asyncpg`` without the ``postgres`` extra. Nothing in
``psych_runtime.telemetry`` (or anywhere else in Psych) imports this module for you,
so a consumer who never asks for OTel never pays the import cost, and Psych
works with no OTel installed at all.

## Why parentage is built explicitly, never read from ambient context

The usual OpenTelemetry idiom makes the "current span" implicit through a
contextvar (``tracer.start_as_current_span``). Psych's ``Telemetry`` port
nests explicitly instead: ``span.start_span(...)`` called on the span you are
already inside (``psych_runtime.telemetry.port``). This adapter honours that for every
*nested* span: a child's parent context is built from the parent ``Span`` object it
was opened on, via ``trace.set_span_in_context``, never read back out of
ambient/current context. That keeps two concurrent children of the same
parent correctly attributed regardless of asyncio task scheduling.

The one exception is a *root* call (``OtelTelemetry.start_span`` itself,
never a span's own ``start_span``): it uses the ambient OTel context as its
parent, which is exactly what ``psych_runtime.run``'s ``root_or_external`` schema
constraint asks for -- if the consumer's own instrumentation already has a
span open (a web framework's request span, say), the Run nests inside it;
otherwise it is a genuine root.

## Why attributes are buffered rather than written incrementally

Psych's port lets ``None`` delete a previously-set attribute
(``psych_runtime.telemetry.port.SpanAttributes``). OpenTelemetry spans have no such
operation: once ``set_attribute`` has been called, that key cannot be
retracted from the real span. So this adapter never writes to the
OpenTelemetry span incrementally -- it keeps its own merged attribute dict
exactly as ``psych_runtime.telemetry.conformance.RecordingTelemetry`` does, and
writes the whole thing to the real span exactly once, at settlement, right
before ``end()``. Nothing is exported before that (a ``SimpleSpanProcessor``
exports on ``end()``), so there is nothing stale to retract.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span as OtelSpan
from opentelemetry.trace import Status, StatusCode, Tracer

from psych_runtime.telemetry.port import (
    NOOP_TELEMETRY,
    SpanAttributes,
    SpanAttributeValue,
    SpanStatus,
    TelemetrySpan,
)

__all__ = ["OtelTelemetry"]

_STATUS_CODE: dict[SpanStatus, StatusCode] = {
    SpanStatus.OK: StatusCode.OK,
    SpanStatus.ERROR: StatusCode.ERROR,
}


def _merge(target: dict[str, SpanAttributeValue], attributes: SpanAttributes) -> None:
    """Merge ``attributes`` into ``target`` in place, atomically: a payload
    that fails to read leaves ``target`` untouched, and a ``None`` value
    deletes the key (``psych_runtime.telemetry.port``)."""
    try:
        items = list(attributes.items())
    except Exception:
        return
    for key, value in items:
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


class _LiveOtelSpan:
    """One open span, backed by a real (not yet ended) OpenTelemetry
    ``Span``. Mirrors ``psych_runtime.telemetry.conformance._LiveRecordingSpan``'s
    merge/status/settle logic, targeting a real span instead of a
    ``CapturedSpan``."""

    def __init__(self, tracer: Tracer, otel_span: OtelSpan) -> None:
        self._tracer = tracer
        self._otel_span = otel_span
        self._attributes: dict[str, SpanAttributeValue] = {}
        self._status = SpanStatus.OK
        self._status_description = ""
        self._explicit_status = False
        self._had_exception = False
        self._settled = False

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        if self._settled:
            return NOOP_TELEMETRY.start_span(name, attributes=attributes)
        parent_context = trace.set_span_in_context(self._otel_span)
        return _otel_span_cm(self._tracer, name, parent_context, attributes)

    def set_attributes(self, attributes: SpanAttributes) -> None:
        if self._settled:
            return
        _merge(self._attributes, attributes)

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        if self._settled:
            return
        payload: dict[str, SpanAttributeValue] = {}
        if attributes is not None:
            _merge(payload, attributes)
        with contextlib.suppress(Exception):
            self._otel_span.add_event(name, attributes=payload)

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
        with contextlib.suppress(Exception):
            self._otel_span.record_exception(exc)

    def _settle(self) -> None:
        if self._settled:
            return
        self._settled = True
        if not self._explicit_status and self._had_exception:
            self._status = SpanStatus.ERROR
        with contextlib.suppress(Exception):
            if self._attributes:
                self._otel_span.set_attributes(self._attributes)
            self._otel_span.set_status(
                Status(_STATUS_CODE[self._status], self._status_description or None)
            )
            self._otel_span.end()


@asynccontextmanager
async def _otel_span_cm(
    tracer: Tracer,
    name: str,
    parent_context: Context | None,
    attributes: SpanAttributes | None,
) -> AsyncIterator[TelemetrySpan]:
    otel_span = tracer.start_span(
        name,
        context=parent_context,
        record_exception=False,
        set_status_on_exception=False,
    )
    live = _LiveOtelSpan(tracer, otel_span)
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


class OtelTelemetry:
    """``Telemetry`` backed by a real OpenTelemetry ``Tracer``.

    Attributes:
        tracer: an existing ``Tracer`` to use, e.g. from a consumer's own
            ``TracerProvider``. Defaults to ``opentelemetry.trace.get_tracer``
            with ``tracer_name``, which resolves against whatever global
            ``TracerProvider`` the consumer has configured (or a no-op one if
            they configured none -- OpenTelemetry's own default, independent
            of Psych's).
    """

    def __init__(self, tracer: Tracer | None = None, *, tracer_name: str = "psych") -> None:
        self._tracer = tracer if tracer is not None else trace.get_tracer(tracer_name)

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        return _otel_span_cm(self._tracer, name, None, attributes)
