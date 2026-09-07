"""The SecretResolver port: identity derivation and secret-value hygiene.

DESIGN.md §10.4. Pure logic, no IO: this is what makes ``stable_credential_
identity`` and ``ResolvedCredential`` unit-testable rather than functional,
and what makes the pool key's safety provable without a server in the loop.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from psych_runtime.core.scope import Scope
from psych_runtime.tools.secrets import (
    InMemorySecretResolver,
    ResolvedCredential,
    stable_credential_identity,
)

pytestmark = pytest.mark.unit


class TestStableCredentialIdentity:
    def test_the_same_inputs_produce_the_same_identity(self) -> None:
        scope = Scope(tenant="acme", principal="alice")
        first = stable_credential_identity(scope, "api_key", "secret-value")
        second = stable_credential_identity(scope, "api_key", "secret-value")
        assert first == second

    def test_a_different_tenant_gets_a_different_identity(self) -> None:
        one = stable_credential_identity(Scope(tenant="acme"), "api_key", "secret-value")
        other = stable_credential_identity(Scope(tenant="globex"), "api_key", "secret-value")
        assert one != other

    def test_a_different_principal_gets_a_different_identity(self) -> None:
        scope_alice = Scope(tenant="acme", principal="alice")
        scope_bob = Scope(tenant="acme", principal="bob")
        one = stable_credential_identity(scope_alice, "api_key", "secret-value")
        other = stable_credential_identity(scope_bob, "api_key", "secret-value")
        assert one != other

    def test_a_different_credential_name_gets_a_different_identity(self) -> None:
        scope = Scope(tenant="acme")
        one = stable_credential_identity(scope, "api_key", "secret-value")
        other = stable_credential_identity(scope, "oauth_token", "secret-value")
        assert one != other

    def test_a_rotated_value_gets_a_different_identity(self) -> None:
        """The whole point: a pool keyed on this identity must not reuse a
        connection authenticated with a credential that has since rotated."""
        scope = Scope(tenant="acme")
        before = stable_credential_identity(scope, "api_key", "value-before-rotation")
        after = stable_credential_identity(scope, "api_key", "value-after-rotation")
        assert before != after

    def test_the_identity_never_contains_the_raw_value(self) -> None:
        scope = Scope(tenant="acme")
        identity = stable_credential_identity(scope, "api_key", "sk-super-secret-1234567890")
        assert "sk-super-secret-1234567890" not in identity

    def test_ambiguous_field_boundaries_do_not_collide(self) -> None:
        """Concatenating tenant/principal/name/value without separators would
        let ("ab", "c") and ("a", "bc") collide. The digest must tell them
        apart even though their naive string concatenation is identical."""
        scope_one = Scope(tenant="ab", principal="c")
        scope_two = Scope(tenant="a", principal="bc")
        one = stable_credential_identity(scope_one, "name", "value")
        other = stable_credential_identity(scope_two, "name", "value")
        assert one != other


class TestResolvedCredentialNeverLeaks:
    def test_the_secret_value_is_absent_from_repr_and_str(self) -> None:
        secret_value = "sk-do-not-print-me-abcdef123456"
        credential = ResolvedCredential(identity="deadbeef", secret=SecretStr(secret_value))
        assert secret_value not in repr(credential)
        assert secret_value not in str(credential)

    def test_the_model_is_frozen(self) -> None:
        credential = ResolvedCredential(identity="deadbeef", secret=SecretStr("x"))
        with pytest.raises(ValueError, match="frozen"):
            credential.identity = "changed"  # type: ignore[misc]


class TestInMemorySecretResolver:
    async def test_resolving_an_unset_name_returns_none(self) -> None:
        resolver = InMemorySecretResolver()
        result = await resolver.resolve(Scope(tenant="acme"), "never_set")
        assert result is None

    async def test_resolving_a_set_value_returns_a_credential_carrying_it(self) -> None:
        resolver = InMemorySecretResolver()
        scope = Scope(tenant="acme")
        resolver.set(scope, "api_key", "the-actual-secret")
        credential = await resolver.resolve(scope, "api_key")
        assert credential is not None
        assert credential.secret.get_secret_value() == "the-actual-secret"

    async def test_two_scopes_never_share_a_value_for_the_same_name(self) -> None:
        resolver = InMemorySecretResolver()
        scope_a = Scope(tenant="tenant-a")
        scope_b = Scope(tenant="tenant-b")
        resolver.set(scope_a, "api_key", "token-a")
        resolver.set(scope_b, "api_key", "token-b")

        credential_a = await resolver.resolve(scope_a, "api_key")
        credential_b = await resolver.resolve(scope_b, "api_key")

        assert credential_a is not None
        assert credential_b is not None
        assert credential_a.secret.get_secret_value() == "token-a"
        assert credential_b.secret.get_secret_value() == "token-b"
        assert credential_a.identity != credential_b.identity

    async def test_a_rotation_changes_the_identity_returned_on_the_next_resolve(self) -> None:
        resolver = InMemorySecretResolver()
        scope = Scope(tenant="acme")
        resolver.set(scope, "api_key", "before")
        before = await resolver.resolve(scope, "api_key")
        assert before is not None

        resolver.set(scope, "api_key", "after")
        after = await resolver.resolve(scope, "api_key")
        assert after is not None

        assert before.identity != after.identity

    async def test_clear_makes_a_credential_unresolvable_again(self) -> None:
        resolver = InMemorySecretResolver()
        scope = Scope(tenant="acme")
        resolver.set(scope, "api_key", "value")
        resolver.clear(scope, "api_key")
        assert await resolver.resolve(scope, "api_key") is None
