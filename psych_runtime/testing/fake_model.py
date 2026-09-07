"""The scriptable fake model.

DESIGN.md §22: no automated test may make a real network call, and that rule is
only livable if the fake is strong enough to express the awkward cases. A fake
that only ever returns a clean final answer produces a test suite that has never
seen a malformed tool call, a stalled stream or a connection that dies mid-token,
which means the runtime code that has to survive those has never been tested
either.

``FakeModel`` scripts a run turn by turn. Each call to ``stream()`` consumes
exactly one scripted turn, in order:

```python
model = (
    FakeModel()
    .turn(text="Let me look that up.", tool_calls=[("lookup", {"id": "A1"})])
    .turn(text="Your order shipped.")
)
```

## What each scripted behaviour is for

- ``turn()`` scripts ordinary content: text, reasoning, tool calls, usage and
  finish reason, all streamed as more than one delta by default so a test never
  gets one blob to assert against.
- A malformed tool call is a ``ToolCallScript`` built with ``raw_arguments``
  instead of ``arguments``: whatever string is given is sent verbatim, valid
  JSON or not. The fake never validates or repairs it.
- A tool call naming a tool the request never offered needs no special support:
  script it in ``turn()`` and simply do not include that name in the
  ``ModelRequest.tools`` the test constructs. The fake never reads
  ``request.tools`` to decide what it is allowed to call, on purpose, because
  policing that is the resolver's job and a fake that polices it would hide a
  resolver bug.
- ``stalls()`` scripts a gap longer than the request's own
  ``idle_timeout_seconds``, to exercise the idle-timeout guard (DESIGN.md
  §8.5). Left unset, the gap is derived from the request that is actually
  passed to ``stream()``, so the fake respects whatever timeout the test wired
  in rather than guessing a number of its own.
- ``aborts_mid_token()`` scripts a stream that stops without ever emitting
  ``StreamDone``. ``ModelClient.stream`` documents this as the case that must
  not be mistaken for a short, complete answer.
- ``raises_transient()`` and ``raises_permanent()`` script a stream that fails
  before producing anything, at a status code the test controls, so
  ``psych_runtime.model.transient.classify_status`` and ``is_transient`` can be
  exercised against a real number rather than a guess.

## Running past the end of the script

Silently repeating the last turn would let a runaway tool-calling loop look
like a passing test. Instead ``stream()`` raises ``FakeModelScriptExhausted``,
naming how many turns were scripted and which call went past the end.

## What it records

Every ``ModelRequest`` handed to ``stream()`` lands on ``.requests``, in order,
whether or not the resulting stream is ever consumed. ``.last_request`` is a
convenience for the common case of asserting on the most recent one. This is
how a test proves the tool set is really recomputed each turn and that the
system prompt is assembled in the order DESIGN.md §19 fixes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from re import findall
from typing import Any, Final, Self

from psych_runtime.core.errors import PsychError, TransientError
from psych_runtime.core.ids import ToolCallId, new_tool_call_id
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.port import (
    ModelRequest,
    ReasoningDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
)

__all__ = [
    "FakeModel",
    "FakeModelScriptExhausted",
    "FakePermanentFailure",
    "FakeTransientFailure",
    "ToolCallScript",
]

_DEFAULT_KNOWN_MODELS: Final[tuple[str, ...]] = ("fake-standard", "fake-reasoning")

_DEFAULT_FRAGMENT_SIZE: Final = 4
"""Default width of a tool-call argument fragment, chosen small enough that
anything but a trivial payload splits into more than one delta, the way a real
provider's streaming JSON encoder does."""

_DEFAULT_STALL_SECONDS: Final = 0.05
"""Fallback stall length when the request disables its own idle timeout
(``idle_timeout_seconds == 0``) and the script did not pin a duration. Short,
because the only thing left to prove at that point is that the fake really
does stall."""

_STALL_MARGIN: Final = 0.05
"""Added to a request's ``idle_timeout_seconds`` when deriving a stall length,
so the gap reliably exceeds the deadline rather than landing exactly on it."""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FakeTransientFailure(TransientError):
    """A scripted retryable provider failure, carrying a real status code.

    Subclassing ``TransientError`` means ``is_transient`` recognises it without
    inspecting the status at all, the same way it recognises the idle-timeout
    guard's own error. ``status_code`` is carried anyway so a test can run the
    same number through ``classify_status`` and confirm the two agree.
    """

    def __init__(self, status_code: int, message: str | None = None) -> None:
        self.status_code = status_code
        super().__init__(message or f"fake model: transient failure, HTTP {status_code}")


class FakePermanentFailure(PsychError):
    """A scripted non-retryable provider failure, carrying a real status code."""

    def __init__(self, status_code: int, message: str | None = None) -> None:
        self.status_code = status_code
        super().__init__(message or f"fake model: permanent failure, HTTP {status_code}")


class FakeModelScriptExhausted(PsychError):
    """``stream()`` was called more times than the script has turns.

    Repeating the last turn would let a model stuck in a tool-calling loop look
    like it eventually produced a clean final answer. This fails loudly instead
    and names both how many turns were scripted and which call went past them,
    so the failure explains itself without a debugger.
    """

    def __init__(self, *, scripted: int, call_number: int) -> None:
        self.scripted = scripted
        self.call_number = call_number
        noun = "turn" if scripted == 1 else "turns"
        super().__init__(
            f"FakeModel script exhausted: only {scripted} {noun} scripted, but "
            f"stream() was called for the {_ordinal(call_number)} time (call "
            f"#{call_number}). Script another turn, or this is the runaway loop "
            "the test is supposed to catch."
        )


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallScript:
    """One tool call to script inside a turn.

    Exactly one of ``arguments`` or ``raw_arguments`` must be given.
    ``arguments`` is JSON-encoded automatically, the normal case. ``raw_arguments``
    is sent to the wire verbatim, valid JSON or not: this is how a malformed
    tool call is scripted, by passing text that will not parse, such as a
    truncated object.

    ``fragments``, if given, is the exact sequence of strings the arguments are
    split across on the wire; they must concatenate back to the arguments
    exactly. Left unset, the fake splits them itself into fixed-size pieces,
    enough to prove a consumer accumulates fragments rather than assuming one
    delta carries the whole payload.
    """

    name: str
    arguments: dict[str, Any] | None = None
    raw_arguments: str | None = None
    call_id: ToolCallId | None = None
    fragments: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if (self.arguments is None) == (self.raw_arguments is None):
            raise ValueError(
                "ToolCallScript needs exactly one of `arguments` or "
                f"`raw_arguments`, got arguments={self.arguments!r} "
                f"raw_arguments={self.raw_arguments!r}"
            )
        if self.fragments is not None and "".join(self.fragments) != self.wire_arguments:
            raise ValueError(
                "fragments must concatenate back to the wire arguments exactly, "
                f"got {self.fragments!r} for {self.wire_arguments!r}"
            )

    @property
    def wire_arguments(self) -> str:
        """What actually goes on the wire, fragment by fragment."""
        if self.raw_arguments is not None:
            return self.raw_arguments
        assert self.arguments is not None  # guaranteed by __post_init__
        return json.dumps(self.arguments)


def _normalize_tool_call(spec: ToolCallScript | tuple[str, dict[str, Any]]) -> ToolCallScript:
    if isinstance(spec, ToolCallScript):
        return spec
    name, arguments = spec
    return ToolCallScript(name=name, arguments=arguments)


def _default_fragments(text: str) -> tuple[str, ...]:
    # A tool call with empty arguments still needs one fragment: it is the only
    # delta that carries the call's id and name.
    if text == "":
        return ("",)
    return tuple(
        text[i : i + _DEFAULT_FRAGMENT_SIZE] for i in range(0, len(text), _DEFAULT_FRAGMENT_SIZE)
    )


# ---------------------------------------------------------------------------
# Text and reasoning chunking
# ---------------------------------------------------------------------------


def _split_into_chunks(text: str) -> tuple[str, ...]:
    """Default delta boundaries for streamed text: one delta per word.

    Concatenating the chunks reproduces ``text`` exactly, and anything longer
    than one word yields more than one chunk, so a test gets assert-able
    boundaries without hand-splitting the string itself. A test that needs
    specific boundaries passes a sequence of strings instead.
    """
    return tuple(findall(r"\S+\s*", text))


def _as_chunks(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        return _split_into_chunks(value)
    return tuple(value)


# ---------------------------------------------------------------------------
# Scripted turns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _NormalTurn:
    text_chunks: tuple[str, ...]
    reasoning_chunks: tuple[str, ...]
    tool_calls: tuple[ToolCallScript, ...]
    usage: Usage
    finish_reason: str
    chunk_delay_seconds: float
    cost: Cost | None = None
    """What a provider would have reported for this turn, when a test wants to
    exercise that path. ``None`` is the ordinary case and leaves the runtime to
    compute one from its price table."""


@dataclass(frozen=True, slots=True)
class _StallTurn:
    seconds: float | None


@dataclass(frozen=True, slots=True)
class _AbortTurn:
    text_chunks: tuple[str, ...]
    chunk_delay_seconds: float


@dataclass(frozen=True, slots=True)
class _RaiseTurn:
    exception: Exception


_ScriptedTurn = _NormalTurn | _StallTurn | _AbortTurn | _RaiseTurn


def _stall_seconds(turn: _StallTurn, request: ModelRequest) -> float:
    if turn.seconds is not None:
        return turn.seconds
    base = (
        request.idle_timeout_seconds
        if request.idle_timeout_seconds > 0
        else (_DEFAULT_STALL_SECONDS)
    )
    return base + _STALL_MARGIN


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


class FakeModel:
    """A scriptable ``ModelClient``, and nothing else.

    Structural, not a subclass: ``psych_runtime.model.port.ModelClient`` is a
    ``Protocol``, and this satisfies it by shape.
    """

    def __init__(self, *, known_models: Sequence[str] | None = None) -> None:
        self._script: list[_ScriptedTurn] = []
        self._next_index = 0
        self._known_models: tuple[str, ...] = (
            tuple(known_models) if known_models is not None else _DEFAULT_KNOWN_MODELS
        )
        self.requests: list[ModelRequest] = []

    # -- scripting ------------------------------------------------------

    def turn(
        self,
        *,
        text: str | Sequence[str] = "",
        tool_calls: Sequence[ToolCallScript | tuple[str, dict[str, Any]]] = (),
        reasoning: str | Sequence[str] | None = None,
        usage: Usage | None = None,
        cost: Cost | None = None,
        finish_reason: str | None = None,
        chunk_delay_seconds: float = 0.0,
    ) -> Self:
        """Script one ordinary turn: text, tool calls, reasoning, or all three.

        ``text`` and ``reasoning`` may be a plain string, split into several
        deltas automatically, or a sequence of strings to pin exact chunk
        boundaries. Each element of ``tool_calls`` is either a
        ``(name, arguments)`` pair or a ``ToolCallScript`` for anything more
        specific: a malformed payload, an explicit call id, or pinned argument
        fragments. Naming a tool the request never offered needs no special
        marker; the fake does not check ``request.tools`` at all.

        ``finish_reason`` defaults to ``"tool_calls"`` when the turn has any,
        ``"stop"`` otherwise, and can be overridden either way, since a real
        provider does not always agree with that convention.

        ``chunk_delay_seconds`` sleeps before every delta this turn yields,
        including the first, so time-to-first-token is measurable and never
        zero when a test sets it.
        """
        normalized_calls = tuple(_normalize_tool_call(call) for call in tool_calls)
        resolved_finish = (
            finish_reason
            if finish_reason is not None
            else ("tool_calls" if normalized_calls else "stop")
        )
        self._script.append(
            _NormalTurn(
                text_chunks=_as_chunks(text),
                reasoning_chunks=_as_chunks(reasoning) if reasoning is not None else (),
                tool_calls=normalized_calls,
                usage=usage if usage is not None else Usage(),
                cost=cost,
                finish_reason=resolved_finish,
                chunk_delay_seconds=chunk_delay_seconds,
            )
        )
        return self

    def stalls(self, seconds: float | None = None) -> Self:
        """Script a turn that goes quiet for ``seconds`` without a chunk.

        Left unset, the stall lasts exactly the request's own
        ``idle_timeout_seconds`` plus a small margin, so it reliably trips
        whatever idle-deadline guard wraps the stream without the caller
        needing to know that number itself. When the request disables the
        timeout (``idle_timeout_seconds == 0``), it falls back to a short
        fixed delay instead, since there is nothing left to exceed.
        """
        self._script.append(_StallTurn(seconds=seconds))
        return self

    def aborts_mid_token(self, text: str = "", *, chunk_delay_seconds: float = 0.0) -> Self:
        """Script a turn that stops without ever emitting ``StreamDone``.

        Optionally streams ``text`` first, the same way ``turn()`` streams it,
        so the abort can land mid-sentence rather than before anything at all.
        A caller reading this stream must treat it as a retryable failure, not
        as a short, complete answer; that is the whole point of the case.
        """
        self._script.append(
            _AbortTurn(text_chunks=_as_chunks(text), chunk_delay_seconds=chunk_delay_seconds)
        )
        return self

    def raises_transient(self, status_code: int = 503, *, message: str | None = None) -> Self:
        """Script a turn that raises a retryable failure at ``status_code``."""
        self._script.append(_RaiseTurn(exception=FakeTransientFailure(status_code, message)))
        return self

    def raises_permanent(self, status_code: int = 400, *, message: str | None = None) -> Self:
        """Script a turn that raises a non-retryable failure at ``status_code``."""
        self._script.append(_RaiseTurn(exception=FakePermanentFailure(status_code, message)))
        return self

    # -- ModelClient ----------------------------------------------------

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        call_number = len(self.requests)
        if self._next_index >= len(self._script):
            raise FakeModelScriptExhausted(scripted=len(self._script), call_number=call_number)
        turn = self._script[self._next_index]
        self._next_index += 1
        return self._emit(turn, request)

    async def known_models(self) -> Sequence[str]:
        return self._known_models

    @property
    def last_request(self) -> ModelRequest:
        """The most recent request handed to ``stream()``.

        Raises ``LookupError`` if ``stream()`` has not been called yet, rather
        than returning ``None`` and letting a test fail on an unrelated
        ``AttributeError`` two lines later.
        """
        if not self.requests:
            raise LookupError("FakeModel.stream() has not been called yet")
        return self.requests[-1]

    # -- internals --------------------------------------------------------

    async def _emit(self, turn: _ScriptedTurn, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        if isinstance(turn, _RaiseTurn):
            raise turn.exception

        if isinstance(turn, _StallTurn):
            # The gap itself is the point: an idle-timeout guard reads chunks
            # with its own deadline racing this sleep, and awaiting here rather
            # than blocking is what lets that race actually happen.
            await asyncio.sleep(_stall_seconds(turn, request))
            yield StreamDone(finish_reason="stop", usage=Usage())
            return

        if isinstance(turn, _AbortTurn):
            for chunk in turn.text_chunks:
                if turn.chunk_delay_seconds:
                    await asyncio.sleep(turn.chunk_delay_seconds)
                yield TextDelta(text=chunk)
            return  # no StreamDone: this is the mid-token abort

        for chunk in turn.reasoning_chunks:
            if turn.chunk_delay_seconds:
                await asyncio.sleep(turn.chunk_delay_seconds)
            yield ReasoningDelta(text=chunk)

        for chunk in turn.text_chunks:
            if turn.chunk_delay_seconds:
                await asyncio.sleep(turn.chunk_delay_seconds)
            yield TextDelta(text=chunk)

        for call_index, call in enumerate(turn.tool_calls):
            fragments = (
                call.fragments
                if call.fragments is not None
                else _default_fragments(call.wire_arguments)
            )
            call_id = call.call_id if call.call_id is not None else new_tool_call_id()
            for fragment_index, fragment in enumerate(fragments):
                if turn.chunk_delay_seconds:
                    await asyncio.sleep(turn.chunk_delay_seconds)
                yield ToolCallDelta(
                    index=call_index,
                    id=call_id if fragment_index == 0 else None,
                    name=call.name if fragment_index == 0 else None,
                    arguments_fragment=fragment,
                )

        yield StreamDone(finish_reason=turn.finish_reason, usage=turn.usage, cost=turn.cost)
