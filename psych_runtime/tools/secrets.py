"""The SecretResolver port: a credential name in, a value out, keyed by Scope.

DESIGN.md §10.4. Psych never stores a raw credential. A Spec carries a
credential *name* (``HttpTool.credential``, ``McpServer.credential``); this
port is the only place that name becomes a value, resolved fresh for the
Scope asking rather than cached forever, so a rotated or revoked credential
takes effect on the next resolve rather than on the next restart.

## Why resolution returns an identity alongside the value

The MCP connection pool (``psych_runtime.tools.mcp``) keys its pool by
``(scope, server, credential)``, and the credential half of that key must be
the *actual resolved credential*, not a proxy for it such as the Spec's
credential name. Two different Scopes naming the same credential name can
resolve to different values (per-tenant secrets storage is the whole point),
and the same Scope naming the same credential name can resolve to a *new*
value after a rotation. A pool key built from the name alone would conflate
all of these; a pool key holding the secret itself would put a raw credential
in a dict key, in a repr taken during debugging, and potentially in a log.

``resolve`` therefore returns both: ``secret``, the value, wrapped in
Pydantic's ``SecretStr`` so it never appears in a default repr or str; and
``identity``, a stable one-way digest of the value that is safe everywhere the
value is not. ``stable_credential_identity`` computes that digest, and a real
``SecretResolver`` should call it rather than invent a weaker one: a truncated
value leaks part of the secret, and a version counter does not change on an
out-of-band rotation, which is exactly when the pool key must.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, SecretStr

from psych_runtime.core.errors import PsychError
from psych_runtime.core.scope import Scope

__all__ = [
    "CredentialNotFound",
    "InMemorySecretResolver",
    "OAuthBearerSession",
    "OAuthSecretResolver",
    "ResolvedCredential",
    "SecretResolver",
    "stable_credential_identity",
]


class ResolvedCredential(BaseModel):
    """One resolved credential: a secret value plus its non-secret identity.

    ``identity`` is safe wherever ``secret`` is not: as a pool key, in a log
    line, in a repr taken while debugging. Pydantic's ``SecretStr`` already
    keeps ``secret`` out of this model's default repr and str (it prints as
    ``SecretStr('**********')``), and the model is frozen so neither field can
    be swapped for an unmasked one after construction.
    """

    model_config = ConfigDict(frozen=True)

    identity: str
    secret: SecretStr


@runtime_checkable
class SecretResolver(Protocol):
    """Resolves a credential name to a value, for exactly the Scope asking.

    An implementation should resolve fresh, or from its own short-lived
    cache, rather than memoising forever: DESIGN.md §10.2's "fresh at every
    turn boundary" rule for tools applies here too, since a credential a
    consumer just revoked must stop working on the next resolve rather than
    the next process restart.
    """

    async def resolve(self, scope: Scope, name: str) -> ResolvedCredential | None:
        """Return the credential named ``name`` for ``scope``.

        Returns:
            The resolved credential, or ``None`` if ``scope`` has no value
            for ``name``. Not finding one is not itself an error: a Spec
            naming a credential nobody configured is a decision for the
            caller, which usually raises ``CredentialNotFound`` for a
            required credential and treats it as "server unreachable" for an
            optional one.
        """
        ...


class CredentialNotFound(PsychError):
    """A Spec named a credential that has no value for this Scope.

    Raised by a caller of ``SecretResolver.resolve`` (``psych_runtime.tools.mcp``
    among them) rather than by the port itself, so that whether a missing
    credential is fatal stays a decision the caller makes deliberately: Psych
    never falls back to treating "no credential configured" as "connect
    without one", which would be a much quieter failure than this.
    """

    def __init__(self, scope: Scope, name: str) -> None:
        self.name = name
        super().__init__(f"no credential named {name!r} resolved for {scope}")


def stable_credential_identity(scope: Scope, name: str, value: str) -> str:
    """A stable, non-secret identity for one resolved credential value.

    Changes whenever the value changes, so a rotated credential pools
    separately from the connection its predecessor was authenticating: never
    reusing a session against a new secret it was never issued for. Never
    reveals the value: this is a one-way digest, safe to use as a dict key, to
    log, or to put in a pool key's repr, none of which are safe for the value
    itself (DESIGN.md §10.4).

    Scope and credential name are folded into the digest alongside the value
    so that two different names, or two different tenants, that happen to
    share a raw secret value still get distinct identities: the isolation
    this exists for is about the resolved *credential*, not the bytes of the
    secret.
    """
    digest = hashlib.sha256()
    for part in (scope.tenant, scope.principal or "", name, value):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


class InMemorySecretResolver:
    """Test double for the ``SecretResolver`` port.

    Values live in process memory only and are never persisted. This is for
    tests and local development, never for anything holding a real
    credential: a production ``SecretResolver`` belongs in front of whatever
    secret store the consumer already runs.
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str | None, str], str] = {}

    def set(self, scope: Scope, name: str, value: str) -> None:
        """Set the value ``resolve`` returns for ``(scope, name)``.

        Overwriting silently is deliberate: that is what a rotation looks
        like from the resolver's side, and the next ``resolve`` call picking
        up a new ``identity`` for the same name is the behaviour under test.
        """
        self._values[(scope.tenant, scope.principal, name)] = value

    def clear(self, scope: Scope, name: str) -> None:
        """Remove a value, so the next ``resolve`` reports it as unset."""
        self._values.pop((scope.tenant, scope.principal, name), None)

    async def resolve(self, scope: Scope, name: str) -> ResolvedCredential | None:
        value = self._values.get((scope.tenant, scope.principal, name))
        if value is None:
            return None
        return ResolvedCredential(
            identity=stable_credential_identity(scope, name, value),
            secret=SecretStr(value),
        )


@runtime_checkable
class OAuthBearerSession(Protocol):
    """The one method ``OAuthSecretResolver`` needs from an OAuth session.

    Structurally, not nominally, typed against
    ``psych_runtime.tools.oauth.client.OAuthClient.bearer_token``: importing that
    class here would make this module depend on ``psych_runtime.tools.oauth``, which
    itself imports ``ResolvedCredential`` from this module to shape its
    return value, and a real class-to-class dependency in both directions is
    an import cycle in waiting even though nothing forbids it structurally
    today. Declaring the one method actually needed, the same way
    ``psych_runtime.tools.mcp`` declares ``McpTransport`` instead of importing
    ``psych_runtime.model.egress.HttpTransport``, breaks that dependency instead of
    managing it.
    """

    async def bearer_token(self, scope: Scope, *, resource: str) -> ResolvedCredential: ...


class OAuthSecretResolver:
    """Bridges an already-authorized OAuth session into the ``SecretResolver``
    port, so ``psych_runtime.tools.mcp`` (or any other ``SecretResolver`` consumer)
    reads a live, auto-refreshing OAuth access token through the same seam it
    already reads a static credential through.

    This resolver never runs the interactive half of OAuth: the 401
    challenge, discovery, and the browser round trip are
    ``psych_runtime.tools.oauth.client.OAuthClient.start``'s job, called once, out of
    band, by whatever flow a consumer uses to connect a tenant's MCP server
    (their own admin UI, typically) -- not by a Run in flight. ``resolve``
    only ever asks an already-established session for its current,
    refreshed-if-needed token, which is exactly the "resolve fresh, never
    cache forever" contract ``SecretResolver`` documents: a token nearing
    expiry is refreshed transparently, and a stored session's own per-key
    lock (inside the OAuth session, not here) makes two concurrent callers
    share one refresh rather than racing two.

    ``resolve`` never itself returns ``None``: whatever the OAuth session's
    ``bearer_token`` raises -- ``NoActiveSession`` for a resource nobody has
    authorized yet, ``ReauthorizationRequired`` for one whose refresh just
    failed -- propagates instead of being flattened into a plain "no value
    configured", because those two failures call for different responses
    from whoever is running the Run and collapsing them would hide which one
    happened. The ``ResolvedCredential | None`` return type is kept only for
    structural compliance with ``SecretResolver``.
    """

    def __init__(
        self,
        session: OAuthBearerSession,
        *,
        resource_for_name: Callable[[str], str] | None = None,
    ) -> None:
        self._session = session
        self._resource_for_name = resource_for_name if resource_for_name is not None else _identity

    async def resolve(self, scope: Scope, name: str) -> ResolvedCredential | None:
        resource = self._resource_for_name(name)
        return await self._session.bearer_token(scope, resource=resource)


def _identity(name: str) -> str:
    return name
