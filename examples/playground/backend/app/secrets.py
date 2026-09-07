"""A ``SecretResolver`` that answers per account.

``psych_runtime.tools.secrets.InMemorySecretResolver`` is documented as a test double,
keyed per ``(tenant, principal, name)``: every value is ``.set()`` against one
specific ``Scope``, which fits a test asserting tenant isolation.

This one is the real (if deliberately simple) ``SecretResolver`` the README
points a person's MCP client secret at. It reads whatever
``app.settings_store`` holds for the tenant asking, so the name ``eq-secret``
resolves to one account's value for that account and to another's for the
other, and to nothing for an account that has stored neither.

## Why the Scope is not optional here

An earlier version answered the same name for every Scope, on the reasoning
that a local tool serves one operator. That reasoning stopped being true the
moment two people could sign in: the pool keys connections by
``(scope, server, credential identity)``, so a resolver that ignores the Scope
hands tenant B's connection tenant A's OAuth client secret and the pool cannot
tell, because as far as it can see the credential really is the same one.

DESIGN.md calls pooling by URL alone "the bug that ends the project" for
exactly this reason. Resolving by name alone is the same bug one layer down.

## Reads through, never caches

``SecretResolver``'s contract is "resolve fresh, never cache forever", and the
MCP pool re-resolves on every connect. Holding a snapshot here would mean a
secret edited in the console reached the next connection only after a restart,
so this asks the settings store every time. That is a lock and a small JSON
read per connect, not per turn.
"""

from __future__ import annotations

from pydantic import SecretStr

from app.settings_store import SettingsStore
from psych_runtime.core.scope import Scope
from psych_runtime.tools.secrets import ResolvedCredential, stable_credential_identity


class AccountSecretResolver:
    """Resolves a credential name against the asking account's own secrets."""

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def resolve(self, scope: Scope, name: str) -> ResolvedCredential | None:
        workspace = await self._settings.load(scope.tenant)
        value = workspace.secrets.get(name)
        if value is None:
            return None
        return ResolvedCredential(
            identity=stable_credential_identity(scope, name, value),
            secret=SecretStr(value),
        )
