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
  ``isolation`` and ``capture``. The response is ``200`` with
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
- **The isolation the service reports** is compared with what was requested
  and the result withheld if weaker, the same rule every local backend
  follows, so a service that quietly downgraded is caught here.

## What the adapter never does

It never follows a redirect (the transport refuses them), never sends the
program anywhere but the configured base URL, never puts the credential in
a URL, and never logs a response body.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Protocol

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

CredentialSource = Callable[[], Awaitable[str | None]]
"""Where the bearer token comes from, asked on every call so a rotated or
revoked credential takes effect on the next execution. ``None`` means send
no ``Authorization`` header."""


class StreamedResponse(Protocol):
    """The part of an ``httpx.Response`` a streamed execution reads."""

    @property
    def status_code(self) -> int: ...

    def aiter_lines(self) -> AsyncIterator[str]: ...


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
        """
        if not base_url.startswith(("http://", "https://")):
            raise SandboxSetupError("RemoteSandbox base_url must be an http(s) URL")
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._scope = scope
        self._credential = credential
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._connect_timeout = connect_timeout

    async def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/x-ndjson, application/json"}
        if self._credential is not None:
            token = await self._credential()
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

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
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                continue
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
