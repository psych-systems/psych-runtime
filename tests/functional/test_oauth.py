"""The OAuth 2.1 client against real local stub servers.

DESIGN.md §22: no test in the suite makes a real network call, and every
external call goes through an injectable seam. Both are satisfied the same
way ``tests/functional/test_mcp.py`` and ``tests/functional/test_openai_
compat.py`` satisfy them: two ``127.0.0.1`` servers this file starts and
stops itself, real bytes over real loopback sockets, nothing mocked.

``ResourceServerStub`` plays the MCP server (the OAuth *resource server*):
it answers ``/mcp`` with 401 or 403 challenges and serves its own Protected
Resource Metadata. ``AuthorizationServerStub`` plays the authorization
server: RFC 8414 and OpenID Connect Discovery metadata, Dynamic Client
Registration, the authorization endpoint (with a real PKCE check), and the
token endpoint (authorization_code, refresh_token, and client_credentials
grants). Scope enforcement between them is simple by design -- a shared
in-process dict from issued access token to the scope it carries -- because
what is under test is ``psych_runtime.tools.oauth.OAuthClient``'s behaviour, not a
real resource server's token introspection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport
from psych_runtime.tools.oauth import (
    ClientIdentityConfig,
    InMemoryAuthorizationRedirect,
    IssuerMismatch,
    NoActiveSession,
    OAuthClient,
    ReauthorizationRequired,
    StepUpExhausted,
    s256_challenge,
)

pytestmark = pytest.mark.functional

Handler = Callable[
    [str, str, Mapping[str, str], Mapping[str, str], bytes],
    Awaitable[tuple[int, dict[str, str], bytes]],
]

_REASON = {
    200: "OK",
    201: "Created",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
}


# ---------------------------------------------------------------------------
# Raw HTTP/1.1, shared by both stub servers
# ---------------------------------------------------------------------------


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
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
    target = parts[1] if len(parts) > 1 else "/"

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
    return method, target, headers, body


async def _write_response(
    writer: asyncio.StreamWriter, status: int, headers: dict[str, str], body: bytes
) -> None:
    all_headers = {"Connection": "close", "Content-Length": str(len(body))}
    all_headers.update(headers)
    lines = [f"HTTP/1.1 {status} {_REASON.get(status, 'OK')}"]
    lines.extend(f"{key}: {value}" for key, value in all_headers.items())
    lines.append("")
    lines.append("")
    writer.write("\r\n".join(lines).encode())
    writer.write(body)
    await writer.drain()


class _Server:
    """A ``127.0.0.1`` server whose ``handler`` answers every request."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> str:
        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                method, target, headers, body = await _read_request(reader)
                parsed = urlsplit(target)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                status, resp_headers, resp_body = await self._handler(
                    method, parsed.path, query, headers, body
                )
                await _write_response(writer, status, resp_headers, resp_body)
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


def _json(status: int, payload: object) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# The authorization server stub
# ---------------------------------------------------------------------------


@dataclass
class _AuthCode:
    code_challenge: str
    redirect_uri: str
    resource: str
    scope: str | None


class AuthorizationServerStub:
    """Plays the authorization server: discovery metadata (both RFC 8414 and
    OpenID Connect Discovery well-known documents, independently toggleable),
    Dynamic Client Registration, a real PKCE-checking authorization endpoint,
    and a token endpoint handling all three grants this client uses.
    """

    def __init__(
        self,
        *,
        expose_rfc8414: bool = True,
        expose_oidc: bool = True,
        advertise_iss_supported: bool = True,
        emit_iss: bool = True,
        wrong_iss: bool = False,
        expires_in: float = 3600.0,
        refresh_expires_in: float | None = None,
        fail_refresh: bool = False,
    ) -> None:
        self.expose_rfc8414 = expose_rfc8414
        self.expose_oidc = expose_oidc
        self.advertise_iss_supported = advertise_iss_supported
        self.emit_iss = emit_iss
        self.wrong_iss = wrong_iss
        self.expires_in = expires_in
        # Defaults to a value far longer than expires_in, deliberately: tests
        # that force an initial token to read as "expiring" via a large
        # OAuthClient safety margin need the *refreshed* token to clear that
        # same margin, or every refresh would immediately look expiring
        # again and a concurrency test could never observe "exactly one".
        self.refresh_expires_in = (
            refresh_expires_in if refresh_expires_in is not None else expires_in * 1_000_000.0
        )
        self.fail_refresh = fail_refresh

        self.base_url = ""
        self.registrations = 0
        self.registration_requests: list[dict[str, object]] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.issued_access_tokens: list[str] = []
        self.issued_refresh_tokens: list[str] = []
        self.token_scope_by_access: dict[str, str | None] = {}
        self._scope_by_refresh: dict[str, str | None] = {}
        self._codes: dict[str, _AuthCode] = {}
        self._next_code = 0
        self._next_token = 0
        self._server: _Server | None = None

    async def __aenter__(self) -> AuthorizationServerStub:
        self._server = _Server(self._handle)
        self.base_url = await self._server.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        await self._server.__aexit__(*exc_info)

    def refresh_grant_count(self) -> int:
        return sum(1 for req in self.token_requests if req.get("grant_type") == "refresh_token")

    def _metadata(self) -> dict[str, object]:
        return {
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            "registration_endpoint": f"{self.base_url}/register",
            "scopes_supported": ["mcp:read", "mcp:write"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
            "authorization_response_iss_parameter_supported": self.advertise_iss_supported,
        }

    async def _handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and path == "/.well-known/oauth-authorization-server":
            return _json(200, self._metadata()) if self.expose_rfc8414 else (404, {}, b"{}")
        if method == "GET" and path == "/.well-known/openid-configuration":
            return _json(200, self._metadata()) if self.expose_oidc else (404, {}, b"{}")
        if method == "POST" and path == "/register":
            return self._handle_register(body)
        if method == "GET" and path == "/authorize":
            return self._handle_authorize(query)
        if method == "POST" and path == "/token":
            return self._handle_token(body)
        return 404, {}, b"not found"

    def _handle_register(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        self.registrations += 1
        payload = json.loads(body)
        self.registration_requests.append(payload)
        client_id = f"dcr-client-{self.registrations}"
        return _json(201, {"client_id": client_id})

    def _handle_authorize(self, query: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        self.authorize_requests.append(dict(query))
        redirect_uri = query["redirect_uri"]
        self._next_code += 1
        code = f"code-{self._next_code}"
        self._codes[code] = _AuthCode(
            code_challenge=query["code_challenge"],
            redirect_uri=redirect_uri,
            resource=query.get("resource", ""),
            scope=query.get("scope"),
        )
        params: dict[str, str] = {"code": code}
        if "state" in query:
            params["state"] = query["state"]
        if self.emit_iss:
            params["iss"] = f"{self.base_url}-attacker" if self.wrong_iss else self.base_url
        separator = "&" if "?" in redirect_uri else "?"
        return 302, {"Location": f"{redirect_uri}{separator}{urlencode(params)}"}, b""

    def _handle_token(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        params = dict(parse_qsl(body.decode(), keep_blank_values=True))
        self.token_requests.append(params)
        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            return self._handle_authorization_code_grant(params)
        if grant_type == "refresh_token":
            return self._handle_refresh_grant(params)
        if grant_type == "client_credentials":
            return self._issue_token(params.get("scope"))
        return _json(400, {"error": "unsupported_grant_type"})

    def _handle_authorization_code_grant(
        self, params: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        record = self._codes.pop(params.get("code", ""), None)
        if record is None:
            return _json(400, {"error": "invalid_grant", "error_description": "unknown code"})
        if s256_challenge(params.get("code_verifier", "")) != record.code_challenge:
            return _json(400, {"error": "invalid_grant", "error_description": "pkce mismatch"})
        if params.get("redirect_uri") != record.redirect_uri:
            return _json(
                400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
            )
        return self._issue_token(record.scope)

    def _handle_refresh_grant(self, params: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if self.fail_refresh:
            return _json(400, {"error": "invalid_grant", "error_description": "refresh rejected"})
        scope = self._scope_by_refresh.get(params.get("refresh_token", ""))
        return self._issue_token(scope, expires_in=self.refresh_expires_in)

    def _issue_token(
        self, scope: str | None, *, expires_in: float | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        self._next_token += 1
        access_token = f"access-{self._next_token}"
        refresh_token = f"refresh-{self._next_token}"
        self.issued_access_tokens.append(access_token)
        self.issued_refresh_tokens.append(refresh_token)
        self.token_scope_by_access[access_token] = scope
        self._scope_by_refresh[refresh_token] = scope
        payload = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in if expires_in is not None else self.expires_in,
            "refresh_token": refresh_token,
            "scope": scope,
        }
        return _json(200, payload)


# ---------------------------------------------------------------------------
# The resource server stub (the "MCP server")
# ---------------------------------------------------------------------------


class ResourceServerStub:
    """Plays the MCP server: 401/403 challenges, and its own Protected
    Resource Metadata pointing at one authorization server."""

    def __init__(self, authorization_server: AuthorizationServerStub) -> None:
        self.authorization_server = authorization_server
        self.require_scope: str | None = None
        self.base_url = ""
        self.received_authorizations: list[str | None] = []
        self._server: _Server | None = None

    async def __aenter__(self) -> ResourceServerStub:
        self._server = _Server(self._handle)
        self.base_url = await self._server.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        await self._server.__aexit__(*exc_info)

    @property
    def resource(self) -> str:
        return f"{self.base_url}/mcp"

    def _resource_metadata_url(self) -> str:
        return f"{self.base_url}/.well-known/oauth-protected-resource"

    async def _handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, str],
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and path == "/.well-known/oauth-protected-resource":
            payload = {
                "resource": self.resource,
                "authorization_servers": [self.authorization_server.base_url],
                "scopes_supported": ["mcp:read", "mcp:write"],
            }
            return _json(200, payload)

        if path == "/mcp":
            auth = headers.get("authorization")
            self.received_authorizations.append(auth)
            token = auth[len("Bearer ") :] if auth and auth.startswith("Bearer ") else None
            if token is None or token not in self.authorization_server.token_scope_by_access:
                challenge = (
                    f'Bearer resource_metadata="{self._resource_metadata_url()}", scope="mcp:read"'
                )
                return 401, {"WWW-Authenticate": challenge}, b""
            scope = self.authorization_server.token_scope_by_access[token] or ""
            if self.require_scope and self.require_scope not in scope.split():
                challenge = (
                    f'Bearer error="insufficient_scope", scope="{self.require_scope}", '
                    f'resource_metadata="{self._resource_metadata_url()}"'
                )
                return 403, {"WWW-Authenticate": challenge}, b""
            return _json(200, {"ok": True})

        return 404, {}, b"not found"


# ---------------------------------------------------------------------------
# Shared test scaffolding
# ---------------------------------------------------------------------------

_REDIRECT_URIS = ("http://127.0.0.1:0/callback",)


async def _fetch_401_challenge(transport: HttpTransport, scope: Scope, resource: str) -> str:
    response = await transport.request("GET", resource, scope=scope)
    assert response.status_code == 401
    header = response.headers.get("www-authenticate")
    assert header is not None
    return str(header)


# ---------------------------------------------------------------------------
# The full flow
# ---------------------------------------------------------------------------


class TestFullAuthorizationCodeFlow:
    async def test_401_prm_as_metadata_register_authorize_token_retry(self) -> None:
        async with (
            AuthorizationServerStub() as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )

            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            credential = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            assert as_stub.registrations == 1
            assert as_stub.registration_requests[0]["application_type"] == "native"
            assert as_stub.authorize_requests[0]["code_challenge_method"] == "S256"
            assert as_stub.authorize_requests[0]["resource"] == rs_stub.resource

            retry = await transport.request(
                "GET",
                rs_stub.resource,
                scope=scope,
                headers={"Authorization": f"Bearer {credential.secret.get_secret_value()}"},
            )
            assert retry.status_code == 200
            assert retry.json() == {"ok": True}

    async def test_the_pkce_verifier_actually_has_to_match(self) -> None:
        """Not a client-side assertion: the stub AS itself recomputes
        S256(code_verifier) and rejects a mismatch, so a successful flow here
        is proof the client sent a verifier matching its own challenge."""
        async with (
            AuthorizationServerStub() as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )
            assert len(as_stub.issued_access_tokens) == 1


class TestClientCredentials:
    async def test_client_credentials_grant_obtains_a_usable_token(self) -> None:
        async with (
            AuthorizationServerStub() as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(transport=transport)
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            credential = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(),
                grant="client_credentials",
            )

            assert as_stub.token_requests[-1]["grant_type"] == "client_credentials"
            retry = await transport.request(
                "GET",
                rs_stub.resource,
                scope=scope,
                headers={"Authorization": f"Bearer {credential.secret.get_secret_value()}"},
            )
            assert retry.status_code == 200


class TestRefresh:
    async def test_an_expiring_token_is_refreshed_transparently(self) -> None:
        async with (
            AuthorizationServerStub(expires_in=3600.0) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            # A huge safety margin makes a freshly issued token "expiring"
            # immediately, without sleeping: a deterministic way to force
            # the refresh path on the very next call.
            oauth_client = OAuthClient(
                transport=transport,
                redirect=InMemoryAuthorizationRedirect(transport),
                refresh_safety_margin_seconds=1_000_000.0,
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            first = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            second = await oauth_client.bearer_token(scope, resource=rs_stub.resource)

            assert as_stub.refresh_grant_count() == 1
            assert second.secret.get_secret_value() != first.secret.get_secret_value()
            # The pool-key identity must survive a refresh unchanged: it is
            # derived from the grant, never from the access token's bytes.
            assert second.identity == first.identity

    async def test_refresh_failure_falls_back_to_reauthorization_not_a_loop(self) -> None:
        async with (
            AuthorizationServerStub(expires_in=3600.0, fail_refresh=True) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport,
                redirect=InMemoryAuthorizationRedirect(transport),
                refresh_safety_margin_seconds=1_000_000.0,
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            with pytest.raises(ReauthorizationRequired):
                await oauth_client.bearer_token(scope, resource=rs_stub.resource)

            # Exactly one refresh attempt was made -- no retry loop -- and
            # the session was discarded rather than retried.
            assert as_stub.refresh_grant_count() == 1
            with pytest.raises(NoActiveSession):
                await oauth_client.bearer_token(scope, resource=rs_stub.resource)


class TestConcurrentRefresh:
    async def test_two_concurrent_callers_produce_exactly_one_refresh(self) -> None:
        async with (
            AuthorizationServerStub(expires_in=3600.0) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport,
                redirect=InMemoryAuthorizationRedirect(transport),
                refresh_safety_margin_seconds=1_000_000.0,
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            first, second = await asyncio.gather(
                oauth_client.bearer_token(scope, resource=rs_stub.resource),
                oauth_client.bearer_token(scope, resource=rs_stub.resource),
            )

            assert as_stub.refresh_grant_count() == 1
            assert first.secret.get_secret_value() == second.secret.get_secret_value()
            assert first.identity == second.identity


class TestStepUp:
    async def test_step_up_requests_the_union_of_scopes_and_succeeds(self) -> None:
        async with (
            AuthorizationServerStub() as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            rs_stub.require_scope = "mcp:write"
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            first = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            retry = await transport.request(
                "GET",
                rs_stub.resource,
                scope=scope,
                headers={"Authorization": f"Bearer {first.secret.get_secret_value()}"},
            )
            assert retry.status_code == 403
            step_up_header = retry.headers.get("www-authenticate")
            assert step_up_header is not None
            assert "insufficient_scope" in step_up_header

            second = await oauth_client.step_up(
                scope, resource=rs_stub.resource, challenge=step_up_header
            )

            assert second.identity == first.identity
            granted_scope = as_stub.authorize_requests[-1].get("scope", "")
            assert "mcp:read" in granted_scope.split()
            assert "mcp:write" in granted_scope.split()

            retry_again = await transport.request(
                "GET",
                rs_stub.resource,
                scope=scope,
                headers={"Authorization": f"Bearer {second.secret.get_secret_value()}"},
            )
            assert retry_again.status_code == 200

    async def test_step_up_is_bounded_and_then_permanently_fails(self) -> None:
        async with (
            AuthorizationServerStub() as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            rs_stub.require_scope = "mcp:write"
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport,
                redirect=InMemoryAuthorizationRedirect(transport),
                max_step_up_attempts=1,
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )
            step_up_header = 'Bearer error="insufficient_scope", scope="mcp:write"'

            await oauth_client.step_up(scope, resource=rs_stub.resource, challenge=step_up_header)
            with pytest.raises(StepUpExhausted):
                await oauth_client.step_up(
                    scope, resource=rs_stub.resource, challenge=step_up_header
                )
            assert as_stub is not None  # as_stub retained for readability of the assertion above


class TestIssuerMismatch:
    async def test_a_wrong_iss_is_rejected_and_no_token_request_is_ever_made(self) -> None:
        async with (
            AuthorizationServerStub(
                advertise_iss_supported=True, emit_iss=True, wrong_iss=True
            ) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            with pytest.raises(IssuerMismatch):
                await oauth_client.start(
                    scope,
                    resource=rs_stub.resource,
                    challenge=challenge,
                    identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
                )

            assert as_stub.token_requests == []

    async def test_a_missing_iss_when_advertised_supported_is_rejected(self) -> None:
        async with (
            AuthorizationServerStub(advertise_iss_supported=True, emit_iss=False) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            with pytest.raises(IssuerMismatch):
                await oauth_client.start(
                    scope,
                    resource=rs_stub.resource,
                    challenge=challenge,
                    identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
                )
            assert as_stub.token_requests == []


class TestDiscoveryBothMechanisms:
    @pytest.mark.parametrize(
        ("expose_rfc8414", "expose_oidc"),
        [(True, False), (False, True)],
    )
    async def test_discovery_succeeds_via_either_mechanism(
        self, expose_rfc8414: bool, expose_oidc: bool
    ) -> None:
        async with (
            AuthorizationServerStub(
                expose_rfc8414=expose_rfc8414, expose_oidc=expose_oidc
            ) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(transport=transport)
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)

            credential = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(),
                grant="client_credentials",
            )
            assert credential.secret.get_secret_value() in as_stub.issued_access_tokens


class TestSecretsNeverLeak:
    async def test_no_token_value_ever_appears_in_an_exception_or_a_credential_repr(self) -> None:
        async with (
            AuthorizationServerStub(expires_in=3600.0, fail_refresh=True) as as_stub,
            ResourceServerStub(as_stub) as rs_stub,
            HttpTransport() as transport,
        ):
            scope = Scope(tenant="acme")
            oauth_client = OAuthClient(
                transport=transport,
                redirect=InMemoryAuthorizationRedirect(transport),
                refresh_safety_margin_seconds=1_000_000.0,
            )
            challenge = await _fetch_401_challenge(transport, scope, rs_stub.resource)
            credential = await oauth_client.start(
                scope,
                resource=rs_stub.resource,
                challenge=challenge,
                identity=ClientIdentityConfig(redirect_uris=_REDIRECT_URIS),
            )

            caught: Exception | None = None
            try:
                await oauth_client.bearer_token(scope, resource=rs_stub.resource)
            except ReauthorizationRequired as err:
                caught = err
            assert caught is not None

            leaked_surfaces = [repr(credential), str(credential), str(caught), repr(caught)]
            for token_value in [*as_stub.issued_access_tokens, *as_stub.issued_refresh_tokens]:
                for surface in leaked_surfaces:
                    assert token_value not in surface
