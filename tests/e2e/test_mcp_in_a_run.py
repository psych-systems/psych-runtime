"""An MCP tool, reached by a model, inside a real Run.

This is the case nothing covered. Every MCP test drove ``McpPool`` directly and
every agent-loop test used code tools, so the wire between them could be absent
entirely and the suite stayed green. It was absent: nothing ever passed a
``catalog=`` to the resolver or an ``mcp_caller`` to the executor, so a Spec
could declare ``mcp_servers`` and the model would simply never be offered those
tools.

The server here is the same real ``127.0.0.1`` stub ``tests/functional/test_mcp.py``
uses: real sockets, real JSON-RPC, no mock. The model is the scriptable fake,
because the point under test is the tool path rather than the provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.records import TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.oauth import OAuthClient
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.secrets import InMemorySecretResolver

# _AuthServerStub and _protect stayed behind: they are an OAuth authorization
# server, not an MCP one, and only the runtime's own suite drives them.
from tests.functional.test_mcp import _AuthServerStub, _protect

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-42")


@pytest_asyncio.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    http_transport = HttpTransport()
    try:
        yield http_transport
    finally:
        await http_transport.aclose()


async def _run(
    spec: psych_runtime.AgentSpec, model: FakeModel, mcp: McpTools, scope: Scope = SCOPE
) -> psych_runtime.RunReport:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    runtime = Runtime(store=store, model=model, registry=ToolRegistry(), mcp=mcp)
    worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version, scope, input={"message": "go"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
        return await psych_runtime.report(store, run.run_id)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)


def _spec(
    url: str, *, allow: tuple[str, ...] = (), optional: bool = False
) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="use the tools",
        model=psych_runtime.ModelRef(model="fake-standard"),
        mcp_servers=(make_server(url, allow=allow, optional=optional),),
        limits=psych_runtime.Limits(max_turns=6, deadline_seconds=60),
    )


class TestAnMcpToolInsideARun:
    async def test_the_model_is_offered_the_server_tools_and_can_call_one(
        self, transport: HttpTransport
    ) -> None:
        """The headline: publish a Spec naming a server, and the model both
        sees its tools and executes one."""
        async with McpStubServer([wire_tool("lookup_order")]) as stub:
            stub.call_handlers["lookup_order"] = lambda args: (
                f"order {args.get('order_id')} shipped",
                False,
            )
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())

            model = (
                FakeModel()
                .turn(tool_calls=[("lookup_order", {"order_id": "A1"})])
                .turn(text="It shipped.")
            )

            report = await _run(_spec(stub.url), model, McpTools(pool))
            # Every ModelRequest the fake was handed, so this asserts the tool
            # was in the set the model chose from rather than inferring it from
            # the fact that the call succeeded.
            offered = [tool.name for request in model.requests for tool in request.tools]
            await pool.close_all()

        assert report.terminal_state is TerminalState.COMPLETED
        assert "lookup_order" in offered, "the server's tools never reached the model"
        settled = [(c.tool, c.outcome.value if c.outcome else None) for c in report.tool_calls]
        assert settled == [("lookup_order", "ok")]

    async def test_a_tool_the_spec_excluded_is_refused_even_when_named_directly(
        self, transport: HttpTransport
    ) -> None:
        """The control that matters.

        The resolver filtering the offered list is not a control, because the
        model chooses the name it sends. A caller that looked a name up across
        connected servers and invoked whatever answered would let a model reach
        an excluded tool by naming it. So the invocation path narrows again.
        """
        async with McpStubServer([wire_tool("lookup_order"), wire_tool("delete_account")]) as stub:
            called: list[str] = []

            def handler(name: str) -> Callable[[dict[str, object]], tuple[str, bool]]:
                def run(_args: dict[str, object]) -> tuple[str, bool]:
                    called.append(name)
                    return ("done", False)

                return run

            stub.call_handlers["lookup_order"] = handler("lookup_order")
            stub.call_handlers["delete_account"] = handler("delete_account")
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())

            # The Spec grants only lookup_order. The model names the other one.
            model = (
                FakeModel().turn(tool_calls=[("delete_account", {})]).turn(text="Sorry about that.")
            )

            report = await _run(_spec(stub.url, allow=("lookup_order",)), model, McpTools(pool))
            await pool.close_all()

        assert called == [], f"an excluded tool was executed on the server: {called}"
        outcome = report.tool_calls[0].outcome
        assert outcome is None or outcome.value != "ok"

    async def test_two_tenants_do_not_share_a_connection_for_the_same_server(
        self, transport: HttpTransport
    ) -> None:
        """Tenancy rides on the Scope through the pool key, so a name is
        resolved only against the calling Scope's own connections."""
        async with McpStubServer([wire_tool("lookup_order")]) as stub:
            stub.call_handlers["lookup_order"] = lambda _args: ("ok", False)
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool)

            def script() -> FakeModel:
                return FakeModel().turn(tool_calls=[("lookup_order", {})]).turn(text="done")

            first = await _run(_spec(stub.url), script(), mcp, Scope(tenant="tenant-a"))
            second = await _run(_spec(stub.url), script(), mcp, Scope(tenant="tenant-b"))
            keys = list(pool._connections)
            await pool.close_all()

        assert first.terminal_state is TerminalState.COMPLETED
        assert second.terminal_state is TerminalState.COMPLETED
        tenants = {key.tenant for key in keys}
        assert tenants == {"tenant-a", "tenant-b"}, (
            f"two tenants shared one pooled connection: {keys}"
        )

    async def test_an_unreachable_required_server_fails_the_run(
        self, transport: HttpTransport
    ) -> None:
        """DESIGN.md §10.7: silent disappearance produces an agent that
        confidently tells a customer it cannot issue refunds today."""
        pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
        model = FakeModel().turn(text="hello")

        report = await _run(_spec("http://127.0.0.1:9/mcp"), model, McpTools(pool))
        await pool.close_all()

        assert report.terminal_state is TerminalState.FAILED
        # The settlement names the server and the reason, written by the loop
        # itself rather than escaping to the Worker as a generic "the attempt
        # raised" whose only diagnostic was a line on the Worker's stdout.
        assert report.failure is not None
        assert report.failure.kind == "tool_resolution"
        assert "'support'" in report.failure.message
        assert report.failure.traceback is not None
        assert "McpServerUnreachable" in report.failure.traceback
        # No model call was made: the failure is settled before the turn opens.
        assert report.totals.model_calls == 0

    async def test_an_unreachable_optional_server_lets_the_run_continue(
        self, transport: HttpTransport
    ) -> None:
        pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
        model = FakeModel().turn(text="I could not reach that system.")

        report = await _run(_spec("http://127.0.0.1:9/mcp", optional=True), model, McpTools(pool))
        await pool.close_all()

        assert report.terminal_state is TerminalState.COMPLETED


class TestAnOAuthProtectedServerInsideARun:
    """The shape a consumer integrating a hosted MCP server actually has.

    Everything below is real over loopback: a resource server that returns a
    401 with a ``WWW-Authenticate`` challenge, an authorization server serving
    RFC 8414 metadata and a token endpoint, and a Run that ends up calling a
    tool with a bearer token it obtained itself. The pieces were each tested
    alone; this is the first time the whole chain runs inside a Run.
    """

    async def test_a_run_calls_a_tool_behind_client_credentials_oauth(
        self, transport: HttpTransport
    ) -> None:
        async with (
            McpStubServer([wire_tool("search_products")], modern=True) as stub,
            _AuthServerStub() as auth,
        ):
            _protect(stub, auth)
            stub.call_handlers["search_products"] = lambda args: (
                f"3 results for {args.get('query')}",
                False,
            )
            secrets = InMemorySecretResolver()
            secrets.set(SCOPE, "shop-secret", "s3cr3t")
            pool = McpPool(
                transport=transport, secrets=secrets, oauth=OAuthClient(transport=transport)
            )

            spec = psych_runtime.AgentSpec(
                name="shopper",
                instructions="search the catalogue",
                model=psych_runtime.ModelRef(model="fake-standard"),
                mcp_servers=(
                    make_server(
                        stub.url,
                        oauth=psych_runtime.McpOAuth(
                            grant="client_credentials",
                            preregistered_client_id="shop-client",
                            client_secret_credential="shop-secret",
                        ),
                    ),
                ),
                limits=psych_runtime.Limits(max_turns=6, deadline_seconds=60),
            )
            model = (
                FakeModel()
                .turn(tool_calls=[("search_products", {"query": "socks"})])
                .turn(text="Found three.")
            )

            report = await _run(spec, model, McpTools(pool))
            token_grants = [request.get("grant_type") for request in auth.token_requests]
            await pool.close_all()

        assert report.terminal_state is TerminalState.COMPLETED
        settled = [(c.tool, c.outcome.value if c.outcome else None) for c in report.tool_calls]
        assert settled == [("search_products", "ok")]
        assert "client_credentials" in token_grants, (
            f"the client-credentials grant was never exercised: {token_grants}"
        )
        # A pre-registered client id means no Dynamic Client Registration, which
        # the spec deprecates and keeps only for servers without CIMD.
        assert auth.registrations == 0
