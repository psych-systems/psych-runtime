"""The OpenAI-compatible adapter against a real local SSE server.

DESIGN.md §22: functional tests run a component against a real adapter, never
a mock, and no test in the suite makes a real network call. A ``127.0.0.1``
server this test starts and stops itself satisfies both: real bytes travel
over a real socket, and nothing leaves the machine.

The server below speaks just enough raw HTTP/1.1 to control framing at the
byte level, chunked transfer encoding for streamed bodies, ordinary
content-length bodies for errors and ``/models``, and, critically, the exact
moment each ``write`` reaches the socket, which is what lets a test put an SSE
line's two halves in two different TCP writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from psych_runtime.core.errors import PsychError, TransientError
from psych_runtime.core.ids import ToolCallId
from psych_runtime.core.messages import SystemMessage, ToolCall, ToolDefinition, UserMessage
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import AllowAll, DenyAll, EgressPolicy, HttpTransport
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import (
    ModelRequest,
    ReasoningDelta,
    StreamDone,
    TextDelta,
    ToolCallDelta,
)

pytestmark = pytest.mark.functional

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, str, bytes], Awaitable[None]]


# ---------------------------------------------------------------------------
# The stub server
# ---------------------------------------------------------------------------


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, bytes]:
    """Read one HTTP/1.1 request and return its path and body.

    Parses just enough to route the test: the request line for the path, and
    Content-Length to know how many body bytes follow. Anything a real client
    might also send (headers we do not care about) is read and discarded.
    """
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
    header_bytes, _, rest = head.partition(b"\r\n\r\n")
    lines = header_bytes.decode("latin-1").split("\r\n")
    request_line = lines[0] if lines else ""
    path = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else "/"

    content_length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            content_length = int(value.strip())

    body = rest
    while len(body) < content_length:
        chunk = await reader.read(content_length - len(body))
        if not chunk:
            break
        body += chunk
    return path, body


async def write_status(
    writer: asyncio.StreamWriter,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    chunked: bool = True,
) -> None:
    reason = {200: "OK", 400: "Bad Request", 429: "Too Many Requests"}.get(status, "OK")
    all_headers = {"Connection": "close"}
    if chunked:
        all_headers["Transfer-Encoding"] = "chunked"
    all_headers.update(headers or {})
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{key}: {value}" for key, value in all_headers.items())
    lines.append("")
    lines.append("")
    writer.write("\r\n".join(lines).encode())
    await writer.drain()


async def write_chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
    """One chunked-encoding frame, flushed as its own write so a test can
    force a TCP-level split between two frames with a real await in between."""
    if not data:
        return
    writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    await writer.drain()


async def end_chunks(writer: asyncio.StreamWriter) -> None:
    writer.write(b"0\r\n\r\n")
    await writer.drain()


async def write_json_body(
    writer: asyncio.StreamWriter,
    status: int,
    payload: object,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    headers.update(extra_headers or {})
    await write_status(writer, status, headers=headers, chunked=False)
    writer.write(body)
    await writer.drain()


class StubServer:
    """A ``127.0.0.1`` server whose per-test handler drives the wire bytes."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> str:
        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                path, body = await _read_request(reader)
                await self._handler(reader, writer, path, body)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        self._server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@asynccontextmanager
async def running_client(
    handler: Handler, *, scope: Scope | None = None, policy: EgressPolicy | None = None
) -> AsyncIterator[OpenAICompatibleClient]:
    async with StubServer(handler) as base_url:
        transport = HttpTransport(policy=policy) if policy is not None else HttpTransport()
        try:
            yield OpenAICompatibleClient(
                base_url=base_url,
                transport=transport,
                scope=scope if scope is not None else Scope(tenant="acme"),
                api_key="test-key",
            )
        finally:
            await transport.aclose()


def _request(**overrides: object) -> ModelRequest:
    base: dict[str, object] = {
        "model": "gpt-test",
        "messages": (
            SystemMessage(content="be terse"),
            UserMessage(content="hi"),
        ),
    }
    base.update(overrides)
    return ModelRequest.model_validate(base)


def sse(payload: object) -> bytes:
    # ensure_ascii=False so a non-ASCII character in a test payload actually
    # appears as multi-byte UTF-8 on the wire, rather than as a \uXXXX escape.
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


# ---------------------------------------------------------------------------
# A normal streamed completion
# ---------------------------------------------------------------------------


class TestNormalCompletion:
    async def test_streams_text_and_a_clean_done(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            assert path == "/v1/chat/completions"
            payload = json.loads(body)
            assert payload["model"] == "gpt-test"
            assert payload["stream"] is True
            assert payload["stream_options"] == {"include_usage": True}
            assert payload["messages"][0] == {"role": "system", "content": "be terse"}
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]}
                ),
            )
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]}),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": None,
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        text_events = [e for e in events if isinstance(e, TextDelta)]
        done_events = [e for e in events if isinstance(e, StreamDone)]
        assert [e.text for e in text_events] == ["Hel", "lo"]
        assert len(done_events) == 1
        done = done_events[0]
        assert done.finish_reason == "stop"
        assert done.usage.input == 12
        assert done.usage.output == 3

    async def test_reasoning_content_becomes_reasoning_delta(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": "thinking..."},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        reasoning = [e for e in events if isinstance(e, ReasoningDelta)]
        assert [e.text for e in reasoning] == ["thinking..."]


# ---------------------------------------------------------------------------
# Tool calls split across chunks
# ---------------------------------------------------------------------------


class TestToolCalls:
    async def test_tool_call_arguments_assemble_across_chunk_boundaries(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "function": {
                                                "name": "get_weather",
                                                "arguments": '{"loc',
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": 'ation": "'}}
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{"index": 0, "function": {"arguments": 'sf"}'}}]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        tool = ToolDefinition(name="get_weather", description="get it", input_schema={})
        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request(tools=(tool,)))]

        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assert len(deltas) == 3
        assert deltas[0].id == "call_1"
        assert deltas[0].name == "get_weather"
        assembled = "".join(d.arguments_fragment for d in deltas)
        assert json.loads(assembled) == {"location": "sf"}
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "tool_calls"

    async def test_assistant_message_with_tool_calls_serialises_arguments_to_wire(self) -> None:
        seen_body: dict[str, object] = {}

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            seen_body.update(json.loads(body))
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        from psych_runtime.core.messages import AssistantMessage, ToolResultMessage

        request = _request(
            messages=(
                SystemMessage(content="s"),
                UserMessage(content="u"),
                AssistantMessage(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id=ToolCallId("call_9"),
                            name="get_weather",
                            arguments={"location": "nyc"},
                        ),
                    ),
                ),
                ToolResultMessage(
                    tool_call_id=ToolCallId("call_9"), name="get_weather", content="sunny"
                ),
            )
        )
        async with running_client(handler) as client:
            _ = [event async for event in client.stream(request)]

        messages = seen_body["messages"]
        assert isinstance(messages, list)
        assistant_wire = messages[2]
        assert assistant_wire["tool_calls"][0]["id"] == "call_9"
        assert json.loads(assistant_wire["tool_calls"][0]["function"]["arguments"]) == {
            "location": "nyc"
        }
        assert messages[3] == {
            "role": "tool",
            "tool_call_id": "call_9",
            "content": "sunny",
        }


# ---------------------------------------------------------------------------
# SSE frames split mid-line across TCP reads
# ---------------------------------------------------------------------------


class TestFramingAcrossReads:
    async def test_sse_line_split_mid_line_across_two_writes(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            full = sse({"choices": [{"index": 0, "delta": {"content": "hello"}}]})
            midpoint = len(full) // 2
            await write_status(writer, 200)
            # Split one SSE line into two chunked-encoding frames, each its own
            # write+drain, with a real await between them so the two halves
            # cannot be coalesced into a single read on the client side.
            await write_chunk(writer, full[:midpoint])
            await asyncio.sleep(0.02)
            await write_chunk(writer, full[midpoint:])
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert [e.text for e in text_events] == ["hello"]

    async def test_multibyte_utf8_character_split_across_writes(self) -> None:
        """A 3-byte UTF-8 character (the euro sign) cut between two TCP writes
        must still decode correctly rather than raising or corrupting text."""

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            full = sse({"choices": [{"index": 0, "delta": {"content": "5€"}}]})
            euro_start = full.index("€".encode())
            # Split inside the euro sign's 3-byte UTF-8 encoding: the first
            # byte reaches the client in one write, the other two in the next.
            split = euro_start + 1
            await write_status(writer, 200)
            await write_chunk(writer, full[:split])
            await asyncio.sleep(0.02)
            await write_chunk(writer, full[split:])
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert "".join(e.text for e in text_events) == "5€"


# ---------------------------------------------------------------------------
# Usage mapping including cached tokens
# ---------------------------------------------------------------------------


class TestUsageMapping:
    @pytest.mark.parametrize("field", ["cost", "response_cost"])
    async def test_streaming_usage_cost_is_provider_reported(self, field: str) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            field: 0.00017,
                        },
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.cost is not None
        assert done.cost.amount == Decimal("0.00017")
        assert done.cost.source == "provider"

    async def test_cached_tokens_are_subtracted_from_input_not_added(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 1000,
                            "completion_tokens": 50,
                            "prompt_tokens_details": {"cached_tokens": 400},
                            "completion_tokens_details": {"reasoning_tokens": 10},
                            "cache_creation_input_tokens": 200,
                            "cache_creation": {"ephemeral_1h_input_tokens": 80},
                        },
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = next(e for e in events if isinstance(e, StreamDone))
        usage = done.usage
        assert done.usage_reported is True
        # 1000 prompt_tokens includes the 400 cached; input is the remainder.
        assert usage.input == 600
        assert usage.cache_read == 400
        assert usage.output == 50
        assert usage.reasoning == 10
        assert usage.cache_write == 200
        assert usage.cache_write_1h == 80
        # cache_read is disjoint from input: they must not double count.
        assert usage.input + usage.cache_read == 1000

    async def test_missing_usage_fields_default_to_zero(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.usage.cache_read == 0
        assert done.usage.cache_write == 0
        assert done.usage.cache_write_1h == 0
        assert done.usage.reasoning == 0

    async def test_no_usage_chunk_at_all_still_yields_a_done_with_zero_usage(self) -> None:
        """A provider that does not honour stream_options.include_usage still
        finishes cleanly; that is a real completion, not an aborted one."""

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "stop"
        assert done.usage_reported is False
        assert done.usage.input == 0
        assert done.usage.output == 0


# ---------------------------------------------------------------------------
# Idle timeout: only while a read is outstanding
# ---------------------------------------------------------------------------


class TestIdleTimeout:
    async def test_a_stall_past_the_idle_timeout_raises_transient_error(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {"content": "partial"}}]}),
            )
            await asyncio.sleep(2.0)  # longer than the request's idle_timeout_seconds
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            with pytest.raises(TransientError, match="idle"):
                async for _event in client.stream(_request(idle_timeout_seconds=0.2)):
                    pass

    async def test_slow_consumer_does_not_trip_the_idle_timeout(self) -> None:
        """The timer runs only while a network read is outstanding (DESIGN.md
        §8.5); a consumer that is merely slow to process already-received
        events must never be penalised for it."""

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            for i in range(5):
                await write_chunk(
                    writer, sse({"choices": [{"index": 0, "delta": {"content": str(i)}}]})
                )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = []
            async for event in client.stream(_request(idle_timeout_seconds=0.3)):
                # Slower than the idle timeout, applied between already
                # buffered events rather than around a network read.
                await asyncio.sleep(0.15)
                events.append(event)

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert [e.text for e in text_events] == ["0", "1", "2", "3", "4"]

    async def test_zero_disables_the_idle_timeout(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await asyncio.sleep(0.3)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request(idle_timeout_seconds=0))]

        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "stop"


# ---------------------------------------------------------------------------
# A stream that dies without [DONE]
# ---------------------------------------------------------------------------


class TestAbortedStream:
    async def test_connection_closes_before_a_finish_reason_raises_transient(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(writer, sse({"choices": [{"index": 0, "delta": {"content": "he"}}]}))
            # No finish_reason, no usage chunk, no [DONE]: the chunked body is
            # still properly terminated, but the answer itself was cut off.
            await end_chunks(writer)

        async with running_client(handler) as client:
            with pytest.raises(TransientError, match="finish reason"):
                async for _event in client.stream(_request()):
                    pass

    async def test_finish_reason_without_a_usage_chunk_is_not_treated_as_aborted(self) -> None:
        """finish_reason alone is a real completion (see TestUsageMapping); it
        must not be conflated with the no-finish-reason abort case above."""

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
            )
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "length"


# ---------------------------------------------------------------------------
# HTTP status handling
# ---------------------------------------------------------------------------


class TestStatusHandling:
    async def test_429_with_retry_after_is_transient_and_carries_the_delay(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(
                writer, 429, {"error": "rate limited"}, extra_headers={"Retry-After": "7"}
            )

        async with running_client(handler) as client:
            with pytest.raises(TransientError) as excinfo:
                async for _event in client.stream(_request()):
                    pass

        assert excinfo.value.retry_after_seconds == pytest.approx(7.0)

    async def test_400_is_not_retried(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 400, {"error": "bad request: missing model"})

        async with running_client(handler) as client:
            with pytest.raises(PsychError) as excinfo:
                async for _event in client.stream(_request()):
                    pass

        assert not isinstance(excinfo.value, TransientError)
        assert "bad request" in str(excinfo.value)

    async def test_401_is_not_retried(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 401, {"error": "invalid api key"})

        async with running_client(handler) as client:
            with pytest.raises(PsychError) as excinfo:
                async for _event in client.stream(_request()):
                    pass

        assert not isinstance(excinfo.value, TransientError)

    async def test_500_is_transient(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 500, {"error": "upstream exploded"})

        async with running_client(handler) as client:
            with pytest.raises(TransientError):
                async for _event in client.stream(_request()):
                    pass


# ---------------------------------------------------------------------------
# known_models
# ---------------------------------------------------------------------------


class TestKnownModels:
    async def test_known_models_returns_ids_from_the_endpoint(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            assert path == "/v1/models"
            await write_json_body(
                writer,
                200,
                {"data": [{"id": "gpt-test"}, {"id": "gpt-other"}]},
            )

        async with running_client(handler) as client:
            models = await client.known_models()

        assert set(models) == {"gpt-test", "gpt-other"}

    async def test_known_models_is_empty_when_the_endpoint_is_absent(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 404, {"error": "not found"})

        async with running_client(handler) as client:
            models = await client.known_models()

        assert models == ()

    async def test_known_models_is_empty_on_malformed_response(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 200, {"unexpected": "shape"})

        async with running_client(handler) as client:
            models = await client.known_models()

        assert models == ()

    async def test_known_models_is_empty_when_the_connection_refuses(self) -> None:
        transport = HttpTransport()
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:1",  # nothing listens on port 1
            transport=transport,
            scope=Scope(tenant="acme"),
        )
        try:
            models = await client.known_models()
        finally:
            await transport.aclose()
        assert models == ()


# ---------------------------------------------------------------------------
# The egress seam itself
# ---------------------------------------------------------------------------


class TestEgressSeam:
    async def test_deny_all_policy_stops_the_call_before_it_reaches_the_socket(self) -> None:
        reached_server = False

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            nonlocal reached_server
            reached_server = True
            await write_json_body(writer, 200, {"data": []})

        async with running_client(handler, policy=DenyAll()) as client:
            with pytest.raises(PsychError, match="egress policy refused"):
                async for _event in client.stream(_request()):
                    pass

        assert reached_server is False

    async def test_allow_all_policy_is_the_default_and_lets_calls_through(self) -> None:
        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_json_body(writer, 200, {"data": [{"id": "m"}]})

        transport = HttpTransport(policy=AllowAll())
        async with StubServer(handler) as base_url:
            client = OpenAICompatibleClient(
                base_url=base_url, transport=transport, scope=Scope(tenant="acme")
            )
            try:
                models = await client.known_models()
            finally:
                await transport.aclose()
        assert models == ("m",)


class TestProviderDialects:
    """ "OpenAI-compatible" is a family of dialects, not one wire format.

    Every case here is a behaviour observed from a real provider, Cloudflare
    Workers AI, whose ``/accounts/{id}/ai/v1`` endpoint is OpenAI-compatible
    and differs from OpenAI in three ways that each broke a Run. The suite
    passed throughout, because the rest of these tests reproduce OpenAI's
    layout exactly, so the adapter was only ever checked against its own
    assumptions.

    These reproduce the dialect over the same real local socket. Nothing here
    touches the network; the no-network-in-tests rule is worth more
    than testing against the live provider, and the live check lives in
    ``scripts/live-provider-check.py`` for a human to run.
    """

    async def test_usage_stamped_on_every_chunk_reports_the_last_not_the_first(self) -> None:
        """Cloudflare sends ``usage`` on every chunk: zeros while the
        completion runs, then the real totals.

        Reading the first usage-bearing chunk is right for OpenAI and silently
        records zero here. A known price times zero tokens is a confident zero
        cost, which is the pricing failure the project calls out: it makes
        metering look correct and be wrong.
        """

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            zeros = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {"index": 0, "delta": {"content": "hi"}, "finish_reason": None}
                        ],
                        "usage": zeros,
                    }
                ),
            )
            # The chunk carrying finish_reason also carries a zero usage. This
            # is the one that used to latch and end the accounting at nothing.
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": zeros,
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 41,
                            "completion_tokens": 5,
                            "total_tokens": 46,
                        },
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = [e for e in events if isinstance(e, StreamDone)]
        assert len(done) == 1, "exactly one StreamDone, however many usage chunks arrived"
        assert done[0].usage.input == 41
        assert done[0].usage.output == 5

    async def test_an_all_zero_usage_never_displaces_a_real_reading(self) -> None:
        """The ordering above is the provider's convention, not a guarantee.

        A zero usage carries no information and is indistinguishable from "not
        counted yet", so a trailing one must not overwrite a real count.
        """

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 77, "completion_tokens": 9},
                    }
                ),
            )
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                    }
                ),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        done = [e for e in events if isinstance(e, StreamDone)]
        assert done[0].usage.input == 77
        assert done[0].usage.output == 9

    async def test_a_non_string_content_delta_is_rendered_rather_than_fatal(self) -> None:
        """An observed Cloudflare stream carried ``"content": 1``.

        That is a provider bug, but it reached ``TextDelta`` and killed the Run
        with a Pydantic ``string_type`` error two turns in. The model did
        produce the token, so rendering the scalar loses nothing and keeps the
        Run alive.
        """

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            for value in ("order ", 1, ".0", True):
                await write_chunk(
                    writer,
                    sse(
                        {
                            "choices": [
                                {"index": 0, "delta": {"content": value}, "finish_reason": None}
                            ]
                        }
                    ),
                )
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        assert [e.text for e in events if isinstance(e, TextDelta)] == [
            "order ",
            "1",
            ".0",
            "True",
        ]

    async def test_a_structured_content_delta_is_not_coerced_into_a_repr(self) -> None:
        """A list or dict means the provider is speaking content blocks, which
        is a different feature. ``str()`` over a structure would put a Python
        repr in front of the model, which is worse than dropping it."""

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": [{"type": "text", "text": "hi"}]},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        async with running_client(handler) as client:
            events = [event async for event in client.stream(_request())]

        texts = [e.text for e in events if isinstance(e, TextDelta)]
        assert texts == [], f"a structure must not be rendered as a repr, got {texts!r}"

    async def test_an_assistant_tool_call_message_sends_a_string_content(self) -> None:
        """Cloudflare's schema requires assistant ``content`` to be a present
        string and rejects the whole request with a 400 on ``null``.

        OpenAI types it as string-or-null and conventionally sends null, so an
        empty string is the one shape both accept. Verified against the live
        provider: the identical request differing only in this field fails with
        ``null`` and succeeds with ``""``.
        """
        seen: dict[str, object] = {}

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str, body: bytes
        ) -> None:
            seen.update(json.loads(body))
            await write_status(writer, 200)
            await write_chunk(
                writer,
                sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            )
            await write_chunk(writer, b"data: [DONE]\n\n")
            await end_chunks(writer)

        from psych_runtime.core.messages import AssistantMessage

        request = _request(
            messages=(
                UserMessage(content="where is A1?"),
                AssistantMessage(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id=ToolCallId("c1"),
                            name="lookup_order",
                            arguments={"order_id": "A1"},
                        ),
                    ),
                ),
            )
        )
        async with running_client(handler) as client:
            [event async for event in client.stream(request)]

        messages = seen["messages"]
        assert isinstance(messages, list)
        assistant = messages[1]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == "", (
            "an assistant tool-call message must carry a present string, not null: "
            "Cloudflare Workers AI 400s on null and requires the key"
        )
        assert assistant["content"] is not None
