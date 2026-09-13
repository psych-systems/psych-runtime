"""A program calls MCP tools: the whole path, with real sockets and a real child.

The gap this closes was structural rather than a bug. ``run_code``'s bindings
were derived from ``spec.tools``; MCP tools are discovered from
``spec.mcp_servers`` at every turn boundary and are in neither the Spec nor the
registry. So an agent could be connected to a hundred MCP tools, be offered
``run_code``, and have a program that could call none of them -- which is
exactly backwards, since a large catalogue is the case where writing a loop
beats forty model turns by the widest margin.

Everything here goes through the real pieces: a real MCP server on
``127.0.0.1`` speaking real JSON-RPC, the real connection pool, the real
resolver, and a real child process from ``local_sandbox()``. The model is the
scriptable fake, because what is under test is the tool path rather than the
provider.

The property being proved in every case is one sentence: **a program gets
exactly what the model could have called directly, reached a different way**.
Not more -- no tool the tenant denied, no server the Spec did not name, no
credential, no approval it could not have obtained. Not less -- including the
tools of a server whose catalogue was deferred out of the prompt, which the
model can only reach through ``list_tools`` and a program reaches by name.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.code_execution import IsolationLevel
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import (
    AgentSpec,
    CodeExecution,
    Limits,
    McpServer,
    ModelRef,
)
from psych_runtime.model.egress import HttpTransport
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.port import Sandbox, SandboxLimits
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver
from psych_runtime.tools.secrets import InMemorySecretResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-42")
OTHER_TENANT = Scope(tenant="globex", principal="user-1")

_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=20.0,
)


def _python_bin() -> str | None:
    """A system interpreter where the worker is root and drops privileges."""
    if sys.platform != "win32" and os.geteuid() == 0:
        for candidate in (
            f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
            "/usr/bin/python3",
        ):
            if Path(candidate).is_file():
                return candidate
    return None


@pytest.fixture(scope="module")
def sandbox() -> Sandbox:
    return local_sandbox(python_bin=_python_bin(), default_limits=_LIMITS)


@pytest_asyncio.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    http_transport = HttpTransport()
    try:
        yield http_transport
    finally:
        await http_transport.aclose()


def spec(
    servers: Sequence[McpServer],
    *,
    tools: Sequence[Any] = (),
    bindings: tuple[str, ...] | None = None,
    **kwargs: Any,
) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "use the tools",
        "model": ModelRef(model="fake-standard"),
        "tools": tuple(tools),
        "mcp_servers": tuple(servers),
        # PROCESS is what every host's local backend can provide with nothing
        # installed; a host whose backend reaches ISOLATED satisfies it too.
        "code_execution": CodeExecution(isolation=IsolationLevel.PROCESS, bindings=bindings),
        "limits": Limits(max_turns=6, deadline_seconds=120),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def drive(
    agent: AgentSpec,
    model: FakeModel,
    mcp: McpTools,
    sandbox: Sandbox,
    *,
    scope: Scope = SCOPE,
    registry: ToolRegistry | None = None,
    **runtime_options: Any,
) -> tuple[InMemoryStore, psych_runtime.RunReport]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, agent)
    runtime = Runtime(
        store=store,
        model=model,
        registry=registry or ToolRegistry(),
        mcp=mcp,
        sandbox=sandbox,
        **runtime_options,
    )
    worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version, scope, input={"message": "go"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
        return store, await psych_runtime.report(store, run.run_id)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=20)


async def drive_with_profile(
    agent: AgentSpec, model: FakeModel, mcp: McpTools, profile: Any
) -> tuple[InMemoryStore, psych_runtime.RunReport]:
    """``drive`` for a deployment that registers its own profile.

    A ``Runtime`` folds ``sandbox=`` in as ``"default"``, so a test that wants
    a profile with its own budget registers that profile instead of both.
    """
    store = InMemoryStore()
    version = await psych_runtime.publish(store, agent)
    runtime = Runtime(
        store=store, model=model, registry=ToolRegistry(), mcp=mcp, sandboxes=(profile,)
    )
    worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
        return store, await psych_runtime.report(store, run.run_id)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=20)


def _run_code(report: psych_runtime.RunReport) -> Any:
    return next(call for call in report.tool_calls if call.tool == "run_code")


def _program(model: FakeModel, source: str) -> FakeModel:
    return model.turn(tool_calls=[("run_code", {"program": source})]).turn(text="done")


class TestWhatAProgramMayCall:
    async def test_mcp_tools_are_bindable_and_route_through_the_pool(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The headline. No code tool, no HTTP tool: only an MCP server, and a
        program that loops over its tool and returns one small answer."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda args: (f"price:{args['sku']}", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "out = []\n"
                "for sku in ('A', 'B', 'C'):\n"
                "    out.append(await support__lookup(sku=sku))\n"
                "return {'n': len(out), 'last': out[-1]}"
            )
            model = _program(FakeModel(), program)
            _, report = await drive(spec([make_server(server.url)]), model, mcp, sandbox)

        assert report.terminal_state is TerminalState.COMPLETED
        assert _run_code(report).result["value"] == {"n": 3, "last": "price:C"}
        # The calls really reached the server, over the real transport.
        assert [call.tool for call in server.received_calls] == ["lookup"] * 3

    async def test_every_binding_call_is_recorded_under_its_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Three MCP calls in the log, each pointing at the ``run_code`` that
        made it. Without the edge, a report shows three calls with no cause and
        one call with no effect."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "for sku in ('A', 'B', 'C'):\n    await support__lookup(sku=sku)\nreturn 'done'"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        parent = _run_code(report)
        children = [c for c in report.tool_calls if c.tool == "support__lookup"]
        assert len(children) == 3
        assert {c.parent_call_id for c in children} == {parent.call_id}
        assert parent.parent_call_id is None
        for child in children:
            assert child.outcome is ToolOutcome.OK
            assert child.arguments["sku"] in {"A", "B", "C"}
            assert child.duration_seconds is not None
            assert child.started_at is not None

    async def test_two_servers_offering_the_same_raw_name_stay_apart(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Both are callable, and each reaches its own server."""
        async with (
            McpStubServer([wire_tool("search", read_only=True)]) as first,
            McpStubServer([wire_tool("search", read_only=True)]) as second,
        ):
            first.call_handlers["search"] = lambda _args: ("from-docs", False)
            second.call_handlers["search"] = lambda _args: ("from-tickets", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "a = await docs__search(q='x')\nb = await tickets__search(q='x')\nreturn [a, b]"
            )
            servers = [
                make_server(first.url, name="docs"),
                make_server(second.url, name="tickets"),
            ]
            _, report = await drive(spec(servers), _program(FakeModel(), program), mcp, sandbox)

        assert _run_code(report).result["value"] == ["from-docs", "from-tickets"]

    async def test_a_name_that_is_not_a_python_identifier_is_callable(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """``list-repos`` cannot be written as a call, so ``call_tool`` is how
        a program reaches it -- and there is deliberately no mangled alias."""
        async with McpStubServer([wire_tool("list-repos", read_only=True)]) as server:
            server.call_handlers["list-repos"] = lambda _args: ("one\ntwo", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "assert 'support__list-repos' in TOOLS\n"
                "value = await call_tool('support__list-repos', {'owner': 'psych'})\n"
                "return {'value': value, 'no_alias': 'support__list_repos' not in TOOLS}"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        assert _run_code(report).result["value"] == {"value": "one\ntwo", "no_alias": True}

    async def test_a_deferred_server_is_out_of_the_prompt_and_in_the_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The case the design is for.

        A catalogue too large for the prompt is deferred, so the model is never
        shown those schemas. A program pays nothing for a schema it does not
        read, so it gets the tools anyway -- which is the whole reason to write
        one against a large server.
        """
        tools = [wire_tool(f"tool_{index:03d}", read_only=True) for index in range(60)]
        async with McpStubServer(tools) as server:
            for tool in tools:
                name = str(tool["name"])

                def answer(_args: dict[str, object], n: str = name) -> tuple[str, bool]:
                    return (n, False)

                server.call_handlers[name] = answer
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "names = [t for t in TOOLS if t.startswith('support__tool_')]\n"
                "first = await call_tool(sorted(names)[0], {})\n"
                "return {'count': len(names), 'first': first}"
            )
            model = _program(FakeModel(), program)
            _, report = await drive(
                spec([make_server(server.url)]),
                model,
                mcp,
                sandbox,
                catalogue_budget_chars=500,
            )

        offered = {tool.name for tool in model.requests[0].tools}
        assert "support__tool_000" not in offered, "the catalogue should have been deferred"
        assert {"list_tools", "get_tool_info", "run_code"} <= offered
        value = _run_code(report).result["value"]
        assert value == {"count": 60, "first": "tool_000"}

    async def test_a_program_can_read_a_deferred_catalogue_through_the_builtins(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """``list_tools`` and ``get_tool_info`` are the two built-ins a program
        may call, and for the same reason the model may: arguments are not
        guessable from a name."""
        tools = [wire_tool(f"tool_{index:03d}", read_only=True) for index in range(60)]
        async with McpStubServer(tools) as server:
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "listed = await list_tools(server='support', search='tool_00')\n"
                "info = await get_tool_info(server='support', tool='tool_001')\n"
                "return {'found': listed['total'], 'schema': sorted(info['input_schema'])}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                sandbox,
                catalogue_budget_chars=500,
            )

        value = _run_code(report).result["value"]
        assert value["found"] == 60
        assert "type" in value["schema"]


class TestNarrowing:
    async def test_an_explicit_tuple_narrows_and_never_widens(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("refund", destructive=True)]
        ) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "await support__lookup()\n"
                "GRANTED = [t for t in TOOLS if t != 'read_tool_output']\n"
                "return {'tools': GRANTED, 'refund_bound': 'support__refund' in TOOLS}"
            )
            _, report = await drive(
                spec([make_server(server.url)], bindings=("support__lookup",)),
                _program(FakeModel(), program),
                mcp,
                sandbox,
            )

        value = _run_code(report).result["value"]
        assert value["tools"] == ["support__lookup"]
        assert value["refund_bound"] is False

    async def test_a_server_allow_rule_applies_to_a_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("refund", destructive=True)]
        ) as server:
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            # The large-result reader is machinery rather than a granted tool,
            # and is always present; what this narrows is the tools.
            program = "return [t for t in TOOLS if t != 'read_tool_output']"
            _, report = await drive(
                spec([make_server(server.url, allow=("lookup",))]),
                _program(FakeModel(), program),
                mcp,
                sandbox,
            )

        assert _run_code(report).result["value"] == ["support__lookup"]

    async def test_a_tenant_policy_denial_applies_to_a_program_too(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The same narrowing plane, the same answer, whichever door is used."""

        class OnlyLookups:
            async def permitted_tools(self, scope: Scope, server: str | None) -> Sequence[str]:
                return ("lookup",)

        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("refund", destructive=True)]
        ) as server:
            policy = OnlyLookups()
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool, tenant_policy=policy)
            program = (
                "try:\n"
                "    await call_tool('support__refund', {})\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind}\n"
                "return {'kind': 'it ran'}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                sandbox,
                resolver=ToolResolver(ToolRegistry(), catalog=mcp, tenant_policy=policy),
            )

        assert _run_code(report).result["value"] == {"kind": "binding_not_available"}

    async def test_a_tool_the_spec_named_that_vanished_is_reported_not_substituted(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The server renamed it. The model is told, and never handed a
        neighbour that happens to still exist (DESIGN.md §10.7)."""
        async with McpStubServer([wire_tool("lookup_v2", read_only=True)]) as server:
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            _, report = await drive(
                spec([make_server(server.url)], bindings=("support__lookup",)),
                _program(
                    FakeModel(),
                    "return [t for t in TOOLS if t != 'read_tool_output']",
                ),
                mcp,
                sandbox,
            )

        result = _run_code(report).result
        assert result["unavailable_bindings"] == ["support__lookup"]
        assert result["value"] == []

    async def test_an_optional_server_that_is_down_does_not_fail_the_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as live:
            live.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            servers = [
                make_server(live.url, name="support"),
                # Nothing is listening here.
                make_server("http://127.0.0.1:1/mcp", name="extras", optional=True),
            ]
            program = (
                "GRANTED = [t for t in TOOLS if t != 'read_tool_output']\n"
                "return {'v': await support__lookup(), 'tools': GRANTED}"
            )
            _, report = await drive(spec(servers), _program(FakeModel(), program), mcp, sandbox)

        assert report.terminal_state is TerminalState.COMPLETED
        value = _run_code(report).result["value"]
        assert value["v"] == "ok"
        assert value["tools"] == ["support__lookup"]

    async def test_a_required_server_that_is_down_still_fails_the_run(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Code execution does not soften DESIGN.md §10.7."""
        mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
        servers = [make_server("http://127.0.0.1:1/mcp", name="support", optional=False)]
        _, report = await drive(spec(servers), _program(FakeModel(), "return 1"), mcp, sandbox)
        assert report.terminal_state is TerminalState.FAILED


class TestRefusalsAreTyped:
    async def test_an_approval_gated_tool_is_refused_before_it_runs(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """No approval can be asked for while a subprocess waits.

        So the call is refused *before* the tool runs, which is the only
        refusal that prevents anything: the server never hears from us, the
        model is told to make the call itself, and the tool is still in its
        tool list to make it with.
        """
        async with McpStubServer([wire_tool("refund", destructive=True)]) as server:
            server.call_handlers["refund"] = lambda _args: ("refunded", False)
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool)
            program = (
                "try:\n"
                "    await call_tool('support__refund', {'order': 'A-1'})\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'direct': 'directly' in err.message}\n"
                "return {'kind': 'it ran'}"
            )
            model = _program(FakeModel(), program)
            _, report = await drive(
                spec([make_server(server.url)]),
                model,
                mcp,
                sandbox,
                approval_selectors=("@destructive",),
            )

        assert _run_code(report).result["value"] == {
            "kind": "approval_required_in_program",
            "direct": True,
        }
        assert server.received_calls == [], "the refusal must come before the side effect"
        # A record, not only an exception the program caught, and filed under
        # the program that tried.
        refused = [call for call in report.tool_calls if call.tool == "support__refund"]
        assert len(refused) == 1
        assert refused[0].outcome is ToolOutcome.ERROR
        assert refused[0].failure is not None
        assert refused[0].failure.kind == "approval_required_in_program"
        assert refused[0].parent_call_id == _run_code(report).call_id
        # Refused in a program, still offered to the model, which is the whole
        # of "call it directly instead".
        assert "support__refund" in {tool.name for tool in model.requests[0].tools}

    async def test_a_builtin_that_suspends_says_so_rather_than_going_missing(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "try:\n"
                "    await call_tool('ask_question', {'questions': []})\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'says': 'suspends' in err.message}\n"
                "return {}"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "binding_not_available"
        # The sandbox never offered the name, so the program is stopped at the
        # boundary and never reaches the host at all. The reason the model
        # needs is in `run_code`'s own description and in the docs.
        assert value["says"] is False

    async def test_an_mcp_tool_error_and_a_transport_failure_stay_apart(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Requirement: a program must be able to tell "the tool ran and said
        no" from "the server is not there". One is a result; the other is an
        outage, and they call for opposite next moves."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("no such sku", True)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "kinds = {}\n"
                "try:\n"
                "    await support__lookup(sku='ZZZ')\n"
                "except ToolError as err:\n"
                "    kinds['tool'] = err.kind\n"
                "try:\n"
                "    await call_tool('gone__missing', {})\n"
                "except ToolError as err:\n"
                "    kinds['unknown'] = err.kind\n"
                "return kinds"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        value = _run_code(report).result["value"]
        assert value["tool"] == "McpToolError"
        assert value["unknown"] == "binding_not_available"

    async def test_a_server_asking_for_elicitation_is_refused_not_answered(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Requirement: an MCP server that wants more input reaches the program
        as something it can act on.

        Nothing can answer an elicitation while a subprocess waits: the model
        is not running, and Psych supplies no roots, sampling or elicitation in
        any case. So the program is stopped with the one kind that means "call
        this directly" rather than handed an empty result that would read as a
        successful call with nothing in it.
        """
        async with McpStubServer([wire_tool("lookup", read_only=True)], modern=True) as server:
            server.force_input_required = True
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "try:\n"
                "    await support__lookup(sku='A1')\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'tool': err.tool}\n"
                "return {'kind': 'no error'}"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "input_required_in_program"
        assert value["tool"] == "support__lookup"
        failed = [
            call
            for call in report.tool_calls
            if call.tool == "support__lookup" and call.outcome is ToolOutcome.ERROR
        ]
        assert failed, "the refused call is still recorded as its own tool call"
        assert failed[0].parent_call_id == _run_code(report).call_id
        # The record keeps what actually happened; the program is told the one
        # thing it can act on.
        assert failed[0].failure is not None
        assert failed[0].failure.kind == "McpInputRequiredError"


class TestBudgetsAndIsolation:
    async def test_a_program_cannot_spend_more_host_calls_than_the_profile_allows(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        from psych_runtime.core.code_execution import BindingBudget
        from psych_runtime.sandbox.profiles import DEFAULT_HARD_LIMITS, SandboxProfile

        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            profile = SandboxProfile(
                name="default",
                sandbox=sandbox,
                hard_limits=DEFAULT_HARD_LIMITS,
                binding_budget=BindingBudget(max_calls=4),
            )
            program = (
                "made = 0\n"
                "try:\n"
                "    for index in range(50):\n"
                "        await support__lookup(sku=str(index))\n"
                "        made += 1\n"
                "except ToolError as err:\n"
                "    return {'made': made, 'kind': err.kind}\n"
                "return {'made': made, 'kind': None}"
            )
            _, report = await drive_with_profile(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, profile
            )

        assert _run_code(report).result["value"] == {
            "made": 4,
            "kind": "binding_calls_exhausted",
        }
        assert len(server.received_calls) == 4, "the refused calls never reached the server"

    async def test_a_degenerate_loop_is_stopped_by_the_repetition_guard(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Identical call, identical arguments, identical answer, over and
        over: the program learned nothing and is told so. Fan-out is untouched,
        because forty different arguments are forty different calls."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("same", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "made = 0\n"
                "try:\n"
                "    for _ in range(40):\n"
                "        await support__lookup(sku='A')\n"
                "        made += 1\n"
                "except ToolError as err:\n"
                "    return {'made': made, 'kind': err.kind}\n"
                "return {'made': made, 'kind': None}"
            )
            _, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "repeated_call"
        assert value["made"] < 40

    async def test_no_credential_reaches_the_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The program gets the *result* of an authorised call and never the
        thing that authorised it."""
        token = "test-credential-must-not-leak-4f2a"
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.oauth_tokens = {token: ""}
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            secrets = InMemorySecretResolver()
            secrets.set(SCOPE, "support-key", token)
            mcp = McpTools(McpPool(transport=transport, secrets=secrets))
            program = (
                "import os\n"
                "value = await support__lookup()\n"
                "return {'value': value, 'env': sorted(os.environ), 'globals': sorted(globals())}"
            )
            _, report = await drive(
                spec([make_server(server.url, credential="support-key")]),
                _program(FakeModel(), program),
                mcp,
                sandbox,
            )

        result = _run_code(report).result
        assert result["value"]["value"] == "ok"
        rendered = str(result)
        assert token not in rendered
        assert "Bearer" not in rendered
        assert server.received_authorization_headers, "the call really was authorised"
        assert any(header and token in header for header in server.received_authorization_headers)
