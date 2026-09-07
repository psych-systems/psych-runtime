"""A stub MCP server, and the helpers that wire one up.

Exported rather than left in the test suite because it has two consumers and
only one of them is a test. ``examples/playground`` runs capability scenarios
against a real MCP server over a real socket, and it used to reach into
``tests.functional.test_mcp`` for this class. That import cannot work in the
playground's own image: ``.dockerignore`` keeps ``tests`` out of the build
context on purpose, so the backend imported a package that was not there and
died before uvicorn bound a port.

Which is the right way round anyway. ``psych_runtime.testing`` exists to hand
consumers the fixtures Psych tests itself with (DESIGN.md section 21), and a
consumer wiring MCP wants exactly this: a server that speaks the protocol,
records what it was asked, and can be told to misbehave. Nothing here imports
pytest, so it costs an application nothing to depend on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from psych_runtime.core.spec import McpOAuth, McpServer

__all__ = ["McpStubServer", "ReceivedCall", "ReceivedRequest", "make_server", "wire_tool"]


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes]:
    """Read one HTTP/1.1 request: method, path, lowercased headers, body."""
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
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else "/"

    headers: dict[str, str] = {}
    content_length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        val = value.strip()
        if key:
            headers[key] = val
        if key == "content-length":
            content_length = int(val)

    body = rest
    while len(body) < content_length:
        chunk = await reader.read(content_length - len(body))
        if not chunk:
            break
        body += chunk
    return method, path, headers, body


# ---------------------------------------------------------------------------
# Raw HTTP/1.1, write side
# ---------------------------------------------------------------------------

_REASON = {200: "OK", 202: "Accepted", 302: "Found", 400: "Bad Request", 404: "Not Found"}


async def _write_status(
    writer: asyncio.StreamWriter,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    chunked: bool = False,
) -> None:
    # Every response closes its connection from the server's side after one
    # exchange (the SSE stream included, once it ends): telling the client
    # that up front stops httpx's connection pool from trying to reuse a
    # socket this server already tore down, which is a hang, not an error,
    # because the reused write just never gets a reply.
    all_headers: dict[str, str] = {"Connection": "close"}
    if chunked:
        all_headers["Transfer-Encoding"] = "chunked"
    all_headers.update(headers or {})
    lines = [f"HTTP/1.1 {status} {_REASON.get(status, 'OK')}"]
    lines.extend(f"{key}: {value}" for key, value in all_headers.items())
    lines.append("")
    lines.append("")
    writer.write("\r\n".join(lines).encode())
    await writer.drain()


async def _write_chunk(writer: asyncio.StreamWriter, data: bytes) -> None:
    if not data:
        return
    writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
    await writer.drain()


async def _write_json(
    writer: asyncio.StreamWriter,
    status: int,
    payload: object,
    *,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    headers.update(extra_headers or {})
    await _write_status(writer, status, headers=headers, chunked=False)
    writer.write(body)
    await writer.drain()


# ---------------------------------------------------------------------------
# The stub server
# ---------------------------------------------------------------------------


@dataclass
class ReceivedRequest:
    rpc_method: str | None
    authorization: str | None
    session_id: str | None
    protocol_version_header: str | None = None
    mcp_method_header: str | None = None
    mcp_name_header: str | None = None
    meta: dict[str, object] | None = None
    request_id: object | None = None


@dataclass
class ReceivedCall:
    tool: str | None
    arguments: dict[str, object]
    authorization: str | None
    session_id: str | None


def wire_tool(
    name: str, *, read_only: bool = False, destructive: bool = False
) -> dict[str, object]:
    """One tool descriptor in the shape a real MCP ``tools/list`` result uses."""
    annotations: dict[str, object] | None = None
    if read_only:
        annotations = {"readOnlyHint": True}
    elif destructive:
        annotations = {"destructiveHint": True}
    return {
        "name": name,
        "description": f"the {name} tool",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": annotations,
    }


CallHandler = Callable[[dict[str, object]], tuple[str, bool]]


class McpStubServer:
    """A minimal, real MCP server on ``127.0.0.1``, in one of two eras.

    ``modern=False`` (the default) implements enough of the pre-2026-07-28
    Streamable HTTP transport for ``psych_runtime.tools.mcp``'s legacy fallback path
    to speak to: POST for JSON-RPC requests and notifications, a session id
    assigned at ``initialize`` and required on every later request, and a
    standalone GET opening a real SSE stream for server-initiated push. It
    never implements ``server/discover``, which is exactly what makes it a
    legacy server from the new client's point of view: the era-negotiation
    probe gets an "unknown session" error it does not recognise as modern,
    and falls back to ``initialize``. ``legacy_protocol_version`` only
    changes the cosmetic ``protocolVersion`` echoed back at ``initialize``
    (``"2025-06-18"`` or ``"2025-11-25"``); the wire mechanics this class
    implements are identical either way, which is the point being tested. One
    legacy fallback path handles both.

    ``modern=True`` implements the 2026-07-28 shape instead: stateless
    (no session, no ``initialize``), ``server/discover``, ``resultType`` on
    results, ``ttlMs``/``cacheScope`` on ``tools/list``, and
    ``subscriptions/listen`` in place of the GET stream. ``force_error`` and
    ``force_input_required`` are one-shot hooks a test arms before making a
    call, so the typed-error and MRTR-refusal paths can be exercised without
    building a fully conformant server.

    Every accepted connection is tracked so ``__aexit__`` can cancel a
    still-open SSE listener rather than leaving it hung waiting on a queue
    nobody will ever fill again.
    """

    def __init__(
        self,
        tools: list[dict[str, object]],
        *,
        modern: bool = False,
        legacy_protocol_version: str = "2025-06-18",
    ) -> None:
        self.tools = tools
        self.modern = modern
        self.legacy_protocol_version = legacy_protocol_version
        self.init_count = 0
        self.discover_count = 0
        self.list_count = 0
        self.received_calls: list[ReceivedCall] = []
        self.received_requests: list[ReceivedRequest] = []
        self.call_handlers: dict[str, CallHandler] = {}
        self.instructions: str | None = None
        """What this server says it is for, returned from the handshake. MCP
        allows it at ``initialize`` and at ``server/discover``; the client
        reads whichever era it negotiated."""
        self.tools_ttl_ms: int | None = None
        self.tools_cache_scope: str | None = None
        self.force_error: tuple[int, str] | None = None
        self.force_input_required = False
        self.break_stream_once = False
        # OAuth: unset (None) means "no auth required at all", the behaviour
        # every non-OAuth test in this file exercises. Set to a mapping of
        # valid bearer token -> its granted scope (space-separated) to
        # require one, and set `require_scope` to additionally demand a
        # 403 insufficient_scope step-up for tokens missing it.
        self.oauth_tokens: dict[str, str] | None = None
        self.require_scope: str | None = None
        self.protected_resource_metadata: dict[str, object] | None = None
        self.received_authorization_headers: list[str | None] = []
        self._sessions: dict[str, list[asyncio.Queue[bytes | None]]] = {}
        self._modern_listeners: list[tuple[object, asyncio.Queue[bytes | None]]] = []
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> McpStubServer:
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self._server = server
        self._port = server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        # Cancel every accepted connection's handler task before closing the
        # listening socket, not after: on 3.12+, Server.wait_closed() waits
        # for already-accepted connections to finish too, and a still-open
        # SSE listener sits in a queue.get() that nothing but cancellation
        # ever completes. Closing first would deadlock waiting for a task
        # this method itself has not yet cancelled.
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/mcp"

    @property
    def resource_metadata_url(self) -> str:
        return f"http://127.0.0.1:{self._port}/.well-known/oauth-protected-resource"

    def bearer_challenge(self) -> str:
        return f'Bearer resource_metadata="{self.resource_metadata_url}", scope="mcp:read"'

    def insufficient_scope_challenge(self) -> str:
        assert self.require_scope is not None
        return (
            f'Bearer error="insufficient_scope", scope="{self.require_scope}", '
            f'resource_metadata="{self.resource_metadata_url}"'
        )

    def set_tools(self, tools: list[dict[str, object]]) -> None:
        self.tools = tools

    async def wait_for_sse_listener(self, *, timeout: float = 2.0) -> None:
        """Block until at least one notification stream is registered,
        legacy GET or modern ``subscriptions/listen`` alike.

        The client opens its notification listener as a background task
        (``McpConnection.connect`` does not await it), so a test that pushes
        a notification right after connecting can otherwise race ahead of the
        listener actually reaching this server.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if any(self._sessions.values()) or self._modern_listeners:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("no notification listener connected within the timeout")

    async def push_list_changed(self) -> None:
        """Broadcast ``notifications/tools/list_changed`` to every open
        listener, legacy and modern alike, the way a real server would when
        its catalogue changes for reasons no one client caused."""
        legacy_message = json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        ).encode()
        legacy_data = b"data: " + legacy_message + b"\n\n"
        for queues in self._sessions.values():
            for queue in list(queues):
                await queue.put(legacy_data)
        for subscription_id, queue in list(self._modern_listeners):
            modern_message = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {
                        "_meta": {"io.modelcontextprotocol/subscriptionId": subscription_id}
                    },
                }
            ).encode()
            await queue.put(b"data: " + modern_message + b"\n\n")

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            method, path, headers, body = await _read_request(reader)
            if method == "GET" and path == "/.well-known/oauth-protected-resource":
                await self._handle_protected_resource_metadata(writer)
            elif method == "GET":
                await self._handle_sse(writer, headers)
            else:
                await self._handle_post(writer, headers, body)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _wire_tools(self) -> list[dict[str, object]]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool.get("inputSchema", {}),
                "annotations": tool.get("annotations"),
            }
            for tool in self.tools
        ]

    async def _handle_protected_resource_metadata(self, writer: asyncio.StreamWriter) -> None:
        assert self.protected_resource_metadata is not None
        await _write_json(writer, 200, self.protected_resource_metadata)

    def _oauth_gate(self, auth: str | None) -> tuple[int, str] | None:
        """``None`` if this request may proceed; otherwise the ``(status,
        WWW-Authenticate)`` pair ``_handle_post`` should answer with.

        Every check a real OAuth-protected MCP server makes before it even
        looks at the JSON-RPC body: is there a bearer token at all, is it one
        this server issued, and (if ``require_scope`` is set) does it carry
        the scope this call demands.
        """
        if self.oauth_tokens is None:
            return None
        token = auth[len("Bearer ") :] if auth and auth.startswith("Bearer ") else None
        scope = self.oauth_tokens.get(token) if token is not None else None
        if scope is None:
            return 401, self.bearer_challenge()
        if self.require_scope and self.require_scope not in scope.split():
            return 403, self.insufficient_scope_challenge()
        return None

    async def _handle_post(  # noqa: PLR0911 - a protocol dispatcher, one branch per RPC method
        self, writer: asyncio.StreamWriter, headers: dict[str, str], body: bytes
    ) -> None:
        auth = headers.get("authorization")
        self.received_authorization_headers.append(auth)
        challenge = self._oauth_gate(auth)
        if challenge is not None:
            status, www_authenticate = challenge
            await _write_status(
                writer,
                status,
                headers={"WWW-Authenticate": www_authenticate, "Content-Length": "0"},
            )
            return

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            await _write_json(
                writer, 400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}
            )
            return

        rpc_method = payload.get("method")
        session_id = headers.get("mcp-session-id")
        params = payload.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        self.received_requests.append(
            ReceivedRequest(
                rpc_method=rpc_method,
                authorization=auth,
                session_id=session_id,
                protocol_version_header=headers.get("mcp-protocol-version"),
                mcp_method_header=headers.get("mcp-method"),
                mcp_name_header=headers.get("mcp-name"),
                meta=meta if isinstance(meta, dict) else None,
                request_id=payload.get("id"),
            )
        )

        if self.force_error is not None:
            code, message = self.force_error
            self.force_error = None
            await _write_json(
                writer,
                400,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": code, "message": message},
                },
            )
            return

        if self.modern:
            await self._handle_post_modern(writer, payload, rpc_method, auth)
            return

        if rpc_method == "initialize":
            self.init_count += 1
            sid = str(uuid.uuid4())
            self._sessions[sid] = []
            result: dict[str, object] = {
                "protocolVersion": self.legacy_protocol_version,
                "capabilities": {},
                "serverInfo": {"name": "stub", "version": "1.0"},
            }
            if self.instructions is not None:
                result["instructions"] = self.instructions
            await _write_json(
                writer,
                200,
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": result},
                extra_headers={"Mcp-Session-Id": sid},
            )
            return

        if rpc_method == "notifications/initialized":
            await _write_status(writer, 202, headers={"Content-Length": "0"})
            return

        if session_id not in self._sessions:
            await _write_json(
                writer,
                404,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": -32001, "message": "unknown session"},
                },
            )
            return

        if rpc_method == "tools/list":
            self.list_count += 1
            await _write_json(
                writer,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"tools": self._wire_tools()},
                },
            )
            return

        if rpc_method == "tools/call":
            params = payload.get("params") or {}
            name = str(params.get("name"))
            arguments = params.get("arguments") or {}
            self.received_calls.append(
                ReceivedCall(
                    tool=name, arguments=arguments, authorization=auth, session_id=session_id
                )
            )
            handler = self.call_handlers.get(name)
            content, is_error = (
                handler(arguments) if handler is not None else (f"called {name}", False)
            )
            await _write_json(
                writer,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {"content": [{"type": "text", "text": content}], "isError": is_error},
                },
            )
            return

        await _write_json(
            writer,
            400,
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32601, "message": "no method"},
            },
        )

    async def _handle_post_modern(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, object],
        rpc_method: object,
        auth: str | None,
    ) -> None:
        """The 2026-07-28 shape: no session, ``server/discover``,
        ``resultType`` on every result, ``ttlMs``/``cacheScope`` on
        ``tools/list``, and ``subscriptions/listen`` for push."""
        request_id = payload.get("id")

        if rpc_method == "server/discover":
            self.discover_count += 1
            await _write_json(
                writer,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {"tools": {}},
                        **(
                            {} if self.instructions is None else {"instructions": self.instructions}
                        ),
                        "_meta": {
                            "io.modelcontextprotocol/serverInfo": {"name": "stub", "version": "2.0"}
                        },
                    },
                },
            )
            return

        if rpc_method == "tools/list":
            self.list_count += 1
            result: dict[str, object] = {"resultType": "complete", "tools": self._wire_tools()}
            if self.tools_ttl_ms is not None:
                result["ttlMs"] = self.tools_ttl_ms
            if self.tools_cache_scope is not None:
                result["cacheScope"] = self.tools_cache_scope
            await _write_json(writer, 200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            return

        if rpc_method == "tools/call":
            await self._handle_tools_call_modern(writer, payload, request_id, auth)
            return

        if rpc_method == "subscriptions/listen":
            await self._handle_subscriptions_listen(writer, request_id)
            return

        await _write_json(
            writer,
            404,
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "no method"}},
        )

    async def _handle_tools_call_modern(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, object],
        request_id: object,
        auth: str | None,
    ) -> None:
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        name = str(params.get("name"))
        arguments = params.get("arguments") or {}
        arguments = arguments if isinstance(arguments, dict) else {}
        self.received_calls.append(
            ReceivedCall(tool=name, arguments=arguments, authorization=auth, session_id=None)
        )

        if self.break_stream_once:
            self.break_stream_once = False
            # Start a chunked SSE response and then abandon it mid-frame: the
            # socket closes (in _on_connect's finally) before the event is
            # complete, which httpx surfaces as a transport error while still
            # inside the buffered `.request()` call.
            await _write_status(
                writer, 200, headers={"Content-Type": "text/event-stream"}, chunked=True
            )
            await _write_chunk(writer, b'data: {"jsonrpc": "2.0", "id": ' + b"0" * 200)
            return

        if self.force_input_required:
            self.force_input_required = False
            await _write_json(
                writer,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "resultType": "input_required",
                        "inputRequests": {
                            "confirm": {
                                "method": "elicitation/create",
                                "params": {"mode": "form", "message": "confirm?"},
                            }
                        },
                    },
                },
            )
            return

        handler = self.call_handlers.get(name)
        content, is_error = handler(arguments) if handler is not None else (f"called {name}", False)
        await _write_json(
            writer,
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": content}],
                    "isError": is_error,
                },
            },
        )

    async def _handle_subscriptions_listen(
        self, writer: asyncio.StreamWriter, subscription_id: object
    ) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._modern_listeners.append((subscription_id, queue))
        await _write_status(
            writer,
            200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            chunked=True,
        )
        ack = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "_meta": {"io.modelcontextprotocol/subscriptionId": subscription_id},
                    "notifications": {"toolsListChanged": True},
                },
            }
        ).encode()
        await _write_chunk(writer, b"data: " + ack + b"\n\n")
        try:
            while True:
                data = await queue.get()
                if data is None:
                    return
                await _write_chunk(writer, data)
        finally:
            with contextlib.suppress(ValueError):
                self._modern_listeners.remove((subscription_id, queue))

    async def _handle_sse(self, writer: asyncio.StreamWriter, headers: dict[str, str]) -> None:
        session_id = headers.get("mcp-session-id")
        if session_id not in self._sessions:
            await _write_status(writer, 404, headers={"Content-Length": "0"})
            return
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._sessions[session_id].append(queue)
        await _write_status(
            writer,
            200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            chunked=True,
        )
        try:
            while True:
                data = await queue.get()
                if data is None:
                    return
                await _write_chunk(writer, data)
        finally:
            with contextlib.suppress(ValueError):
                self._sessions[session_id].remove(queue)


def make_server(
    url: str,
    *,
    name: str = "support",
    credential: str | None = None,
    oauth: McpOAuth | None = None,
    allow: tuple[str, ...] = (),
    optional: bool = False,
) -> McpServer:
    return McpServer(
        name=name, url=url, credential=credential, oauth=oauth, allow=allow, optional=optional
    )
