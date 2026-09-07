"""The egress seam: the one path all outbound HTTP takes.

DESIGN.md §14. Every outbound call, from HTTP tools, MCP clients, sandboxes
with granted network access, and the model client, goes through this seam so
that a consumer-supplied egress policy covers every path, including paths
added later. A control that covers three of four routes is worse than none,
because someone will believe it covers the fourth.

This module owns exactly two things: the policy protocol a consumer
implements, and the transport every caller uses in place of constructing its
own ``httpx`` client. Nothing in Psych is allowed to hold a bare
``httpx.AsyncClient``; holding one is a way around the seam, so every adapter
takes an ``HttpTransport`` instead and the seam becomes structural rather than
a convention someone can forget.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol, Self, runtime_checkable

import httpx

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.scope import Scope

__all__ = ["DEFAULT_TIMEOUT", "AllowAll", "DenyAll", "EgressPolicy", "HttpTransport"]

DEFAULT_TIMEOUT: Final = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
"""What every request through this seam is bounded by unless a caller says
otherwise.

``httpx.AsyncClient(timeout=None)`` disables every timeout, and that is what
this transport used to be constructed with. A tenant-controlled MCP server (or
an OAuth metadata URL one points at) that accepts a connection and then says
nothing held the turn open until the Run's own deadline -- fifteen minutes by
default -- while the lease heartbeat kept the Run pinned to that Worker.

The read bound is deliberately not generous, because the one call that
legitimately stays silent for minutes is a model stream, and that path passes
its own ``timeout=`` with ``read=None`` and enforces silence itself through
``Limits.stream_idle_seconds`` (DESIGN.md §8.5).
"""


@runtime_checkable
class EgressPolicy(Protocol):
    """Decides whether one outbound request may proceed.

    Called once per request, with enough context for a real decision: which
    tenant is asking, and where the request is headed. The seam calls this
    before opening the connection, not after, so a denial never touches the
    network.

    An implementation should be fast and free of side effects: it runs on
    every outbound call Psych makes, including the read side of a streamed
    model response, so anything slow here is a latency tax on everything.
    """

    async def allow(self, scope: Scope, url: str) -> bool:
        """Return ``True`` if ``url`` may be reached on behalf of ``scope``."""
        ...


class AllowAll:
    """The default policy: every request proceeds.

    A consumer who has not thought about egress yet gets exactly what they
    have today, explicitly, rather than a control that only looks like it is
    there.
    """

    async def allow(self, scope: Scope, url: str) -> bool:  # noqa: ARG002
        return True


class DenyAll:
    """Refuses everything.

    For a test that must prove a code path never reaches the network: give it
    a transport built on ``DenyAll`` and watch it raise instead of connect.
    """

    async def allow(self, scope: Scope, url: str) -> bool:  # noqa: ARG002
        return False


class HttpTransport:
    """The one object that owns outbound HTTP in Psych.

    Wraps a single ``httpx.AsyncClient`` and checks every request against an
    ``EgressPolicy`` before it goes out. The model client, HTTP tool executor
    and MCP client all take one of these rather than building their own
    ``httpx`` client, which is what makes the seam a single seam instead of a
    convention repeated at each call site.

    Two methods only: ``request`` for a call that returns a complete response,
    and ``stream`` for one where the body is consumed incrementally, which the
    OpenAI-compatible model adapter needs for server-sent events. Both check
    the policy before doing anything else.
    """

    def __init__(
        self,
        *,
        policy: EgressPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | float | None = DEFAULT_TIMEOUT,
    ) -> None:
        self._policy: EgressPolicy = policy if policy is not None else AllowAll()
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        if client is not None and client.follow_redirects:
            # A redirect is a second request to a host the policy never saw. A
            # client that follows them turns one allowed URL into an arbitrary
            # one, which is the seam being routed around rather than used.
            raise ValueError(
                "HttpTransport was given an httpx client with follow_redirects=True. A "
                "redirect reaches a host the egress policy never evaluated, so the "
                "policy would cover the first hop and nothing after it."
            )
        # A caller-supplied client is theirs to close; one we built ourselves
        # is ours, so aclose() only tears down what we own.
        self._owns_client = client is None

    async def _check(
        self, method: str, url: str, scope: Scope, params: Mapping[str, str] | None = None
    ) -> None:
        """Ask the policy about the URL that will actually be requested.

        Including the query string: a policy matching on a full URL saw only
        the path when a caller passed ``params`` separately, so a rule written
        against, say, a redirect parameter matched nothing and let the request
        through.
        """
        effective = str(httpx.URL(url).copy_merge_params(params)) if params else url
        if not await self._policy.allow(scope, effective):
            raise AccessDenied(effective, f"egress policy refused {method} {effective} for {scope}")

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        """Make one request and return the complete response.

        Raises:
            AccessDenied: the policy refused ``url`` for ``scope``.
        """
        await self._check(method, url, scope, params)
        kwargs: dict[str, Any] = {"headers": headers, "json": json, "content": content}
        if params is not None:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._client.request(method, url, **kwargs)

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streamed response as an async context manager.

        The policy is checked before the connection opens, same as
        ``request``. The yielded response's body is not read yet; the caller
        drives it with ``aiter_bytes`` or ``aiter_lines``.

        Raises:
            AccessDenied: the policy refused ``url`` for ``scope``.
        """
        await self._check(method, url, scope, params)
        kwargs: dict[str, Any] = {"headers": headers, "json": json, "content": content}
        if params is not None:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = timeout
        async with self._client.stream(method, url, **kwargs) as response:
            yield response

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
