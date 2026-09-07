"""DESIGN.md §23 item 2: a Run survives its Worker being killed mid-tool-call.

This is the one scenario in the set that refuses to simulate anything. Every
other demonstration of durability in this codebase (including
``tests/e2e/test_regression_gate.py``'s own version of this exact claim)
fabricates the crash by hand-appending the Records a real crash would have
produced and manually releasing the lease -- a legitimate way to test the
*reducer's* reconciliation logic in isolation, but it never actually exercises
DESIGN.md §8.2's lease-expiry mechanism, because nothing ever stopped
heartbeating.

So this scenario spawns a second real OS process (``_crash_worker_proc.py``),
lets it claim the Run and start a tool call that blocks for 30 seconds, waits
for the log to show ``tool_call_started``, and sends it ``SIGKILL``. Only then
does a Worker living in *this* process wait out the lease and reclaim it. What
gets checked afterward is exactly DESIGN.md's own wording: the dangling call
settles as ``UNKNOWN`` rather than being silently dropped or silently retried,
a fresh ``attempt_started`` record shows a different Worker picked it up
(``reclaimed_expired_lease=True``), the tool's body -- proven with a marker
file the doomed process writes to disk before it blocks -- ran exactly once,
and the Run still reaches ``COMPLETED``.

Needs a real, reachable PostgreSQL: an in-memory Store lives inside one
process and a real crash needs a second one, so this is the scenario that
most needs the store DESIGN.md §22 asks every consumer to actually have.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.dsns import postgres_dsn
from app.scenarios.support import records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import Record, TerminalState, ToolOutcome
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.report.model import RunReport
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="crash-recovery",
    title="Crash recovery",
    proves=(
        "A Run survives its Worker being killed mid-tool-call: a real OS "
        "process is SIGKILLed while blocked inside a tool call, and a second "
        "Worker reclaims the expired lease, settles the dangling call and "
        "completes the Run."
    ),
    design_ref="§23.2",
    requires=("postgres",),
)

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_LEASE_SECONDS = 2.0
_CLAIM_TIMEOUT = 15.0


def _registry() -> ToolRegistry:
    """Same tool name as ``_crash_worker_proc``. Never actually called here --
    a dangling call is settled, not retried -- but the Spec named it, so an
    offer-computation touching the registry (narrowing, the failure-streak
    advisory) must find it."""
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def slow_carrier_lookup(order_id: str) -> str:
        """Check a shipment's status with a slow carrier API."""
        return f"{order_id}: in transit"

    return registry


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    dsn = postgres_dsn()
    try:
        store = PostgresStore(dsn=dsn)
        try:
            await asyncio.wait_for(store.migrate(), timeout=5.0)
        finally:
            await store.close()
    except Exception as err:
        return f"PostgreSQL is not reachable at {dsn!r} ({err}); run scripts/dev-services.sh start"
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    dsn = postgres_dsn()
    scope = psych_runtime.Scope(tenant="acme", principal="ops")

    store = PostgresStore(dsn=dsn)
    await store.migrate()
    await _truncate(store)

    with tempfile.NamedTemporaryFile(prefix="psych-crash-marker-", delete=False) as marker:
        marker_path = marker.name

    try:
        await emit("publish", "publishing an agent that names a slow, blocking tool")
        spec = AgentSpec(
            name="shipment-tracker",
            instructions="Look up shipments for the customer.",
            model=ModelRef(model="fake-standard"),
            tools=(CodeTool(name="slow_carrier_lookup"),),
        )
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(
            store, version.hash, scope, input={"message": "where is order A1?"}
        )
        run_id = dispatched.run_id

        await _spawn_and_kill_doomed_worker(
            store, dsn, run_id, marker_path, checks=checks, emit=emit
        )
        report, log = await _reclaim_with_second_worker(store, run_id, checks, emit)
        _assert_recovery(checks, report, log, marker_path)

        return checks.result(
            "a killed Worker's Run was reclaimed, the dangling call settled honestly, "
            "and the tool never re-ran",
            run_ids=[str(run_id)],
            report={"report": report_dict(report), "records": records_dict(log)},
        )
    finally:
        await store.close()
        with contextlib.suppress(OSError):
            Path(marker_path).unlink()


async def _spawn_and_kill_doomed_worker(
    store: PostgresStore,
    dsn: str,
    run_id: RunId,
    marker_path: str,
    *,
    checks: Checks,
    emit: ProgressFn,
) -> None:
    """Spawns the real OS process, waits for it to genuinely be mid-tool-call,
    then SIGKILLs it -- see this module's docstring for why nothing here is
    simulated."""
    await emit("spawn", f"spawning a real OS process to run this Worker (lease={_LEASE_SECONDS}s)")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.scenarios._crash_worker_proc",
        dsn,
        str(run_id),
        str(_LEASE_SECONDS),
        marker_path,
        cwd=str(_BACKEND_DIR),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await emit("wait", "waiting for the doomed process's log to show tool_call_started")
        saw_started = await _wait_for_tool_call_started(store, run_id, timeout=_CLAIM_TIMEOUT)
        checks.require(
            "the doomed Worker actually started the tool call before it was killed",
            saw_started,
            "tool_call_started never appeared in the log within the timeout"
            if not saw_started
            else "tool_call_started observed in the real Postgres log",
        )

        await emit("kill", f"sending SIGKILL to pid {proc.pid}")
        proc.kill()
        returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
        checks.require(
            "the process was really killed, not stopped cooperatively",
            returncode == -9 or returncode != 0,
            f"subprocess exit status: {returncode}",
        )
    finally:
        if proc.returncode is None:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


async def _reclaim_with_second_worker(
    store: PostgresStore, run_id: RunId, checks: Checks, emit: ProgressFn
) -> tuple[RunReport, Sequence[Record]]:
    """Waits out the dead Worker's lease with a second, independent Worker,
    then reads back the report and log a reclaim must produce."""
    await emit(
        "reclaim", "starting a second, independent Worker in this process to wait out the lease"
    )
    second_model = FakeModel().turn(
        text="I couldn't confirm that in time, but I've flagged it for a human to check."
    )
    second_runtime = Runtime(store=store, model=second_model, registry=_registry())
    second_worker = Worker(store, second_runtime, poll_interval=0.05, supervisor_interval=0.2)
    worker_task = asyncio.create_task(second_worker.run())
    try:
        settled = await _await_settled(store, run_id, timeout=_LEASE_SECONDS + 20.0)
    finally:
        second_worker.stop()
        await asyncio.wait_for(worker_task, timeout=10.0)

    checks.require(
        "a second Worker reclaimed the Run and settled it",
        settled,
        f"settled within {_LEASE_SECONDS + 20.0:.0f}s of the crash: {settled}"
        if settled
        else f"the Run never reached a settled state within {_LEASE_SECONDS + 20.0:.0f}s",
    )

    report = await psych_runtime.report(store, run_id)
    log = await psych_runtime.records(store, run_id)
    return report, log


def _assert_recovery(
    checks: Checks, report: RunReport, log: Sequence[Record], marker_path: str
) -> None:
    attempts = [r for r in log if r.type == "attempt_started"]
    checks.require(
        "the log shows two Attempts, by two different Workers",
        len(attempts) == 2 and attempts[0].worker_id != attempts[1].worker_id,
        f"worker ids: {[a.worker_id for a in attempts]}",
    )
    second_reclaimed = attempts[1].reclaimed_expired_lease if len(attempts) == 2 else None
    checks.require(
        "the second Attempt is recorded as a reclaim of an expired lease",
        len(attempts) == 2 and attempts[1].reclaimed_expired_lease,
        f"reclaimed_expired_lease={second_reclaimed!r}",
    )
    checks.require(
        "the orphaned tool call settled as UNKNOWN rather than being dropped or retried",
        len(report.tool_calls) == 1 and report.tool_calls[0].outcome is ToolOutcome.UNKNOWN,
        f"tool_calls={[(c.tool, c.outcome) for c in report.tool_calls]}",
    )
    # A Run still in flight has no terminal state. Reading .value unguarded
    # would raise AttributeError and tell a reader nothing about what went
    # wrong -- report the assertion as failed instead.
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the Run still reached COMPLETED after the crash",
        report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={terminal!r}",
    )

    marker_lines = Path(marker_path).read_text(encoding="utf-8").splitlines()
    checks.require(
        "the tool's body executed exactly once -- in the killed process, never again",
        len(marker_lines) == 1,
        f"marker file has {len(marker_lines)} line(s): {marker_lines!r}",
    )


async def _truncate(store: PostgresStore) -> None:
    """Clears this scenario's own dedicated test database before it runs, so
    the reclaiming Worker below claims *this* Run and not some unrelated
    runnable row left over from a previous pytest run against the same
    ``psych_test`` database. Safe here in a way it would not be against a
    real deployment's store: this database exists only for
    ``scripts/dev-services.sh``'s own test suites."""
    pool = await store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE psych_records, psych_versions, psych_runs")


async def _wait_for_tool_call_started(
    store: PostgresStore, run_id: RunId, *, timeout: float
) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        log = await store.read(run_id)
        if any(r.type == "tool_call_started" for r in log):
            return True
        await asyncio.sleep(0.05)
    return False


async def _await_settled(store: PostgresStore, run_id: RunId, *, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        state = await psych_runtime.state(store, run_id)
        if state.settled:
            return True
        await asyncio.sleep(0.05)
    return False
