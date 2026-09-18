"""The playground's egress policy never lets a fresh account reach the inside.

Self-signup is open and every new account has an empty allow-list, which used
to mean ``allow()`` returned ``True`` for everything: a signed-up user could
point an MCP server, an A2A peer or an HTTP tool at ``169.254.169.254`` or at
Postgres on the compose network. Public hosts and loopback (a local model)
stay open by default; link-local, private and metadata addresses need an
explicit, wildcard-free entry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.ports import AccountEgressPolicy, _is_internal_host

from psych_runtime.core.scope import Scope

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acct-1", principal="user-1")


class _Settings:
    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._patterns = patterns

    async def load(self, account_id: str) -> SimpleNamespace:
        return SimpleNamespace(runtime=SimpleNamespace(egress_allow=self._patterns))


def _policy(*patterns: str) -> AccountEgressPolicy:
    return AccountEgressPolicy(_Settings(patterns))  # type: ignore[arg-type]


class TestInternalAddressesAreClassified:
    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",
            "10.0.0.7",
            "172.16.5.5",
            "192.168.1.1",
            "[fd00::1]",
            "0.0.0.0",
            "postgres.internal",
            "metadata.google.internal",
        ],
    )
    def test_internal(self, host: str) -> None:
        assert _is_internal_host(host)

    @pytest.mark.parametrize(
        "host", ["api.example.com", "8.8.8.8", "mcp.vendor.io", "127.0.0.1", "::1", "localhost"]
    )
    def test_public(self, host: str) -> None:
        assert not _is_internal_host(host)


class TestTheDefaultAccount:
    async def test_public_hosts_are_allowed_with_an_empty_list(self) -> None:
        assert await _policy().allow(SCOPE, "https://api.example.com/v1")

    async def test_a_local_model_on_loopback_is_allowed_with_an_empty_list(self) -> None:
        assert await _policy().allow(SCOPE, "http://127.0.0.1:11434/v1")

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.7:5432/",
            "http://postgres.internal/",
        ],
    )
    async def test_internal_hosts_are_refused_with_an_empty_list(self, url: str) -> None:
        assert not await _policy().allow(SCOPE, url)


class TestExplicitEntries:
    async def test_an_exact_entry_opens_one_internal_host(self) -> None:
        policy = _policy("10.0.0.7")
        assert await policy.allow(SCOPE, "http://10.0.0.7:5432/")
        assert not await policy.allow(SCOPE, "http://10.0.0.8:5432/")

    async def test_a_wildcard_never_opens_internal_hosts(self) -> None:
        policy = _policy("*")
        assert await policy.allow(SCOPE, "https://api.example.com/")
        assert not await policy.allow(SCOPE, "http://169.254.169.254/")

    async def test_the_allow_list_still_narrows_public_hosts(self) -> None:
        policy = _policy("*.example.com")
        assert await policy.allow(SCOPE, "https://api.example.com/")
        assert not await policy.allow(SCOPE, "https://api.other.com/")
