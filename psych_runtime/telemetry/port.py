"""The Telemetry port.

DESIGN.md §13.5: OpenTelemetry sits behind a port, and the default does
nothing. A Run must be indifferent to whether telemetry is wired up at all,
so the shape here is deliberately small: one entry point, ``start_span``, used
either as ``telemetry.start_span(...)`` for a root span or
``span.start_span(...)`` from inside an open span for a child. There is no
separate "end span" call.

## Why a context manager and not ``span.end()``

A span exists for exactly the duration of the block that opened it, and settles
on the way out whether that is a return or an exception. An imperative
start/end pair puts the end call in the caller's hands, and the first
``raise`` past it leaves a span open forever. An async context manager cannot
be got wrong that way.

## Why a raising Telemetry cannot break a Run

A consumer's own OpenTelemetry setup, exporter, or third-party backend can be
broken in ways Psych has no control over: a full disk under the exporter, a
misconfigured endpoint, a custom ``Telemetry`` with a typo in it. None of that
is allowed to be the reason a Run failed. ``GuardedTelemetry`` wraps any
``Telemetry`` implementation and turns every failure it produces into "no span
was recorded" rather than a propagated exception, while still letting the
wrapped code run: only the telemetry machinery is caught, never the
application code inside the span.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "NOOP_TELEMETRY",
    "GuardedTelemetry",
    "NoOpTelemetry",
    "SpanAttributeValue",
    "SpanStatus",
    "Telemetry",
    "TelemetrySpan",
]

type SpanAttributeValue = str | int | float | bool | tuple[str, ...]
"""What a span attribute may hold. Matches the OpenTelemetry attribute value
grammar (scalar or homogeneous string tuple) so the OTel adapter never needs
to coerce a value on the way out."""

type SpanAttributes = Mapping[str, SpanAttributeValue | None]
"""Attributes as passed to a call. A value of ``None`` deletes that key, or is
ignored if the key was never set. The same rule holds for the attributes a span
opens with and for every later ``set_attributes``, so a caller building a
mapping conditionally never has to branch on whether to include a key."""


class SpanStatus(StrEnum):
    """A span's outcome. Defaults to ``OK``; an implementation sets ``ERROR``
    automatically on an unhandled exception unless the caller already set a
    status explicitly, and an explicit call always wins (DESIGN.md §13.5).
    The conformance suite's "status" group holds implementations to that."""

    OK = "ok"
    ERROR = "error"


@runtime_checkable
class Telemetry(Protocol):
    """The root of the tree. Every Run's telemetry starts here.

    An implementation must never let a failure of its own escape into the
    caller: wrap an untrusted implementation in ``GuardedTelemetry`` rather
    than relying on callers to guard themselves.
    """

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        """Open a root span named ``name``.

        Returns an async context manager. The span is live for the body of the
        ``async with`` block, settles when the block exits, and its status is
        ``ERROR`` if the block raised and no explicit status was set,
        ``OK`` otherwise.
        """
        ...


@runtime_checkable
class TelemetrySpan(Telemetry, Protocol):
    """An open span. Also a ``Telemetry``, so nesting is just calling
    ``start_span`` again on the span you are already inside."""

    def set_attributes(self, attributes: SpanAttributes) -> None:
        """Merge attributes into the span. Repeated calls merge, they never
        replace what was set before. The conformance suite's "recording" group
        holds implementations to that."""
        ...

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        """Record a named, timestamped sub-event on the span. Events are kept
        in the order they were added."""
        ...

    def set_status(self, status: SpanStatus, description: str = "") -> None:
        """Set the span's status explicitly. Once called, the span's
        automatic-error-on-throw behaviour no longer applies: this call always
        wins over whatever the span would otherwise have settled to."""
        ...

    def record_exception(self, exc: BaseException) -> None:
        """Attach an exception to the span as data, independent of status.
        A span whose body raises records the exception this way automatically;
        call this directly only for an exception that was caught and handled
        without failing the span."""
        ...


# ---------------------------------------------------------------------------
# The no-op default
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Every method is inert. Nothing is stored, nothing is measured."""

    __slots__ = ()

    def start_span(
        self,
        name: str,  # noqa: ARG002
        *,
        attributes: SpanAttributes | None = None,  # noqa: ARG002
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        return _noop_span_cm()

    def set_attributes(self, attributes: SpanAttributes) -> None:  # noqa: ARG002
        return None

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:  # noqa: ARG002
        return None

    def set_status(self, status: SpanStatus, description: str = "") -> None:  # noqa: ARG002
        return None

    def record_exception(self, exc: BaseException) -> None:  # noqa: ARG002
        return None


_NOOP_SPAN: Final[_NoOpSpan] = _NoOpSpan()


@asynccontextmanager
async def _noop_span_cm() -> AsyncIterator[TelemetrySpan]:
    yield _NOOP_SPAN


class NoOpTelemetry:
    """DESIGN.md §13.5's default: OpenTelemetry is opt-in, so a consumer who
    never wires up a ``Telemetry`` gets a Run that behaves identically, just
    without spans."""

    __slots__ = ()

    def start_span(
        self,
        name: str,  # noqa: ARG002
        *,
        attributes: SpanAttributes | None = None,  # noqa: ARG002
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        return _noop_span_cm()


NOOP_TELEMETRY: Final[Telemetry] = NoOpTelemetry()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class _GuardedSpan:
    """Wraps one open span so every method on it is passive to the wrapped
    implementation's own failures."""

    __slots__ = ("_inner",)

    def __init__(self, inner: TelemetrySpan) -> None:
        self._inner = inner

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        try:
            inner_cm = self._inner.start_span(name, attributes=attributes)
        except Exception:
            inner_cm = None
        return _guarded_span_cm(inner_cm)

    def set_attributes(self, attributes: SpanAttributes) -> None:
        with contextlib.suppress(Exception):
            self._inner.set_attributes(attributes)

    def add_event(self, name: str, attributes: SpanAttributes | None = None) -> None:
        with contextlib.suppress(Exception):
            self._inner.add_event(name, attributes)

    def set_status(self, status: SpanStatus, description: str = "") -> None:
        with contextlib.suppress(Exception):
            self._inner.set_status(status, description)

    def record_exception(self, exc: BaseException) -> None:
        with contextlib.suppress(Exception):
            self._inner.record_exception(exc)


@asynccontextmanager
async def _guarded_span_cm(
    inner_cm: AbstractAsyncContextManager[TelemetrySpan] | None,
) -> AsyncIterator[TelemetrySpan]:
    span: TelemetrySpan
    if inner_cm is None:
        span = _NOOP_SPAN
    else:
        try:
            span = await inner_cm.__aenter__()
        except Exception:
            span = _NOOP_SPAN
            inner_cm = None

    guarded = _GuardedSpan(span)
    try:
        yield guarded
    except BaseException as exc:
        if inner_cm is not None:
            suppressed = False
            with contextlib.suppress(Exception):
                suppressed = bool(await inner_cm.__aexit__(type(exc), exc, exc.__traceback__))
            if not suppressed:
                raise
        else:
            raise
    else:
        if inner_cm is not None:
            with contextlib.suppress(Exception):
                await inner_cm.__aexit__(None, None, None)


class GuardedTelemetry:
    """Wraps any ``Telemetry`` implementation so its own failures can never
    reach the caller.

    Use this around a consumer-supplied ``Telemetry`` at the one place the
    agent loop obtains it, rather than trusting every implementation to be as
    careful as ``NoOpTelemetry`` and the recording adapter are. Application
    code running inside a span is untouched: only exceptions raised by the
    telemetry machinery itself (opening a span, closing it, recording an
    attribute) are swallowed.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: Telemetry) -> None:
        self._inner = inner

    def start_span(
        self, name: str, *, attributes: SpanAttributes | None = None
    ) -> AbstractAsyncContextManager[TelemetrySpan]:
        try:
            inner_cm = self._inner.start_span(name, attributes=attributes)
        except Exception:
            inner_cm = None
        return _guarded_span_cm(inner_cm)
