"""HTTP tools against a real local stub server.

DESIGN.md §22: a functional test runs a component against a real adapter,
never a mock, and no test in the suite makes a real network call. The server
below is a ``127.0.0.1`` socket this test starts and stops itself, so real
bytes travel over a real socket and nothing leaves the machine. The pattern
follows ``tests/functional/test_openai_compat.py``.

Every call in this file goes through ``psych_runtime.model.egress.HttpTransport``, the
one seam DESIGN.md §14 requires, wired into ``psych_runtime.tools.http.HttpToolExecutor``
exactly the way the runtime layer is meant to: constructing the real transport
and handing it in as the structural ``HttpToolTransport`` Protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest

from psych_runtime.core.errors import TransientError
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import HttpTool
from psych_runtime.model.egress import HttpTransport
from psych_runtime.model.transient import classify_status, is_transient
from psych_runtime.tools.http import HttpToolError, HttpToolExecutor
from psych_runtime.tools.secrets import CredentialNotFound, InMemorySecretResolver

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme", principal="user-1")


@dataclass
class Request:
    """One parsed HTTP/1.1 request, bundled so a test handler takes one
    argument rather than the six pieces that make it up."""

    writer: asyncio.StreamWriter
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


Handler = Callable[[Request], Awaitable[None]]


async def _read_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Request:
    """Read one HTTP/1.1 request: method, path (with its query string), headers
    (lower-cased names), and body."""
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
    header_bytes, _, rest = head.partition(b"\r\n\r\n")
    lines = header_bytes.decode("latin-1").split("\r\n")
    request_line = lines[0] if lines else ""
    parts = request_line.split(" ")
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"

    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0"))
    body = rest
    while len(body) < content_length:
        chunk = await reader.read(content_length - len(body))
        if not chunk:
            break
        body += chunk
    return Request(writer=writer, method=method, path=path, headers=headers, body=body)


async def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    *,
    content_type: str = "application/json",
) -> None:
    reason = {200: "OK", 400: "Bad Request", 500: "Internal Server Error"}.get(status, "OK")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(headers.encode() + body)
    await writer.drain()


async def write_json(writer: asyncio.StreamWriter, status: int, payload: object) -> None:
    await _write_response(writer, status, json.dumps(payload).encode())


async def write_text(writer: asyncio.StreamWriter, status: int, text: str) -> None:
    await _write_response(writer, status, text.encode(), content_type="text/plain")


class StubServer:
    """A ``127.0.0.1`` server whose per-test handler drives the response."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> str:
        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = await _read_request(reader, writer)
                with contextlib.suppress(Exception):
                    # A slow handler racing a client that already gave up on
                    # a timeout can find the socket gone by the time it
                    # writes; that is the scenario under test, not a bug.
                    await self._handler(request)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        self._server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@asynccontextmanager
async def running_executor(
    handler: Handler, *, max_response_chars: int | None = None
) -> AsyncIterator[tuple[HttpToolExecutor, str, InMemorySecretResolver]]:
    """An ``HttpToolExecutor`` wired to a real ``HttpTransport`` against a
    running stub server, torn down with both when the block exits."""
    async with StubServer(handler) as base_url:
        transport = HttpTransport()
        secrets = InMemorySecretResolver()
        executor = (
            HttpToolExecutor(transport, secrets)
            if max_response_chars is None
            else HttpToolExecutor(transport, secrets, max_response_chars=max_response_chars)
        )
        try:
            yield executor, base_url, secrets
        finally:
            await transport.aclose()


def _tool(**overrides: object) -> HttpTool:
    base: dict[str, object] = {
        "name": "call_api",
        "description": "d",
        "url": "http://replaced/",  # filled in by each test with the real base_url
        "method": "POST",
    }
    base.update(overrides)
    return HttpTool.model_validate(base)


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


class TestTemplating:
    async def test_get_sends_arguments_as_query_parameters(self) -> None:
        seen: dict[str, list[str]] = {}

        async def handler(request: Request) -> None:
            assert request.method == "GET"
            split = urlsplit(request.path)
            assert split.path == "/search"
            seen.update(parse_qs(split.query))
            await write_json(request.writer, 200, {"ok": True})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(method="GET", url=f"{base_url}/search")
            result = await executor.call(SCOPE, tool, {"q": "hello world", "limit": 5})

        assert seen == {"q": ["hello world"], "limit": ["5"]}
        assert result == {"ok": True}

    async def test_post_sends_arguments_as_a_json_body(self) -> None:
        seen_body: dict[str, object] = {}

        async def handler(request: Request) -> None:
            assert request.method == "POST"
            assert request.headers.get("content-type") == "application/json"
            seen_body.update(json.loads(request.body))
            await write_json(request.writer, 200, {"status": "ok"})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(method="POST", url=f"{base_url}/refunds")
            result = await executor.call(SCOPE, tool, {"order_id": "A1", "cents": 500})

        assert seen_body == {"order_id": "A1", "cents": 500}
        assert result == {"status": "ok"}

    async def test_path_parameters_are_filled_and_removed_from_the_body(self) -> None:
        seen_path = ""
        seen_body: dict[str, object] = {}

        async def handler(request: Request) -> None:
            nonlocal seen_path
            seen_path = request.path
            seen_body.update(json.loads(request.body))
            await write_json(request.writer, 200, {"status": "refunded"})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(method="POST", url=f"{base_url}/orders/{{order_id}}/refund")
            result = await executor.call(SCOPE, tool, {"order_id": "A/1", "cents": 500})

        assert seen_path == "/orders/A%2F1/refund"
        assert seen_body == {"cents": 500}  # order_id was consumed by the path
        assert result == {"status": "refunded"}

    async def test_a_missing_path_argument_is_a_permanent_failure(self) -> None:
        reached = False

        async def handler(request: Request) -> None:
            nonlocal reached
            reached = True
            await write_json(request.writer, 200, {"unreachable": True})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(method="POST", url=f"{base_url}/orders/{{order_id}}/refund")
            with pytest.raises(ValueError, match="order_id"):
                await executor.call(SCOPE, tool, {"cents": 500})

        assert reached is False, "templating fails before any request is sent"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    async def test_a_resolved_credential_is_sent_as_a_bearer_token(self) -> None:
        seen_auth: str | None = None

        async def handler(request: Request) -> None:
            nonlocal seen_auth
            seen_auth = request.headers.get("authorization")
            await write_json(request.writer, 200, {"ok": True})

        async with running_executor(handler) as (executor, base_url, secrets):
            secrets.set(SCOPE, "payments-key", "sk-secret-value")
            tool = _tool(url=f"{base_url}/charge", credential="payments-key")
            await executor.call(SCOPE, tool, {})

        assert seen_auth == "Bearer sk-secret-value"

    async def test_an_unresolved_credential_raises_credential_not_found(self) -> None:
        reached = False

        async def handler(request: Request) -> None:
            nonlocal reached
            reached = True
            await write_json(request.writer, 200, {"unreachable": True})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/charge", credential="never-configured")
            with pytest.raises(CredentialNotFound):
                await executor.call(SCOPE, tool, {})

        assert reached is False, "credential resolution fails before any request is sent"


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestFailureClassification:
    async def test_a_500_becomes_a_transient_http_tool_error(self) -> None:
        async def handler(request: Request) -> None:
            await write_text(request.writer, 500, "upstream is on fire")

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/flaky")
            with pytest.raises(HttpToolError) as excinfo:
                await executor.call(SCOPE, tool, {})

        assert excinfo.value.status_code == 500
        assert "upstream is on fire" in str(excinfo.value)
        assert classify_status(500) is True
        assert is_transient(excinfo.value) is True

    async def test_a_400_becomes_a_permanent_http_tool_error(self) -> None:
        async def handler(request: Request) -> None:
            await write_json(request.writer, 400, {"error": "missing field"})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/flaky")
            with pytest.raises(HttpToolError) as excinfo:
                await executor.call(SCOPE, tool, {})

        assert excinfo.value.status_code == 400
        assert classify_status(400) is False
        assert is_transient(excinfo.value) is False

    async def test_a_failure_body_is_excerpted_not_passed_through_whole(self) -> None:
        huge = "x" * 20_000

        async def handler(request: Request) -> None:
            await write_text(request.writer, 500, huge)

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/flaky")
            with pytest.raises(HttpToolError) as excinfo:
                await executor.call(SCOPE, tool, {})

        # Comfortably under ToolFailure.message's 8192-character cap once the
        # agent loop wraps this into one, with room to spare for its own prose.
        assert len(str(excinfo.value)) < 4000
        assert "omitted" in str(excinfo.value)

    async def test_a_timeout_becomes_a_transient_error(self) -> None:
        async def handler(request: Request) -> None:
            # Slower than the tool's own timeout below, but still bounded so
            # the connection task finishes and this test does not leak one.
            await asyncio.sleep(0.3)
            await write_json(request.writer, 200, {"too": "late"})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/slow", timeout_seconds=0.05)
            with pytest.raises(TransientError):
                await executor.call(SCOPE, tool, {})


# ---------------------------------------------------------------------------
# Large responses
# ---------------------------------------------------------------------------


class TestLargeResponses:
    async def test_a_large_success_body_is_excerpted_not_passed_through_whole(self) -> None:
        huge = "y" * 200_000

        async def handler(request: Request) -> None:
            await write_text(request.writer, 200, huge)

        async with running_executor(handler, max_response_chars=1000) as (
            executor,
            base_url,
            _,
        ):
            tool = _tool(url=f"{base_url}/big")
            result = await executor.call(SCOPE, tool, {})

        assert isinstance(result, str)
        assert len(result) < 1200
        assert "truncated" in result

    async def test_a_small_json_response_is_returned_parsed(self) -> None:
        async def handler(request: Request) -> None:
            await write_json(request.writer, 200, {"order_id": "A1", "status": "shipped"})

        async with running_executor(handler) as (executor, base_url, _):
            tool = _tool(url=f"{base_url}/small")
            result = await executor.call(SCOPE, tool, {})

        assert result == {"order_id": "A1", "status": "shipped"}


# ---------------------------------------------------------------------------
# bind(): wiring into ToolExecutor's http_caller shape
# ---------------------------------------------------------------------------


class TestBind:
    async def test_bind_closes_over_scope_and_matches_the_tool_and_arguments_shape(self) -> None:
        async def handler(request: Request) -> None:
            await write_json(request.writer, 200, {"ok": True})

        async with running_executor(handler) as (executor, base_url, _):
            caller = executor.bind(SCOPE)
            tool = _tool(url=f"{base_url}/anything")
            result = await caller(tool, {})

        assert result == {"ok": True}
