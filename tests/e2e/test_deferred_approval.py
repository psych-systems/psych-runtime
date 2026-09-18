"""A deferred server's tools are approval-gated exactly like preloaded ones.

The model reaches a deferred catalogue through ``call_tool``, whose own
definition is hard-coded ``write``. Gating on that wrapper meant a
``@destructive`` selector that suspended a preloaded tool let the very same
tool run once its server's catalogue grew past the deferral budget -- and
deferral is automatic above ``DEFAULT_CATALOGUE_BUDGET_CHARS``, so a server
growing silently switched approval off. The gate now classifies ``call_tool``
by the target it names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.records import SuspendReason
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, Limits, McpServer, ModelRef
from psych_runtime.model.egress import HttpTransport
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.policy import AllowAll, Decision
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.secrets import InMemorySecretResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-42")


@pytest_asyncio.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    http_transport = HttpTransport()
    try:
        yield http_transport
    finally:
        await http_transport.aclose()


def _spec(url: str, *, preload: bool | None) -> AgentSpec:
    return AgentSpec(
        name="support",
        instructions="use the tools",
        model=ModelRef(model="fake-standard"),
        mcp_servers=(McpServer(name="support", url=url, preload=preload),),
        limits=Limits(max_turns=4, deadline_seconds=60),
    )


async def _run(
    store: InMemoryStore, spec: AgentSpec, model: FakeModel, mcp: McpTools, **kwargs: Any
) -> Any:
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
    journal = await Journal.open(store, dispatched.run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=ToolRegistry(), mcp=mcp, **kwargs)
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return await psych_runtime.state(store, dispatched.run_id)


class TestDeferredCatalogueApproval:
    async def test_a_destructive_deferred_tool_suspends_for_approval(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("wipe", destructive=True)]) as server:
            server.call_handlers["wipe"] = lambda _args: ("wiped", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = FakeModel().turn(
                tool_calls=[("call_tool", {"server": "support", "tool": "wipe", "arguments": {}})]
            )
            state = await _run(
                InMemoryStore(),
                _spec(server.url, preload=False),
                model,
                mcp,
                approval_selectors=("@destructive",),
            )
            assert state.suspended
            assert state.suspend_reason is SuspendReason.APPROVAL
            assert server.received_calls == [], "the tool must not run before a decision"

        offered = {tool.name for tool in model.requests[0].tools}
        assert "support__wipe" not in offered, "the catalogue should have been deferred"

    async def test_a_read_only_deferred_tool_runs_without_asking(
        self, transport: HttpTransport
    ) -> None:
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("found", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = (
                FakeModel()
                .turn(
                    tool_calls=[
                        ("call_tool", {"server": "support", "tool": "lookup", "arguments": {}})
                    ]
                )
                .turn(text="done")
            )
            state = await _run(
                InMemoryStore(),
                _spec(server.url, preload=False),
                model,
                mcp,
                approval_selectors=("@destructive",),
            )
            assert not state.suspended
            assert [call.tool for call in server.received_calls] == ["lookup"]

    async def test_the_policy_is_asked_about_the_target_not_the_wrapper(
        self, transport: HttpTransport
    ) -> None:
        """A name-keyed Policy rule written against the tool as a preloaded
        catalogue would show it has to match through the wrapper too."""
        asked: list[str] = []

        class Recording(AllowAll):
            async def allow_tool(
                self, scope: Scope, tool: str, arguments: dict[str, Any]
            ) -> Decision:
                asked.append(tool)
                return await super().allow_tool(scope, tool, arguments)

        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("found", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = (
                FakeModel()
                .turn(
                    tool_calls=[
                        ("call_tool", {"server": "support", "tool": "lookup", "arguments": {}})
                    ]
                )
                .turn(text="done")
            )
            await _run(
                InMemoryStore(), _spec(server.url, preload=False), model, mcp, policy=Recording()
            )
        assert asked == ["support__lookup"]

    async def test_an_unresolvable_target_fails_closed(self, transport: HttpTransport) -> None:
        """A tool the Run cannot see is graded destructive rather than inheriting
        the wrapper's ``write``."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = FakeModel().turn(
                tool_calls=[
                    ("call_tool", {"server": "support", "tool": "missing", "arguments": {}})
                ]
            )
            state = await _run(
                InMemoryStore(),
                _spec(server.url, preload=False),
                model,
                mcp,
                approval_selectors=("@destructive",),
            )
            assert state.suspended
            assert server.received_calls == []
