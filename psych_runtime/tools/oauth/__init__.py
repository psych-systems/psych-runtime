"""OAuth 2.1 authorization for MCP clients.

DESIGN.md §10.4's isolation discipline extended to OAuth: never key a
registration, a session, or a pool identity by less than
``(tenant, principal, issuer/resource)``, the same reasoning that keeps
``psych_runtime.tools.mcp`` from pooling MCP connections by URL alone. See
``psych_runtime.tools.oauth.client`` for the detail.

## What this package does not do

It does not run a browser, and it does not listen for the authorization
code callback: Psych is a library and owns no HTTP server (DESIGN.md §1).
``AuthorizationRedirectPort`` is the seam a consumer implements for that
step; ``InMemoryAuthorizationRedirect`` is a test-only stand-in that
performs the browser's one HTTP hop itself, over a real loopback socket.

## The call surface ``psych_runtime.tools.mcp`` uses

``OAuthClient`` is the only class that module needs to construct or hold:

* ``start(scope, resource=..., challenge=..., identity=..., grant=...)`` on a
  401, returning a ``ResolvedCredential`` shaped exactly like
  ``psych_runtime.tools.secrets.SecretResolver.resolve``'s.
* ``step_up(scope, resource=..., challenge=...)`` on a 403
  ``insufficient_scope``, returning the same shape.
* ``bearer_token(scope, resource=...)`` for every subsequent request on an
  already-authorized connection -- refreshes transparently, and concurrent
  callers for the same key share one refresh.
* ``evict(scope, resource=...)`` to forget a session outside the normal
  expiry path.

All four take a plain ``resource`` string (the MCP server's URL) and
canonicalise it internally; none of them need the caller to track an issuer.
"""

from __future__ import annotations

from psych_runtime.tools.oauth.challenge import (
    BearerChallenge,
    find_bearer_challenge,
    parse_www_authenticate,
)
from psych_runtime.tools.oauth.client import (
    GrantKind,
    OAuthClient,
    validate_authorization_response_issuer,
)
from psych_runtime.tools.oauth.errors import (
    AuthorizationDenied,
    ClientRegistrationError,
    DiscoveryError,
    InvalidCanonicalUri,
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
    authorization_server_metadata_urls,
    fetch_authorization_server_metadata,
    fetch_protected_resource_metadata,
    protected_resource_metadata_urls,
)
from psych_runtime.tools.oauth.pkce import (
    PkcePair,
    canonicalize_resource_uri,
    generate_pkce_pair,
    generate_state,
    s256_challenge,
    union_scopes,
)
from psych_runtime.tools.oauth.redirect import (
    AuthorizationCallback,
    AuthorizationRedirectPort,
    InMemoryAuthorizationRedirect,
)
from psych_runtime.tools.oauth.registration import (
    ClientIdentity,
    ClientIdentityConfig,
    resolve_client_identity,
)
from psych_runtime.tools.oauth.tokens import TokenSet
from psych_runtime.tools.oauth.transport import OAuthTransport

__all__ = [
    "AuthorizationCallback",
    "AuthorizationDenied",
    "AuthorizationRedirectPort",
    "AuthorizationServerMetadata",
    "BearerChallenge",
    "ClientIdentity",
    "ClientIdentityConfig",
    "ClientRegistrationError",
    "DiscoveryError",
    "GrantKind",
    "InMemoryAuthorizationRedirect",
    "InvalidCanonicalUri",
    "IssuerMismatch",
    "NoActiveSession",
    "OAuthClient",
    "OAuthError",
    "OAuthTransport",
    "PkcePair",
    "PkceRequired",
    "ProtectedResourceMetadata",
    "ReauthorizationRequired",
    "StepUpExhausted",
    "TokenRequestFailed",
    "TokenSet",
    "UnsupportedGrant",
    "authorization_server_metadata_urls",
    "canonicalize_resource_uri",
    "fetch_authorization_server_metadata",
    "fetch_protected_resource_metadata",
    "find_bearer_challenge",
    "generate_pkce_pair",
    "generate_state",
    "parse_www_authenticate",
    "protected_resource_metadata_urls",
    "resolve_client_identity",
    "s256_challenge",
    "union_scopes",
    "validate_authorization_response_issuer",
]
