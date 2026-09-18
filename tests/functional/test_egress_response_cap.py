"""Every complete response read through the egress seam is bounded.

An Agent Card, an A2A reply, an OAuth discovery document a hostile MCP server
pointed us at: each body is chosen by a remote party, and ``response.json()``
on an unbounded ``aread()`` was a worker one response could take down. The
seam reads incrementally and refuses past a ceiling, and the model adapter's
error-body and stream reads are bounded the same way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import (
    DEFAULT_MAX_RESPONSE_BYTES,
    HttpTransport,
    ResponseTooLarge,
    read_capped,
)
from psych_runtime.model.openai_compat import _iter_sse_events

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme", principal="user-1")


def _transport(body: bytes, status: int = 200) -> HttpTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"x-echo": "yes"})

    return HttpTransport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestRequestIsBounded:
    async def test_a_small_body_comes_back_whole_with_status_and_headers(self) -> None:
        async with _transport(b'{"ok": true}', status=201) as transport:
            response = await transport.request("GET", "https://example.test/card", scope=SCOPE)
        assert response.status_code == 201
        assert response.json() == {"ok": True}
        assert response.headers["x-echo"] == "yes"

    async def test_a_body_past_the_ceiling_is_refused_not_buffered(self) -> None:
        async with _transport(b"x" * 4096) as transport:
            with pytest.raises(ResponseTooLarge) as caught:
                await transport.request(
                    "GET", "https://example.test/big", scope=SCOPE, max_bytes=1024
                )
        assert caught.value.limit == 1024
        assert "example.test" in str(caught.value)
        assert "xxxx" not in str(caught.value), "the body must not leak into the message"

    async def test_the_default_ceiling_is_generous_but_finite(self) -> None:
        assert DEFAULT_MAX_RESPONSE_BYTES == 16 * 1024 * 1024
        async with _transport(b"x" * 100) as transport:
            response = await transport.request("GET", "https://example.test/ok", scope=SCOPE)
        assert len(response.content) == 100


class TestStreamReadsAreBounded:
    async def test_read_capped_refuses_past_the_limit(self) -> None:
        async with (
            _transport(b"abcdef") as transport,
            transport.stream("GET", "https://example.test/s", scope=SCOPE) as response,
        ):
            with pytest.raises(ResponseTooLarge):
                await read_capped(response, 3)

    async def test_an_endless_sse_stream_is_cut_off(self) -> None:
        async def endless() -> AsyncIterator[bytes]:
            while True:
                yield b"data: {}\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_AsyncByteStream(endless()))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        seen: list[str] = []

        async def consume(response: httpx.Response) -> None:
            async for event in _iter_sse_events(response, 0, max_bytes=200):
                seen.append(event)

        async with (
            HttpTransport(client=client) as transport,
            transport.stream("POST", "https://example.test/v1", scope=SCOPE) as response,
        ):
            with pytest.raises(ResponseTooLarge):
                await consume(response)
        assert 0 < len(seen) < 100, "some events were delivered before the cut"


class _AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk
