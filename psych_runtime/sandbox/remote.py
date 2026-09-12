"""The remote ``Sandbox`` adapter: a sandbox service the consumer operates or buys.

DESIGN.md §18. A deployment that runs its programs somewhere else -- a
service in its own infrastructure that wraps containers or microVMs, a
gateway in front of a hosted provider, a colleague's VM farm -- implements
one small HTTP protocol and this adapter speaks it. The core package is not
coupled to any vendor: the protocol below is the only thing it knows, and
``psych_runtime.testing.sandbox_service`` is a complete reference
implementation of the other side, used by this adapter's own tests and
runnable against any local ``Sandbox``.

## The protocol

Four routes under a base URL, JSON everywhere, credentials in an
``Authorization: Bearer`` header the adapter obtains from a callable at
call time and never stores in a Spec, a Record or a log.

- ``GET {base}/v1/sandbox`` returns a ``SandboxDescription`` as JSON. The
  service says what it can promise; the adapter relays it with one change,
  ``backend`` becomes ``remote:<what the service said>``, so a report never
  mistakes a claim for an observation this process made.
- ``POST {base}/v1/executions`` starts one execution. The body carries
  ``execution_id`` (minted here, so a cancel names exactly this attempt),
  ``program``, ``bindings`` (names only), ``limits``, ``network``,
  ``isolation`` and ``capture``. **A service that cannot meet ``isolation``
  or ``network`` must refuse before executing the program** -- status
  ``409`` with a JSON ``{"error": ...}`` body, or a single ``error`` frame
  with kind ``isolation_unavailable`` -- because a refusal issued after the
  program has run does not undo what it did. The response is ``200`` with
  ``application/x-ndjson``: one JSON object per line, streamed, in the same
  vocabulary the local bootstrap speaks (``psych_runtime.sandbox.protocol``):
  ``ready``, then any number of ``call`` frames, then exactly one ``done``
  carrying a ``result`` (a ``SandboxResult`` as JSON) or one ``error``
  carrying ``kind`` and ``message``.
- ``POST {base}/v1/executions/{id}/replies`` answers one ``call`` frame:
  ``{"id": n, "ok": true, "value": ...}`` or ``{"id": n, "ok": false,
  "message": "..."}``. ``204``.
- ``POST {base}/v1/executions/{id}/cancel`` asks the service to end the
  execution and kill everything it started. ``202``.

## Deterministic semantics for everything that can go wrong

- **The service refuses or errors** (any status but ``200``): a
  ``provider_error`` failure naming the status, never the response body
  verbatim (it might echo the credential) and never raised.
- **The stream ends before ``done``**, or a frame is malformed: a
  ``provider_error`` failure that says whether the program completed is
  unknown. Nothing is retried: the program may have had side effects
  through its bindings, and a retry would repeat them.
- **The wall clock passes** with a small grace for transport latency: the
  adapter posts a cancel, stops reading, and reports ``timeout`` exactly as
  a local backend would.
- **The caller cancels**: the adapter posts a cancel and reports
  ``cancelled``. A cancel request that itself fails is ignored; the service
  is expected to end orphaned executions on its own clock too.
- **``describe()``** is the one call retried: twice, on a transport error
  only, because it has no side effects.
- **A service that cannot meet the requested terms** is not sent the
  program at all: when an execution names an isolation level, the adapter
  reads the service's own description first and refuses locally when what
  it advertises does not satisfy the request. That description is cached
  for the life of the adapter's last successful read, so the pre-flight
  costs one request rather than one per execution.
- **The isolation the service reports afterwards** is still compared with
  what was requested and the result withheld if weaker. That is the
  backstop, not the control: it catches a service that advertised one thing
  and delivered another, which is a service to stop using.

## What the adapter never does

It never follows a redirect (the transport refuses them), never sends the
program anywhere but the configured base URL, never puts the credential in
a URL, and never logs a response body. It refuses to send a credential over
plaintext ``http://`` unless the host is loopback and the caller opted in
explicitly, because a bearer token on the wire is a bearer token for
whoever is on the wire. It also bounds what it will read back: a single
frame, a whole stream, the number of frames and the number of blank lines
each have a ceiling, so a faulty or hostile service cannot exhaust this
worker's memory with one enormous line or its time with millions of tiny
ones. Reading hands the event loop back as it goes, because a parse that
runs to the end without yielding would disable the very timeout meant to
end the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from ipaddress import ip_address
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from psych_runtime.core.code_execution import IsolationLevel
from psych_runtime.core.scope import Scope
from psych_runtime.sandbox._local import DEFAULT_CAPTURE, withhold_if_weaker
from psych_runtime.sandbox.port import (
    HostBinding,
    OutputCapture,
    SandboxDescription,
    SandboxFailure,
    SandboxLimits,
    SandboxResult,
    SandboxSetupError,
)
from psych_runtime.sandbox.protocol import (
    CallFrame,
    SandboxProtocolError,
    build_reply_frame,
    parse_call_or_done_frame,
    parse_ready_frame,
)

__all__ = ["CredentialSource", "RemoteSandbox", "RemoteTransport", "StreamedResponse"]

_DEFAULT_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
_WALL_GRACE_SECONDS: Final = 5.0
"""Added to the wall clock before the adapter gives up on the stream: the
service enforces the real limit and needs a moment to say so."""
_DESCRIBE_TIMEOUT_SECONDS: Final = 15.0
_DESCRIBE_ATTEMPTS: Final = 3

_MAX_FRAME_BYTES: Final = 8 * 1024 * 1024
"""The largest single NDJSON line this adapter will accept from a service.

A frame carries a result, and a result carries captured output, so this is
generous -- but it is finite. Trusting the other side to respect the capture
limits it was sent is the same mistake as trusting a child process to respect
its own rlimits: a faulty service and a hostile one produce the same enormous
line, and `str.strip()` on it has already cost the memory."""

_MAX_STREAM_BYTES: Final = 64 * 1024 * 1024
"""The largest whole stream. Bounds a service that answers with many frames
rather than one big one."""

_MAX_FRAMES: Final = 100_000
"""The most lines this adapter will read from one execution, blanks included.

Bytes are not the only budget a hostile answer can exhaust. A stream well
inside the byte ceiling can still be millions of lines, and every one of them
costs a find, a slice and a decode. A real execution sends a `ready`, a `done`
and one frame per captured chunk, so five figures is already far past
anything a service has reason to send."""

_MAX_BLANK_LINES: Final = 1_000
"""The most empty lines one execution may contain.

NDJSON allows them and nothing needs them, so they are the cheapest padding a
hostile service can send: no frame to parse, no bytes to speak of, just work.
A separate ceiling means the failure says which one was hit."""

_LINES_PER_YIELD: Final = 512
"""How many lines are parsed before the reader hands the event loop back.

One chunk can hold a great many lines, and parsing them all in one pass is
synchronous work no timeout can interrupt -- including the wall-clock timeout
around this very stream. Yielding on a count rather than on a clock keeps it
deterministic."""

CredentialSource = Callable[[], Awaitable[str | None]]
"""Where the bearer token comes from, asked on every call so a rotated or
revoked credential takes effect on the next execution. ``None`` means send
no ``Authorization`` header."""


class StreamedResponse(Protocol):
    """The part of an ``httpx.Response`` a streamed execution reads."""

    @property
    def status_code(self) -> int: ...

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...


class RemoteTransport(Protocol):
    """What this adapter needs from ``psych_runtime.HttpTransport``, structurally.

    ``psych_runtime.sandbox`` sits beside ``psych_runtime.model`` in the layering and
    may not import it, so the seam is named by shape. Every request this
    adapter makes goes through it, which is what keeps a remote sandbox on
    the egress policy's side of the line (DESIGN.md §14).
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> Any: ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> AbstractAsyncContextManager[Any]: ...


def _check_frame_size(pending: int) -> None:
    if pending > _MAX_FRAME_BYTES:
        raise SandboxProtocolError(
            f"a frame was larger than this adapter accepts "
            f"({pending} bytes against a {_MAX_FRAME_BYTES} byte ceiling)"
        )


class _FrameBudget:
    """Everything one response stream is allowed to spend, counted as it goes.

    Four ceilings, because a hostile answer has four ways to be expensive and
    only one of them is total size: one endless line, an endless number of
    lines, an endless number of *empty* lines, and too many bytes overall. Each
    is reported by name, so a failure says which budget was spent rather than
    "the service misbehaved".
    """

    __slots__ = ("blanks", "bytes_read", "lines")

    def __init__(self) -> None:
        self.bytes_read = 0
        self.lines = 0
        self.blanks = 0

    def read(self, count: int) -> None:
        self.bytes_read += count
        if self.bytes_read > _MAX_STREAM_BYTES:
            raise SandboxProtocolError(
                f"the response stream passed this adapter's {_MAX_STREAM_BYTES} byte "
                "ceiling before the execution ended"
            )

    def line(self, *, blank: bool) -> None:
        self.lines += 1
        if self.lines > _MAX_FRAMES:
            raise SandboxProtocolError(
                f"the response stream passed this adapter's {_MAX_FRAMES} line ceiling "
                "before the execution ended"
            )
        if blank:
            self.blanks += 1
            if self.blanks > _MAX_BLANK_LINES:
                raise SandboxProtocolError(
                    f"the response stream passed this adapter's {_MAX_BLANK_LINES} blank "
                    "line ceiling before the execution ended"
                )


async def _iter_frame_lines(response: StreamedResponse) -> AsyncIterator[str]:
    """Yield one non-empty NDJSON line at a time, bounded while it is still bytes.

    The size has to be judged before the whole line exists, not after. A
    hostile or broken service can answer with a single line that goes on
    forever, and a reader that hands back complete lines has already allocated
    it by the time its length can be measured. So chunks are read as they
    arrive, the unterminated remainder is checked against the frame ceiling on
    every chunk, and the running total is checked against the stream ceiling.
    The service was sent capture limits, but a limit the other side enforces is
    a request to it and never a property of it.

    Cost is bounded three more ways, because size is not the only budget. Lines
    and blank lines are counted against their own ceilings, since a stream can
    sit well inside the byte ceiling and still be millions of them. The buffer
    is walked with an index and compacted once per chunk rather than sliced
    from the front once per line, which would make a chunk of newlines
    quadratic. And the loop hands the event loop back every few hundred lines,
    counted across the whole response and never reset at a chunk boundary the
    service chose: parsing is synchronous, so without that a flood blocks
    everything else in the process -- including the wall-clock timeout that is
    supposed to end this stream.
    """
    buffer = bytearray()
    budget = _FrameBudget()
    since_yield = 0
    async for chunk in response.aiter_bytes():
        budget.read(len(chunk))
        buffer.extend(chunk)
        start = 0
        while True:
            index = buffer.find(b"\n", start)
            if index < 0:
                break
            raw_line = bytes(buffer[start:index])
            start = index + 1
            _check_frame_size(len(raw_line))
            line = _decode_frame_line(raw_line)
            budget.line(blank=not line)
            since_yield += 1
            if since_yield >= _LINES_PER_YIELD:
                since_yield = 0
                # Nothing else in this process runs while the loop above does.
                # Counted across the whole response rather than per chunk: the
                # service picks the chunk boundaries, so a counter reset at one
                # is a budget it can spend without limit by sending many small
                # chunks instead of one large one.
                await asyncio.sleep(0)
            if line:
                yield line
        if start:
            del buffer[:start]
        _check_frame_size(len(buffer))
    if buffer:
        _check_frame_size(len(buffer))
        line = _decode_frame_line(bytes(buffer))
        budget.line(blank=not line)
        if line:
            yield line


def _decode_frame_line(raw_line: bytes) -> str:
    try:
        return raw_line.decode("utf-8").strip()
    except UnicodeDecodeError as err:
        raise SandboxProtocolError(f"a frame was not valid UTF-8: {err}") from err


class RemoteSandbox:
    """``Sandbox`` over the protocol in the module docstring."""

    def __init__(
        self,
        *,
        base_url: str,
        transport: RemoteTransport,
        scope: Scope,
        credential: CredentialSource | None = None,
        default_limits: SandboxLimits | None = None,
        connect_timeout: float = 20.0,
        allow_insecure_http: bool = False,
    ) -> None:
        """Build an adapter. Nothing is contacted until a method is called.

        Args:
            base_url: where the service lives. No trailing slash needed.
            transport: the egress seam every request goes through.
            scope: the Scope the egress policy is asked about. A sandbox
                service is deployment infrastructure, so this is normally a
                deployment-level Scope rather than a tenant's; a consumer
                who runs one service per tenant builds one adapter per
                profile.
            credential: the bearer token source. Never a Spec field.
            default_limits: used when ``run()`` is not given ``limits``.
            connect_timeout: how long to wait for the service to start
                streaming.
            allow_insecure_http: permit a plaintext ``http://`` base URL
                that is not loopback. Refused by default when a credential
                is configured, because a bearer token sent in clear is a
                bearer token for anyone on the path between here and the
                service. Loopback needs no opt-in: a service on this host
                is reached without touching a network.

        Raises:
            SandboxSetupError: the base URL is not http(s), or it is
                plaintext http to somewhere other than loopback while a
                credential is configured and ``allow_insecure_http`` is not
                set.
        """
        if not base_url.startswith(("http://", "https://")):
            raise SandboxSetupError("RemoteSandbox base_url must be an http(s) URL")
        if (
            base_url.startswith("http://")
            and credential is not None
            and not allow_insecure_http
            and not _is_loopback(base_url)
        ):
            raise SandboxSetupError(
                f"RemoteSandbox refuses to send a credential to {base_url!r} over plaintext "
                "http. Use https, point at a loopback address for local development, or "
                "pass allow_insecure_http=True to say the network in between is trusted."
            )
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._scope = scope
        self._credential = credential
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._connect_timeout = connect_timeout
        self._described: SandboxDescription | None = None

    async def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/x-ndjson, application/json"}
        if self._credential is not None:
            token = await self._credential()
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

    async def _preflight(self, isolation: IsolationLevel, network: bool) -> str | None:
        """Why this service must not be sent this program, or ``None``.

        The remote equivalent of the local backends' two-phase handshake. A
        local child reports its own containment and is sent the program only
        if that is acceptable; here the service's description plays the same
        part, read once and reused, so a service that already says it cannot
        reach the requested level never receives the program at all.

        A description that cannot be read is not treated as consent: an
        execution that asked for a level does not proceed against a service
        this adapter could not question.
        """
        described = self._described
        if described is None:
            fresh = await self.describe()
            described = self._described
            if described is None:
                return (
                    "the sandbox service's own description could not be read, so the terms "
                    f"this execution required ({isolation.value!r}) could not be confirmed "
                    f"before sending it the program: {'; '.join(fresh.problems) or 'no detail'}"
                )
        advertised = described.isolation
        if advertised is None or not advertised.satisfies(isolation):
            offered = advertised.value if advertised is not None else "none"
            return (
                f"the sandbox service advertises {offered!r} isolation and this execution "
                f"required {isolation.value!r}. The program was not sent."
            )
        if network and not described.network_grant_supported:
            return (
                "this execution asked for network access and the sandbox service says it "
                "cannot grant it. The program was not sent."
            )
        return None

    async def describe(self) -> SandboxDescription:
        last: str | None = None
        for _attempt in range(_DESCRIBE_ATTEMPTS):
            try:
                response = await self._transport.request(
                    "GET",
                    f"{self._base}/v1/sandbox",
                    scope=self._scope,
                    headers=await self._headers(),
                    timeout=_DESCRIBE_TIMEOUT_SECONDS,
                )
            except Exception as err:  # transport-level only; the seam's own refusals included
                last = f"{type(err).__name__}: {err}"
                continue
            status = int(getattr(response, "status_code", 0))
            if status != 200:
                return _not_ready(f"the sandbox service answered {status} to GET /v1/sandbox")
            try:
                described = SandboxDescription.model_validate(response.json())
            except (ValidationError, ValueError) as err:
                return _not_ready(f"the sandbox service's description was not readable: {err}")
            self._described = described
            return described.model_copy(
                update={
                    "backend": f"remote:{described.backend}",
                    "notes": (
                        *described.notes,
                        "guarantees are the service's own report; nothing in this process "
                        "observed them",
                    ),
                }
            )
        return _not_ready(f"the sandbox service could not be reached: {last}")

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
        bound: dict[str, HostBinding] = dict(bindings or {})
        active_limits = limits or self._default_limits
        active_capture = capture or DEFAULT_CAPTURE
        started = time.monotonic()
        if cancel is not None and cancel.is_set():
            return _failure(
                "cancelled", "the execution was cancelled before it was sent", started
            ).model_copy(update={"cancelled": True})

        if isolation is not None:
            refusal = await self._preflight(isolation, network)
            if refusal is not None:
                return _failure("isolation_unavailable", refusal, started)

        execution_id = f"exec_{secrets.token_hex(12)}"
        body = {
            "execution_id": execution_id,
            "program": program,
            "bindings": sorted(bound),
            "limits": active_limits.model_dump(mode="json"),
            "network": network,
            "isolation": isolation.value if isolation is not None else None,
            "capture": active_capture.model_dump(mode="json"),
        }
        headers = await self._headers()
        session = _Session(self, execution_id, headers, bound)
        try:
            outcome = await asyncio.wait_for(
                session.execute(body, cancel),
                timeout=active_limits.wall_seconds + _WALL_GRACE_SECONDS + self._connect_timeout,
            )
        except TimeoutError:
            await session.cancel_remote()
            return _failure(
                "timeout",
                f"execution exceeded its {active_limits.wall_seconds}s wall-clock limit and "
                "the sandbox service did not report an end in time; it was told to cancel",
                started,
            ).model_copy(update={"limit_hit": "wall_seconds"})
        except asyncio.CancelledError:
            await session.cancel_remote()
            raise
        if outcome.cancelled:
            await session.cancel_remote()
            return _failure(
                "cancelled", "the execution was cancelled and the sandbox service was told", started
            ).model_copy(update={"cancelled": True})
        if outcome.error is not None:
            return _failure("provider_error", outcome.error, started)
        assert outcome.result is not None
        result = outcome.result.model_copy(
            update={"duration_seconds": max(outcome.result.duration_seconds, 0.0)}
        )
        return withhold_if_weaker(result, isolation, backend="remote")


class _Outcome:
    __slots__ = ("cancelled", "error", "result")

    def __init__(self) -> None:
        self.result: SandboxResult | None = None
        self.error: str | None = None
        self.cancelled = False


class _Session:
    """One execution's conversation with the service."""

    def __init__(
        self,
        owner: RemoteSandbox,
        execution_id: str,
        headers: Mapping[str, str],
        bindings: Mapping[str, HostBinding],
    ) -> None:
        self._owner = owner
        self._id = execution_id
        self._headers = dict(headers)
        self._bindings = bindings
        self._cancel_sent = False

    @property
    def _url(self) -> str:
        return f"{self._owner._base}/v1/executions"

    async def execute(self, body: Mapping[str, Any], cancel: asyncio.Event | None) -> _Outcome:
        outcome = _Outcome()
        reading = asyncio.ensure_future(self._read(body, outcome))
        waiters: list[asyncio.Future[Any]] = [reading]
        cancel_wait: asyncio.Task[bool] | None = None
        if cancel is not None:
            cancel_wait = asyncio.ensure_future(cancel.wait())
            waiters.append(cancel_wait)
        try:
            finished, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if reading in finished:
                reading.result()
            else:
                outcome.cancelled = True
        finally:
            if cancel_wait is not None:
                cancel_wait.cancel()
            if not reading.done():
                reading.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reading
        return outcome

    async def _read(self, body: Mapping[str, Any], outcome: _Outcome) -> None:
        transport = self._owner._transport
        scope = self._owner._scope
        try:
            async with transport.stream(
                "POST",
                self._url,
                scope=scope,
                headers={**self._headers, "content-type": "application/json"},
                json=dict(body),
                timeout=None,
            ) as response:
                status = int(getattr(response, "status_code", 0))
                if status != 200:
                    outcome.error = (
                        f"the sandbox service answered {status} instead of streaming the "
                        "execution; the program was not run"
                    )
                    return
                await self._consume(response, outcome)
        except SandboxProtocolError as err:
            outcome.error = (
                f"the sandbox service broke the protocol: {err}. Whether the program ran "
                "to completion is unknown."
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            outcome.error = (
                f"the connection to the sandbox service failed ({type(err).__name__}: {err}). "
                "Whether the program ran to completion is unknown."
            )

    async def _consume(self, response: StreamedResponse, outcome: _Outcome) -> None:
        expected_id = 0
        seen_ready = False
        async for line in _iter_frame_lines(response):
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as err:
                raise SandboxProtocolError(f"a line was not valid JSON: {err}") from err
            if not isinstance(frame, dict):
                raise SandboxProtocolError("a frame was not a JSON object")
            if not seen_ready:
                parse_ready_frame(frame)
                seen_ready = True
                continue
            if frame.get("type") == "error":
                message = frame.get("message")
                kind = frame.get("kind")
                outcome.error = (
                    f"{kind}: {message}" if isinstance(message, str) else "the service failed"
                )
                return
            if frame.get("type") == "done":
                outcome.result = _parse_result(frame.get("result"))
                return
            parsed = parse_call_or_done_frame(
                frame, expected_call_id=expected_id, known_bindings=self._bindings.keys()
            )
            if isinstance(parsed, CallFrame):
                expected_id += 1
                await self._answer(parsed)
        if outcome.result is None and outcome.error is None:
            raise SandboxProtocolError("the stream ended before a done frame")

    async def _answer(self, call: CallFrame) -> None:
        binding = self._bindings[call.name]
        try:
            value = await binding(call.arguments)
            json.dumps(value)
        except Exception as err:  # a binding's failure is data for the program
            reply = build_reply_frame(call.id, ok=False, message=str(err))
        else:
            reply = build_reply_frame(call.id, ok=True, value=value)
        response = await self._owner._transport.request(
            "POST",
            f"{self._url}/{self._id}/replies",
            scope=self._owner._scope,
            headers={**self._headers, "content-type": "application/json"},
            json=reply,
            timeout=30.0,
        )
        status = int(getattr(response, "status_code", 0))
        if status not in (200, 202, 204):
            raise SandboxProtocolError(f"the service answered {status} to a binding reply")

    async def cancel_remote(self) -> None:
        if self._cancel_sent:
            return
        self._cancel_sent = True
        with contextlib.suppress(Exception):
            await self._owner._transport.request(
                "POST",
                f"{self._url}/{self._id}/cancel",
                scope=self._owner._scope,
                headers=self._headers,
                timeout=10.0,
            )


def _parse_result(raw: Any) -> SandboxResult:
    if not isinstance(raw, dict):
        raise SandboxProtocolError("a done frame carried no result object")
    try:
        return SandboxResult.model_validate(raw)
    except ValidationError as err:
        raise SandboxProtocolError(f"a done frame's result was not readable: {err}") from err


def _failure(kind: str, message: str, started: float) -> SandboxResult:
    return SandboxResult(
        duration_seconds=time.monotonic() - started,
        failure=SandboxFailure(kind=kind, message=message),
    )


def _is_loopback(base_url: str) -> bool:
    """Whether this URL's host is this machine, reached without a network.

    Only the literal loopback spellings: a name that resolves to loopback
    today can resolve elsewhere tomorrow, and this decides whether a
    credential may travel in clear.
    """
    host = urlsplit(base_url).hostname
    if host is None:
        return False
    host = host.strip("[]").lower()
    if host in {"localhost", "::1"}:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _not_ready(problem: str) -> SandboxDescription:
    from psych_runtime.sandbox.port import SandboxGuarantees  # noqa: PLC0415 - avoids a cycle

    return SandboxDescription(
        backend="remote",
        platform="unknown",
        isolation=None,
        guarantees=SandboxGuarantees(),
        ready=False,
        problems=(problem,),
    )
