"""The authorization-code grant's browser step, as a port.

The authorization code grant needs a real user, a real browser, and a real
redirect back to something that can read the callback's query parameters.
Psych is a library and owns no HTTP server (DESIGN.md §1): it will never
listen for that redirect itself, the same reasoning that keeps it from
owning an HTTP server for anything else. ``AuthorizationRedirectPort`` is the
seam a consumer implements instead -- in their own web app, desktop shell, or
CLI, wherever they already have a way to open a URL for a user and receive a
callback.

``InMemoryAuthorizationRedirect`` below is not a fake in the sense of
skipping the network: it performs the actual browser step by making one real
HTTP request to the authorization endpoint and reading the redirect it gets
back, over a real loopback socket in tests. What it does not do is put a
human in front of a consent screen, which is the one part of this step a
library cannot simulate and a real consumer's implementation must provide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

import httpx

from psych_runtime.core.scope import Scope
from psych_runtime.tools.oauth.errors import AuthorizationDenied
from psych_runtime.tools.oauth.transport import OAuthTransport

__all__ = ["AuthorizationCallback", "AuthorizationRedirectPort", "InMemoryAuthorizationRedirect"]

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class AuthorizationCallback:
    """The query parameters a browser received on the redirect back from the
    authorization server, exactly as sent -- nothing here is validated yet.
    ``OAuthClient`` applies the RFC 9207 issuer check and the ``state``
    comparison; this type only carries what arrived.
    """

    code: str | None
    state: str | None
    iss: str | None
    error: str | None
    error_description: str | None


@runtime_checkable
class AuthorizationRedirectPort(Protocol):
    """Gets a user through an authorization URL and reports what came back.

    A real implementation opens ``authorization_url`` in the user's browser
    (or hands it to whatever UI layer the consumer's platform already has
    for this), runs the consumer's own OAuth callback endpoint, and once a
    request lands on it, returns that request's query parameters as an
    ``AuthorizationCallback``. ``state`` is the value the client generated
    for this request; a real implementation SHOULD use it to correlate the
    callback with the request that started it if more than one
    authorization can be in flight, but it is ``OAuthClient`` that performs
    the actual comparison against the value it sent.
    """

    async def authorize(
        self, scope: Scope, *, authorization_url: str, state: str
    ) -> AuthorizationCallback: ...


class InMemoryAuthorizationRedirect:
    """A same-process stand-in for a real browser and callback receiver.

    For tests and local development only. It performs the one HTTP hop a
    browser would make -- a GET to the authorization endpoint -- through the
    supplied ``OAuthTransport`` (so it still goes through the egress seam and
    still only ever reaches loopback in tests), reads the authorization
    server's redirect response without following it, and parses the
    ``Location`` header's query string exactly as a callback receiver would.
    It never renders a consent screen or makes a decision on the user's
    behalf: whatever the stub or real authorization server decides to put in
    that redirect is what comes back.
    """

    def __init__(self, transport: OAuthTransport) -> None:
        self._transport = transport

    async def authorize(
        self,
        scope: Scope,
        *,
        authorization_url: str,
        state: str,  # noqa: ARG002 - part of the port
    ) -> AuthorizationCallback:
        try:
            response = await self._transport.request(
                "GET", authorization_url, scope=scope, headers={"Accept": "text/html"}
            )
        except httpx.HTTPError as err:
            raise AuthorizationDenied(f"authorization endpoint was unreachable: {err}") from err
        if response.status_code not in _REDIRECT_STATUSES:
            raise AuthorizationDenied(
                f"authorization endpoint returned HTTP {response.status_code} instead of "
                "a redirect back to the client"
            )
        location = response.headers.get("location")
        if not location:
            raise AuthorizationDenied("authorization endpoint's redirect had no Location header")
        query = urlsplit(location).query
        params = dict(parse_qsl(query, keep_blank_values=True))
        return AuthorizationCallback(
            code=params.get("code"),
            state=params.get("state"),
            iss=params.get("iss"),
            error=params.get("error"),
            error_description=params.get("error_description"),
        )
