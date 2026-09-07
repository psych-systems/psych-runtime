"""Protected Resource Metadata (RFC 9728) and authorization server metadata
(RFC 8414 / OpenID Connect Discovery 1.0): the models, the well-known URL
construction rules, and the fetch-and-validate functions.

DESIGN.md §14: both fetches go through ``OAuthTransport``, never a bare
``httpx`` client.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from psych_runtime.core.scope import Scope
from psych_runtime.tools.oauth.errors import DiscoveryError
from psych_runtime.tools.oauth.transport import OAuthTransport

__all__ = [
    "AuthorizationServerMetadata",
    "ProtectedResourceMetadata",
    "fetch_authorization_server_metadata",
    "fetch_protected_resource_metadata",
    "protected_resource_metadata_urls",
]


class ProtectedResourceMetadata(BaseModel):
    """RFC 9728 §2's fields, limited to the ones this client acts on."""

    model_config = ConfigDict(frozen=True)

    resource: str
    authorization_servers: tuple[str, ...] = Field(default_factory=tuple)
    scopes_supported: tuple[str, ...] | None = None
    bearer_methods_supported: tuple[str, ...] | None = None


class AuthorizationServerMetadata(BaseModel):
    """RFC 8414 §2 / OpenID Connect Discovery 1.0 §3's fields, limited to the
    ones this client acts on. Both specs define supersets of this and agree
    on the fields named here, which is what lets one model serve both
    discovery mechanisms."""

    model_config = ConfigDict(frozen=True)

    issuer: str
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    scopes_supported: tuple[str, ...] | None = None
    response_types_supported: tuple[str, ...] = Field(default_factory=tuple)
    grant_types_supported: tuple[str, ...] | None = None
    code_challenge_methods_supported: tuple[str, ...] | None = None
    token_endpoint_auth_methods_supported: tuple[str, ...] | None = None
    authorization_response_iss_parameter_supported: bool = False
    client_id_metadata_document_supported: bool = False


def protected_resource_metadata_urls(resource: str) -> tuple[str, ...]:
    """The well-known URIs to try for Protected Resource Metadata, in the
    order the MCP authorization spec's discovery page requires: the
    path-aware URI first (when the resource has a path), then the root
    ``.well-known`` URI. Used only when a 401's ``WWW-Authenticate`` header
    carried no ``resource_metadata`` URL to fetch directly.
    """
    parsed = urlsplit(resource)
    root = urlunsplit(
        (parsed.scheme, parsed.netloc, "/.well-known/oauth-protected-resource", "", "")
    )
    path = parsed.path.rstrip("/")
    if not path:
        return (root,)
    path_aware = urlunsplit(
        (parsed.scheme, parsed.netloc, f"/.well-known/oauth-protected-resource{path}", "", "")
    )
    return (path_aware, root)


async def _get_json(transport: OAuthTransport, scope: Scope, url: str) -> tuple[int, object] | None:
    """``None`` means the endpoint was unreachable at the transport level
    (connection refused, DNS failure, and so on); a non-2xx or non-JSON
    response is returned so the caller can decide whether to try the next
    candidate URL or report exactly what went wrong."""
    try:
        response = await transport.request(
            "GET", url, scope=scope, headers={"Accept": "application/json"}
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return (response.status_code, None)
    try:
        return (200, response.json())
    except (json.JSONDecodeError, ValueError):
        return (200, None)


async def fetch_protected_resource_metadata(
    transport: OAuthTransport,
    scope: Scope,
    *,
    resource: str,
    resource_metadata_url: str | None,
) -> ProtectedResourceMetadata:
    """Fetch RFC 9728 Protected Resource Metadata.

    When the 401 that triggered discovery carried a ``resource_metadata`` URL
    in its ``WWW-Authenticate`` challenge, that exact URL is used, per the
    MCP spec's discovery priority: "use the resource metadata URL from the
    parsed WWW-Authenticate headers when present; otherwise fall back to
    constructing and requesting the well-known URIs". Only when it is absent
    do we fall back to ``protected_resource_metadata_urls``.

    Raises:
        DiscoveryError: no candidate URL returned a usable document, or the
            document has no ``authorization_servers``.
    """
    candidates = (
        (resource_metadata_url,)
        if resource_metadata_url
        else protected_resource_metadata_urls(resource)
    )
    attempted: list[str] = []
    for url in candidates:
        attempted.append(url)
        result = await _get_json(transport, scope, url)
        if result is None or result[1] is None:
            continue
        _, payload = result
        try:
            metadata = ProtectedResourceMetadata.model_validate(payload)
        except ValidationError:
            continue
        if not metadata.authorization_servers:
            raise DiscoveryError(
                f"Protected Resource Metadata at {url!r} lists no authorization_servers"
            )
        return metadata
    raise DiscoveryError(
        f"could not fetch Protected Resource Metadata for {resource!r}; tried {attempted}"
    )


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    """The well-known URIs to try for authorization server metadata, in the
    exact priority order the MCP authorization spec's discovery page
    requires.

    For an issuer with a path component: OAuth 2.0 Authorization Server
    Metadata with path *insertion*, then OpenID Connect Discovery with path
    insertion, then OpenID Connect Discovery with path *appending* (the
    ordering RFC 8414 §5 describes for interoperating with OIDC providers
    that pre-date path-aware metadata). For an issuer with no path: the two
    root well-known URIs, RFC 8414 before OpenID Connect Discovery.
    """
    parsed = urlsplit(issuer)
    path = parsed.path.rstrip("/")
    if not path:
        return (
            urlunsplit(
                (parsed.scheme, parsed.netloc, "/.well-known/oauth-authorization-server", "", "")
            ),
            urlunsplit((parsed.scheme, parsed.netloc, "/.well-known/openid-configuration", "", "")),
        )
    return (
        urlunsplit(
            (parsed.scheme, parsed.netloc, f"/.well-known/oauth-authorization-server{path}", "", "")
        ),
        urlunsplit(
            (parsed.scheme, parsed.netloc, f"/.well-known/openid-configuration{path}", "", "")
        ),
        urlunsplit(
            (parsed.scheme, parsed.netloc, f"{path}/.well-known/openid-configuration", "", "")
        ),
    )


async def fetch_authorization_server_metadata(
    transport: OAuthTransport, scope: Scope, *, issuer: str
) -> AuthorizationServerMetadata:
    """Fetch and validate authorization server metadata for ``issuer``,
    trying RFC 8414 and OpenID Connect Discovery in the spec's priority
    order.

    Every candidate document is validated per RFC 8414 §3.3 / OpenID Connect
    Discovery §4.3 before it is trusted: its ``issuer`` field must be
    identical to ``issuer``. A document claiming to speak for a different
    issuer (the attacker.example / honest.example example the spec itself
    gives) is never returned from here.

    A mismatch skips that candidate and tries the next rather than ending the
    search. These URLs are alternative locations for the *same* issuer, so
    one disagreeing says nothing about the others -- and servers exist that
    answer every well-known path with a single document, where the first URL
    tried disagrees and a later one matches exactly. Skipping costs nothing:
    the loop's only exit with a value is still the equality check, so nothing
    a mismatched document said is ever acted on.

    Raises:
        DiscoveryError: no candidate URL returned a document with a matching
            ``issuer``, or none was reachable at all. When documents *were*
            found and disagreed, the error names each one and the issuer it
            declared -- a resource advertising an authorization server whose
            metadata contradicts it is otherwise invisible.
    """
    attempted: list[str] = []
    mismatched: list[str] = []
    for url in authorization_server_metadata_urls(issuer):
        attempted.append(url)
        result = await _get_json(transport, scope, url)
        if result is None or result[1] is None:
            continue
        _, payload = result
        if not isinstance(payload, dict) or payload.get("issuer") != issuer:
            # Skipped, not fatal. The candidates above are alternatives for
            # the *same* issuer, and deployments exist that answer every
            # well-known path with one document -- so the first URL tried can
            # disagree while a later one matches exactly. Aborting the
            # sequence here made those unreachable while protecting nothing:
            # the security property is that a document whose `issuer` differs
            # is never *trusted*, and that still holds, since the only exit
            # from this loop with a value is the equality check below. What
            # was found is kept for the error, because "expected X, every
            # candidate said Y" is the sentence that identifies a
            # misconfigured server, and a bare "not found" does not.
            found = payload.get("issuer") if isinstance(payload, dict) else None
            mismatched.append(f"{url} declared issuer {found!r}")
            continue
        try:
            return AuthorizationServerMetadata.model_validate(payload)
        except ValidationError as err:
            raise DiscoveryError(f"authorization server metadata at {url!r} is malformed") from err
    if mismatched:
        raise DiscoveryError(
            f"no authorization server metadata declares issuer {issuer!r}: " + "; ".join(mismatched)
        )
    raise DiscoveryError(
        f"could not fetch authorization server metadata for issuer {issuer!r}; tried {attempted}"
    )
