"""DESIGN.md §23 item 6, connection half: an MCP server is usable the moment
a live Run's resolver first reaches for it, with no pre-warming and no
Worker restart.

``psych_runtime.tools.resolver`` resolves the tool set fresh every turn (its own
docstring: "Connect an MCP server and the next turn has its tools, with no
restart"), which means the very first turn of a Run naming an MCP server
already needs the connection -- there is no separate "connect" step a
consumer runs ahead of time. This scenario proves the property that
actually matters: the pool holds nothing for this server before the Run is
even dispatched (no pre-warming at boot, no pre-warming at publish), the
same already-running Worker that has never heard of this server connects to
it the moment this Run's first turn asks, and the connection is reused --
not re-established -- across every later turn of the same Run.

The server is the same real ``127.0.0.1`` stub
``tests/functional/test_mcp.py`` uses for the runtime's own MCP suite --
real sockets, real JSON-RPC, no mock -- reused here rather than
reimplemented, the way ``tests/e2e/test_mcp_in_a_run.py`` already reuses it
for the equivalent test.
"""

from __future__ import annotations

import asyncio

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.records import TerminalState
from psych_runtime.model.egress import HttpTransport
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.secrets import InMemorySecretResolver

INFO = ScenarioInfo(
    id="mcp-mid-run",
    title="MCP server connected mid-Run",
    proves=(
        "An MCP server is usable the moment a Run's resolver first reaches "
        "for it: nothing is pre-connected before the Run is dispatched, the "
        "already-running Worker connects to a server it has never seen the "
        "instant a turn needs it, and reuses that one connection -- never "
        "reconnecting -- across every later turn, with no restart anywhere."
    ),
    design_ref="§23.6",
    requires=(),
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def note(text: str) -> str:
        """Jot down a note before checking anything external."""
        return f"noted: {text}"

    return registry


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    transport = HttpTransport()
    try:
        async with McpStubServer([wire_tool("lookup_order")]) as stub:
            stub.call_handlers["lookup_order"] = lambda args: (
                f"order {args.get('order_id')} shipped",
                False,
            )
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            checks.require(
                "the pool holds no connection before any Run naming this server exists",
                len(pool._connections) == 0,
                f"pool already had {len(pool._connections)} connection(s) before dispatch",
            )

            store = InMemoryStore()
            spec = psych_runtime.AgentSpec(
                name="support",
                instructions="Note what you're doing, then use the tools.",
                model=psych_runtime.ModelRef(model="fake-standard"),
                tools=(psych_runtime.CodeTool(name="note"),),
                mcp_servers=(make_server(stub.url, allow=("lookup_order",)),),
                limits=psych_runtime.Limits(max_turns=6, deadline_seconds=60),
            )
            model = (
                FakeModel()
                .turn(tool_calls=[("note", {"text": "checking on this order"})])
                .turn(tool_calls=[("lookup_order", {"order_id": "A1"})])
                .turn(text="It shipped.")
            )
            version = await psych_runtime.publish(store, spec)
            # The same already-running Worker; it was never told about this
            # server ahead of time, and nothing about starting it connected
            # anything -- see the assertion above, taken before dispatch.
            runtime = Runtime(store=store, model=model, registry=_registry(), mcp=McpTools(pool))
            worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)

            await emit("dispatch", "dispatching a Run naming a server the pool has never touched")
            dispatched = await psych_runtime.dispatch(
                store, version, SCOPE_A, input={"message": "go"}
            )

            worker_task = asyncio.create_task(worker.run())
            try:
                async for _record in psych_runtime.stream(store, dispatched.run_id):
                    pass
            finally:
                worker.stop()
                await asyncio.wait_for(worker_task, timeout=10)

            checks.require(
                "the pool connected to the server -- lazily, the moment this Run's own "
                "resolver first needed it, with nothing pre-warmed",
                len(pool._connections) == 1,
                f"pool now holds {len(pool._connections)} connection(s)",
            )
            checks.require(
                "the server saw exactly one handshake -- reused across every turn, "
                "never reconnected turn to turn",
                stub.init_count == 1,
                f"stub.init_count={stub.init_count} across {len(model.requests)} turns",
            )

            first_turn_tools = {tool.name for tool in model.requests[0].tools}
            checks.require(
                "the server's tool was already on offer on the very first turn -- no "
                "restart, no republish, no separate connect step was needed",
                "lookup_order" in first_turn_tools,
                f"turn 1 tools: {sorted(first_turn_tools)}",
            )

            report = await psych_runtime.report(store, dispatched.run_id)
            terminal = report.terminal_state.value if report.terminal_state else None
            checks.require(
                "the Run completed, tool call and all, on the very Worker that started it",
                report.terminal_state is TerminalState.COMPLETED,
                f"terminal_state={terminal!r}",
            )
            settled = [(c.tool, c.outcome.value if c.outcome else None) for c in report.tool_calls]
            checks.require(
                "both calls -- the code tool and the MCP tool -- settled ok",
                settled == [("note", "ok"), ("lookup_order", "ok")],
                f"tool_calls={settled}",
            )

            log = await psych_runtime.records(store, dispatched.run_id)
            await pool.close_all()
            return checks.result(
                "the MCP server was connected to lazily, entirely inside this Run's own "
                "lifetime, reused rather than reconnected, with no restart anywhere",
                run_ids=[str(dispatched.run_id)],
                report={"report": report_dict(report), "records": records_dict(log)},
            )
    finally:
        await transport.aclose()
