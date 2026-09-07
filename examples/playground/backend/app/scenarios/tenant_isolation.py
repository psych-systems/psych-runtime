"""DESIGN.md §23 item 6, isolation half: two tenants, one server URL, no
shared connection and no shared credential.

Pooling an MCP client by URL alone is the isolation bug that
ends the project": it eventually sends one tenant's OAuth token on another
tenant's call. ``McpPool`` keys by ``(scope, server, credential)`` instead
(DESIGN.md §10.4), and this scenario proves it against the wire rather than
against internal state -- two Scopes, each with its own credential for the
same server ``credential`` name, calling the same tool through the same
pool, and the *stub server itself* reporting which bearer token arrived on
which call.

Reuses the real loopback stub from ``tests/functional/test_mcp.py`` --
real sockets, real JSON-RPC, no mock -- the same server
``tests/e2e/test_mcp_in_a_run.py`` already reuses for the runtime's own
tenancy test.
"""

from __future__ import annotations

from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool
from psych_runtime.tools.secrets import InMemorySecretResolver

INFO = ScenarioInfo(
    id="tenant-isolation",
    title="Tenant isolation",
    proves=(
        "Two tenants calling the same MCP server URL through the same pool "
        "never share a connection or a credential: the server itself sees a "
        "distinct handshake per tenant and the exact token each tenant "
        "configured, never the other tenant's."
    ),
    design_ref="§23.6",
    requires=(),
)


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    transport = HttpTransport()
    try:
        async with McpStubServer([wire_tool("echo")]) as stub:
            secrets = InMemorySecretResolver()
            scope_a = Scope(tenant="tenant-a", principal="alice")
            scope_b = Scope(tenant="tenant-b", principal="bob")
            secrets.set(scope_a, "api_key", "token-for-tenant-a")
            secrets.set(scope_b, "api_key", "token-for-tenant-b")

            pool = McpPool(transport=transport, secrets=secrets)
            server = make_server(stub.url, credential="api_key")

            await emit("connect", "connecting both tenants to the same server URL")
            connection_a = await pool.get_or_connect(scope_a, server)
            connection_b = await pool.get_or_connect(scope_b, server)

            checks.require(
                "the two tenants got two distinct connection objects, never one shared",
                connection_a is not connection_b,
                f"connection_a is connection_b -> {connection_a is connection_b}",
            )
            checks.require(
                "the server itself saw two separate handshakes, one per tenant",
                stub.init_count == 2,
                f"stub.init_count={stub.init_count}",
            )

            await emit("call", "tenant A calls twice, tenant B calls once, over the same pool")
            await connection_a.call_tool("echo", {})
            await connection_a.call_tool("echo", {})
            await connection_b.call_tool("echo", {})

            tool_calls = [c for c in stub.received_calls if c.tool == "echo"]
            observed = [c.authorization for c in tool_calls]
            expected = [
                "Bearer token-for-tenant-a",
                "Bearer token-for-tenant-a",
                "Bearer token-for-tenant-b",
            ]
            checks.require(
                "the credential the server actually received matches the calling tenant, "
                "every time, in the order the calls were made",
                observed == expected,
                f"observed authorization headers={observed}",
            )

            pool_tenants = {key.tenant for key in pool._connections}
            checks.require(
                "the pool's own key space keeps the two tenants apart",
                pool_tenants == {"tenant-a", "tenant-b"},
                f"pool key tenants={sorted(pool_tenants)}",
            )

            await pool.close_all()
            return checks.result(
                "one server URL, two tenants, two connections, two credentials -- "
                "verified against what the server itself received on the wire",
                run_ids=[],
                report={
                    "stub_init_count": stub.init_count,
                    "authorization_headers_received": observed,
                },
            )
    finally:
        await transport.aclose()
