"""The OpenAI-compatible wire adapter.

DESIGN.md §19. Psych speaks the OpenAI-compatible ``/v1/chat/completions`` SSE
wire protocol directly with ``httpx``, through the egress seam, without a
provider SDK. The consumer supplies a compatible endpoint; this module never
has to know which provider is actually serving the request.

## Scope binds at construction, not per call

``ModelClient.stream`` (``psych_runtime.model.port``) takes a ``ModelRequest`` and
nothing else, so it carries no ``Scope`` for the egress policy to check
against. This adapter is therefore constructed per ``Scope``, the same way
DESIGN.md §10.4 pools MCP clients by ``(scope, server, credential)`` rather
than by URL alone: a consumer builds one client per tenant rather than sharing
one client across tenants and hoping the request body is the only thing that
differs.

## One attempt, not a retry loop

DESIGN.md §8.6 puts the retry budget on the Run, not on any one call, so this
adapter makes exactly one attempt per ``stream()`` call. On a retryable
failure it raises ``TransientError`` and stops; the runtime layer above it
owns deciding whether the Run's budget allows trying again.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from psych_runtime.core.errors import PsychError, TransientError
from psych_runtime.core.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from psych_runtime.core.scope import Scope
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.egress import HttpTransport
from psych_runtime.model.port import (
    ModelRequest,
    ReasoningDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
)
from psych_runtime.model.transient import classify_status

__all__ = ["OpenAICompatibleClient"]

# httpx's own read timeout is deliberately disabled below (read=None) so the
# only thing that can fail a stalled read is the idle-timeout logic in
# _iter_sse_events, applied around exactly one network read at a time
# (DESIGN.md §8.5). Connect, write and pool waits are not "the model is
# thinking", so those keep a conventional bound.
_CONNECT_TIMEOUT: Final = 30.0
_WRITE_TIMEOUT: Final = 30.0
_POOL_TIMEOUT: Final = 30.0

_ERROR_BODY_TRUNCATE: Final = 2000
"""How much of an error response body to keep in the raised message. Enough to
diagnose, not so much that a provider's HTML error page floods the log."""


class OpenAICompatibleClient:
    """``ModelClient`` over the OpenAI-compatible chat completions wire format.

    Attributes are private; construct with keyword arguments only.

    Args:
        base_url: the API root, e.g. ``https://proxy.internal/v1``. Requests
            go to ``{base_url}/chat/completions`` and ``{base_url}/models``.
        transport: the egress seam. Never construct an ``httpx`` client here
            directly; that would be a second, uncontrolled path to the
            network.
        scope: the tenant this client instance acts for. Stamped on every
            egress check this client makes.
        api_key: sent as ``Authorization: Bearer <api_key>`` when set. Some
            proxy deployments authenticate at the network layer instead and
            need none.
        headers: extra headers merged into every request, for a proxy that
            wants a tenant header or a routing hint.
    """

    def __init__(
        self,
        *,
        base_url: str,
        transport: HttpTransport,
        scope: Scope,
        api_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._scope = scope
        self._api_key = api_key
        self._extra_headers = dict(headers) if headers is not None else {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Call the model and yield events until the stream ends.

        Raises:
            TransientError: a retryable failure: 5xx, 429, 408, a connection
                error, or an idle timeout past ``request.idle_timeout_seconds``.
            PsychError: a non-retryable failure: 400, 401, 403, or a malformed
                response.
        """
        payload = _build_payload(request)
        url = f"{self._base_url}/chat/completions"
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT, read=None, write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT
        )
        state = _StreamState()

        try:
            async with self._transport.stream(
                "POST",
                url,
                scope=self._scope,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise _error_for_status(response.status_code, body, response.headers)
                async for data in _iter_sse_events(response, request.idle_timeout_seconds):
                    text = data.strip()
                    if not text:
                        continue
                    if text == "[DONE]":
                        break
                    try:
                        chunk = json.loads(text)
                    except json.JSONDecodeError as err:
                        raise PsychError(
                            f"malformed SSE payload from model: {text[:500]!r}"
                        ) from err
                    for event in _events_from_chunk(chunk, state):
                        yield event
        except httpx.HTTPError as err:
            raise _error_for_transport(err) from err

        if state.done_emitted:
            return
        if state.finish_reason is not None:
            # One StreamDone, here, carrying whatever usage the stream reported.
            # `state.usage` is None when the provider sent no usage-bearing
            # chunk at all (stream_options.include_usage is not universally
            # honoured); the completion still finished cleanly, so that is a
            # real done event with zero usage rather than a truncated one.
            state.done_emitted = True
            yield StreamDone(
                finish_reason=state.finish_reason,
                usage=state.usage or Usage(),
                # Reported, not resolved: which of this and a locally computed
                # figure gets written into the Record is the consumer's choice
                # (`psych_runtime.model.pricing.CostPolicy`), made where the Runtime is
                # assembled rather than here.
                cost=(
                    Cost(
                        amount=state.reported_cost,
                        currency=state.reported_currency,
                        model=request.model,
                        source="provider",
                    )
                    if state.reported_cost is not None
                    else None
                ),
            )
            return
        # No finish_reason ever arrived: the connection ended mid-response.
        # DESIGN.md §8.5 is explicit that this is an abort mid-token, not a
        # short answer, so it is retried rather than accepted.
        raise TransientError(
            "model stream ended without a finish reason or StreamDone; the "
            "provider closed the connection mid-response"
        )

    async def known_models(self) -> Sequence[str]:
        """Model ids this deployment will accept, or ``()`` when it will not say.

        DESIGN.md §19: an empty sequence is "I cannot tell you", the honest
        answer from a proxy that can reach models it was never told about, and
        publish-time validation treats empty as "do not check" rather than as
        "nothing is valid".
        """
        url = f"{self._base_url}/models"
        try:
            response = await self._transport.request(
                "GET", url, scope=self._scope, headers=self._headers()
            )
        except httpx.HTTPError:
            return ()
        if response.status_code != 200:
            return ()
        try:
            body = response.json()
            entries = body["data"]
            return tuple(str(entry["id"]) for entry in entries)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return ()


# ---------------------------------------------------------------------------
# Request assembly
# ---------------------------------------------------------------------------


def _message_to_wire(message: Message) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, UserMessage):
        wire: dict[str, Any] = {"role": "user", "content": message.content}
        if message.name is not None:
            wire["name"] = message.name
        return wire
    if isinstance(message, AssistantMessage):
        # `reasoning` is dropped here on purpose (psych_runtime.model.port docstring):
        # the plain OpenAI wire shape has no slot a provider will accept
        # opaque reasoning content back into, so replaying it would either be
        # ignored or rejected depending on the proxy.
        # `""` rather than `None` for an assistant message with no text.
        # OpenAI's schema types assistant `content` as string-or-null and
        # conventionally sends null alongside tool_calls, so both are valid
        # there. Cloudflare Workers AI's schema requires a string and rejects
        # the whole request with a 400 on null; it also requires the key to be
        # present, so omitting it is not an option either. An empty string is
        # the one shape both accept. Verified against Cloudflare directly: the
        # identical request differing only in this field fails with null and
        # succeeds with "".
        wire = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
        return wire
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    raise AssertionError(f"unhandled message type: {type(message).__name__}")


def _tool_to_wire(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema or {"type": "object", "properties": {}},
        },
    }


def _build_payload(request: ModelRequest) -> dict[str, Any]:
    """``ModelRequest`` to the OpenAI-compatible wire body.

    ``cache_breakpoints`` is silently unused: the plain OpenAI wire shape has
    no explicit breakpoint control, and ``ModelRequest``'s own docstring says
    a provider that lacks the feature ignores the field rather than erroring.
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [_message_to_wire(message) for message in request.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        payload["tools"] = [_tool_to_wire(tool) for tool in request.tools]
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.max_output_tokens is not None:
        payload["max_completion_tokens"] = request.max_output_tokens
    if request.reasoning_effort is not None:
        payload["reasoning_effort"] = request.reasoning_effort
    if request.stop:
        payload["stop"] = list(request.stop)
    if request.extra:
        # Deliberately last and deliberately capable of overriding anything
        # above: ModelRequest.extra is provider-specific passthrough outside
        # the contract (psych_runtime.model.port), so the caller who set it wins.
        payload.update(request.extra)
    return payload


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


@dataclass
class _StreamState:
    """Tracks the two halves of completion that OpenAI splits across chunks.

    ``finish_reason`` typically arrives on the last content-bearing chunk.
    Where usage arrives is where "OpenAI-compatible" stops being one thing.

    OpenAI sends it once, on a separate trailing chunk with an empty
    ``choices`` list, when ``stream_options.include_usage`` is honoured.
    Cloudflare Workers AI sends a ``usage`` object on *every* chunk: zeros
    while the completion is still running, then the real totals on the last
    one. Reading the first usage that arrives is therefore correct for OpenAI
    and silently records zero for Cloudflare, which is worse than failing,
    because a known price times zero tokens is a confident zero cost.

    So usage is accumulated rather than latched, and ``StreamDone`` is emitted
    once at the end of the stream. For OpenAI that is the same value it always
    was; for a per-chunk provider it is the difference between right and zero.

    ``usage`` keeps the last chunk that reported *something*. An all-zero usage
    object carries no information and is indistinguishable from "not counted
    yet", so it never displaces a real reading.
    """

    finish_reason: str | None = None
    done_emitted: bool = False
    usage: Usage | None = None
    reported_cost: Decimal | None = None
    """What the provider said this call cost, if it said anything.

    Latched the same way as usage and for the same reason: a gateway that
    stamps its bookkeeping on every chunk sends the real figure only at the
    end, and a zero arriving first is a placeholder rather than a free call.
    """
    reported_currency: str = "USD"


def _events_from_chunk(chunk: dict[str, Any], state: _StreamState) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    choices = chunk.get("choices") or []
    for choice in choices:
        delta = choice.get("delta") or {}

        content = _text_of(delta.get("content"))
        if content:
            events.append(TextDelta(text=content))

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            events.append(ReasoningDelta(text=reasoning))

        for tool_call in delta.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            events.append(
                ToolCallDelta(
                    index=tool_call.get("index", 0),
                    id=tool_call.get("id"),
                    name=function.get("name"),
                    arguments_fragment=function.get("arguments") or "",
                )
            )

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            state.finish_reason = finish_reason

    usage_json = chunk.get("usage")
    if usage_json is not None:
        usage = _usage_from_wire(usage_json)
        if _reports_anything(usage):
            state.usage = usage

    reported = _reported_cost(chunk)
    if reported is not None:
        state.reported_cost = reported

    return events


def _reported_cost(chunk: dict[str, Any]) -> Decimal | None:
    """A provider-reported cost off one chunk, or ``None``.

    Three shapes, because "OpenAI-compatible" says nothing about billing and
    each gateway invented its own place to put this:

    - ``_hidden_params.response_cost`` -- computed against configured rates.
    - ``usage.cost`` -- a gateway-reported cost.
    - ``cost`` at the top level -- several smaller proxies.

    Read as fields off a JSON body, never by importing anyone's SDK: Psych
    depends on ``pydantic`` and ``httpx`` and that is the whole list.

    ``Decimal(str(...))`` rather than ``Decimal(float)``: these arrive as JSON
    numbers, and going through binary floating point would put
    ``0.000105000000000000004`` in a Record about money. A value that will not
    parse is ignored rather than raised on, because a malformed billing extra
    is not a reason to fail a completion the model already finished.
    """
    hidden = chunk.get("_hidden_params")
    usage_json = chunk.get("usage")
    candidates = [
        hidden.get("response_cost") if isinstance(hidden, dict) else None,
        usage_json.get("cost") if isinstance(usage_json, dict) else None,
        chunk.get("cost"),
    ]
    for candidate in candidates:
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            value = Decimal(str(candidate))
        except (ArithmeticError, ValueError):
            continue
        # A negative cost is not a credit Psych knows how to model, and `Cost`
        # refuses one anyway; skipping beats raising inside a stream.
        if value >= 0:
            return value
    return None


def _reports_anything(usage: Usage) -> bool:
    """Whether a usage object carries a count, as opposed to all zeros.

    A provider that stamps ``usage`` on every chunk sends zeros until the
    completion finishes. Those are placeholders, not measurements, and letting
    one overwrite a real reading is how token counts become silently wrong.
    """
    return any(getattr(usage, field) for field in usage.__class__.model_fields)


def _text_of(content: object) -> str | None:
    """A chunk's text, coerced from whatever scalar the provider actually sent.

    OpenAI types ``delta.content`` as a string. Cloudflare Workers AI sometimes
    sends a bare JSON number instead: an observed stream carried
    ``"content": 1``, which reached ``TextDelta`` and killed the Run with a
    Pydantic ``string_type`` error two turns in.

    That is a provider bug, but a Run dying over it is Psych's problem. The
    model did produce that token, so the repair that loses nothing is to render
    the scalar and carry on.

    A list or dict is *not* coerced. Those mean the provider is speaking the
    content-blocks shape rather than sending malformed text, which is a
    different feature and needs deliberate support rather than ``str()`` over a
    structure, which would put a Python repr in front of the model.
    """
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, bool | int | float):
        return str(content)
    return None


def _usage_from_wire(usage_json: dict[str, Any]) -> Usage:
    """Map wire usage fields to ``Usage``, keeping ``cache_read`` disjoint from
    ``input`` (DESIGN.md §13.1): OpenAI's ``prompt_tokens`` counts cached
    tokens too, so the cached count is subtracted rather than added on top.
    """
    prompt_tokens = int(usage_json.get("prompt_tokens") or 0)
    completion_tokens = int(usage_json.get("completion_tokens") or 0)

    prompt_details = usage_json.get("prompt_tokens_details") or {}
    cached_tokens = int(prompt_details.get("cached_tokens") or 0)

    completion_details = usage_json.get("completion_tokens_details") or {}
    reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)

    # Cache write accounting, as reported by proxies that pass
    # it through under these names. `cache_write_1h` is a subset of
    # `cache_write`, never additional to it (Usage's own invariant), so it is
    # folded up into the total if a provider reports the subset but not the
    # aggregate.
    cache_write = int(usage_json.get("cache_creation_input_tokens") or 0)
    cache_creation = usage_json.get("cache_creation") or {}
    cache_write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
    cache_write = max(cache_write, cache_write_1h)

    input_tokens = max(prompt_tokens - cached_tokens, 0)

    return Usage(
        input=input_tokens,
        output=completion_tokens,
        cache_read=cached_tokens,
        cache_write=cache_write,
        cache_write_1h=cache_write_1h,
        reasoning=reasoning_tokens,
    )


class _SseLineBuffer:
    """Turns decoded text into complete SSE events, one line at a time.

    Kept separate from the network read loop below so that loop's only job is
    reading with the idle timeout around it; nothing in here ever awaits,
    which is what makes that separation sound.

    Handles the parts a naive line-splitter gets wrong: multi-line ``data:``
    fields (joined with ``\\n`` into one event, per the SSE spec), ``:``-prefixed
    keep-alive comments, and other named fields (``event:``, ``id:``,
    ``retry:``), which this wire protocol never uses and which are ignored
    rather than rejected.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._data_lines: list[str] = []

    def feed(self, text: str) -> list[str]:
        """Add decoded text and return every event it completes."""
        self._buffer += text
        events: list[str] = []
        while True:
            newline = self._buffer.find("\n")
            if newline == -1:
                break
            line, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
            event = self._consume_line(line.removesuffix("\r"))
            if event is not None:
                events.append(event)
        return events

    def _consume_line(self, line: str) -> str | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":") or not line.startswith("data:"):
            return None  # keep-alive comment, or a field this protocol ignores
        value = line.removeprefix("data:")
        self._data_lines.append(value.removeprefix(" "))
        return None

    def _dispatch(self) -> str | None:
        if not self._data_lines:
            return None
        event = "\n".join(self._data_lines)
        self._data_lines = []
        return event

    def flush(self) -> str | None:
        """The event in progress at end of stream, if any data was collected
        without a terminating blank line."""
        return self._dispatch()


async def _iter_sse_events(
    response: httpx.Response, idle_timeout_seconds: float
) -> AsyncIterator[str]:
    """Yield each SSE event's ``data:`` payload, one event per yield.

    A chunk boundary can land mid-line, mid-multibyte-character or mid-event,
    so bytes are fed through an incremental UTF-8 decoder rather than decoded
    one read at a time, and line assembly is delegated to ``_SseLineBuffer``.

    The idle timeout (DESIGN.md §8.5) wraps only the ``anext`` call that
    performs the network read. Nothing else in this function awaits, so a
    consumer that is slow to pull events never extends or resets a timer that
    was never running while it was thinking.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    parser = _SseLineBuffer()
    byte_iter = response.aiter_bytes()

    while True:
        try:
            if idle_timeout_seconds > 0:
                async with asyncio.timeout(idle_timeout_seconds):
                    chunk = await anext(byte_iter)
            else:
                chunk = await anext(byte_iter)
        except StopAsyncIteration:
            break
        except TimeoutError as err:
            raise TransientError(
                f"model stream idle for more than {idle_timeout_seconds}s"
            ) from err

        for event in parser.feed(decoder.decode(chunk)):
            yield event

    for event in parser.feed(decoder.decode(b"", final=True)):
        yield event
    trailing = parser.flush()
    if trailing is not None:
        yield trailing


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as either delta-seconds or an HTTP-date."""
    if value is None:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _error_for_status(status: int, body: str, headers: httpx.Headers) -> Exception:
    message = f"model provider returned HTTP {status}: {body[:_ERROR_BODY_TRUNCATE]}"
    if classify_status(status):
        retry_after = _parse_retry_after(headers.get("retry-after"))
        return TransientError(message, retry_after_seconds=retry_after)
    return PsychError(message)


def _error_for_transport(err: httpx.HTTPError) -> Exception:
    # httpx's own hierarchy (ConnectError, ReadError, ReadTimeout,
    # RemoteProtocolError, PoolTimeout, ...) all derive from TransportError,
    # and every one of them means the network or the connection is the
    # problem rather than what we asked for, so all of them are transient.
    if isinstance(err, httpx.TransportError):
        return TransientError(f"transport error calling model: {err}")
    return PsychError(f"transport error calling model: {err}")
