"""MCP connection pooling, catalogue caching and narrowing, against a real
local stub server.

DESIGN.md §10.3, §10.4, §10.7. The one thing this file exists to prove, ahead
of everything else: two Scopes that differ by tenant or by credential never
share a pooled connection, and the credential that goes out on the wire for
one Scope's call is never the other Scope's. Everything else about caching
and narrowing is here too, but that is the test that matters most.

The stub server below speaks the subset of the MCP Streamable HTTP transport
``psych_runtime.tools.mcp`` speaks: JSON-RPC over POST for requests, a session id
minted at ``initialize`` and required on every later request, and a
standalone GET opening a real SSE stream for server-to-client push
(``notifications/tools/list_changed``). Real sockets on ``127.0.0.1``, no
mocking of the client under test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, McpOAuth, McpServer, ModelRef
from psych_runtime.core.version import publish as publish_version
from psych_runtime.model.egress import HttpTransport
from psych_runtime.testing.mcp_stub import (
    McpStubServer,
    _read_request,
    _write_json,
    _write_status,
    make_server,
    wire_tool,
)
from psych_runtime.tools.deferred import (
    DEFAULT_CATALOGUE_BUDGET_CHARS,
    MAX_SERVER_DESCRIPTION_CHARS,
    DeferredDiscovery,
    catalogue_chars,
    summarise_description,
)
from psych_runtime.tools.mcp import (
    McpHeaderMismatchError,
    McpInputRequiredError,
    McpMissingClientCapabilityError,
    McpPool,
    McpPoolKey,
    McpProtocolError,
    McpResourceNotFoundError,
    McpServerUnreachable,
    McpToolError,
    McpTools,
    McpUnsupportedProtocolVersionError,
    resolve_mcp_server,
    resolve_mcp_servers,
)
from psych_runtime.tools.oauth import (
    ClientIdentityConfig,
    InMemoryAuthorizationRedirect,
    OAuthClient,
    s256_challenge,
)
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ResolvedTools, ToolResolver
from psych_runtime.tools.secrets import CredentialNotFound, InMemorySecretResolver

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# Raw HTTP/1.1, read side
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    http_transport = HttpTransport()
    try:
        yield http_transport
    finally:
        await http_transport.aclose()


# ---------------------------------------------------------------------------
# The isolation test: the point of this ticket
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_two_scopes_sharing_a_url_never_share_a_connection_or_a_token(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope_a = Scope(tenant="tenant-a", principal="alice")
            scope_b = Scope(tenant="tenant-b", principal="bob")
            secrets.set(scope_a, "api_key", "token-for-tenant-a")
            secrets.set(scope_b, "api_key", "token-for-tenant-b")

            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            connection_a = await pool.get_or_connect(scope_a, server)
            connection_b = await pool.get_or_connect(scope_b, server)

            assert connection_a is not connection_b
            assert stub.init_count == 2  # two distinct handshakes, one per tenant

            await connection_a.call_tool("echo", {})
            await connection_a.call_tool("echo", {})
            await connection_b.call_tool("echo", {})

            # Proven against what the stub server actually received, not
            # against internal state: this is the isolation the pool key
            # exists to guarantee. The calls happened in a known order (two
            # from tenant A, then one from tenant B), and each one carried
            # exactly the token for the tenant that made it, never the other
            # tenant's.
            tool_calls = [c for c in stub.received_calls if c.tool == "echo"]
            assert [c.authorization for c in tool_calls] == [
                "Bearer token-for-tenant-a",
                "Bearer token-for-tenant-a",
                "Bearer token-for-tenant-b",
            ]

            await pool.close_all()

    async def test_same_scope_and_credential_reuse_one_connection(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a", principal="alice")
            secrets.set(scope, "api_key", "token-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            first = await pool.get_or_connect(scope, server)
            second = await pool.get_or_connect(scope, server)

            assert first is second
            assert stub.init_count == 1
            await pool.close_all()

    async def test_same_url_same_tenant_different_credential_gets_a_different_connection(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a", principal="alice")
            secrets.set(scope, "api_key_v1", "token-v1")
            secrets.set(scope, "api_key_v2", "token-v2")
            pool = McpPool(transport=transport, secrets=secrets)

            server_v1 = make_server(stub.url, credential="api_key_v1")
            server_v2 = make_server(stub.url, credential="api_key_v2")

            connection_v1 = await pool.get_or_connect(scope, server_v1)
            connection_v2 = await pool.get_or_connect(scope, server_v2)

            assert connection_v1 is not connection_v2
            assert stub.init_count == 2
            await pool.close_all()

    async def test_same_scope_and_url_different_credential_after_rotation(
        self, transport: HttpTransport
    ) -> None:
        """A credential named the same way but rotated to a new value pools
        separately: the pool key is the resolved identity, not the name."""
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a", principal="alice")
            secrets.set(scope, "api_key", "token-before-rotation")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            before = await pool.get_or_connect(scope, server)

            secrets.set(scope, "api_key", "token-after-rotation")
            after = await pool.get_or_connect(scope, server)

            assert before is not after
            assert stub.init_count == 2
            await pool.close_all()


# ---------------------------------------------------------------------------
# Credential value never leaks
# ---------------------------------------------------------------------------


class TestCredentialNeverLeaks:
    async def test_secret_value_never_appears_in_repr_or_pool_key(
        self, transport: HttpTransport
    ) -> None:
        secret_value = "sk-do-not-print-me-1234567890"
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a", principal="alice")
            secrets.set(scope, "api_key", secret_value)
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            connection = await pool.get_or_connect(scope, server)

            assert secret_value not in repr(connection)
            assert secret_value not in str(connection)

            credential = await secrets.resolve(scope, "api_key")
            assert credential is not None
            assert secret_value not in repr(credential)
            assert secret_value not in str(credential)
            assert credential.identity != secret_value

            key = McpPoolKey.from_scope(
                scope=scope, server=server, credential_identity=credential.identity
            )
            assert secret_value not in repr(key)
            assert key.credential_identity == credential.identity

            with pytest.raises(McpServerUnreachable) as excinfo:
                raise McpServerUnreachable("support", f"boom near {credential.identity}")
            assert secret_value not in str(excinfo.value)

            await pool.close_all()


# ---------------------------------------------------------------------------
# Catalogue caching: connect, TTL, on-demand, list_changed
# ---------------------------------------------------------------------------


class TestCatalogueCaching:
    async def test_a_newly_connected_server_is_usable_with_no_restart(
        self, transport: HttpTransport
    ) -> None:
        """DESIGN.md §10.2/§10.7: connecting mid-Run is usable on the very
        next resolution, with no restart of the pool or the transport."""
        async with McpStubServer([wire_tool("read_orders")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)

            # "Turn 1": this server was never in the Spec's mcp_servers yet.
            turn_one_servers: tuple[McpServer, ...] = ()
            turn_one = await resolve_mcp_servers(pool, scope, turn_one_servers)
            assert turn_one == ()

            # "Turn 2": the Spec now includes it. Same pool, same transport,
            # no restart of anything.
            server = make_server(stub.url)
            turn_two = await resolve_mcp_servers(pool, scope, (server,))
            assert len(turn_two) == 1
            assert turn_two[0].reachable is True
            assert [t.name for t in turn_two[0].tools] == ["read_orders"]

            await pool.close_all()

    async def test_second_list_tools_within_ttl_does_not_re_request(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=10.0)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            assert stub.list_count == 1  # the synchronous fetch on connect

            await connection.list_tools()
            await connection.list_tools()
            assert stub.list_count == 1  # still within the TTL

            await pool.close_all()

    async def test_list_tools_refetches_after_the_ttl_expires(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=0.05)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            assert stub.list_count == 1

            await asyncio.sleep(0.15)
            await connection.list_tools()
            assert stub.list_count >= 2  # the background sweep, on-demand, or both

            await pool.close_all()

    async def test_an_etag_matched_refresh_does_not_rebuild_the_catalogue(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=0.05)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            first = await connection.list_tools()

            await asyncio.sleep(0.15)
            second = await connection.list_tools(force=True)

            # A real request happened (list_count grew) but the tool list is
            # byte-for-byte the same content, so the tuple object is reused
            # rather than rebuilt.
            assert stub.list_count >= 2
            assert second is first

            await pool.close_all()

    async def test_list_changed_notification_invalidates_the_cache(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            # A long TTL: only the notification, not the sweep, should cause
            # the next fetch below.
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=60.0)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            before = await connection.list_tools()
            assert [t.name for t in before] == ["echo"]
            list_count_before = stub.list_count

            await stub.wait_for_sse_listener()
            stub.set_tools([wire_tool("echo"), wire_tool("read_orders")])
            await stub.push_list_changed()
            await asyncio.sleep(0.2)  # let the background listener react

            assert stub.list_count > list_count_before  # refreshed by the push
            after = await connection.list_tools()  # within the (long) TTL
            assert sorted(t.name for t in after) == ["echo", "read_orders"]
            assert stub.list_count == list_count_before + 1  # not re-requested above

            await pool.close_all()


# ---------------------------------------------------------------------------
# Unreachable connections
# ---------------------------------------------------------------------------


class TestUnreachable:
    async def test_a_required_unreachable_server_fails(self, transport: HttpTransport) -> None:
        secrets = InMemorySecretResolver()
        scope = Scope(tenant="tenant-a")
        pool = McpPool(transport=transport, secrets=secrets)
        server = make_server("http://127.0.0.1:1/mcp", optional=False)

        with pytest.raises(McpServerUnreachable):
            await resolve_mcp_server(pool, scope, server)

    async def test_an_optional_unreachable_server_omits_its_tools(
        self, transport: HttpTransport
    ) -> None:
        secrets = InMemorySecretResolver()
        scope = Scope(tenant="tenant-a")
        pool = McpPool(transport=transport, secrets=secrets)
        server = make_server("http://127.0.0.1:1/mcp", optional=True)

        resolution = await resolve_mcp_server(pool, scope, server)

        assert resolution.reachable is False
        assert resolution.tools == ()
        assert resolution.unavailable_reason is not None


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


class TestNarrowing:
    async def test_a_spec_allow_list_narrows_the_offered_tools(
        self, transport: HttpTransport
    ) -> None:
        tools = [wire_tool(f"read_{c}") for c in "abcde"]
        tools += [wire_tool(f"write_{c}") for c in "abcde"]
        async with McpStubServer(tools) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, allow=("read_*",))

            resolution = await resolve_mcp_server(pool, scope, server)

            assert resolution.reachable is True
            assert sorted(t.name for t in resolution.tools) == [
                "read_a",
                "read_b",
                "read_c",
                "read_d",
                "read_e",
            ]
            await pool.close_all()

    async def test_an_empty_allow_list_grants_everything_the_server_offers(
        self, transport: HttpTransport
    ) -> None:
        tools = [wire_tool("a"), wire_tool("b")]
        async with McpStubServer(tools) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            resolution = await resolve_mcp_server(pool, scope, server)

            assert sorted(t.name for t in resolution.tools) == ["a", "b"]
            await pool.close_all()

    async def test_annotations_map_unannotated_to_write(self, transport: HttpTransport) -> None:
        tools = [
            wire_tool("look_up", read_only=True),
            wire_tool("delete_it", destructive=True),
            wire_tool("plain"),
        ]
        async with McpStubServer(tools) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            resolution = await resolve_mcp_server(pool, scope, server)

            by_name = {t.name: t for t in resolution.tools}
            assert by_name["look_up"].annotations == frozenset({"read-only"})
            assert by_name["delete_it"].annotations == frozenset({"write", "destructive"})
            assert by_name["plain"].annotations == frozenset({"write"})
            await pool.close_all()


# ---------------------------------------------------------------------------
# Calling a tool
# ---------------------------------------------------------------------------


class TestCallTool:
    async def test_call_tool_returns_the_flattened_text_content(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("greet")]) as stub:
            stub.call_handlers["greet"] = lambda args: (f"hello {args.get('who')}", False)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("greet", {"who": "world"})

            assert result.content == "hello world"
            assert result.is_error is False
            await pool.close_all()

    async def test_an_error_result_is_reported_as_such(self, transport: HttpTransport) -> None:
        async with McpStubServer([wire_tool("broken")]) as stub:
            stub.call_handlers["broken"] = lambda _args: ("it broke", True)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("broken", {})

            assert result.is_error is True
            assert result.content == "it broke"
            await pool.close_all()


# ---------------------------------------------------------------------------
# Malformed server responses
# ---------------------------------------------------------------------------


class TestProtocolErrors:
    async def test_a_non_json_response_raises_a_protocol_error(
        self, transport: HttpTransport
    ) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await _read_request(reader)
                await _write_status(
                    writer, 200, headers={"Content-Type": "text/plain", "Content-Length": "9"}
                )
                writer.write(b"not json!")
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        server_task = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server_task.sockets[0].getsockname()[1]
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(f"http://127.0.0.1:{port}/mcp")

            with pytest.raises(McpProtocolError):
                await pool.get_or_connect(scope, server)
        finally:
            server_task.close()
            await server_task.wait_closed()


# ---------------------------------------------------------------------------
# Missing credential is a configuration error, not a transient outage
# ---------------------------------------------------------------------------


class TestMissingCredential:
    async def test_a_credential_name_with_no_value_raises_before_connecting(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()  # nothing configured
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="never_set")

            with pytest.raises(CredentialNotFound):
                await pool.get_or_connect(scope, server)

            assert stub.init_count == 0  # never even attempted the handshake


# ---------------------------------------------------------------------------
# Era negotiation: one code path, three servers
# ---------------------------------------------------------------------------


class TestEraNegotiationModern:
    async def test_a_2026_07_28_server_uses_discover_and_never_handshakes(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("greet")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)

            assert stub.discover_count == 1
            assert stub.init_count == 0  # no initialize handshake, ever

            tools = await connection.list_tools()
            assert [t.name for t in tools] == ["greet"]
            await pool.close_all()

    async def test_meta_fields_and_headers_are_present_and_correct(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("greet")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            await connection.call_tool("greet", {"who": "world"})

            calls = [r for r in stub.received_requests if r.rpc_method == "tools/call"]
            assert len(calls) == 1
            call = calls[0]
            assert call.protocol_version_header == "2026-07-28"
            assert call.mcp_method_header == "tools/call"
            assert call.mcp_name_header == "greet"
            assert call.session_id is None  # no Mcp-Session-Id under 2026-07-28
            assert call.meta is not None
            assert call.meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
            assert call.meta["io.modelcontextprotocol/clientCapabilities"] == {}
            client_info = call.meta["io.modelcontextprotocol/clientInfo"]
            assert isinstance(client_info, dict)
            assert client_info["name"] == "psych"

            # tools/list and server/discover carry no Mcp-Name (not a
            # tools/call, resources/read, or prompts/get request).
            discover = next(r for r in stub.received_requests if r.rpc_method == "server/discover")
            assert discover.mcp_method_header == "server/discover"
            assert discover.mcp_name_header is None
            await pool.close_all()

    async def test_a_complete_result_type_is_transparent(self, transport: HttpTransport) -> None:
        """The stub always sets ``resultType: "complete"``; nothing about it
        should be visible to a caller beyond the call simply succeeding."""
        async with McpStubServer([wire_tool("greet")], modern=True) as stub:
            stub.call_handlers["greet"] = lambda args: (f"hi {args.get('who')}", False)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("greet", {"who": "world"})

            assert result.content == "hi world"
            await pool.close_all()


class TestEraNegotiationLegacy:
    async def test_a_2025_06_18_server_has_no_discover_and_falls_back(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer(
            [wire_tool("read_orders")], legacy_protocol_version="2025-06-18"
        ) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)

            assert stub.discover_count == 0  # the stub never implements it
            assert stub.init_count == 1  # fell back to the handshake

            tools = await connection.list_tools()
            assert [t.name for t in tools] == ["read_orders"]

            # No `resultType` in a legacy result; per the spec's normatively
            # stated backward-compat rule, its absence MUST be treated as
            # "complete", which is exactly what just happened above without
            # anything raising.
            result = await connection.call_tool("read_orders", {})
            assert result.is_error is False
            await pool.close_all()

    async def test_a_2025_11_25_server_falls_back_the_same_way(
        self, transport: HttpTransport
    ) -> None:
        """Mechanically identical to the 2025-06-18 case: both predate
        ``server/discover`` and per-request ``_meta``, so one legacy fallback
        path handles both without distinguishing them."""
        async with McpStubServer(
            [wire_tool("read_orders")], legacy_protocol_version="2025-11-25"
        ) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)

            assert stub.discover_count == 0
            assert stub.init_count == 1
            tools = await connection.list_tools()
            assert [t.name for t in tools] == ["read_orders"]
            await pool.close_all()


# ---------------------------------------------------------------------------
# MRTR: input_required is a refusal, never a silent empty success
# ---------------------------------------------------------------------------


class TestInputRequired:
    async def test_input_required_fails_the_call_with_an_actionable_message(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("needs_login")], modern=True) as stub:
            stub.force_input_required = True
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)

            with pytest.raises(McpInputRequiredError) as excinfo:
                await connection.call_tool("needs_login", {})
            assert "elicitation/create" in str(excinfo.value)
            assert "Psych implements no roots, sampling, or elicitation" in str(excinfo.value)
            await pool.close_all()


# ---------------------------------------------------------------------------
# CacheableResult: server ttlMs wins, private scope never crosses pool keys
# ---------------------------------------------------------------------------


class TestCacheableResult:
    async def test_server_ttl_ms_overrides_the_configured_default(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            stub.tools_ttl_ms = 50  # 0.05s: far shorter than the pool default below
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            # A long default TTL: only the server's own ttlMs should force
            # the refetch below, not the pool's configured default.
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=60.0)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            assert stub.list_count == 1

            await asyncio.sleep(0.15)
            await connection.list_tools()  # force=False: freshness check only
            assert stub.list_count >= 2  # refetched because ttlMs, not the 60s default, applied

            await pool.close_all()

    async def test_a_private_cache_scope_is_never_shared_across_pool_keys(
        self, transport: HttpTransport
    ) -> None:
        """``cacheScope: "private"`` is satisfied by this module's structure:
        each tenant gets its own ``McpConnection`` and thus its own
        catalogue, so a "private" entry has nowhere else to leak to. Proven
        the same way tenant isolation is proven elsewhere: against what the
        stub actually received, not against internal state."""
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            stub.tools_cache_scope = "private"
            secrets = InMemorySecretResolver()
            secrets.set(Scope(tenant="tenant-a"), "api_key", "token-a")
            secrets.set(Scope(tenant="tenant-b"), "api_key", "token-b")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            connection_a = await pool.get_or_connect(Scope(tenant="tenant-a"), server)
            connection_b = await pool.get_or_connect(Scope(tenant="tenant-b"), server)

            assert connection_a is not connection_b
            tools_a = await connection_a.list_tools()
            tools_b = await connection_b.list_tools()
            assert [t.name for t in tools_a] == [t.name for t in tools_b] == ["echo"]
            # Two independent fetches, one per tenant's own connection: the
            # "private" catalogue was never handed from one to the other.
            assert stub.list_count == 2
            await pool.close_all()


# ---------------------------------------------------------------------------
# subscriptions/listen replaces the GET stream under 2026-07-28
# ---------------------------------------------------------------------------


class TestSubscriptionsListen:
    async def test_tools_list_changed_arrives_on_the_listen_stream(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets, catalogue_ttl_seconds=60.0)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            before = await connection.list_tools()
            assert [t.name for t in before] == ["echo"]
            list_count_before = stub.list_count

            await stub.wait_for_sse_listener()
            stub.set_tools([wire_tool("echo"), wire_tool("read_orders")])
            await stub.push_list_changed()
            await asyncio.sleep(0.2)

            assert stub.list_count > list_count_before  # invalidated by the push
            after = await connection.list_tools()  # within the (long) TTL
            assert sorted(t.name for t in after) == ["echo", "read_orders"]
            assert stub.list_count == list_count_before + 1  # not re-requested above

            await pool.close_all()


# ---------------------------------------------------------------------------
# No resumability: a broken stream is re-issued as a new request, not resumed
# ---------------------------------------------------------------------------


class TestBrokenStreamReissue:
    async def test_a_broken_response_is_retried_as_a_new_request_id_not_resumed(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            stub.break_stream_once = True

            with pytest.raises(McpServerUnreachable):
                await connection.call_tool("echo", {})

            # The retry is an ordinary new call, not a resume: it succeeds
            # because it is a fresh request with a fresh id, never because
            # anything was resumed (2026-07-28 has no Last-Event-ID to
            # resume with, and this client never attempted one even under
            # the legacy transport).
            result = await connection.call_tool("echo", {})
            assert result.content == "called echo"

            tool_calls = [r for r in stub.received_requests if r.rpc_method == "tools/call"]
            assert len(tool_calls) == 2
            assert tool_calls[0].request_id != tool_calls[1].request_id
            await pool.close_all()


# ---------------------------------------------------------------------------
# Error code mapping
# ---------------------------------------------------------------------------


class TestErrorCodeMapping:
    async def test_header_mismatch_maps_to_its_typed_error(self, transport: HttpTransport) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)
            connection = await pool.get_or_connect(scope, server)

            stub.force_error = (-32020, "Mcp-Name does not match body")
            with pytest.raises(McpHeaderMismatchError):
                await connection.call_tool("echo", {})
            await pool.close_all()

    async def test_missing_client_capability_maps_to_its_typed_error(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)
            connection = await pool.get_or_connect(scope, server)

            stub.force_error = (-32021, "sampling capability required")
            with pytest.raises(McpMissingClientCapabilityError):
                await connection.call_tool("echo", {})
            await pool.close_all()

    async def test_unsupported_protocol_version_maps_to_its_typed_error(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)
            connection = await pool.get_or_connect(scope, server)

            stub.force_error = (-32022, "unsupported protocol version")
            with pytest.raises(McpUnsupportedProtocolVersionError):
                await connection.call_tool("echo", {})
            await pool.close_all()

    async def test_resource_not_found_maps_to_its_typed_error(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("echo")], modern=True) as stub:
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)
            connection = await pool.get_or_connect(scope, server)

            stub.force_error = (-32602, "resource not found")
            with pytest.raises(McpResourceNotFoundError):
                await connection.call_tool("echo", {})
            await pool.close_all()

    async def test_legacy_resource_not_found_code_still_maps(
        self, transport: HttpTransport
    ) -> None:
        """Clients SHOULD still accept the pre-2026-07-28 code from a server
        implementing an earlier revision, over the real legacy wire path
        (post-handshake, session id and all), not just in isolation."""
        async with McpStubServer([wire_tool("echo")]) as stub:  # legacy stub
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url)
            connection = await pool.get_or_connect(scope, server)

            stub.force_error = (-32002, "no such resource")
            with pytest.raises(McpResourceNotFoundError):
                await connection.call_tool("echo", {})
            await pool.close_all()


# ---------------------------------------------------------------------------
# OAuth: a minimal authorization server, both grants this client uses
# ---------------------------------------------------------------------------


@dataclass
class _AuthCode:
    code_challenge: str
    redirect_uri: str
    scope: str


class _AuthServerStub:
    """A minimal OAuth authorization server on ``127.0.0.1``, real sockets,
    speaking just enough to service both grants ``OAuthClient`` uses: RFC
    8414 / OpenID Connect Discovery metadata (identical documents at both
    well-known paths), Dynamic Client Registration, a real PKCE-checking
    authorization endpoint, and a token endpoint handling
    ``client_credentials``, ``authorization_code`` and ``refresh_token``.

    Both grants have to be live in the same test file: a Spec naming one
    server that needs ``client_credentials`` and another that needs
    ``authorization_code`` is exactly the shape per-server OAuth exists for, and
    proving they share one ``McpPool`` without crossing needs a real
    authorization endpoint to redirect through, not just a token endpoint.

    ``issued_scopes`` (access token -> granted scope) is handed straight to
    ``McpStubServer.oauth_tokens`` by the tests below: the two stubs share
    this one dict so the "resource server" can recognise a token the
    "authorization server" just issued, the same way a real deployment's
    token introspection would, without this file needing to implement one.
    """

    def __init__(self, *, token_prefix: str = "at") -> None:
        self.base_url = ""
        self._server: asyncio.AbstractServer | None = None
        self.registrations = 0
        self.token_requests: list[dict[str, str]] = []
        self.issued_scopes: dict[str, str] = {}
        self._scope_by_refresh: dict[str, str] = {}
        self._codes: dict[str, _AuthCode] = {}
        self._next_code = 0
        self._next_token = 0
        # Distinguishes tokens issued by two different _AuthServerStub
        # instances in the same test: two separate authorization servers
        # both starting their own counter at 1 would otherwise mint the
        # identical string "at-1", making a same-value assertion pass by
        # coincidence rather than by proving isolation.
        self._token_prefix = token_prefix
        self._expires_in = 3600.0
        # Deliberately far longer than `_expires_in`: a test forcing a fresh
        # token to read as "expiring" via a large OAuthClient safety margin
        # needs the *refreshed* token to clear that same margin, or every
        # refresh immediately looks expiring again and a "exactly one
        # refresh happened" assertion could never hold.
        self._refresh_expires_in = self._expires_in * 1_000_000.0

    async def __aenter__(self) -> _AuthServerStub:
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self._server = server
        port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def _metadata(self) -> dict[str, object]:
        return {
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            "registration_endpoint": f"{self.base_url}/register",
            "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "authorization_response_iss_parameter_supported": True,
        }

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            _method, path, _headers, body = await _read_request(reader)
            await self._handle(writer, path, body)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _handle(self, writer: asyncio.StreamWriter, path: str, body: bytes) -> None:
        if path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
        ):
            await _write_json(writer, 200, self._metadata())
            return
        if path == "/register":
            self.registrations += 1
            await _write_json(writer, 201, {"client_id": f"dcr-client-{self.registrations}"})
            return
        if path.startswith("/authorize"):
            await self._handle_authorize(writer, path)
            return
        if path == "/token":
            await self._handle_token(writer, body)
            return
        await _write_json(writer, 404, {"error": "not_found"})

    async def _handle_authorize(self, writer: asyncio.StreamWriter, path: str) -> None:
        query = dict(parse_qsl(urlsplit(path).query, keep_blank_values=True))
        redirect_uri = query["redirect_uri"]
        self._next_code += 1
        code = f"code-{self._next_code}"
        self._codes[code] = _AuthCode(
            code_challenge=query["code_challenge"],
            redirect_uri=redirect_uri,
            scope=query.get("scope", ""),
        )
        params: dict[str, str] = {"code": code, "iss": self.base_url}
        if "state" in query:
            params["state"] = query["state"]
        separator = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{separator}{urlencode(params)}"
        await _write_status(writer, 302, headers={"Location": location, "Content-Length": "0"})

    async def _handle_token(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        params = dict(parse_qsl(body.decode(), keep_blank_values=True))
        self.token_requests.append(params)
        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            record = self._codes.pop(params.get("code", ""), None)
            if record is None:
                await _write_json(writer, 400, {"error": "invalid_grant"})
                return
            if s256_challenge(params.get("code_verifier", "")) != record.code_challenge:
                await _write_json(writer, 400, {"error": "invalid_grant"})
                return
            if params.get("redirect_uri") != record.redirect_uri:
                await _write_json(writer, 400, {"error": "invalid_grant"})
                return
            await self._issue_token(writer, record.scope, self._expires_in)
        elif grant_type == "refresh_token":
            scope = self._scope_by_refresh.get(params.get("refresh_token", ""), "")
            await self._issue_token(writer, scope, self._refresh_expires_in)
        else:
            scope = params.get("scope") or ""
            await self._issue_token(writer, scope, self._expires_in)

    async def _issue_token(
        self, writer: asyncio.StreamWriter, scope: str, expires_in: float
    ) -> None:
        self._next_token += 1
        access_token = f"{self._token_prefix}-{self._next_token}"
        refresh_token = f"r{self._token_prefix}-{self._next_token}"
        self.issued_scopes[access_token] = scope
        self._scope_by_refresh[refresh_token] = scope
        await _write_json(
            writer,
            200,
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": expires_in,
                "refresh_token": refresh_token,
                "scope": scope,
            },
        )


def _protect(stub: McpStubServer, auth: _AuthServerStub) -> None:
    """Wire ``stub`` up as an OAuth resource server for ``auth``, the way a
    real MCP server fronted by that authorization server would announce
    itself: Protected Resource Metadata naming ``auth`` and validating
    bearer tokens against whatever ``auth`` has issued."""
    stub.oauth_tokens = auth.issued_scopes
    stub.protected_resource_metadata = {
        "resource": stub.url,
        "authorization_servers": [auth.base_url],
        "scopes_supported": ["mcp:read", "mcp:write"],
    }


# ---------------------------------------------------------------------------
# OAuth: the 401/403 challenge drives OAuthClient, and the pool key follows
# ---------------------------------------------------------------------------


class TestOAuthProtectedServer:
    async def test_401_challenge_triggers_oauth_then_the_retry_succeeds(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("echo", {})

            assert result.content == "called echo"
            assert auth.registrations == 1  # one Dynamic Client Registration
            assert connection.credential_identity is not None
            await pool.close_all()

    async def test_pooled_under_the_post_auth_key_not_the_pre_auth_key(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)

            pre_auth_key = McpPoolKey.from_scope(
                scope=scope, server=server, credential_identity=None
            )
            post_auth_key = McpPoolKey.from_scope(
                scope=scope, server=server, credential_identity=connection.credential_identity
            )

            assert pool._connections.get(pre_auth_key) is None
            assert pool._connections.get(post_auth_key) is connection

            # A second call for the same (scope, server) does not repeat the
            # OAuth exchange or reconnect: it is redirected from the
            # pre-auth key straight to the already-established connection.
            again = await pool.get_or_connect(scope, server)
            assert again is connection
            assert auth.registrations == 1
            assert stub.discover_count == 1

            await pool.close_all()

    async def test_a_refresh_does_not_change_the_pool_key_or_reconnect(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            # A huge safety margin makes every issued token read as
            # "expiring" immediately, so the very next request refreshes
            # deterministically rather than waiting out a real expiry.
            oauth = OAuthClient(transport=transport, refresh_safety_margin_seconds=1_000_000.0)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            identity_before = connection.credential_identity
            assert identity_before is not None

            await connection.call_tool("echo", {})

            assert connection.credential_identity == identity_before
            refresh_requests = [
                r for r in auth.token_requests if r.get("grant_type") == "refresh_token"
            ]
            assert len(refresh_requests) == 1
            assert stub.discover_count == 1  # no reconnect happened
            post_auth_key = McpPoolKey.from_scope(
                scope=scope, server=server, credential_identity=identity_before
            )
            assert pool._connections[post_auth_key] is connection

            await pool.close_all()

    async def test_403_insufficient_scope_triggers_step_up_and_one_retry(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("danger")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            identity_before = connection.credential_identity

            # Only once connected does this server start demanding a scope
            # broader than the initial challenge asked for: the step-up
            # path, not the initial-authorization path.
            stub.require_scope = "mcp:write"
            result = await connection.call_tool("danger", {})

            assert result.content == "called danger"
            assert connection.credential_identity == identity_before  # same grant throughout
            served_calls = [c for c in stub.received_calls if c.tool == "danger"]
            assert len(served_calls) == 1  # the 403 attempt never reached the handler
            client_credentials_requests = [
                r for r in auth.token_requests if r.get("grant_type") == "client_credentials"
            ]
            assert len(client_credentials_requests) == 2  # the initial grant, then the step-up

            await pool.close_all()

    async def test_401_with_no_oauth_configured_names_oauth_as_the_cause(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            pool = McpPool(transport=transport, secrets=secrets)  # no oauth= configured
            server = make_server(stub.url)

            with pytest.raises(McpServerUnreachable) as excinfo:
                await pool.get_or_connect(scope, server)

            message = str(excinfo.value)
            assert "OAuth" in message
            assert "401" in message

    async def test_two_tenants_get_different_connections_and_send_only_their_own_token(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope_a = Scope(tenant="tenant-a")
            scope_b = Scope(tenant="tenant-b")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection_a = await pool.get_or_connect(scope_a, server)
            connection_b = await pool.get_or_connect(scope_b, server)

            assert connection_a is not connection_b
            assert connection_a.credential_identity != connection_b.credential_identity
            assert auth.registrations == 2  # a distinct client identity per tenant

            credential_a = connection_a._credential
            credential_b = connection_b._credential
            assert credential_a is not None
            assert credential_b is not None
            token_a = credential_a.secret.get_secret_value()
            token_b = credential_b.secret.get_secret_value()
            assert token_a != token_b

            await connection_a.call_tool("echo", {})
            await connection_a.call_tool("echo", {})
            await connection_b.call_tool("echo", {})

            # Proven against what the stub server actually received, not
            # against internal state, the same discipline
            # TestTenantIsolation applies to a static credential: each call
            # carried exactly its own tenant's OAuth token, never the
            # other's.
            tool_calls = [c for c in stub.received_calls if c.tool == "echo"]
            assert [c.authorization for c in tool_calls] == [
                f"Bearer {token_a}",
                f"Bearer {token_a}",
                f"Bearer {token_b}",
            ]

            await pool.close_all()


class TestOAuthLegacyServer:
    async def test_a_401_on_the_legacy_handshake_also_triggers_oauth(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")]) as stub,  # legacy stub
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)
            server = make_server(stub.url)

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("echo", {})

            assert result.content == "called echo"
            assert stub.init_count == 1  # the handshake succeeded once authorized
            await pool.close_all()


# ---------------------------------------------------------------------------
# One McpPool serving two servers with different OAuth identities
# and different grant kinds, in the same Run
# ---------------------------------------------------------------------------


class TestOAuthMultipleServersOnOnePool:
    """The exact shape per-server OAuth exists for: a tenant integrating a
    hosted CRM (``client_credentials``, one client registration) and a hosted
    ticketing system (``authorization_code``, a different registration)
    behind two different authorization servers, in one Run. Before it,
    ``McpPool`` took one ``oauth_identity``/``oauth_grant`` pair for
    the whole pool and a Spec like this needed two pools; ``McpServer.oauth``
    now carries that configuration per server, so one pool and one
    ``OAuthClient`` serve both without either server's identity or token
    reaching the other's connection.
    """

    async def test_two_servers_use_their_own_identity_and_grant_without_crossing(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as crm_stub,
            McpStubServer([wire_tool("echo")], modern=True) as ticketing_stub,
            _AuthServerStub(token_prefix="crm-at") as crm_auth,
            _AuthServerStub(token_prefix="ticketing-at") as ticketing_auth,
        ):
            _protect(crm_stub, crm_auth)
            _protect(ticketing_stub, ticketing_auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            # One OAuthClient, shared by both servers, exactly the point:
            # OAuthClient already keys sessions by (scope, resource) and
            # registrations by (scope, issuer), so nothing about serving two
            # issuers at once is new to it. What was missing is this pool's
            # ability to tell it *which* identity and grant apply to which
            # server, which McpServer.oauth now supplies.
            oauth = OAuthClient(
                transport=transport, redirect=InMemoryAuthorizationRedirect(transport)
            )
            pool = McpPool(transport=transport, secrets=secrets, oauth=oauth)

            crm_server = make_server(
                crm_stub.url,
                name="crm",
                oauth=McpOAuth(grant="client_credentials", preregistered_client_id="crm-app"),
            )
            ticketing_server = make_server(
                ticketing_stub.url,
                name="ticketing",
                oauth=McpOAuth(
                    grant="authorization_code",
                    preregistered_client_id="ticketing-app",
                    redirect_uris=("https://app.invalid/callback",),
                ),
            )

            crm_connection = await pool.get_or_connect(scope, crm_server)
            ticketing_connection = await pool.get_or_connect(scope, ticketing_server)

            assert crm_connection is not ticketing_connection
            assert crm_connection.credential_identity is not None
            assert ticketing_connection.credential_identity is not None
            assert crm_connection.credential_identity != ticketing_connection.credential_identity

            # Neither used Dynamic Client Registration: both named a
            # preregistered_client_id, and each authorization server only
            # ever saw its own server's client_id, never the other's.
            assert crm_auth.registrations == 0
            assert ticketing_auth.registrations == 0
            assert {r.get("client_id") for r in crm_auth.token_requests} == {"crm-app"}
            assert {r.get("client_id") for r in ticketing_auth.token_requests} == {"ticketing-app"}
            # And the grant each one actually drove is the one its Spec
            # asked for, not the other's or the pool's old single default.
            assert {r["grant_type"] for r in crm_auth.token_requests} == {"client_credentials"}
            assert {r["grant_type"] for r in ticketing_auth.token_requests} == {
                "authorization_code"
            }

            crm_result = await crm_connection.call_tool("echo", {})
            ticketing_result = await ticketing_connection.call_tool("echo", {})
            assert crm_result.content == "called echo"
            assert ticketing_result.content == "called echo"

            crm_credential = crm_connection._credential
            ticketing_credential = ticketing_connection._credential
            assert crm_credential is not None
            assert ticketing_credential is not None
            crm_token = crm_credential.secret.get_secret_value()
            ticketing_token = ticketing_credential.secret.get_secret_value()
            assert crm_token != ticketing_token

            # Proven against the wire, the same discipline TestTenantIsolation
            # applies to a single server's two tenants: each resource server
            # received only its own token, on calls that actually reached it,
            # never the other server's -- even though both connections came
            # out of one pool and authorized through one OAuthClient.
            crm_calls = [c for c in crm_stub.received_calls if c.tool == "echo"]
            ticketing_calls = [c for c in ticketing_stub.received_calls if c.tool == "echo"]
            assert [c.authorization for c in crm_calls] == [f"Bearer {crm_token}"]
            assert [c.authorization for c in ticketing_calls] == [f"Bearer {ticketing_token}"]

            await pool.close_all()

    async def test_a_server_with_no_oauth_config_falls_back_to_the_pool_default(
        self, transport: HttpTransport
    ) -> None:
        """A Spec that sets no ``McpServer.oauth`` at all keeps using the
        pool-wide default identity and grant. ``McpServer.oauth`` adds a
        per-server override, it does not remove the pool-level default that
        existing callers already rely on."""
        async with (
            McpStubServer([wire_tool("echo")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            secrets = InMemorySecretResolver()
            scope = Scope(tenant="tenant-a")
            oauth = OAuthClient(transport=transport)
            pool = McpPool(
                transport=transport,
                secrets=secrets,
                oauth=oauth,
                oauth_identity=ClientIdentityConfig(preregistered_client_id="pool-default-app"),
            )
            server = make_server(stub.url)  # no oauth= set on the Spec

            connection = await pool.get_or_connect(scope, server)
            result = await connection.call_tool("echo", {})

            assert result.content == "called echo"
            assert {r.get("client_id") for r in auth.token_requests} == {"pool-default-app"}
            await pool.close_all()


class TestConnectionStatusAndProbe:
    """What a platform shows about a connection, from the pool rather than from
    a second connection attempt of its own."""

    async def test_status_describes_a_live_connection_without_touching_the_network(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("a"), wire_tool("b")]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="acme", principal="user-1")
            await pool.get_or_connect(scope, make_server(stub.url))
            before = stub.list_count

            statuses = pool.connections()
            assert stub.list_count == before, "reading status made a request"

            assert len(statuses) == 1
            status = statuses[0]
            assert status.tenant == "acme"
            assert status.principal == "user-1"
            assert status.url == stub.url
            assert status.tool_count == 2
            assert status.tool_names == ("a", "b")
            assert status.era == "legacy"
            assert status.catalogue_etag is not None
            assert status.catalogue_age_seconds is not None
            assert status.credential_identity is None
            await pool.close_all()

    async def test_a_probe_reports_the_tools_it_found(self, transport: HttpTransport) -> None:
        async with McpStubServer([wire_tool("lookup_order")]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            probe = await pool.probe(Scope(tenant="acme"), make_server(stub.url))
            await pool.close_all()

        assert probe.ok
        assert [tool.name for tool in probe.tools] == ["lookup_order"]
        assert probe.era == "legacy"
        assert "1 tool" in probe.detail

    async def test_a_probe_names_the_failure_rather_than_raising(
        self, transport: HttpTransport
    ) -> None:
        pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
        probe = await pool.probe(Scope(tenant="acme"), make_server("http://127.0.0.1:9/mcp"))
        await pool.close_all()

        assert not probe.ok
        # The type, so a caller can tell "cannot reach it" from "no credential"
        # from "it answered with nonsense" without reading the message.
        assert probe.error_type == "McpServerUnreachable"
        assert probe.tools == ()

    async def test_a_probe_reports_a_missing_credential_as_such(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("a")]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            probe = await pool.probe(
                Scope(tenant="acme"), make_server(stub.url, credential="nothing-stored")
            )
            await pool.close_all()

        assert not probe.ok
        assert probe.error_type == "CredentialNotFound"


class TestAToolThatReportsAnError:
    """``isError: true`` is how MCP reports a tool failure, and it must reach the
    log as a failure rather than as a successful result that happens to say so."""

    async def test_an_is_error_result_raises_rather_than_returning(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("issue_refund")]) as stub:
            stub.call_handlers["issue_refund"] = lambda _args: ("card declined", True)
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            connection = await pool.get_or_connect(Scope(tenant="acme"), make_server(stub.url))

            with pytest.raises(McpToolError) as caught:
                await connection.call_tool_text("issue_refund", {})
            await pool.close_all()

        assert caught.value.tool == "issue_refund"
        assert "card declined" in str(caught.value)

    async def test_a_successful_call_comes_back_as_plain_text(
        self, transport: HttpTransport
    ) -> None:
        """Not the McpToolResult object: that reached the log, and the log
        rendered it as a Python repr in memory and as JSON from a real store,
        so one Spec showed the model two different things (DESIGN.md §23.8)."""
        async with McpStubServer([wire_tool("lookup_order")]) as stub:
            stub.call_handlers["lookup_order"] = lambda _args: ("A1 shipped", False)
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            connection = await pool.get_or_connect(Scope(tenant="acme"), make_server(stub.url))
            result = await connection.call_tool_text("lookup_order", {})
            await pool.close_all()

        assert result == "A1 shipped"


def _schema_of(tool: dict[str, object]) -> dict[str, object]:
    """A wire tool's input schema, or an empty one.

    The wire shape is `dict[str, object]`, so the schema arrives typed as
    `object` and cannot be handed to `dict()` without a check. Narrowing here
    rather than silencing it keeps the helper honest about a server that sends
    something other than an object for `inputSchema`.
    """
    schema = tool.get("inputSchema")
    return dict(schema) if isinstance(schema, dict) else {}


def catalogue_chars_of(wire_tools: list[dict[str, object]]) -> int:
    """What these wire-shaped tools would cost the prompt.

    The tests hold tools in the shape a server sends them; the library measures
    ``ToolDefinition``s. This converts between the two so a test can assert
    against the same number the resolver decides on, rather than against a
    figure it worked out separately.
    """
    return catalogue_chars(
        [
            ToolDefinition(
                name=str(tool["name"]),
                description=str(tool.get("description", "")),
                input_schema=_schema_of(tool),
            )
            for tool in wire_tools
        ]
    )


class TestDeferredDisclosure:
    """A catalogue too large to put in a prompt is discovered instead.

    The case this exists for is measured, not hypothetical: a production server
    this project connects to offers 351 tools whose schemas are 2.0 MB, sent on
    every turn of every Run when they are preloaded.
    """

    @staticmethod
    def _many(count: int) -> list[dict[str, object]]:
        """Many tools, each tiny. Roughly sixty characters apiece."""
        return [wire_tool(f"tool_{index:03d}", read_only=True) for index in range(count)]

    @staticmethod
    def _heavy(count: int, *, fields: int = 40) -> list[dict[str, object]]:
        """Few tools, each with a real schema. This is the shape a count-based
        rule got wrong: three of these outweigh two hundred of ``_many``."""
        tools: list[dict[str, object]] = []
        for index in range(count):
            tool = wire_tool(f"heavy_{index:03d}", read_only=True)
            tool["description"] = "A tool with a substantial schema. " * 12
            tool["inputSchema"] = {
                "type": "object",
                "properties": {
                    f"field_{n:02d}": {
                        "type": "string",
                        "description": (
                            f"Field {n} of this tool's input. Holds a valid identifier "
                            "for the resource the operation is performed against."
                        ),
                    }
                    for n in range(fields)
                },
                "required": ["field_00"],
            }
            tools.append(tool)
        return tools

    async def _resolve(
        self,
        transport: HttpTransport,
        server: McpServer,
        *,
        budget_chars: int | None = None,
    ) -> tuple[ResolvedTools, McpPool]:
        pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
        # Passed explicitly rather than splatted from a conditional dict: mypy
        # cannot narrow `**dict[str, int]` back onto named parameters, and
        # spelling the default out keeps the test honest about which value it
        # is exercising.
        resolver = ToolResolver(
            ToolRegistry(),
            catalog=McpTools(pool),
            catalogue_budget_chars=(
                DEFAULT_CATALOGUE_BUDGET_CHARS if budget_chars is None else budget_chars
            ),
        )
        spec = AgentSpec(name="agent", model=ModelRef(model="fake-standard"), mcp_servers=(server,))
        resolved = await resolver.resolve(spec, Scope(tenant="acme"))
        return resolved, pool

    async def test_a_catalogue_over_the_character_budget_is_deferred(
        self, transport: HttpTransport
    ) -> None:
        heavy = self._heavy(12)
        assert catalogue_chars_of(heavy) > DEFAULT_CATALOGUE_BUDGET_CHARS
        async with McpStubServer(heavy) as stub:
            resolved, pool = await self._resolve(transport, make_server(stub.url))
            await pool.close_all()

        names = {definition.name for definition in resolved.definitions}
        # None of the server's own tools, and the three that reach them.
        assert not any(name.startswith("heavy_") for name in names)
        assert {"list_tools", "get_tool_info", "call_tool"} <= names
        assert resolved.deferred_servers == ("support",)
        # And the model is told the server is there, or it will never look.
        assert any("not in your tool list" in advisory for advisory in resolved.advisories)

    async def test_many_tiny_tools_are_preloaded(self, transport: HttpTransport) -> None:
        """The case a tool *count* got backwards.

        Sixty trivial tools are about 3.5 KB all told, which is nothing, and
        the old rule deferred them for being sixty. Cost is what matters, so
        they are preloaded now.
        """
        tiny = self._many(60)
        assert catalogue_chars_of(tiny) < DEFAULT_CATALOGUE_BUDGET_CHARS
        async with McpStubServer(tiny) as stub:
            resolved, pool = await self._resolve(transport, make_server(stub.url))
            await pool.close_all()

        names = {definition.name for definition in resolved.definitions}
        assert len([name for name in names if name.startswith("tool_")]) == 60
        assert resolved.deferred_servers == ()
        assert "list_tools" not in names

    async def test_a_few_heavy_tools_outweigh_many_light_ones(
        self, transport: HttpTransport
    ) -> None:
        """The comparison stated directly: three tools can cost more than
        sixty, which is the whole argument for measuring rather than counting."""
        assert catalogue_chars_of(self._heavy(3)) > catalogue_chars_of(self._many(60))

    async def test_a_small_catalogue_is_still_preloaded(self, transport: HttpTransport) -> None:
        async with McpStubServer(self._many(3)) as stub:
            resolved, pool = await self._resolve(transport, make_server(stub.url))
            await pool.close_all()

        names = {definition.name for definition in resolved.definitions}
        assert names == {"tool_000", "tool_001", "tool_002"}
        assert resolved.deferred_servers == ()
        assert "list_tools" not in names

    async def test_the_budget_is_configurable(self, transport: HttpTransport) -> None:
        """A deployment with a tighter prompt budget defers sooner. Same
        server, same Spec, different Runtime."""
        tiny = self._many(60)
        async with McpStubServer(tiny) as stub:
            server = make_server(stub.url)
            generous, pool = await self._resolve(transport, server, budget_chars=1_000_000)
            await pool.close_all()
            mean, pool = await self._resolve(transport, server, budget_chars=100)
            await pool.close_all()

        assert generous.deferred_servers == ()
        assert mean.deferred_servers == ("support",)

    async def test_preload_true_overrides_the_size_rule(self, transport: HttpTransport) -> None:
        heavy = self._heavy(12)
        async with McpStubServer(heavy) as stub:
            server = McpServer(name="support", url=stub.url, preload=True)
            resolved, pool = await self._resolve(transport, server)
            await pool.close_all()

        assert len(resolved.definitions) == 12
        assert resolved.deferred_servers == ()

    async def test_preload_false_defers_a_catalogue_that_would_fit(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer(self._many(3)) as stub:
            server = McpServer(name="support", url=stub.url, preload=False)
            resolved, pool = await self._resolve(transport, server)
            await pool.close_all()

        assert resolved.deferred_servers == ("support",)
        assert not any(d.name.startswith("tool_") for d in resolved.definitions)

    async def test_discovery_lists_reads_and_calls_through_the_same_narrowing(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer(self._many(60)) as stub:
            stub.call_handlers["tool_007"] = lambda _args: ("did the thing", False)
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool)
            scope = Scope(tenant="acme")
            # An allow-list excluding most of the catalogue: discovery must
            # respect it, because showing a model a tool it cannot call only
            # teaches it to ask for something that will be refused.
            server = make_server(stub.url, allow=("tool_007", "tool_008"))
            spec = AgentSpec(
                name="agent", model=ModelRef(model="fake-standard"), mcp_servers=(server,)
            )
            discovery = DeferredDiscovery(mcp, scope)

            listed = await discovery.call(spec, "list_tools", {"server": "support"})
            info = await discovery.call(
                spec, "get_tool_info", {"server": "support", "tool": "tool_007"}
            )
            called = await discovery.call(
                spec, "call_tool", {"server": "support", "tool": "tool_007", "arguments": {}}
            )
            with pytest.raises(AccessDenied):
                await discovery.call(
                    spec, "get_tool_info", {"server": "support", "tool": "tool_009"}
                )
            with pytest.raises(AccessDenied):
                await discovery.call(spec, "list_tools", {"server": "not-connected"})
            await pool.close_all()

        assert [entry["name"] for entry in listed["tools"]] == ["tool_007", "tool_008"]
        assert listed["total"] == 2
        assert info["name"] == "tool_007"
        assert info["input_schema"] == {"type": "object", "properties": {}}
        assert called == "did the thing"

    async def test_a_call_the_allow_list_excludes_is_refused_by_the_caller(
        self, transport: HttpTransport
    ) -> None:
        """Discovery is a door, not a bypass: call_tool goes through the same
        narrowing an ordinary tool call does."""
        async with McpStubServer(self._many(60)) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="acme")
            server = make_server(stub.url, allow=("tool_007",))
            spec = AgentSpec(
                name="agent", model=ModelRef(model="fake-standard"), mcp_servers=(server,)
            )
            discovery = DeferredDiscovery(McpTools(pool), scope)
            with pytest.raises(AccessDenied):
                await discovery.call(
                    spec, "call_tool", {"server": "support", "tool": "tool_009", "arguments": {}}
                )
            await pool.close_all()


class TestServerDescriptions:
    """A model choosing between three connected servers needs to know what
    they are. `eq-admin`, `deepwiki` and `crm` are three names and nothing
    else, and with a deferred catalogue the tools are not there to explain
    them either.

    The rule that shapes this: a description is runtime data, never Spec data.
    """

    async def test_describing_a_server_does_not_move_the_version_hash(
        self, transport: HttpTransport
    ) -> None:
        """The whole reason this is not a field on `McpServer`.

        A description changes for reasons that have nothing to do with the
        agent: someone writes a better one, the server grows a new area. If it
        lived in the Spec, editing it would republish every agent that names
        the server, and a Version would claim the agent had changed when it
        had not.
        """
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            server = make_server(stub.url)
            spec = AgentSpec(
                name="agent", model=ModelRef(model="fake-standard"), mcp_servers=(server,)
            )
            before = publish_version(spec).hash

            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            described = McpTools(pool, describe_server=lambda _sc, _s: "The order system.")
            resolver = ToolResolver(ToolRegistry(), catalog=described)
            resolved = await resolver.resolve(spec, Scope(tenant="acme"))
            await pool.close_all()

        after = publish_version(spec).hash
        assert before == after, "describing a connection must not republish the agent"
        assert any("The order system." in advisory for advisory in resolved.advisories)

    async def test_the_servers_own_instructions_are_used_when_nobody_described_it(
        self, transport: HttpTransport
    ) -> None:
        """MCP servers may describe themselves at the handshake. This client
        used to drop that field on the floor."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            stub.instructions = "Reads documentation for public repositories."
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool)
            scope = Scope(tenant="acme")
            server = make_server(stub.url)
            # Connect first: a description is read from the pool, never by
            # opening a connection of its own.
            await mcp.tools_for(scope, server)
            described = await mcp.describe(scope, server)
            await pool.close_all()

        assert described == "Reads documentation for public repositories."

    async def test_the_consumer_wins_over_the_server(self, transport: HttpTransport) -> None:
        """Whoever wired the connection knows their deployment; the server's
        author knows the server. When both have an opinion the deployment's
        wins."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            stub.instructions = "Generic upstream blurb."
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool, describe_server=lambda _sc, _s: "Our order system.")
            scope = Scope(tenant="acme")
            server = make_server(stub.url)
            await mcp.tools_for(scope, server)
            described = await mcp.describe(scope, server)
            await pool.close_all()

        assert described == "Our order system."

    async def test_two_tenants_naming_one_url_get_their_own_description(
        self, transport: HttpTransport
    ) -> None:
        """The reason the hook takes a Scope.

        A description is a fact about a *connection*, and a connection is per
        tenant. Two tenants can point at the same URL and mean different things
        by it, so a hook keyed on the server alone would put one tenant's words
        into the other's system prompt. Nothing about that is hypothetical: a
        console where each account configures its own servers is the ordinary
        case.
        """
        # Only one of the two tenants has described this server, which also
        # covers the fallback: the other must reach the server's own words
        # rather than borrow the words the first tenant wrote.
        said: dict[str, str] = {"acme": "Our order system."}

        def describe(scope: Scope, _server: McpServer) -> str | None:
            return said.get(scope.tenant)

        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            stub.instructions = "Generic upstream blurb."
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool, describe_server=describe)
            server = make_server(stub.url)
            acme, globex = Scope(tenant="acme"), Scope(tenant="globex")
            await mcp.tools_for(acme, server)
            await mcp.tools_for(globex, server)
            for_acme = await mcp.describe(acme, server)
            for_globex = await mcp.describe(globex, server)
            await pool.close_all()

        assert for_acme == "Our order system."
        assert for_globex == "Generic upstream blurb."

    async def test_no_description_anywhere_produces_no_line_and_no_error(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool)
            scope = Scope(tenant="acme")
            server = make_server(stub.url)
            await mcp.tools_for(scope, server)
            described = await mcp.describe(scope, server)
            resolver = ToolResolver(ToolRegistry(), catalog=mcp)
            spec = AgentSpec(
                name="agent", model=ModelRef(model="fake-standard"), mcp_servers=(server,)
            )
            resolved = await resolver.resolve(spec, Scope(tenant="acme"))
            await pool.close_all()

        assert described is None
        # A small preloaded server with nothing to say adds no block at all:
        # the tool list already tells the model everything it would.
        assert resolved.advisories == ()

    async def test_a_preloaded_server_is_described_too(self, transport: HttpTransport) -> None:
        """The argument for a description does not depend on deferral. An
        agent with three small servers still has to tell them apart."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool, describe_server=lambda _sc, _s: "Customer records.")
            resolver = ToolResolver(ToolRegistry(), catalog=mcp)
            spec = AgentSpec(
                name="agent",
                model=ModelRef(model="fake-standard"),
                mcp_servers=(make_server(stub.url),),
            )
            resolved = await resolver.resolve(spec, Scope(tenant="acme"))
            await pool.close_all()

        assert {d.name for d in resolved.definitions} == {"lookup"}
        block = "\n".join(resolved.advisories)
        assert "Customer records." in block
        assert "in your tool list" in block

    async def test_a_description_is_capped_and_collapsed(self) -> None:
        long = "Sentence about the server. " * 100
        capped = summarise_description(long)
        assert capped is not None
        assert len(capped) <= MAX_SERVER_DESCRIPTION_CHARS + 1  # the ellipsis
        assert summarise_description("  spread\n  over lines  ") == "spread over lines"
        assert summarise_description("   ") is None
        assert summarise_description(None) is None

    async def test_a_consumer_lookup_that_raises_does_not_fail_the_turn(
        self, transport: HttpTransport
    ) -> None:
        """A description is decoration on a prompt. Failing a Run over one
        would be absurd, and a consumer's own lookup is the most likely thing
        in this path to be wrong."""

        def explode(_scope: Scope, _server: McpServer) -> str | None:
            raise RuntimeError("the description lookup is broken")

        async with McpStubServer([wire_tool("lookup", read_only=True)]) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            resolver = ToolResolver(ToolRegistry(), catalog=McpTools(pool, describe_server=explode))
            spec = AgentSpec(
                name="agent",
                model=ModelRef(model="fake-standard"),
                mcp_servers=(make_server(stub.url),),
            )
            resolved = await resolver.resolve(spec, Scope(tenant="acme"))
            await pool.close_all()

        assert {d.name for d in resolved.definitions} == {"lookup"}
