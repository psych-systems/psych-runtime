"""A deployment ceiling for the size of a single server-sent event.

The HTTP client underneath the MCP SDK refuses any one server-sent event
larger than a megabyte. A Streamable HTTP server answers a JSON-RPC request
by writing the whole response as one event, so that limit is really a limit
on how large a single MCP response may be -- and ``tools/list`` from a server
that exposes a few hundred tools with full JSON Schemas passes it easily.

Two things then go wrong at once. The response is refused, which is a real
limit worth having but set far too low for a catalogue. And the refusal is
lost: the SDK reads its event stream inside a ``except Exception`` that
swallows the cause and synthesises "SSE stream ended without a response",
which reads exactly like a network fault. Every layer below reports success,
so the failure is attributed to the server, the network, or OAuth, none of
which is involved.

This module addresses both. ``BoundedEventSource`` raises the ceiling to a
value a catalogue can actually use, and reports a breach of the new ceiling
as ``SseEventTooLarge`` -- deliberately a ``BaseException``, because the
SDK's ``except Exception`` would otherwise swallow this one too and put back
the misleading message this module exists to remove. Nothing recovers from a
response that cannot be read, so unwinding the connection is the right
outcome; what matters is that it unwinds saying what happened.

The ceiling is process-wide rather than per-agent. It belongs to the HTTP
client shared by every connection in the process, and there is no point in
the SDK's call path where a per-connection value could be threaded through.
``set_max_sse_event_bytes`` therefore configures a deployment, in the same
sense as a blob offload threshold, and the last caller wins.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from types import ModuleType
from typing import Final, cast

import httpx2
from httpx2 import EventSource, ServerSentEvent, SSEError

__all__ = [
    "DEFAULT_MAX_SSE_EVENT_BYTES",
    "BoundedEventSource",
    "SseEventTooLarge",
    "install_event_source",
    "max_sse_event_bytes",
    "patched_modules",
    "set_max_sse_event_bytes",
    "unpatched_modules",
]

DEFAULT_MAX_SSE_EVENT_BYTES: Final = 16 * 1024 * 1024
"""Large enough for the tool catalogue of a server with several hundred tools,
and still bounded: an MCP server is a third party, and a response no ceiling
applies to is one that can exhaust the memory of the process reading it."""

_max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES
_USE_PSYCH_LIMIT: Final = object()


class SseEventTooLarge(BaseException):
    """One server-sent event exceeded the configured ceiling.

    A ``BaseException`` on purpose, and the reason is the whole point of this
    module: the SDK reads its event stream inside a bare ``except Exception``.
    Anything catchable is caught there, discarded, and reported as a stream
    that ended without a response -- which names neither the size, nor the
    ceiling, nor the request that hit it. Deriving from ``BaseException``
    passes through that handler so the connection fails with its actual cause.

    It is translated at Psych's boundary and never escapes to a caller.
    """

    def __init__(self, limit: int, url: str) -> None:
        self.limit = limit
        self.url = url
        super().__init__(f"a single server-sent event from {url} exceeded the {limit}-byte ceiling")


def max_sse_event_bytes() -> int:
    """The ceiling currently applied to every new event stream."""
    return _max_event_bytes


def set_max_sse_event_bytes(limit: int) -> None:
    """Set the process-wide ceiling. Applies to streams opened afterwards."""
    if limit <= 0:
        raise ValueError("the maximum server-sent event size must be positive")
    global _max_event_bytes  # noqa: PLW0603 - one process-wide deployment ceiling
    _max_event_bytes = limit


class BoundedEventSource(EventSource):
    """An ``EventSource`` bounded by Psych's ceiling rather than the default.

    The ceiling is read per stream rather than captured at import, so that
    ``set_max_sse_event_bytes`` takes effect without reinstalling anything.
    """

    def __init__(
        self,
        response: httpx2.Response,
        max_event_size: int | object | None = _USE_PSYCH_LIMIT,
    ) -> None:
        # MCP constructs EventSource without an explicit limit, which selects
        # the deployment ceiling. Preserve an explicit value for other callers:
        # importing Psych must not silently weaken a stricter bound chosen by
        # application code that also uses httpx2 directly.
        effective = (
            _max_event_bytes
            if max_event_size is _USE_PSYCH_LIMIT
            else cast("int | None", max_event_size)
        )
        self._psych_event_limit = effective
        super().__init__(response, max_event_size=effective)

    def _too_large(self) -> SseEventTooLarge:
        # ``None`` disables the parent parser's limit, so this path cannot be
        # reached for it. Keep the defensive check beside that invariant so it
        # remains true under optimized Python as well.
        limit = self._psych_event_limit
        if limit is None:  # pragma: no cover - the parent has no size check
            raise RuntimeError("an unlimited event source reported a size breach")
        return SseEventTooLarge(limit, str(self.response.request.url))

    def _size_is_the_only_risk(self) -> bool:
        """Whether an ``SSEError`` from the parser can only be the size one.

        The parser raises that type for exactly two reasons: a content type
        that is not an event stream, and an event past the ceiling. The first
        is decided by a header, before anything is parsed. Reading that header
        here -- rather than matching on the message, which would break the
        moment the wording changed -- says which of the two a later error must
        be, and leaves the content-type error to surface unaltered.
        """
        content_type, _, _ = self.response.headers.get("content-type", "").partition(";")
        return content_type.strip().lower() == "text/event-stream"

    def __iter__(self) -> Iterator[ServerSentEvent]:
        size_only = self._size_is_the_only_risk()
        # Cast because the parent annotates an Iterator but returns a
        # generator, and closing it is the whole point of the block below.
        inner = cast("Generator[ServerSentEvent, None, None]", super().__iter__())
        # Closed in a ``finally``, and this matters more than it looks. The
        # parent holds the response open inside a generator's ``with``. A
        # consumer that stops early closes *this* generator, not the one it
        # wraps, so without this the inner generator -- and the socket it is
        # reading -- would be left to the garbage collector. The parent has no
        # such problem, because there it is the consumer that holds it directly.
        try:
            while True:
                try:
                    event = next(inner)
                except StopIteration:
                    return
                except SSEError as err:
                    if not size_only:
                        raise
                    raise self._too_large() from err
                yield event
        finally:
            inner.close()

    async def __aiter__(self) -> AsyncIterator[ServerSentEvent]:
        size_only = self._size_is_the_only_risk()
        inner = cast("AsyncGenerator[ServerSentEvent, None]", super().__aiter__())
        try:
            while True:
                try:
                    event = await anext(inner)
                except StopAsyncIteration:
                    return
                except SSEError as err:
                    if not size_only:
                        raise
                    raise self._too_large() from err
                yield event
        finally:
            await inner.aclose()


def _client_modules() -> tuple[ModuleType, ...]:
    """Every imported module that could construct an event source.

    ``httpx2`` itself is included because the SDK reaches the class both ways:
    one module imported the symbol by value, another looks it up on ``httpx2``
    at call time. Patching only the first leaves the second on the default
    ceiling -- a failure that would show up on one transport and not the other.
    """
    names = [name for name in sys.modules if name == "httpx2" or name.startswith("mcp.")]
    return tuple(sys.modules[name] for name in sorted(names))


def unpatched_modules() -> tuple[str, ...]:
    """Modules still holding the unbounded-by-Psych class, for a guard test."""
    return tuple(
        module.__name__
        for module in _client_modules()
        if getattr(module, "EventSource", None) is EventSource
    )


def patched_modules() -> tuple[str, ...]:
    return tuple(
        module.__name__
        for module in _client_modules()
        if getattr(module, "EventSource", None) is BoundedEventSource
    )


def install_event_source() -> tuple[str, ...]:
    """Point every event-source binding at ``BoundedEventSource``.

    Idempotent, and safe to call after further SDK modules are imported.
    Returns the names of the modules rebound by this call.
    """
    rebound: list[str] = []
    for module in _client_modules():
        if getattr(module, "EventSource", None) is EventSource:
            module.EventSource = BoundedEventSource  # type: ignore[attr-defined]
            rebound.append(module.__name__)
    return tuple(rebound)
