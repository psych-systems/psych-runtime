"""Client registration: turning a Spec-level configuration into a
``client_id`` (and, for a confidential client, a ``client_secret``), by one
of the three mechanisms MCP's client-registration page defines.

## Priority order

The client-registration spec page states the normative order explicitly,
and it is not the order the Authorization overview page lists the three
mechanisms in (Client ID Metadata Documents, pre-registration, Dynamic
Client Registration): the page with the actual "Clients supporting all
options SHOULD use the following priority order" list puts pre-registration
first:

1. Pre-registered client information, if the consumer configured it.
2. Client ID Metadata Documents, if the authorization server advertises
   ``client_id_metadata_document_supported``.
3. Dynamic Client Registration, as a fallback, if the authorization server
   has a ``registration_endpoint``.
4. Prompt the user for client information -- not applicable here. Psych is a
   library with no UI (DESIGN.md §1); a consumer that wants this reachable
   configures ``ClientIdentityConfig`` themselves before calling in, which is
   the library equivalent of "the user provides it."

This module follows that order. A consumer wanting CIMD tried ahead of a
pre-registered id for a particular server configures only ``cimd_url`` and
leaves ``preregistered_client_id`` unset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from psych_runtime.core.scope import Scope
from psych_runtime.tools.oauth.errors import ClientRegistrationError
from psych_runtime.tools.oauth.metadata import AuthorizationServerMetadata
from psych_runtime.tools.oauth.transport import OAuthTransport

__all__ = [
    "ClientIdentity",
    "ClientIdentityConfig",
    "resolve_client_identity",
]

_ApplicationType = Literal["native", "web"]


class ClientIdentityConfig(BaseModel):
    """How a consumer wants a client id obtained for one authorization
    server integration. Every field is optional; ``resolve_client_identity``
    applies the priority order above to whichever fields are set.
    """

    model_config = ConfigDict(frozen=True)

    preregistered_client_id: str | None = None
    preregistered_client_secret: SecretStr | None = None
    cimd_url: str | None = None
    """An HTTPS URL, controlled by the consumer, hosting this client's own
    Client ID Metadata Document. This package never hosts that document --
    Psych owns no HTTP server (DESIGN.md §1) -- it only ever uses the URL as
    a ``client_id`` value."""
    allow_dynamic_registration: bool = True
    application_type: _ApplicationType = "native"
    """MCP clients MUST specify an appropriate application_type during
    Dynamic Client Registration; omitting it defaults to "web" under OIDC,
    which conflicts with native-style redirect URIs. "native" is correct for
    an embedded library talking to a loopback or app-scheme redirect; a
    consumer building a server-side web app in front of Psych sets "web"."""
    client_name: str = "psych"
    redirect_uris: tuple[str, ...] = ()
    """Required (non-empty) for the authorization code grant; ignored for
    client credentials, which has no redirect."""


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """A resolved client id, and how it was obtained."""

    client_id: str
    client_secret: SecretStr | None
    via: Literal["pre-registered", "cimd", "dcr"]


async def resolve_client_identity(
    transport: OAuthTransport,
    scope: Scope,
    *,
    as_metadata: AuthorizationServerMetadata,
    config: ClientIdentityConfig,
    grant_types: tuple[str, ...],
) -> ClientIdentity:
    """Obtain a client id for ``as_metadata.issuer``, per the priority order
    documented on this module.

    Raises:
        ClientRegistrationError: none of the three mechanisms apply, or
            Dynamic Client Registration was attempted and refused.
    """
    if config.preregistered_client_id is not None:
        return ClientIdentity(
            client_id=config.preregistered_client_id,
            client_secret=config.preregistered_client_secret,
            via="pre-registered",
        )

    if config.cimd_url is not None and as_metadata.client_id_metadata_document_supported:
        return ClientIdentity(client_id=config.cimd_url, client_secret=None, via="cimd")

    if config.allow_dynamic_registration and as_metadata.registration_endpoint:
        return await _register_dynamically(
            transport,
            scope,
            registration_endpoint=as_metadata.registration_endpoint,
            config=config,
            grant_types=grant_types,
        )

    raise ClientRegistrationError(
        f"no client identity mechanism available for issuer {as_metadata.issuer!r}: "
        "no preregistered_client_id was configured, the authorization server does "
        "not advertise client_id_metadata_document_supported (or no cimd_url was "
        "configured), and Dynamic Client Registration is disabled or the server "
        "has no registration_endpoint"
    )


class _DynamicClientRegistrationResponse(BaseModel):
    """RFC 7591 §3.2.1's response fields, limited to what this client reads."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    client_secret: SecretStr | None = None


async def _register_dynamically(
    transport: OAuthTransport,
    scope: Scope,
    *,
    registration_endpoint: str,
    config: ClientIdentityConfig,
    grant_types: tuple[str, ...],
) -> ClientIdentity:
    body: dict[str, object] = {
        "client_name": config.client_name,
        "application_type": config.application_type,
        "grant_types": list(grant_types),
        "token_endpoint_auth_method": "none",
    }
    if "authorization_code" in grant_types:
        if not config.redirect_uris:
            raise ClientRegistrationError(
                "Dynamic Client Registration for the authorization_code grant requires "
                "at least one entry in ClientIdentityConfig.redirect_uris"
            )
        body["redirect_uris"] = list(config.redirect_uris)
        body["response_types"] = ["code"]

    try:
        response = await transport.request(
            "POST",
            registration_endpoint,
            scope=scope,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=body,
        )
    except httpx.HTTPError as err:
        raise ClientRegistrationError(
            f"Dynamic Client Registration at {registration_endpoint!r} was unreachable: {err}"
        ) from err

    if response.status_code not in (200, 201):
        raise ClientRegistrationError(
            f"Dynamic Client Registration at {registration_endpoint!r} returned HTTP "
            f"{response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as err:
        raise ClientRegistrationError(
            f"Dynamic Client Registration at {registration_endpoint!r} returned a body "
            "that is not valid JSON"
        ) from err
    try:
        parsed = _DynamicClientRegistrationResponse.model_validate(payload)
    except ValidationError as err:
        raise ClientRegistrationError(
            f"Dynamic Client Registration at {registration_endpoint!r} returned a response "
            "with no usable client_id"
        ) from err
    return ClientIdentity(client_id=parsed.client_id, client_secret=parsed.client_secret, via="dcr")
