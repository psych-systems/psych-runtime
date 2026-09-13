"""A result too large to hand a program in one piece.

The transfer ceiling exists so one binding call cannot move an arbitrary
number of bytes into a subprocess. That leaves a question the ceiling alone
does not answer: what a program gets when the tool it called returned more
than that. Three answers, and the tests here are about keeping them apart:

- under the ceiling, the whole result, exactly as before;
- over it and preserved under a handle, a reference the program reads in
  bounded windows on the host;
- over it and not preserved, an explicit refusal that says so.

The one answer that must never happen is a fourth: a truncated result that
still looks like a result. A program that computes over a silently shortened
answer returns a confident wrong number, and nothing downstream can tell.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.code_execution import BindingBudget, IsolationLevel
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
from psych_runtime.sandbox.profiles import DEFAULT_HARD_LIMITS, SandboxProfile
from psych_runtime.store.blob_memory import InMemoryBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.secrets import InMemorySecretResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-42")

_LIMITS = SandboxLimits(
    cpu_seconds=15.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=8 * 1024 * 1024,
    process_count=16,
    wall_seconds=40.0,
)

ROWS = 4_000
PAYLOAD = "\n".join(f"{index},widget-{index},{index % 7}" for index in range(ROWS))
"""About 74 KB of CSV -- near 78 KB once JSON-encoded, which is what a budget
actually measures. Comfortably over every ceiling used here, and shaped so a
program can compute an answer nobody could guess from a preview."""

PAYLOAD_BYTES = len(json.dumps(PAYLOAD).encode())
"""What one export costs a budget: the serialised size, not the raw one.

Computed rather than written down, so the byte ceilings below stay meaningful
if the shape of the payload ever changes."""

MATCHING = len([index for index in range(ROWS) if index % 7 == 3])


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
        "limits": Limits(max_turns=6, deadline_seconds=120),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def profile(sandbox: Sandbox, budget: BindingBudget) -> SandboxProfile:
    return SandboxProfile(
        name="default",
        sandbox=sandbox,
        hard_limits=DEFAULT_HARD_LIMITS,
        binding_budget=budget,
    )


async def drive(
    agent: AgentSpec,
    model: FakeModel,
    mcp: McpTools,
    profile: SandboxProfile,
    **options: Any,
) -> tuple[InMemoryStore, psych_runtime.RunReport]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, agent)
    runtime = Runtime(
        store=store,
        model=model,
        registry=ToolRegistry(),
        mcp=mcp,
        sandboxes=(profile,),
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


def _last_program(report: psych_runtime.RunReport) -> Any:
    """The last ``run_code`` call in the report.

    A seeded Run has an earlier one written by hand; the live program is
    always the last."""
    return [call for call in report.tool_calls if call.tool == "run_code"][-1]


def _program(model: FakeModel, source: str) -> FakeModel:
    return model.turn(tool_calls=[("run_code", {"program": source})]).turn(text="done")


def _exporting_server() -> McpStubServer:
    return McpStubServer([wire_tool("export", read_only=True)])


class TestPagingAResultTooLargeToSend:
    async def test_a_program_filters_a_large_result_through_bounded_reads(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The feature, end to end, and the number is the proof.

        The program never holds the 90 KB. It pages through the result a
        window at a time, counts what it wanted, and returns three integers.
        A count that matches the payload is something no preview, no
        truncation and no guess could produce -- it is only reachable by
        having read every line.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "kind = type(handle).__name__\n"
                "matching = 0\n"
                "seen = 0\n"
                "offset = 0\n"
                "while True:\n"
                "    page = await handle.read(offset=offset, limit=200)\n"
                "    lines = page['content'].splitlines()\n"
                "    seen += len(lines)\n"
                "    matching += len([line for line in lines if line.endswith(',3')])\n"
                "    offset += len(lines)\n"
                "    if offset >= page['total_lines'] or not lines:\n"
                "        break\n"
                "return {'kind': kind, 'seen': seen, 'matching': matching,\n"
                "        'total': page['total_lines'], 'size': handle.size_bytes}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "ToolResultHandle"
        assert value["seen"] == ROWS
        assert value["matching"] == MATCHING
        assert value["total"] == ROWS
        assert value["size"] > 8_192

    async def test_only_the_small_answer_reaches_the_next_model_request(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The economic argument, asserted rather than assumed.

        The tool ran, the whole result is in the Run's log, and the model's
        next prompt carries one number. Neither the payload nor any window of
        it appears in what was sent.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "page = await handle.read(pattern=',3$', limit=100)\n"
                "return page['total_matches']"
            )
            model = _program(FakeModel(), program)
            _, report = await drive(
                spec([make_server(server.url)]),
                model,
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == MATCHING
        sent = str(model.requests[-1].messages)
        assert PAYLOAD not in sent
        assert "widget-3999" not in sent
        assert len(sent) < 40_000

    async def test_the_call_is_still_recorded_as_the_success_it_was(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Paging is a transfer decision, not an outcome.

        The tool ran and answered; what changed is how the answer reached the
        program. A record that said otherwise would make a report claim a
        failure that never happened, and would take the call out of the log's
        account of what this Run actually did.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = "handle = await support__export()\nreturn handle.stored"
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == "blob"
        export = next(call for call in report.tool_calls if call.tool == "support__export")
        assert export.outcome is ToolOutcome.OK
        assert export.failure is None
        assert export.result_handle is not None
        assert export.parent_call_id == _run_code(report).call_id
        # The reads are calls of their own, under the same program.
        reads = [call for call in report.tool_calls if call.tool == "read_tool_output"]
        assert reads == [] or all(
            read.parent_call_id == _run_code(report).call_id for read in reads
        )

    async def test_a_handle_cannot_be_mistaken_for_the_result(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The silent-wrong-answer path, closed.

        ``len(rows)`` and ``rows.get("total", 0)`` are what a program actually
        writes, and on a truncated result both return something plausible and
        wrong. Here they raise, with a kind to branch on and a sentence saying
        what to call instead.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "seen = {}\n"
                "for label, thunk in (\n"
                "    ('len', lambda: len(handle)),\n"
                "    ('index', lambda: handle[0]),\n"
                "    ('get', lambda: handle.get('total', 0)),\n"
                "    ('iterate', lambda: list(handle)),\n"
                "):\n"
                "    try:\n"
                "        thunk()\n"
                "        seen[label] = 'no error'\n"
                "    except ToolError as err:\n"
                "        seen[label] = err.kind\n"
                "return seen"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == {
            "len": "result_not_inline",
            "index": "result_not_inline",
            "get": "result_not_inline",
            "iterate": "result_not_inline",
        }

    async def test_a_result_that_fits_is_still_sent_whole(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Nothing changes below the ceiling. A program that was written
        against a string still gets a string."""
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: ("a,b,c", False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "raw = await support__export()\nreturn {'kind': type(raw).__name__, 'value': raw}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
            )

        assert _run_code(report).result["value"] == {"kind": "str", "value": "a,b,c"}

    async def test_a_window_wider_than_the_ceiling_says_to_ask_for_less(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The one refusal a program can act on without help.

        A read is bounded by the same transfer ceiling as anything else, and a
        program that asks for a thousand lines of a wide result will exceed it.
        Handing back another handle would be consistent and useless; what it
        needs to hear is that the window was too wide.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "try:\n"
                "    await handle.read(limit=800)\n"
                "except ToolError as err:\n"
                "    wide = err.kind\n"
                "page = await handle.read(limit=50)\n"
                "return {'wide': wide, 'then': len(page['content'].splitlines())}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == {
            "wide": "binding_result_too_large",
            "then": 50,
        }


class TestWhatAHandleIsWorth:
    """A handle is a capability, and it belongs to one execution.

    Six rows, five of which are refusals. The five are refused **in the same
    words**, and that is a property rather than an accident of implementation:
    two of them name a result this Run really does hold, and a refusal that
    read differently for those would turn the reader into a way of asking the
    Run what it has stored.

    Every rejected attempt is still a call the program made, so it is in the
    log under its own program, with its own failure. A read a program was not
    entitled to make is not the same as a read that never happened, and a
    report that showed nothing would be describing a Run that did not occur.
    """

    async def _seed_first_turn(
        self,
        store: InMemoryStore,
        run_id: Any,
        *,
        from_program: bool,
        handle: str,
        scope: Scope = SCOPE,
    ) -> None:
        """Write a first turn that stored a large result under ``handle``.

        Hand-written because the thing being tested is a *later* execution's
        view of it, and a scripted model cannot be told a handle that does not
        exist until it runs. ``from_program`` decides which of the two same-Run
        rows this is: a direct call the model made, or a call an earlier
        ``run_code`` made.
        """
        claimed = await store.claim(
            psych_runtime.WorkerId("wrk_seed"), datetime.now(UTC), lease_seconds=60
        )
        assert claimed == run_id
        seed = await Journal.open(store, run_id, scope)
        await seed.append(type="attempt_started", worker_id="wrk_seed", attempt_number=1)
        await seed.append(type="turn_started", turn=1)
        await seed.append(type="model_call_started", turn=1, model="fake-standard")
        issued = ("call-seed-code",) if from_program else ("call-seed-export",)
        await seed.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 10},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=issued,
        )
        if from_program:
            await seed.append(
                type="tool_call_started",
                call_id="call-seed-code",
                tool="run_code",
                arguments={"program": "return await support__export()"},
                turn=1,
            )
        await seed.append(
            type="tool_call_started",
            call_id="call-seed-export",
            tool="support__export",
            arguments={},
            turn=1,
            parent_call_id="call-seed-code" if from_program else None,
        )
        await seed.append(
            type="tool_call_finished",
            call_id="call-seed-export",
            outcome=ToolOutcome.OK,
            result={"preview": "elided"},
            result_handle=handle,
            result_bytes=90_000,
        )
        if from_program:
            await seed.append(
                type="tool_call_finished",
                call_id="call-seed-code",
                outcome=ToolOutcome.OK,
                result={"ok": True, "value": None},
            )
        await store.release(run_id, psych_runtime.WorkerId("wrk_seed"), RunState.RUNNABLE)

    async def _reading(
        self,
        transport: HttpTransport,
        sandbox: Sandbox,
        handle_literal: str,
        *,
        seed: str | None = None,
        scope: Scope = SCOPE,
    ) -> Any:
        """Run one program that tries to read ``handle_literal``.

        ``seed`` is the handle a first turn stored, when the row needs this Run
        to actually hold one.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "try:\n"
                f"    await call_tool('read_tool_output', {{'handle': {handle_literal}}})\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'message': err.message}\n"
                "return {'kind': None}"
            )
            store = InMemoryStore()
            agent = spec([make_server(server.url)])
            version = await psych_runtime.publish(store, agent)
            run = await psych_runtime.dispatch(store, version, scope, input={"message": "go"})
            if seed is not None:
                raise AssertionError("use _seeded_reading for a seeded Run")
            runtime = Runtime(
                store=store,
                model=_program(FakeModel(), program),
                registry=ToolRegistry(),
                mcp=mcp,
                sandboxes=(profile(sandbox, BindingBudget(max_result_bytes=8_192)),),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )
            worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
            task = asyncio.create_task(worker.run())
            try:
                async for _ in psych_runtime.stream(store, run.run_id):
                    pass
            finally:
                worker.stop()
                await asyncio.wait_for(task, timeout=20)
            return await psych_runtime.report(store, run.run_id)

    async def _seeded_reading(
        self,
        transport: HttpTransport,
        sandbox: Sandbox,
        *,
        from_program: bool,
    ) -> tuple[Any, str]:
        """A Run that already holds a handle, and a later program that tries it."""
        handle = "res_seeded_handle"
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            store = InMemoryStore()
            agent = spec([make_server(server.url)])
            version = await psych_runtime.publish(store, agent)
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            await self._seed_first_turn(store, run.run_id, from_program=from_program, handle=handle)
            program = (
                "try:\n"
                f"    await call_tool('read_tool_output', {{'handle': {handle!r}}})\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'message': err.message}\n"
                "return {'kind': None}"
            )
            runtime = Runtime(
                store=store,
                model=_program(FakeModel(), program),
                registry=ToolRegistry(),
                mcp=mcp,
                sandboxes=(profile(sandbox, BindingBudget(max_result_bytes=8_192)),),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )
            worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
            task = asyncio.create_task(worker.run())
            try:
                async for _ in psych_runtime.stream(store, run.run_id):
                    pass
            finally:
                worker.stop()
                await asyncio.wait_for(task, timeout=20)
            report = await psych_runtime.report(store, run.run_id)
            state = await psych_runtime.state(store, run.run_id)
        assert handle in state.result_handles, "this Run really does hold that handle"
        return report, handle

    # -- the five refusals ---------------------------------------------------

    async def test_a_forged_handle_is_refused(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """A string the program made up. Names nothing, and is refused before
        anything is looked up."""
        report = await self._reading(transport, sandbox, "'res_deadbeef'")
        assert _last_program(report).result["value"]["kind"] == "binding_handle_not_yours"

    async def test_a_handle_from_another_run_is_refused(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Real, and issued by a different Run of the same agent and tenant."""
        other = await _handle_from_its_own_run(transport, sandbox, SCOPE)
        report = await self._reading(transport, sandbox, repr(other))
        assert _last_program(report).result["value"]["kind"] == "binding_handle_not_yours"

    async def test_a_handle_from_another_tenant_is_refused(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Real, and issued under a different tenant.

        Refused by the same check as every other row, which is worth being
        precise about: a handle resolves only inside the Run that minted it, so
        there is no second cross-tenant mechanism here to test -- the ownership
        check refuses this before any lookup could happen, and the reader would
        refuse it again afterwards. What this proves is that the earlier check
        does not have a gap the later one was covering.
        """
        other = await _handle_from_its_own_run(
            transport, sandbox, Scope(tenant="other-corp", principal="user-9")
        )
        report = await self._reading(transport, sandbox, repr(other))
        assert _last_program(report).result["value"]["kind"] == "binding_handle_not_yours"

    async def test_a_handle_from_a_direct_call_in_the_same_run_is_refused(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The row that matters most, because everything else would allow it.

        The model called the tool directly in an earlier turn of *this* Run and
        the result was stored under a handle. That handle resolves: it is this
        Run's, the reader would find it, and the reducer's own handle check
        passes. It is refused anyway, because a program reads back what its own
        calls produced and the rest of the Run's stored results are not a
        capability anybody granted it.
        """
        report, handle = await self._seeded_reading(transport, sandbox, from_program=False)
        value = _last_program(report).result["value"]
        assert value["kind"] == "binding_handle_not_yours"
        assert handle not in value["message"], "the refusal does not echo the handle back"

    async def test_a_handle_from_an_earlier_program_in_the_same_run_is_refused(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Earned by a program, and not inherited by the next one.

        A capability that outlived the execution it was issued to would be one
        the model could carry between turns: read a handle out of one program's
        return value, hand it to the next program, and page a result that
        program never asked for.
        """
        report, _ = await self._seeded_reading(transport, sandbox, from_program=True)
        assert _last_program(report).result["value"]["kind"] == "binding_handle_not_yours"

    async def test_every_refusal_reads_the_same(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Two of these name something real. None of them says so."""
        forged = await self._reading(transport, sandbox, "'res_deadbeef'")
        seeded, _ = await self._seeded_reading(transport, sandbox, from_program=False)
        messages = {
            _last_program(forged).result["value"]["message"],
            _last_program(seeded).result["value"]["message"],
        }
        assert len(messages) == 1, messages

    # -- the row that succeeds ----------------------------------------------

    async def test_a_handle_this_execution_earned_reads(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The sixth row, and the regression check for the other five: the
        ownership rule must not have made paging unusable."""
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "direct = await call_tool('read_tool_output',\n"
                "                         {'handle': handle.handle, 'limit': 20})\n"
                "return {'lines': len(direct['content'].splitlines()),\n"
                "        'total': direct['total_lines']}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == {"lines": 20, "total": ROWS}

    # -- the refusal is a recorded call --------------------------------------

    async def test_a_rejected_read_is_recorded_under_its_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """A read a program was not entitled to make still happened.

        It is in the log as its own tool call, nested under the ``run_code``
        that made it, failed with the kind the program saw. A report that
        omitted it would describe a Run in which the program never tried --
        and the attempt is exactly what someone reviewing the Run needs to
        see.
        """
        report = await self._reading(transport, sandbox, "'res_deadbeef'")
        program_call = _last_program(report)
        reads = [call for call in report.tool_calls if call.tool == "read_tool_output"]
        assert len(reads) == 1
        rejected = reads[0]
        assert rejected.parent_call_id == program_call.call_id
        assert rejected.outcome is ToolOutcome.ERROR
        assert rejected.failure is not None
        assert rejected.failure.kind == "binding_handle_not_yours"
        # And the arguments are kept, so the attempt is legible rather than
        # merely counted.
        assert rejected.arguments == {"handle": "res_deadbeef"}


async def _handle_from_its_own_run(transport: HttpTransport, sandbox: Sandbox, scope: Scope) -> str:
    """One handle, minted by a Run of its own under ``scope``."""
    async with _exporting_server() as server:
        server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
        mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
        store = InMemoryStore()
        version = await psych_runtime.publish(store, spec([make_server(server.url)]))
        run = await psych_runtime.dispatch(store, version, scope, input={"message": "go"})
        runtime = Runtime(
            store=store,
            model=_program(FakeModel(), "handle = await support__export()\nreturn handle.handle"),
            registry=ToolRegistry(),
            mcp=mcp,
            sandboxes=(profile(sandbox, BindingBudget(max_result_bytes=8_192)),),
            blob=InMemoryBlobStore(),
            blob_offload_bytes=4_096,
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            async for _ in psych_runtime.stream(store, run.run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=20)
        report = await psych_runtime.report(store, run.run_id)
    issued = _run_code(report).result["value"]
    assert isinstance(issued, str)
    return issued


class TestWhenItCannotBePaged:
    async def test_a_result_nothing_preserved_is_refused_explicitly(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Over the ceiling and under no handle: said plainly.

        The Spec's own ``large_result_bytes`` is set above the payload, so the
        result is never elided and no handle is ever minted for it -- there is
        nothing to page from. The program is told exactly that, with a kind it
        can branch on, rather than being handed a shortened answer.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "try:\n"
                "    await support__export()\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'says': 'nothing to page' in str(err)}\n"
                "return {'kind': None}"
            )
            _, report = await drive(
                spec(
                    [make_server(server.url)],
                    limits=Limits(max_turns=6, deadline_seconds=120, large_result_bytes=10_000_000),
                ),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_result_bytes=8_192)),
            )

        assert _run_code(report).result["value"] == {
            "kind": "binding_result_not_paged",
            "says": True,
        }
        # It ran. The refusal is about the transfer, and the log still holds
        # the whole answer.
        export = next(call for call in report.tool_calls if call.tool == "support__export")
        assert export.outcome is ToolOutcome.OK


class TestReadsCostWhatCallsCost:
    async def test_reads_spend_the_call_budget(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """A read is a host call and is counted as one.

        Otherwise paging would be the way around every budget in the feature:
        one cheap call for the handle, then unlimited reads.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "reads = 0\n"
                "try:\n"
                "    for index in range(20):\n"
                "        await handle.read(offset=index * 10, limit=10)\n"
                "        reads += 1\n"
                "except ToolError as err:\n"
                "    return {'reads': reads, 'kind': err.kind}\n"
                "return {'reads': reads, 'kind': None}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(sandbox, BindingBudget(max_calls=4, max_result_bytes=8_192)),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        assert _run_code(report).result["value"] == {
            "reads": 3,
            "kind": "binding_calls_exhausted",
        }

    async def test_reads_spend_the_traffic_budget(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """And the bytes they move are counted too, which is the budget that
        actually bounds paging: a program may not read a 90 KB result in
        90 KB of windows when its allowance is smaller than that."""
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "read_bytes = 0\n"
                "try:\n"
                "    for index in range(60):\n"
                "        page = await handle.read(offset=index * 200, limit=200)\n"
                "        read_bytes += len(page['content'])\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'read_bytes': read_bytes}\n"
                "return {'kind': None, 'read_bytes': read_bytes}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(
                    sandbox,
                    BindingBudget(max_calls=100, max_result_bytes=8_192, max_total_bytes=20_000),
                ),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "binding_traffic_exhausted"
        assert 0 < value["read_bytes"] < len(PAYLOAD)


class TestTheCostOfResultsNobodyReceives:
    """A handle is not a way to make a large result free.

    The transfer budget counts what crossed into the sandbox, and a result
    replaced by a handle crosses as about a hundred bytes however large it
    was. Everything expensive about it has already happened by then: the tool
    ran, the host fetched and parsed the answer, and anything over the elision
    threshold was written to the BlobStore *before* the program was told it
    exists. Metering only the crossing would price a gigabyte at the size of
    its handle, and a program could spend a Run's storage in a loop.

    So two more budgets, and they are not the same one twice. ``produced``
    bounds the work the host did; ``preserved`` bounds what is still sitting in
    the log or the BlobStore afterwards, which is the one somebody pays for.
    """

    async def test_repeated_oversized_exports_exhaust_the_produced_budget(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Each call transfers a handle and costs the host 90 KB.

        The allowance is two and a half exports, so the program gets three --
        the third is allowed because the first two had not yet spent it, and
        the fourth is refused. That is only true if what is counted is what
        the tool produced rather than what the program received: each call
        transfers about a hundred bytes of handle. The transfer budget here is
        untouched and enormous by comparison; if it were doing the bounding,
        this loop would run to its end.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "made = 0\n"
                "try:\n"
                "    for _ in range(20):\n"
                "        await support__export(page=made)\n"
                "        made += 1\n"
                "except ToolError as err:\n"
                "    return {'made': made, 'kind': err.kind}\n"
                "return {'made': made, 'kind': None}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(
                    sandbox,
                    BindingBudget(
                        max_result_bytes=8_192,
                        max_total_bytes=64 * 1024 * 1024,
                        max_produced_bytes=(PAYLOAD_BYTES * 5) // 2,
                    ),
                ),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )
            issued = len(server.received_calls)

        value = _run_code(report).result["value"]
        assert value["kind"] == "binding_produced_bytes_exhausted"
        assert value["made"] == 3, value
        # Three exports really did cost more than the allowance, which is what
        # the refusal is about -- and each one moved a handle, not a payload.
        assert value["made"] * PAYLOAD_BYTES > (PAYLOAD_BYTES * 5) // 2
        # The refusal happened before the tool ran, so the server saw only the
        # calls that were actually allowed.
        assert issued == 3, issued

    async def test_the_preserved_budget_is_per_run_and_survives_recovery(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Storage spent by a dead Attempt is still spent.

        The first Attempt stored two oversized results and died. The second
        folds the same log, sees the same number, and is refused before it can
        store a third -- which is the whole point of reading it from the log
        rather than remembering it: a program that can arrange to be restarted
        must not be able to fill a BlobStore that way.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
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
                arguments={"program": "..."},
                turn=1,
            )
            for index in range(2):
                await dead.append(
                    type="tool_call_started",
                    call_id=f"call-export-{index}",
                    tool="support__export",
                    arguments={"page": index},
                    turn=1,
                    parent_call_id="call-code",
                )
                await dead.append(
                    type="tool_call_finished",
                    call_id=f"call-export-{index}",
                    outcome=ToolOutcome.OK,
                    result={"preview": "elided"},
                    result_handle=f"res_dead_{index}",
                    result_bytes=90_000,
                )
            await store.release(run.run_id, psych_runtime.WorkerId("wrk_dead"), RunState.RUNNABLE)

            program = (
                "try:\n"
                "    await support__export(page='again')\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind}\n"
                "return {'kind': None}"
            )
            runtime = Runtime(
                store=store,
                model=_program(FakeModel(), program),
                registry=ToolRegistry(),
                mcp=mcp,
                sandboxes=(
                    profile(
                        sandbox,
                        BindingBudget(max_result_bytes=8_192, max_preserved_bytes_per_run=150_000),
                    ),
                ),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )
            worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
            task = asyncio.create_task(worker.run())
            try:
                async for _ in psych_runtime.stream(store, run.run_id):
                    pass
            finally:
                worker.stop()
                await asyncio.wait_for(task, timeout=20)
            report = await psych_runtime.report(store, run.run_id)
            issued = len(server.received_calls)

        value = _last_program(report).result["value"]
        assert value["kind"] == "binding_preserved_bytes_exhausted_for_run"
        assert issued == 0, "the tool must not run once the Run's storage is spent"

    async def test_paging_spends_the_transfer_budget_not_the_produced_one(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """The two budgets meter different things, and reads prove it.

        One export produces 90 KB. Paging it back a window at a time moves far
        less than that into the sandbox per call but adds up, and it is the
        *transfer* budget that stops it -- the produced budget is untouched by
        a read, because a read produces nothing new.
        """
        async with _exporting_server() as server:
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            program = (
                "handle = await support__export()\n"
                "read = 0\n"
                "try:\n"
                "    for index in range(200):\n"
                "        page = await handle.read(offset=index * 100, limit=100)\n"
                "        read += len(page['content'])\n"
                "except ToolError as err:\n"
                "    return {'kind': err.kind, 'read': read}\n"
                "return {'kind': None, 'read': read}"
            )
            _, report = await drive(
                spec([make_server(server.url)]),
                _program(FakeModel(), program),
                mcp,
                profile(
                    sandbox,
                    BindingBudget(
                        max_calls=500,
                        max_result_bytes=8_192,
                        max_total_bytes=20_000,
                        max_produced_bytes=10 * 1024 * 1024,
                    ),
                ),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )

        value = _run_code(report).result["value"]
        assert value["kind"] == "binding_traffic_exhausted"
        assert 0 < value["read"] < len(PAYLOAD)


class TestCancellation:
    async def test_an_abort_during_paging_stops_the_program(
        self, transport: HttpTransport, sandbox: Sandbox
    ) -> None:
        """Reading is not a way to outlive an abort.

        The program pages in a loop, and the abort lands while it is doing so.
        A read is a host call like any other, so the same boundary applies: the
        execution is cancelled where it stands and the remaining reads never
        happen.

        The signal is fired from a tool the program itself calls, not from a
        timer. A timer races the machine -- sandbox startup and the first model
        call can easily outlast it, and an abort that arrives before the turn
        begins tests the Worker's shutdown path rather than this one.
        """
        async with McpStubServer(
            [wire_tool("export", read_only=True), wire_tool("stop", read_only=True)]
        ) as server:
            abort = AbortSignal()
            server.call_handlers["export"] = lambda _args: (PAYLOAD, False)

            def stop(_args: dict[str, object]) -> tuple[str, bool]:
                # Fired from inside the program's own run, after it has paged.
                abort.fire(AbortReason.SHUTDOWN)
                return ("stopping", False)

            server.call_handlers["stop"] = stop
            mcp = McpTools(McpPool(transport=transport, secrets=InMemorySecretResolver()))
            store = InMemoryStore()
            agent = spec([make_server(server.url)])
            version = await psych_runtime.publish(store, agent)
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            journal = await Journal.open(store, run.run_id, SCOPE)
            await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
            program = (
                "handle = await support__export()\n"
                "before = 0\n"
                "for index in range(3):\n"
                "    await handle.read(offset=index * 10, limit=10)\n"
                "    before += 1\n"
                "await support__stop()\n"
                "for index in range(4000):\n"
                "    await handle.read(offset=index * 3, limit=5)\n"
                "return before"
            )
            runtime = Runtime(
                store=store,
                model=_program(FakeModel(), program),
                registry=ToolRegistry(),
                mcp=mcp,
                sandboxes=(
                    profile(
                        sandbox,
                        BindingBudget(max_calls=10_000, max_result_bytes=8_192),
                    ),
                ),
                blob=InMemoryBlobStore(),
                blob_offload_bytes=4_096,
            )
            header = await store.get_run(run.run_id)
            assert header is not None
            await runtime(journal, header, abort)
            state = await psych_runtime.state(store, run.run_id)

        code = next(result for result in state.tool_results if result.tool == "run_code")
        assert code.outcome is ToolOutcome.ABORTED
        assert code.result is None
        reads = [result for result in state.tool_results if result.tool == "read_tool_output"]
        # Three before the signal, and nothing like the four thousand the
        # program went on to ask for.
        assert 3 <= len(reads) < 100, len(reads)
        assert state.terminal_state is not TerminalState.COMPLETED
