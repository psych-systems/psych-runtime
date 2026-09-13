"""What a program's tool calls are worth when things go wrong.

Every case here is about a property the binding path shares with a direct tool
call and must not quietly lose by going through a subprocess:

- a large result belongs to the *program*, not to the prompt;
- a handle is still tied to the Run that minted it;
- a call whose outcome nobody observed is not replayed;
- a cancelled Run stops a program and does not re-run it;
- a catalogue that changed between turns is re-read rather than remembered;
- OAuth still refreshes, and the token still never crosses the boundary.

The MCP server is the real ``127.0.0.1`` stub, and programs run in a real
child process from ``local_sandbox()``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.code_execution import IsolationLevel
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeExecution, Limits, McpServer, ModelRef
from psych_runtime.model.egress import HttpTransport
from psych_runtime.runtime.abort import AbortReason, AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.port import Sandbox, SandboxLimits
from psych_runtime.store.blob_memory import InMemoryBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver
from psych_runtime.tools.secrets import InMemorySecretResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-42")

_LIMITS = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=4 * 1024 * 1024,
    process_count=16,
    wall_seconds=30.0,
)


def _python_bin() -> str | None:
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


def spec(servers: Sequence[McpServer], **kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "use the tools",
        "model": ModelRef(model="fake-standard"),
        "mcp_servers": tuple(servers),
        "code_execution": CodeExecution(isolation=IsolationLevel.PROCESS),
        "limits": Limits(max_turns=8, deadline_seconds=120),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def drive(
    agent: AgentSpec, model: FakeModel, mcp: McpTools, sandbox: Sandbox, **options: Any
) -> tuple[InMemoryStore, psych_runtime.RunReport]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, agent)
    runtime = Runtime(
        store=store,
        model=model,
        registry=ToolRegistry(),
        mcp=mcp,
        sandbox=sandbox,
        **options,
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


def _programs(report: psych_runtime.RunReport) -> list[Any]:
    """Every ``run_code`` call in order.

    A recovery test has two: the dead Attempt's, settled UNKNOWN with no
    result, and the reclaiming Attempt's own."""
    return [call for call in report.tool_calls if call.tool == "run_code"]


def _program(model: FakeModel, source: str) -> FakeModel:
    return model.turn(tool_calls=[("run_code", {"program": source})]).turn(text="done")


class TestLargeResults:
    async def test_a_large_mcp_result_reaches_the_program_whole(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The point of the feature, and the thing easiest to lose.

        A tool result is elided, and above a threshold offloaded to the
        BlobStore, so it does not fill the model's context. A program is not
        the model's context: it is the thing that was written to reduce the
        result, so it has to receive all of it. Reading the *record* back would
        hand it the elided view -- and an offloaded record's ``result`` is
        ``None`` by the record's own rule, so the program would filter nothing
        and return a confident wrong answer.
        """
        rows = 4_000
        async with McpStubServer([wire_tool("export", read_only=True)]) as server:
            payload = "\n".join(f"{index},widget-{index},{index % 7}" for index in range(rows))
            server.call_handlers["export"] = lambda _args: (payload, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "raw = await support__export()\n"
                "lines = raw.splitlines()\n"
                "matching = [line for line in lines if line.endswith(',3')]\n"
                "return {'rows': len(lines), 'matching': len(matching), 'bytes': len(raw)}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                sandbox,
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        value = _run_code(report).result["value"]
        assert value["rows"] == rows
        assert value["bytes"] == len(
            "\n".join(f"{index},widget-{index},{index % 7}" for index in range(rows))
        )
        assert value["matching"] == len([i for i in range(rows) if i % 7 == 3])

    async def test_the_large_result_is_still_elided_for_the_model(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The program saw all of it; the prompt never does.

        That asymmetry is the whole economic argument for code execution, so it
        is asserted rather than assumed: the binding's own record is elided and
        offloaded exactly as a direct call's would be, and what the model reads
        is the program's three-number answer.
        """
        async with McpStubServer([wire_tool("export", read_only=True)]) as server:
            payload = "x" * 200_000
            server.call_handlers["export"] = lambda _args: (payload, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = _program(FakeModel(), "raw = await support__export()\nreturn len(raw)")
            _, report = await drive(
                spec([make_server(server.url)]),
                model,
                mcp,
                sandbox,
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == 200_000
        binding = next(call for call in report.tool_calls if call.tool == "support__export")
        assert binding.result_handle is not None, "the binding's own record is elided"
        # The 200 KB never entered a prompt: the second turn's conversation
        # carries the run_code payload, and nothing in it is that large.
        sent = str(model.requests[-1].messages)
        assert payload not in sent
        assert len(sent) < 50_000


class TestDurability:
    async def test_a_binding_call_nobody_saw_finish_is_unknown_and_not_replayed(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """A Worker died with an MCP call in flight from inside a program.

        Whether the far side acted is genuinely unknown, and a program's calls
        are not automatically safe to retry -- an MCP server's tool is somebody
        else's side effect. So the reclaiming Attempt records ``UNKNOWN`` and
        does not run it again, exactly as it would for a direct call. Nothing
        about going through a sandbox makes a side effect repeatable.
        """
        async with McpStubServer([wire_tool("refund", destructive=True)]) as server:
            server.call_handlers["refund"] = lambda _args: ("refunded", False)
            store = InMemoryStore()
            agent = spec([make_server(server.url)])
            version = await psych_runtime.publish(store, agent)
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})

            claimed = await store.claim(
                psych_runtime.WorkerId("wrk_dead"), datetime.now(UTC), lease_seconds=60
            )
            assert claimed == run.run_id
            dead = await Journal.open(store, run.run_id, SCOPE)
            await dead.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
            await dead.append(type="turn_started", turn=1)
            await dead.append(type="model_call_started", turn=1, model="fake-standard")
            await dead.append(
                type="model_call_finished",
                turn=1,
                model="fake-standard",
                usage={"input": 10},
                cost=None,
                timings={},
                finish_reason="tool_calls",
                tool_calls=("call-code",),
            )
            await dead.append(
                type="tool_call_started",
                call_id="call-code",
                tool="run_code",
                arguments={"program": "return await support__refund()"},
                turn=1,
            )
            # The binding call the program had already issued, filed under it.
            await dead.append(
                type="tool_call_started",
                call_id="call-refund",
                tool="support__refund",
                arguments={"order": "A-1"},
                turn=1,
                parent_call_id="call-code",
            )
            # The lease expires with neither call settled: that is what a kill
            # looks like from the store's side.
            await store.release(run.run_id, psych_runtime.WorkerId("wrk_dead"), RunState.RUNNABLE)

            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            model = FakeModel().turn(text="I could not confirm the refund.")
            runtime = Runtime(
                store=store,
                model=model,
                registry=ToolRegistry(),
                mcp=mcp,
                sandbox=sandbox,
            )
            worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
            task = asyncio.create_task(worker.run())
            try:
                async for _ in psych_runtime.stream(store, run.run_id):
                    pass
            finally:
                worker.stop()
                await asyncio.wait_for(task, timeout=20)

            state = await psych_runtime.state(store, run.run_id)

        assert state.terminal_state is TerminalState.COMPLETED
        assert not state.has_dangling_tool_calls
        outcomes = {result.tool: result.outcome for result in state.tool_results}
        assert outcomes["support__refund"] is ToolOutcome.UNKNOWN
        assert outcomes["run_code"] is ToolOutcome.UNKNOWN
        assert server.received_calls == [], "the reclaiming Attempt must not replay it"

    async def test_the_parent_relationship_survives_into_the_report(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The edge is in the log, so it is in the report, so it is in a trace
        drawn from either."""
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = "await support__lookup(sku='A')\nreturn 'done'"
            store, report = await drive(
                spec([make_server(server.url)]), _program(FakeModel(), program), mcp, sandbox
            )

        parent = _run_code(report)
        child = next(call for call in report.tool_calls if call.tool == "support__lookup")
        assert child.parent_call_id == parent.call_id
        # And from the raw log, which is what any other reader would use.
        records = await store.read(report.run_id)
        started = [
            r for r in records if r.type == "tool_call_started" and r.tool == "support__lookup"
        ]
        assert len(started) == 1
        assert started[0].parent_call_id == parent.call_id


class TestTheRunAllowanceIsDurable:
    """``max_calls_per_run`` is spent by the Run, not by the process.

    A Worker can die holding a lease and another can reclaim the same Attempt.
    An allowance kept in memory resets at exactly that moment, so a model that
    can arrange a crash -- or merely suffer one -- would get a fresh thousand
    calls per Attempt and the per-Run cap would bound nothing at all. The
    number therefore comes from the Run's own folded log, which is the same
    before and after a reclaim because it is derived from records.
    """

    async def _spend(
        self,
        store: InMemoryStore,
        run_id: Any,
        *,
        bindings: int,
        direct: int,
    ) -> None:
        """Write a first Attempt that spent ``bindings`` calls from a program.

        The calls name a tool the second Attempt does not use. Recovery
        settles a dangling call ``UNKNOWN``, and a streak of those withholds
        the tool from the next turn's set -- a real behaviour, and one that
        would hide this test's subject behind a missing binding.

        Hand-written rather than driven, because what is being tested is what a
        *reclaiming* Worker makes of records somebody else left behind: the
        first Attempt has to end without settling them, which a healthy Attempt
        never does.
        """
        claimed = await store.claim(
            psych_runtime.WorkerId("wrk_dead"), datetime.now(UTC), lease_seconds=60
        )
        assert claimed == run_id
        dead = await Journal.open(store, run_id, SCOPE)
        await dead.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
        await dead.append(type="turn_started", turn=1)
        await dead.append(type="model_call_started", turn=1, model="fake-standard")
        await dead.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 10},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-code",),
        )
        # The program itself. Never counted: it is the execution, not a call
        # the execution made.
        await dead.append(
            type="tool_call_started",
            call_id="call-code",
            tool="run_code",
            arguments={"program": "..."},
            turn=1,
        )
        for index in range(bindings):
            await dead.append(
                type="tool_call_started",
                call_id=f"call-binding-{index}",
                tool="support__ping",
                arguments={"sku": f"A-{index}"},
                turn=1,
                parent_call_id="call-code",
            )
        for index in range(direct):
            # A direct model call of the same tool, with no parent. Not a
            # program's call, and not counted -- which is also what every
            # record written before programs could call tools looks like.
            await dead.append(
                type="tool_call_started",
                call_id=f"call-direct-{index}",
                tool="support__ping",
                arguments={"sku": f"B-{index}"},
                turn=1,
            )
        # The lease expires with nothing settled: a kill, from the store's side.
        await store.release(run_id, psych_runtime.WorkerId("wrk_dead"), RunState.RUNNABLE)

    async def _reclaim(
        self,
        store: InMemoryStore,
        run_id: Any,
        model: FakeModel,
        mcp: McpTools,
        profile: Any,
    ) -> Any:
        runtime = Runtime(
            store=store, model=model, registry=ToolRegistry(), mcp=mcp, sandboxes=(profile,)
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            async for _ in psych_runtime.stream(store, run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=20)
        return await psych_runtime.report(store, run_id)

    def _profile(self, sandbox: Sandbox, max_calls_per_run: int) -> Any:
        from psych_runtime.core.code_execution import BindingBudget
        from psych_runtime.sandbox.profiles import DEFAULT_HARD_LIMITS, SandboxProfile

        return SandboxProfile(
            name="default",
            sandbox=sandbox,
            hard_limits=DEFAULT_HARD_LIMITS,
            binding_budget=BindingBudget(max_calls_per_run=max_calls_per_run),
        )

    async def test_a_reclaimed_attempt_does_not_get_a_fresh_allowance(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Attempt 1 spent the whole allowance; Attempt 2 gets none of it back.

        The refusal has to arrive *before* the tool runs, so the assertion that
        matters is the server's: a cap that refused after the call would have
        bounded the log and not the side effects.
        """
        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("ping", read_only=True)]
        ) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            store = InMemoryStore()
            version = await psych_runtime.publish(store, spec([make_server(server.url)]))
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            await self._spend(store, run.run_id, bindings=2, direct=1)

            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "try:\n"
                "    await support__lookup(sku='Z')\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind}\n"
                "return {'kind': None}"
            )
            report = await self._reclaim(
                store,
                run.run_id,
                _program(FakeModel(), program),
                mcp,
                self._profile(sandbox, max_calls_per_run=2),
            )

            assert server.received_calls == [], (
                "the reclaimed Attempt spent an allowance the Run had already used"
            )

        # The last one: the first is the dead Attempt's own program, settled
        # UNKNOWN by recovery and never re-run.
        value = _programs(report)[-1].result["value"]
        assert value["kind"] == "binding_calls_exhausted_for_run"

    async def test_a_reclaimed_attempt_gets_exactly_what_is_left(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Three of four spent, so the second Attempt makes exactly one call.

        This is also what proves the counting rule, and it is the reason the
        first Attempt wrote a direct call and a ``run_code`` of its own: with
        an allowance of four and two binding calls already spent, a rule that
        counted either of those would leave one call or none, and a rule that
        counted neither leaves two. The program asks for three.
        """
        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("ping", read_only=True)]
        ) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            store = InMemoryStore()
            version = await psych_runtime.publish(store, spec([make_server(server.url)]))
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            await self._spend(store, run.run_id, bindings=2, direct=1)

            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "made = 0\n"
                "try:\n"
                "    for index in range(3):\n"
                "        await support__lookup(sku=str(index))\n"
                "        made += 1\n"
                "except ToolError as err:\n"
                "    return {'made': made, 'kind': err.kind}\n"
                "return {'made': made, 'kind': None}"
            )
            report = await self._reclaim(
                store,
                run.run_id,
                _program(FakeModel(), program),
                mcp,
                self._profile(sandbox, max_calls_per_run=4),
            )
            issued = [call.tool for call in server.received_calls]

        assert _programs(report)[-1].result["value"] == {
            "made": 2,
            "kind": "binding_calls_exhausted_for_run",
        }
        assert issued == ["lookup", "lookup"], "the refused call never reached the server"

    async def test_a_second_program_in_one_attempt_shares_the_allowance(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The per-execution cap is not a per-program allowance to re-buy.

        Two programs in the same Attempt, one allowance between them: a model
        that answers a refusal by writing a second program gets the same
        answer, and the second program's own first call is the one refused.
        """
        async with McpStubServer([wire_tool("lookup", read_only=True)]) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            store = InMemoryStore()
            version = await psych_runtime.publish(store, spec([make_server(server.url)]))
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            once = (
                "made = 0\n"
                "try:\n"
                "    for index in range(2):\n"
                "        await support__lookup(sku=str(index))\n"
                "        made += 1\n"
                "except ToolError as err:\n"
                "    return {'made': made, 'kind': err.kind}\n"
                "return {'made': made, 'kind': None}"
            )
            model = (
                FakeModel()
                .turn(tool_calls=[("run_code", {"program": once})])
                .turn(tool_calls=[("run_code", {"program": once})])
                .turn(text="done")
            )
            report = await self._reclaim(
                store, run.run_id, model, mcp, self._profile(sandbox, max_calls_per_run=2)
            )
            issued = len(server.received_calls)

        programs = _programs(report)
        assert len(programs) == 2
        assert programs[0].result["value"] == {"made": 2, "kind": None}
        assert programs[1].result["value"] == {
            "made": 0,
            "kind": "binding_calls_exhausted_for_run",
        }
        assert issued == 2


class TestCancellation:
    async def test_aborting_during_a_binding_ends_the_program_and_does_not_replay_it(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """An abort while an MCP call is in flight ends the whole execution.

        Three things are being asserted at once, and the third is the one that
        matters most:

        - the in-flight MCP operation is *cancelled*, not left to finish: the
          cancel scope fires and the execution ends without an answer, so the
          call is recorded as aborted rather than as a result;
        - the program ends there, in well under the loop it was told to run;
        - and the call that had already gone out is **not re-issued**. An MCP
          tool is somebody else's side effect, and a cancelled program is not
          a reason to do it a second time.

        A program doing nothing but arithmetic is not interrupted this way: an
        abort is noticed at the boundaries the host controls -- before the
        start, and around a binding -- and a pure-compute loop ends at its wall
        clock instead. That is a real limit of the mechanism and is documented
        rather than papered over here.
        """
        async with McpStubServer([wire_tool("slow", read_only=True)]) as server:
            abort = AbortSignal()
            released = asyncio.Event()

            def slow(_args: dict[str, object]) -> tuple[str, bool]:
                # Fired from the server's own handler, so the abort lands while
                # the host is waiting on this very call.
                abort.fire(AbortReason.SHUTDOWN)
                return ("eventually", False)

            server.call_handlers["slow"] = slow
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            store = InMemoryStore()
            program = (
                "seen = []\nfor _ in range(5):\n    seen.append(await support__slow())\nreturn seen"
            )
            agent = spec([make_server(server.url)])
            version = await psych_runtime.publish(store, agent)
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            journal = await Journal.open(store, run.run_id, SCOPE)
            await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
            runtime = Runtime(
                store=store,
                model=_program(FakeModel(), program),
                registry=ToolRegistry(),
                mcp=mcp,
                sandbox=sandbox,
            )
            header = await store.get_run(run.run_id)
            assert header is not None
            try:
                await runtime(journal, header, abort)
            finally:
                released.set()

            state = await psych_runtime.state(store, run.run_id)

        issued = [call.tool for call in server.received_calls]
        assert issued == ["slow"], f"the call was re-issued: {issued}"
        code = next(result for result in state.tool_results if result.tool == "run_code")
        # An abort is not a program failure with an explanation in it: the
        # execution is cancelled where it stands, so `run_code` is recorded as
        # aborted with no result rather than as a program that returned one.
        # A program that wants to explain itself has to finish, and this one
        # was not allowed to.
        assert code.outcome is ToolOutcome.ABORTED
        assert code.result is None
        assert code.failure is None
        # Five calls were asked for and one went out, so the loop ended at the
        # abort rather than running to its end.
        assert code.duration_seconds is not None
        assert code.duration_seconds < 5.0


class TestTheCatalogueMoves:
    async def test_a_narrowing_between_turns_takes_effect_at_the_next_turn(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The binding set is resolved per turn, not per Attempt.

        A tenant withdraws a tool between one turn and the next. The second
        program must not still be able to call it. A set computed once when the
        Attempt started -- which is what this replaced -- would have kept the
        access, and access narrows and never widens (DESIGN.md §10.5).

        A tenant policy is used rather than a server-side removal because it is
        deterministic: the policy is asked at every turn boundary, so the turn
        the answer changes is the turn the answer changes. That MCP's own
        catalogue invalidation works is proved separately, against the
        notification, in ``tests/functional/test_mcp.py``.
        """

        class NarrowsOnceTheToolHasBeenUsed:
            """Empty means "everything" in this protocol, so the first answer
            is the wide one and every answer after the flip is narrow."""

            def __init__(self) -> None:
                self.narrowed = False

            async def permitted_tools(self, scope: Scope, server: str | None) -> Sequence[str]:
                return ("lookup",) if self.narrowed else ()

        async with McpStubServer(
            [wire_tool("lookup", read_only=True), wire_tool("legacy", read_only=True)]
        ) as server:
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            policy = NarrowsOnceTheToolHasBeenUsed()

            def legacy(_args: dict[str, object]) -> tuple[str, bool]:
                # The withdrawal happens inside the first turn, after the
                # binding set for that turn was resolved: deterministic,
                # rather than a sleep racing the loop.
                policy.narrowed = True
                return ("ok", False)

            server.call_handlers["legacy"] = legacy
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            mcp = McpTools(pool, tenant_policy=policy)
            second = (
                "GRANTED = [t for t in TOOLS if t != 'read_tool_output']\n"
                "try:\n"
                "    await call_tool('support__legacy', {})\n"
                "except ToolError as err:\n"
                "    return {'tools': GRANTED, 'kind': err.kind}\n"
                "return {'tools': GRANTED, 'kind': None}"
            )
            model = (
                FakeModel()
                .turn(
                    tool_calls=[
                        (
                            "run_code",
                            {
                                "program": (
                                    "await support__legacy()\n"
                                    "return sorted(t for t in TOOLS if t != 'read_tool_output')"
                                )
                            },
                        )
                    ]
                )
                .turn(tool_calls=[("run_code", {"program": second})])
                .turn(text="done")
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                model,
                mcp,
                sandbox,
                resolver=ToolResolver(ToolRegistry(), catalog=mcp, tenant_policy=policy),
            )

        programs = [call for call in report.tool_calls if call.tool == "run_code"]
        assert len(programs) == 2
        assert programs[0].result["value"] == ["support__legacy", "support__lookup"]
        second_value = programs[1].result["value"]
        assert second_value["tools"] == ["support__lookup"]
        assert second_value["kind"] == "binding_not_available"
        assert [call.tool for call in server.received_calls] == ["legacy"], (
            "the second program must not have reached the withdrawn tool"
        )


class TestOAuth:
    async def test_a_program_calls_through_oauth_without_seeing_a_token(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The exchange happens on the host, and the program is handed a
        result rather than the thing that bought it.

        Two calls in one program, so the pooled connection and its
        authorization are reused exactly as they are for two direct calls: the
        program does not get its own client, its own token, or its own way to
        obtain one.
        """
        from psych_runtime.tools.oauth import OAuthClient
        from tests.functional.test_mcp import _AuthServerStub, _protect

        async with (
            McpStubServer([wire_tool("lookup", read_only=True)]) as server,
            _AuthServerStub() as auth,
        ):
            _protect(server, auth)
            server.call_handlers["lookup"] = lambda _args: ("ok", False)
            secrets = InMemorySecretResolver()
            secrets.set(SCOPE, "shop-secret", "s3cr3t")
            pool = McpPool(
                transport=transport, secrets=secrets, oauth=OAuthClient(transport=transport)
            )
            mcp = McpTools(pool)
            program = (
                "import os\n"
                "values = [await support__lookup(), await support__lookup(sku='B')]\n"
                "return {'values': values, 'env': sorted(os.environ)}"
            )
            agent = spec(
                [
                    make_server(
                        server.url,
                        oauth=psych_runtime.McpOAuth(
                            grant="client_credentials",
                            preregistered_client_id="shop-client",
                            client_secret_credential="shop-secret",
                            issuer=auth.base_url,
                        ),
                    )
                ]
            )
            try:
                _, report = await drive(agent, _program(FakeModel(), program), mcp, sandbox)
                grants = [request.get("grant_type") for request in auth.token_requests]
            finally:
                await pool.close_all()

        result = _run_code(report).result
        assert result["value"]["values"] == ["ok", "ok"]
        assert "client_credentials" in grants, f"OAuth was never exercised: {grants}"

        # Nothing that authorised those calls crossed into the program: not in
        # its environment, not in its result, not in the record of either.
        rendered = str(result)
        assert "Bearer" not in rendered
        assert "s3cr3t" not in rendered
        assert not any("TOKEN" in name.upper() for name in result["value"]["env"])
        binding = next(call for call in report.tool_calls if call.tool == "support__lookup")
        assert "Bearer" not in str(binding.arguments)
        assert "s3cr3t" not in str(binding.result)

        # And the host really did authorise them.
        # `any`, not `all`: the first request is the unauthenticated one that
        # draws the 401 challenge, which is how the exchange starts.
        assert any(
            header and header.startswith("Bearer ")
            for header in server.received_authorization_headers
        )
