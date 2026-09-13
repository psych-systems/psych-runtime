"""A real sandbox service on loopback, for testing ``RemoteSandbox`` and for
showing what one looks like.

``psych_runtime.sandbox.remote`` documents four HTTP routes; this module serves
them from a plain ``asyncio`` server with no HTTP library, over a real TCP
socket on ``127.0.0.1``, so a test of the remote adapter exercises genuine
request framing, streaming, concurrent connections (a binding reply arrives
on a second connection while the first is streaming) and disconnection,
rather than a mocked transport that can only fail the way its author
imagined.

Two things sit behind the routes:

- ``SandboxService`` wraps any ``Sandbox`` (a real local one, or the
  ``ScriptedSandbox`` below) and speaks the protocol for it. A consumer
  writing their own service can read this as the reference: it is the whole
  of the other side, including how a binding call travels back to the
  client and how a cancel ends a running program.
- ``ScriptedSandbox`` runs nothing. It answers each program with a result
  the test scripted, so adapter behaviour (framing, replies, faults) can be
  tested without a child process, and a consumer's own tests of an agent
  that uses ``run_code`` can script the sandbox exactly as they script the
  model. It is a test double, not a backend: DESIGN.md §18's rejection of
  in-process execution is about running the model's program, which this
  never does.

Faults are injected by name (``SandboxService.faults``) so a test can make
the service refuse credentials, disconnect after ``ready``, send a
malformed frame, hang, or answer with a server error, and assert that the
adapter reports each honestly and never retries an execution.

It lives in the shipped package rather than the test suite for the same
reason ``psych_runtime.testing.mcp_stub`` does: a consumer's tests need it as
much as this repository's do, and nothing here imports pytest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from psych_runtime.core.code_execution import Enforcement, IsolationLevel, ResultHandle
from psych_runtime.sandbox.port import (
    HostBinding,
    OutputCapture,
    Sandbox,
    SandboxDescription,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimits,
    SandboxResult,
    describe_sandbox,
)

__all__ = ["SandboxService", "ScriptedSandbox", "ServiceFault", "scripted_result"]


ServiceFault = str
"""One of: ``reject_auth``, ``server_error``, ``disconnect_after_ready``,
``malformed_frame``, ``hang_after_ready``, ``slow_ready``, ``error_frame``,
``weaker_isolation``, ``giant_frame``.

``weaker_isolation`` is the service that accepted terms it could not meet and
said so only afterwards -- what the adapter's post-hoc check exists to catch.
``giant_frame`` is the service that answers with more bytes than any limit it
was sent."""


def scripted_result(
    *,
    value: Any = None,
    stdout: str = "",
    stderr: str = "",
    failure: SandboxFailure | None = None,
    isolation: IsolationLevel | None = IsolationLevel.ISOLATED,
) -> SandboxResult:
    """A ``SandboxResult`` for ``ScriptedSandbox`` to hand back."""
    enforced = Enforcement.ENFORCED
    guarantees = (
        SandboxGuarantees(
            filesystem=enforced,
            network=enforced,
            process_tree=enforced,
            identity=enforced,
            cpu=enforced,
            memory=enforced,
            file_size=enforced,
            process_count=enforced,
            wall_clock=enforced,
            environment=enforced,
        )
        if isolation is IsolationLevel.ISOLATED
        else SandboxGuarantees(
            process_tree=enforced, wall_clock=enforced, environment=enforced, cpu=enforced
        )
    )
    return SandboxResult(
        stdout=stdout,
        stderr=stderr,
        stdout_data=stdout.encode("utf-8"),
        stderr_data=stderr.encode("utf-8"),
        stdout_size=len(stdout.encode("utf-8")),
        stderr_size=len(stderr.encode("utf-8")),
        value=value,
        failure=failure,
        duration_seconds=0.01,
        network_denied=isolation is IsolationLevel.ISOLATED,
        isolation=isolation,
        guarantees=guarantees,
    )


@dataclass
class ScriptedSandbox:
    """A ``Sandbox`` that returns scripted results and runs nothing.

    ``script`` maps a program's exact source to the result to return;
    ``default`` answers anything not scripted. ``bindings_to_call`` names
    bindings the fake "program" calls before returning, so a test can prove
    a binding travelled through a service and back. Every call is recorded
    on ``calls``.
    """

    script: dict[str, SandboxResult] = field(default_factory=dict)
    default: SandboxResult = field(default_factory=lambda: scripted_result(value=None))
    bindings_to_call: tuple[tuple[str, dict[str, Any]], ...] = ()
    isolation: IsolationLevel | None = IsolationLevel.ISOLATED
    calls: list[dict[str, Any]] = field(default_factory=list)
    binding_results: list[Any] = field(default_factory=list)

    async def describe(self) -> SandboxDescription:
        return SandboxDescription(
            backend="scripted",
            platform="test",
            isolation=self.isolation,
            guarantees=self.default.guarantees,
            mechanisms=("scripted",),
            network_grant_supported=True,
            artifacts_supported=True,
        )

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, HostBinding] | None = None,
        limits: SandboxLimits | None = None,
        network: bool = False,
        isolation: IsolationLevel | None = None,
        capture: OutputCapture | None = None,
        cancel: asyncio.Event | None = None,
    ) -> SandboxResult:
        self.calls.append(
            {
                "program": program,
                "bindings": sorted(bindings or {}),
                "limits": limits,
                "network": network,
                "isolation": isolation,
                "capture": capture,
            }
        )
        if cancel is not None and cancel.is_set():
            return SandboxResult(
                duration_seconds=0.0,
                failure=SandboxFailure(kind="cancelled", message="cancelled before start"),
                cancelled=True,
            )
        for name, arguments in self.bindings_to_call:
            binding = (bindings or {}).get(name)
            if binding is None:
                continue
            try:
                self.binding_results.append(await binding(arguments))
            except Exception as err:  # the program would see this as its own exception
                return scripted_result(
                    failure=SandboxFailure(kind="exception", message=str(err)),
                    isolation=self.isolation,
                )
        result = self.script.get(program, self.default)
        if isolation is not None and (
            result.isolation is None or not result.isolation.satisfies(isolation)
        ):
            return result.model_copy(
                update={
                    "value": None,
                    "failure": SandboxFailure(
                        kind="isolation_unavailable",
                        message="the scripted sandbox does not reach the requested level",
                    ),
                }
            )
        return result


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass
class _Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class SandboxService:
    """The reference sandbox service, on a loopback TCP port.

    Use as an async context manager; ``base_url`` is valid inside it.
    """

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        token: str | None = None,
        faults: set[ServiceFault] | None = None,
        ready_delay: float = 0.0,
    ) -> None:
        self._sandbox = sandbox
        self._token = token
        self.faults: set[ServiceFault] = set(faults or ())
        self._ready_delay = ready_delay
        self._server: asyncio.Server | None = None
        self._executions: dict[str, _Execution] = {}
        self.requests: list[_Request] = []
        self.cancelled: list[str] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._connections: list[asyncio.StreamWriter] = []

    @property
    def base_url(self) -> str:
        assert self._server is not None, "use SandboxService as an async context manager"
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def __aenter__(self) -> SandboxService:
        self._server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._connections):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for execution in self._executions.values():
            execution.cancel.set()
        with contextlib.suppress(OSError):
            await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)

    # -- HTTP plumbing ---------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._connections.append(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            request = await _read_request(reader)
            if request is None:
                return
            self.requests.append(request)
            await self._route(request, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(ValueError):
                self._connections.remove(writer)

    async def _route(self, request: _Request, writer: asyncio.StreamWriter) -> None:
        if self._token is not None or "reject_auth" in self.faults:
            expected = f"Bearer {self._token}" if self._token is not None else None
            given = request.headers.get("authorization")
            if "reject_auth" in self.faults or given != expected:
                await _respond(writer, 401, {"error": "unauthorized"})
                return
        if "server_error" in self.faults:
            await _respond(writer, 500, {"error": "boom"})
            return
        if request.method == "GET" and request.path == "/v1/sandbox":
            description = await describe_sandbox(self._sandbox)
            await _respond(writer, 200, description.model_dump(mode="json"))
            return
        if request.method == "POST" and request.path == "/v1/executions":
            await self._start_execution(request, writer)
            return
        await self._route_execution(request, writer)

    async def _route_execution(self, request: _Request, writer: asyncio.StreamWriter) -> None:
        parts = request.path.strip("/").split("/")
        if request.method != "POST" or len(parts) != 4 or parts[:2] != ["v1", "executions"]:
            await _respond(writer, 404, {"error": "not found"})
            return
        execution = self._executions.get(parts[2])
        if execution is None:
            await _respond(writer, 404, {"error": "no such execution"})
        elif parts[3] == "replies":
            execution.deliver_reply(json.loads(request.body or b"{}"))
            await _respond(writer, 204, None)
        elif parts[3] == "cancel":
            self.cancelled.append(parts[2])
            execution.cancel.set()
            await _respond(writer, 202, {"cancelled": True})
        else:
            await _respond(writer, 404, {"error": "not found"})

    async def _injected_fault(
        self,
        writer: asyncio.StreamWriter,
        send: Callable[[Mapping[str, Any]], Any],
        execution: _Execution,
    ) -> bool:
        """Act out whichever fault is armed, if any. ``True`` means the
        execution ends here and the program never runs."""
        if "disconnect_after_ready" in self.faults:
            writer.close()
            return True
        if "hang_after_ready" in self.faults:
            await execution.cancel.wait()
            return True
        if "malformed_frame" in self.faults:
            writer.write(b"this is not json\n")
            await writer.drain()
            return True
        if "giant_frame" in self.faults:
            writer.write(b'{"type":"done","result":"' + b"x" * (9 * 1024 * 1024) + b'"}\n')
            await writer.drain()
            return True
        if "error_frame" in self.faults:
            await send({"type": "error", "kind": "provider_error", "message": "capacity"})
            return True
        return False

    async def _start_execution(self, request: _Request, writer: asyncio.StreamWriter) -> None:
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            await _respond(writer, 400, {"error": "bad json"})
            return
        execution_id = str(body.get("execution_id") or "")
        if not execution_id:
            await _respond(writer, 400, {"error": "execution_id required"})
            return
        execution = _Execution(execution_id)
        self._executions[execution_id] = execution
        await _start_stream(writer)

        async def send(frame: Mapping[str, Any]) -> None:
            writer.write((json.dumps(frame) + "\n").encode())
            await writer.drain()

        if self._ready_delay or "slow_ready" in self.faults:
            await asyncio.sleep(self._ready_delay or 0.5)
        description = await describe_sandbox(self._sandbox)
        await send(
            {
                "type": "ready",
                "network_denied": description.guarantees.network is Enforcement.ENFORCED,
                "canary_readable": description.guarantees.filesystem is not Enforcement.ENFORCED,
                "uid": None,
                "platform": description.platform,
            }
        )
        if await self._injected_fault(writer, send, execution):
            return

        bindings = {
            name: execution.binding(name, send) for name in body.get("bindings", []) if name
        }
        limits = SandboxLimits.model_validate(body["limits"]) if body.get("limits") else None
        capture = OutputCapture.model_validate(body["capture"]) if body.get("capture") else None
        requested = body.get("isolation")
        isolation = IsolationLevel(requested) if isinstance(requested, str) else None

        # Before the program runs, which is the only moment a refusal still
        # prevents anything. A service that ran the program and then said "I
        # could not give you isolation" has already let it touch whatever it
        # was going to touch. The reference implementation refuses here so
        # that anyone reading it to build their own service copies that
        # order rather than inventing the late one.
        if isolation is not None and not (
            description.isolation is not None and description.isolation.satisfies(isolation)
        ):
            offered = description.isolation.value if description.isolation else "none"
            await send(
                {
                    "type": "error",
                    "kind": "isolation_unavailable",
                    "message": (
                        f"this service reaches {offered!r} isolation and the execution "
                        f"required {isolation.value!r}; the program was not run"
                    ),
                }
            )
            return
        if "weaker_isolation" in self.faults:
            isolation = None
        result = await self._sandbox.run(
            str(body.get("program", "")),
            bindings=bindings,
            limits=limits,
            network=bool(body.get("network", False)),
            isolation=isolation,
            capture=capture,
            cancel=execution.cancel,
        )
        if "weaker_isolation" in self.faults:
            result = result.model_copy(update={"isolation": IsolationLevel.PROCESS})
        await send({"type": "done", "result": result.model_dump(mode="json")})


class _RelayedBindingFailure(RuntimeError):
    """A binding failure the client answered with, on its way to the program.

    Carries the client's ``kind`` so ``binding_failure_kind`` reads it back
    unchanged rather than replacing it with this class's own name.
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


class _Execution:
    """One running execution's reply futures."""

    def __init__(self, execution_id: str) -> None:
        self.id = execution_id
        self.cancel = asyncio.Event()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0

    def binding(self, name: str, send: Callable[[Mapping[str, Any]], Any]) -> HostBinding:
        async def call(arguments: Mapping[str, Any]) -> Any:
            call_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[call_id] = future
            await send({"type": "call", "id": call_id, "name": name, "arguments": dict(arguments)})
            reply = await future
            if reply.get("ok"):
                value = reply.get("value")
                if reply.get("result") == "handle" and isinstance(value, Mapping):
                    # Rebuilt, for the same reason the failure kind is: this is
                    # the last hop, and whatever it hands the child is what the
                    # child's protocol will re-encode. Returning the bare dict
                    # would drop the distinction here and nowhere else, so a
                    # program behind a remote sandbox would be the only one
                    # handed a dictionary where every other backend gives it a
                    # readable handle.
                    return ResultHandle(
                        handle=str(value.get("handle", "")),
                        tool=str(value.get("tool", "")),
                        size_bytes=int(value.get("size_bytes", 0)),
                        stored=str(value.get("stored", "log")),
                    )
                return value
            kind = reply.get("kind")
            # The kind travels the last hop too, or a program behind a remote
            # sandbox would be the only one that cannot tell a refusal from an
            # outage.
            raise _RelayedBindingFailure(
                kind if isinstance(kind, str) else "tool_failed",
                str(reply.get("message") or "host tool call failed"),
            )

        return call

    def deliver_reply(self, reply: dict[str, Any]) -> None:
        future = self._pending.pop(int(reply.get("id", -1)), None)
        if future is not None and not future.done():
            future.set_result(reply)


async def _read_request(reader: asyncio.StreamReader) -> _Request | None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30.0)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    lines = head.decode("latin-1").split("\r\n")
    request_line = lines[0].split(" ")
    if len(request_line) < 2:
        return None
    method, target = request_line[0].upper(), request_line[1]
    path = target.split("?", 1)[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    body = await reader.readexactly(length) if length else b""
    return _Request(method=method, path=path, headers=headers, body=body)


async def _respond(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
    reasons = {
        200: "OK",
        202: "Accepted",
        204: "No Content",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        500: "Internal Server Error",
    }
    body = b"" if payload is None else json.dumps(payload).encode()
    head = (
        f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
    with contextlib.suppress(OSError):
        await writer.drain()


async def _start_stream(writer: asyncio.StreamWriter) -> None:
    writer.write(
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: application/x-ndjson\r\n"
        b"cache-control: no-store\r\n"
        b"connection: close\r\n\r\n"
    )
    await writer.drain()
