"""``OAuthClient``: the orchestrator that turns a 401/403 challenge into a
usable access token, keeps it fresh, and steps it up on demand.

## Isolation, the same way ``psych_runtime.tools.mcp`` does it

One ``OAuthClient`` is meant to be shared process-wide, across every tenant
and every MCP server, the same way ``psych_runtime.tools.mcp.McpPool`` is one pool
for everything. That only works if every piece of state this class keeps is
keyed by more than just "which resource" -- DESIGN.md §10.4 calls pooling by
URL alone the bug that ends the project, and the same mistake here would
mean tenant A's step-up silently handing tenant B's session a broader scope.
So every internal key below carries the ``Scope``'s ``(tenant, principal)``
pair explicitly, the same discipline ``McpPoolKey`` uses:

* Client *registration* (a ``client_id``, and a ``client_secret`` for a
  confidential client) is cached per ``(tenant, principal, issuer)``,
  because the MCP spec requires credentials to be bound to the issuer that
  issued them and never reused across issuers.
* A *session* (the current token, its refresh token if it has one, and the
  scope so far) is stored per ``(tenant, principal, resource)``, because an
  access token is audience-bound to one resource and a caller asking for a
  bearer token knows which resource it is calling, not which issuer serves
  it.

## Why the pool-key identity is not the token

``bearer_token`` returns a ``psych_runtime.tools.secrets.ResolvedCredential``, the
same type a plain static-secret ``SecretResolver`` returns, so
``psych_runtime.tools.mcp``'s pool key (``McpPoolKey.credential_identity``) can be
built from it unchanged. That identity is a hash of ``(tenant, principal,
issuer, resource, client_id)`` -- never the access token's bytes. A token
refresh or a step-up both replace the stored ``TokenSet`` in place without
changing this identity, on purpose: the grant is the same grant throughout,
only the credential's current value moved, and an MCP connection pooled on
this identity should keep reusing its pooled connection across a refresh
instead of reconnecting on every renewal.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from psych_runtime.core.scope import Scope
from psych_runtime.tools.oauth.challenge import BearerChallenge, find_bearer_challenge
from psych_runtime.tools.oauth.errors import (
    AuthorizationDenied,
    DiscoveryError,
    IssuerMismatch,
    NoActiveSession,
    OAuthError,
    PkceRequired,
    ReauthorizationRequired,
    StepUpExhausted,
    TokenRequestFailed,
    UnsupportedGrant,
)
from psych_runtime.tools.oauth.metadata import (
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    fetch_authorization_server_metadata,
    fetch_protected_resource_metadata,
)
from psych_runtime.tools.oauth.pkce import (
    canonicalize_resource_uri,
    generate_pkce_pair,
    generate_state,
    union_scopes,
)
from psych_runtime.tools.oauth.redirect import AuthorizationRedirectPort
from psych_runtime.tools.oauth.registration import (
    ClientIdentity,
    ClientIdentityConfig,
    resolve_client_identity,
)
from psych_runtime.tools.oauth.tokens import TokenSet
from psych_runtime.tools.oauth.transport import OAuthTransport
from psych_runtime.tools.secrets import ResolvedCredential

__all__ = ["GrantKind", "OAuthClient", "validate_authorization_response_issuer"]

GrantKind = Literal["authorization_code", "client_credentials"]


# ---------------------------------------------------------------------------
# RFC 9207 §2.4 issuer validation -- a pure function, the exact four-row table
# ---------------------------------------------------------------------------


def validate_authorization_response_issuer(
    *, iss_parameter_supported: bool, iss: str | None, expected_issuer: str
) -> None:
    """The exact four-row RFC 9207 §2.4 table the MCP authorization spec
    requires a client apply, before the authorization code -- or an error
    response's ``error``/``error_description``/``error_uri`` -- is acted on
    or displayed.

    Comparison is a plain Python ``!=`` on the raw strings: RFC 3986 §6.2.1
    simple string comparison, deliberately with no case folding, default-port
    elision, trailing-slash, or percent-encoding normalisation first. Doing
    any of that here would be exactly the normalisation the spec forbids.

    | ``iss_parameter_supported`` | ``iss`` | action                        |
    |---|---|---|
    | ``True``  | present | compare to ``expected_issuer``      |
    | ``True``  | absent  | reject                              |
    | ``False`` | present | compare to ``expected_issuer``      |
    | ``False`` | absent  | proceed                             |

    Raises:
        IssuerMismatch: row 2 (advertised support, no ``iss``), or either
            comparison row finding a mismatch.
    """
    if iss_parameter_supported and iss is None:
        raise IssuerMismatch(
            "authorization server advertises authorization_response_iss_parameter_supported "
            "but the authorization response carried no iss parameter"
        )
    if iss is not None and iss != expected_issuer:
        raise IssuerMismatch(
            "iss in the authorization response does not match the issuer recorded when "
            "the authorization request was built"
        )


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RegistrationKey:
    tenant: str
    principal: str | None
    issuer: str

    @classmethod
    def from_scope(cls, *, scope: Scope, issuer: str) -> _RegistrationKey:
        tenant, principal = scope.pool_key
        return cls(tenant=tenant, principal=principal, issuer=issuer)


@dataclass(frozen=True, slots=True)
class _SessionKey:
    tenant: str
    principal: str | None
    resource: str

    @classmethod
    def from_scope(cls, *, scope: Scope, resource: str) -> _SessionKey:
        tenant, principal = scope.pool_key
        return cls(tenant=tenant, principal=principal, resource=resource)


@dataclass(frozen=True, slots=True)
class _Session:
    resource: str
    issuer: str
    as_metadata: AuthorizationServerMetadata
    identity: ClientIdentity
    grant: GrantKind
    redirect_uri: str | None
    requested_scope: str | None
    step_up_attempts: int
    tokens: TokenSet


def _grant_identity(scope: Scope, *, issuer: str, resource: str, client_id: str) -> str:
    """A stable, non-secret identity for one (scope, issuer, resource,
    client) grant. Never derived from the access token's bytes -- see this
    module's docstring for why that matters across refresh and step-up."""
    digest = hashlib.sha256()
    for part in (scope.tenant, scope.principal or "", issuer, resource, client_id):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _grant_types_for(grant: GrantKind) -> tuple[str, ...]:
    if grant == "authorization_code":
        return ("authorization_code", "refresh_token")
    return ("client_credentials",)


def _select_scope(challenge: BearerChallenge, prm: ProtectedResourceMetadata) -> str | None:
    """The MCP authorization spec's scope selection priority: the
    challenge's ``scope`` first, else the Protected Resource Metadata's
    ``scopes_supported``, else no ``scope`` parameter at all."""
    if challenge.scope:
        return challenge.scope
    if prm.scopes_supported:
        return " ".join(prm.scopes_supported)
    return None


def _require_bearer_challenge(header: str) -> BearerChallenge:
    parsed = find_bearer_challenge(header)
    if parsed is None:
        raise DiscoveryError(f"WWW-Authenticate header has no Bearer challenge: {header!r}")
    return parsed


def _normalize_challenge(challenge: str | BearerChallenge) -> BearerChallenge:
    if isinstance(challenge, BearerChallenge):
        return challenge
    return _require_bearer_challenge(challenge)


def _build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    resource: str,
    state: str,
    scope: str | None,
) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
        "state": state,
    }
    if scope:
        params["scope"] = scope
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{urlencode(params)}"


class _TokenResponse(BaseModel):
    """The token endpoint's success response, limited to the fields this
    client reads. ``SecretStr`` on both token fields for the same reason
    ``TokenSet`` uses it: this model is one hop away from a debugger's repr
    of in-flight state."""

    model_config = ConfigDict(frozen=True)

    access_token: SecretStr
    token_type: str | None = None
    expires_in: float | None = None
    refresh_token: SecretStr | None = None
    scope: str | None = None


def _parse_token_error(response: httpx.Response) -> tuple[str, str | None]:
    try:
        payload = response.json()
    except ValueError:
        return ("http_error", f"token endpoint returned HTTP {response.status_code}")
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        description = payload.get("error_description")
        return (payload["error"], description if isinstance(description, str) else None)
    return ("http_error", f"token endpoint returned HTTP {response.status_code}")


class OAuthClient:
    """A process-wide OAuth 2.1 client for MCP servers.

    Construct one per process, the same as ``psych_runtime.tools.mcp.McpPool``, and
    share it across every tenant and every server; every store this class
    keeps is scope-isolated internally (see the module docstring).

    ``redirect`` is required to call ``start`` or ``step_up`` with
    ``grant="authorization_code"``; a consumer using ``client_credentials``
    exclusively may omit it.
    """

    def __init__(
        self,
        *,
        transport: OAuthTransport,
        redirect: AuthorizationRedirectPort | None = None,
        max_step_up_attempts: int = 3,
        refresh_safety_margin_seconds: float = 30.0,
    ) -> None:
        self._transport = transport
        self._redirect = redirect
        self._max_step_up_attempts = max_step_up_attempts
        self._refresh_safety_margin_seconds = refresh_safety_margin_seconds
        self._registrations: dict[_RegistrationKey, ClientIdentity] = {}
        self._sessions: dict[_SessionKey, _Session] = {}
        self._locks: dict[_SessionKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # -- the public flow --------------------------------------------------

    async def start(
        self,
        scope: Scope,
        *,
        resource: str,
        challenge: str | BearerChallenge,
        identity: ClientIdentityConfig,
        grant: GrantKind = "authorization_code",
        redirect_uri: str | None = None,
        require_pkce: bool = True,
    ) -> ResolvedCredential:
        """Run the full flow from a 401 challenge to a usable access token:
        Protected Resource Metadata, authorization server metadata, client
        registration, the grant itself, and (for ``authorization_code``) the
        RFC 9207 issuer check on the callback.

        ``challenge`` is either the raw ``WWW-Authenticate`` header value
        from the 401, or an already-parsed ``BearerChallenge``.

        Raises:
            UnsupportedGrant: ``require_pkce=False`` was passed for
                ``authorization_code``. OAuth 2.1 has no non-PKCE variant of
                this grant, so this is refused rather than honoured.
            DiscoveryError: the challenge has no ``Bearer`` scheme, or
                Protected Resource Metadata / authorization server metadata
                could not be fetched or validated.
            PkceRequired: the authorization server does not advertise
                ``S256`` PKCE support.
            ClientRegistrationError: no client identity mechanism applies.
            OAuthError: ``authorization_code`` was requested with no
                ``redirect_uri`` available, or this client was constructed
                with no ``AuthorizationRedirectPort``.
            IssuerMismatch: the RFC 9207 check on the callback failed.
            AuthorizationDenied: the authorization server declined, or the
                callback's ``state`` did not match.
            TokenRequestFailed: the token endpoint returned an error.
        """
        if grant == "authorization_code" and not require_pkce:
            raise UnsupportedGrant(
                "OAuth 2.1 requires PKCE for the authorization_code grant; there is no "
                "non-PKCE variant for this client to fall back to"
            )

        canonical_resource = canonicalize_resource_uri(resource)
        parsed_challenge = _normalize_challenge(challenge)

        prm = await fetch_protected_resource_metadata(
            self._transport,
            scope,
            resource=canonical_resource,
            resource_metadata_url=parsed_challenge.resource_metadata,
        )
        issuer, as_metadata = await self._discover_authorization_server(
            scope, prm.authorization_servers, canonical_resource
        )

        if as_metadata.token_endpoint is None:
            raise DiscoveryError(f"authorization server {issuer!r} metadata has no token_endpoint")

        resolved_redirect_uri = redirect_uri
        if grant == "authorization_code":
            self._require_pkce_support(as_metadata, issuer=issuer)
            if self._redirect is None:
                raise OAuthError(
                    "OAuthClient.start was called with grant='authorization_code' but this "
                    "client was constructed with no AuthorizationRedirectPort"
                )
            if resolved_redirect_uri is None:
                if not identity.redirect_uris:
                    raise OAuthError(
                        "the authorization_code grant needs a redirect_uri: pass one to "
                        "start(), or set ClientIdentityConfig.redirect_uris"
                    )
                resolved_redirect_uri = identity.redirect_uris[0]
            if as_metadata.authorization_endpoint is None:
                raise DiscoveryError(
                    f"authorization server {issuer!r} metadata has no authorization_endpoint"
                )

        client_identity = await self._resolve_identity(
            scope, issuer=issuer, as_metadata=as_metadata, config=identity, grant=grant
        )
        requested_scope = _select_scope(parsed_challenge, prm)

        if grant == "authorization_code":
            assert resolved_redirect_uri is not None
            tokens = await self._authorize_via_browser(
                scope,
                as_metadata=as_metadata,
                client=client_identity,
                redirect_uri=resolved_redirect_uri,
                resource=canonical_resource,
                requested_scope=requested_scope,
            )
        else:
            tokens = await self._client_credentials_token(
                scope,
                as_metadata=as_metadata,
                client=client_identity,
                resource=canonical_resource,
                requested_scope=requested_scope,
            )

        session = _Session(
            resource=canonical_resource,
            issuer=issuer,
            as_metadata=as_metadata,
            identity=client_identity,
            grant=grant,
            redirect_uri=resolved_redirect_uri,
            requested_scope=tokens.scope or requested_scope,
            step_up_attempts=0,
            tokens=tokens,
        )
        self._sessions[_SessionKey.from_scope(scope=scope, resource=canonical_resource)] = session
        return self._resolved(scope, session)

    async def step_up(
        self, scope: Scope, *, resource: str, challenge: str | BearerChallenge
    ) -> ResolvedCredential:
        """Re-authorize with the union of previously-requested and
        newly-challenged scopes, after a 403 ``insufficient_scope``, and
        retry-friendly: the caller retries its original request with the
        ``ResolvedCredential`` this returns.

        Reuses the session's already-resolved client identity and
        authorization server metadata from ``start`` -- a step-up is a
        broader request from the *same* client to the *same* issuer, not a
        new registration.

        Raises:
            NoActiveSession: no session exists yet for ``resource``; call
                ``start`` first.
            StepUpExhausted: this resource has already been stepped up
                ``max_step_up_attempts`` times.
        """
        canonical_resource = canonicalize_resource_uri(resource)
        parsed_challenge = _normalize_challenge(challenge)
        key = _SessionKey.from_scope(scope=scope, resource=canonical_resource)
        lock = await self._lock_for(key)
        async with lock:
            session = self._sessions.get(key)
            if session is None:
                raise NoActiveSession(canonical_resource)
            if session.step_up_attempts >= self._max_step_up_attempts:
                raise StepUpExhausted(canonical_resource, session.step_up_attempts)

            union_scope = union_scopes(session.requested_scope, parsed_challenge.scope)
            if session.grant == "authorization_code":
                assert session.redirect_uri is not None
                tokens = await self._authorize_via_browser(
                    scope,
                    as_metadata=session.as_metadata,
                    client=session.identity,
                    redirect_uri=session.redirect_uri,
                    resource=canonical_resource,
                    requested_scope=union_scope,
                )
            else:
                tokens = await self._client_credentials_token(
                    scope,
                    as_metadata=session.as_metadata,
                    client=session.identity,
                    resource=canonical_resource,
                    requested_scope=union_scope,
                )

            updated = replace(
                session,
                requested_scope=tokens.scope or union_scope,
                step_up_attempts=session.step_up_attempts + 1,
                tokens=tokens,
            )
            self._sessions[key] = updated
            return self._resolved(scope, updated)

    async def bearer_token(self, scope: Scope, *, resource: str) -> ResolvedCredential:
        """The current access token for a resource ``start`` (or
        ``step_up``) already authorized, refreshing first if it is expiring
        within the configured safety margin.

        Concurrent callers for the same ``(scope, resource)`` share one
        refresh: the second caller to acquire the per-key lock observes the
        token the first caller just installed and, finding it no longer
        expiring, skips its own refresh rather than requesting a second one.

        Raises:
            NoActiveSession: no session exists for ``resource``.
            ReauthorizationRequired: an authorization-code token cannot be
                refreshed, or the authorization server refused renewal. The
                session is discarded and the caller must start again.
        """
        canonical_resource = canonicalize_resource_uri(resource)
        key = _SessionKey.from_scope(scope=scope, resource=canonical_resource)
        lock = await self._lock_for(key)
        async with lock:
            session = self._sessions.get(key)
            if session is None:
                raise NoActiveSession(canonical_resource)

            now = time.monotonic()
            if session.tokens.is_expiring(
                now=now, safety_margin_seconds=self._refresh_safety_margin_seconds
            ):
                if session.grant != "client_credentials" and session.tokens.refresh_token is None:
                    self._sessions.pop(key, None)
                    raise ReauthorizationRequired(
                        canonical_resource,
                        "access token expired and no refresh_token was issued",
                    )
                try:
                    if session.grant == "client_credentials":
                        new_tokens = await self._client_credentials_token(
                            scope,
                            as_metadata=session.as_metadata,
                            client=session.identity,
                            resource=canonical_resource,
                            requested_scope=session.requested_scope,
                        )
                    else:
                        new_tokens = await self._refresh(scope, session)
                except TokenRequestFailed as err:
                    self._sessions.pop(key, None)
                    raise ReauthorizationRequired(canonical_resource, str(err)) from err
                session = replace(session, tokens=new_tokens)
                self._sessions[key] = session

            return self._resolved(scope, session)

    def evict(self, scope: Scope, *, resource: str) -> None:
        """Forget a stored session, e.g. after ``ReauthorizationRequired``
        was handled elsewhere, or a consumer-driven logout."""
        canonical_resource = canonicalize_resource_uri(resource)
        self._sessions.pop(_SessionKey.from_scope(scope=scope, resource=canonical_resource), None)

    # -- discovery + registration ------------------------------------------

    async def _discover_authorization_server(
        self, scope: Scope, advertised: Sequence[str], resource: str
    ) -> tuple[str, AuthorizationServerMetadata]:
        """Find an authorization server whose metadata validates, and say which.

        Tries every issuer in ``advertised`` (Protected Resource Metadata's
        ``authorization_servers``, in the order the resource listed them), then
        falls back to ``resource`` itself -- the MCP endpoint URL, which the
        MCP authorization spec already names as the issuer to assume when a
        resource publishes no metadata at all.

        The fallback exists because a resource can advertise an authorization
        server identifier whose own metadata document disagrees with it, which
        is a real and common deployment: one observed server answered every
        well-known path with a document declaring ``<base>/mcp`` while its
        resource metadata named ``<base>``. Only the identifier derived from
        the endpoint URL is self-consistent there, and clients that reach it
        connect while stricter ones cannot -- for no gain, since the endpoint
        URL is the very thing the caller asked to talk to.

        **This does not loosen the issuer check.** Each candidate is still
        validated by ``fetch_authorization_server_metadata``, which accepts a
        document only when its ``issuer`` is identical to the identifier used
        to build the URL it came from. Widening the set of *identifiers* tried
        is not the same as trusting a document that disagrees with one: an
        attacker who could serve metadata under the resource's own URL already
        controls the resource.

        Raises:
            DiscoveryError: no advertised issuer and not the resource URI
                yielded usable metadata. The error names every attempt, since
                a mismatch between what a resource advertises and what its
                authorization server declares is invisible otherwise.
        """
        candidates: list[str] = list(advertised)
        if resource not in candidates:
            candidates.append(resource)

        failures: list[str] = []
        for candidate in candidates:
            try:
                metadata = await fetch_authorization_server_metadata(
                    self._transport, scope, issuer=candidate
                )
            except DiscoveryError as err:
                failures.append(f"{candidate}: {err}")
                continue
            return candidate, metadata

        raise DiscoveryError(
            f"no usable authorization server for resource {resource!r}. " + " | ".join(failures)
        )

    def _require_pkce_support(
        self, as_metadata: AuthorizationServerMetadata, *, issuer: str
    ) -> None:
        methods = as_metadata.code_challenge_methods_supported
        if not methods or "S256" not in methods:
            raise PkceRequired(
                f"authorization server {issuer!r} does not advertise S256 PKCE support "
                "(code_challenge_methods_supported); MCP clients MUST refuse to proceed"
            )

    async def _resolve_identity(
        self,
        scope: Scope,
        *,
        issuer: str,
        as_metadata: AuthorizationServerMetadata,
        config: ClientIdentityConfig,
        grant: GrantKind,
    ) -> ClientIdentity:
        key = _RegistrationKey.from_scope(scope=scope, issuer=issuer)
        # Only a Dynamic Client Registration result is worth caching: it is
        # the one mechanism with a real network side effect (and a server
        # that hands out a fresh client_id on every call would otherwise get
        # one per resource instead of one per issuer). Pre-registered and
        # CIMD client ids are free to recompute and always reflect whatever
        # config the caller passed for *this* resource.
        if config.preregistered_client_id is None and config.cimd_url is None:
            cached = self._registrations.get(key)
            if cached is not None:
                return cached
        result = await resolve_client_identity(
            self._transport,
            scope,
            as_metadata=as_metadata,
            config=config,
            grant_types=_grant_types_for(grant),
        )
        if result.via == "dcr":
            self._registrations[key] = result
        return result

    # -- grants -------------------------------------------------------------

    async def _authorize_via_browser(
        self,
        scope: Scope,
        *,
        as_metadata: AuthorizationServerMetadata,
        client: ClientIdentity,
        redirect_uri: str,
        resource: str,
        requested_scope: str | None,
    ) -> TokenSet:
        assert self._redirect is not None
        assert as_metadata.authorization_endpoint is not None
        assert as_metadata.token_endpoint is not None

        pkce = generate_pkce_pair()
        state = generate_state()
        authorization_url = _build_authorization_url(
            authorization_endpoint=as_metadata.authorization_endpoint,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=pkce.challenge,
            resource=resource,
            state=state,
            scope=requested_scope,
        )

        callback = await self._redirect.authorize(
            scope, authorization_url=authorization_url, state=state
        )

        # RFC 9207 §2.4 first, and strictly before anything below looks at
        # callback.error: a mismatch here means we cannot trust the response
        # came from the authorization server we recorded, and that includes
        # not trusting its error fields.
        validate_authorization_response_issuer(
            iss_parameter_supported=as_metadata.authorization_response_iss_parameter_supported,
            iss=callback.iss,
            expected_issuer=as_metadata.issuer,
        )

        if callback.error is not None:
            suffix = f": {callback.error_description}" if callback.error_description else ""
            raise AuthorizationDenied(f"authorization server declined: {callback.error}{suffix}")
        if callback.state != state:
            raise AuthorizationDenied("authorization callback state did not match the request")
        if not callback.code:
            raise AuthorizationDenied("authorization callback carried no code")

        params = {
            "grant_type": "authorization_code",
            "code": callback.code,
            "redirect_uri": redirect_uri,
            "code_verifier": pkce.verifier,
            "resource": resource,
        }
        return await self._token_endpoint_request(
            scope,
            token_endpoint=as_metadata.token_endpoint,
            client_id=client.client_id,
            client_secret=client.client_secret,
            params=params,
        )

    async def _client_credentials_token(
        self,
        scope: Scope,
        *,
        as_metadata: AuthorizationServerMetadata,
        client: ClientIdentity,
        resource: str,
        requested_scope: str | None,
    ) -> TokenSet:
        assert as_metadata.token_endpoint is not None
        params: dict[str, str] = {"grant_type": "client_credentials", "resource": resource}
        if requested_scope:
            params["scope"] = requested_scope
        return await self._token_endpoint_request(
            scope,
            token_endpoint=as_metadata.token_endpoint,
            client_id=client.client_id,
            client_secret=client.client_secret,
            params=params,
        )

    async def _refresh(self, scope: Scope, session: _Session) -> TokenSet:
        assert session.tokens.refresh_token is not None
        assert session.as_metadata.token_endpoint is not None
        params = {
            "grant_type": "refresh_token",
            "refresh_token": session.tokens.refresh_token.get_secret_value(),
            "resource": session.resource,
        }
        return await self._token_endpoint_request(
            scope,
            token_endpoint=session.as_metadata.token_endpoint,
            client_id=session.identity.client_id,
            client_secret=session.identity.client_secret,
            params=params,
        )

    async def _token_endpoint_request(
        self,
        scope: Scope,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: SecretStr | None,
        params: Mapping[str, str],
    ) -> TokenSet:
        body = dict(params)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if client_secret is not None:
            # client_secret_basic (RFC 6749 §2.3.1): the secret goes in the
            # Authorization header, never in the form body alongside
            # everything else that might end up in an access log.
            credentials = f"{client_id}:{client_secret.get_secret_value()}".encode()
            headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode('ascii')}"
        else:
            body["client_id"] = client_id

        try:
            response = await self._transport.request(
                "POST",
                token_endpoint,
                scope=scope,
                headers=headers,
                content=urlencode(body).encode(),
            )
        except httpx.HTTPError as err:
            raise TokenRequestFailed("transport_error", str(err)) from err

        if response.status_code != 200:
            error, description = _parse_token_error(response)
            raise TokenRequestFailed(error, description)

        try:
            payload = response.json()
        except ValueError as err:
            raise TokenRequestFailed(
                "invalid_response", "token endpoint response was not valid JSON"
            ) from err
        try:
            parsed = _TokenResponse.model_validate(payload)
        except ValidationError as err:
            raise TokenRequestFailed(
                "invalid_response", "token endpoint response has no usable access_token"
            ) from err

        return TokenSet(
            access_token=parsed.access_token,
            token_type=parsed.token_type or "Bearer",
            refresh_token=parsed.refresh_token,
            scope=parsed.scope,
            obtained_at=time.monotonic(),
            expires_in=parsed.expires_in,
        )

    # -- shared -------------------------------------------------------------

    def _resolved(self, scope: Scope, session: _Session) -> ResolvedCredential:
        identity = _grant_identity(
            scope,
            issuer=session.issuer,
            resource=session.resource,
            client_id=session.identity.client_id,
        )
        return ResolvedCredential(identity=identity, secret=session.tokens.access_token)

    async def _lock_for(self, key: _SessionKey) -> asyncio.Lock:
        # A lock per (tenant, principal, resource), not one global lock, so
        # refreshing server A for tenant X never blocks refreshing server B
        # for tenant Y. The guard lock is held only long enough to look up
        # or create the per-key lock, never across a refresh or a grant.
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock
