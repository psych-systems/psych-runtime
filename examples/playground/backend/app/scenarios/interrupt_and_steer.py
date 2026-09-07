"""DESIGN.md §23 item 3: interrupt during a tool call, then steer.

An interrupt while a tool call is genuinely in flight is a race, not a
courtesy: ``psych_runtime.interrupt()`` appends ``abort_requested`` through its own
fresh ``Journal``, and the Attempt that is still running holds a *stale*
local sequence number (DESIGN.md §6's single-writer rule -- see
``psych_runtime.runtime.journal``'s own docstring on why a losing writer is refused
rather than retried). Whichever side loses that race finds out the hard way:
its next append conflicts.

This scenario does not soften that. It dispatches a Run against a tool that
genuinely sleeps, waits for the log to show the tool call actually started,
interrupts it mid-flight, and immediately dispatches a second Run carrying a
follow-up message -- proving the three things DESIGN.md actually promises: the
interrupted Run never reaches a normal completion, ``abort_requested`` precedes
its ``run_settled`` in the log, and the follow-up message starts and finishes
a wholly separate Run. It does *not* assert the interrupted Run's exact
terminal state, and says why in its own result: on this exact race, the
in-flight Attempt's next write loses to the interrupt's, raises
``SeqConflict``, and ``Worker._settle_failure`` records a plain
``TerminalState.FAILED`` rather than recognising the abort already sitting in
the log and settling ``ABORTED``. The Run still stops, and the log still
tells the whole story in order -- which is what this item is actually
checking -- but a reader who expects a tidy ``ABORTED`` on this exact timing
should see the real value instead of a rounded one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import TerminalState
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="interrupt-and-steer",
    title="Interrupt and steer",
    proves=(
        "An interrupt landing while a tool call is genuinely in flight stops "
        "the Run, and a message dispatched immediately afterward starts a new "
        "Run carrying it -- both visible in the log, in order."
    ),
    design_ref="§23.3",
    requires=(),
)

_TOOL_SLEEP_SECONDS = 0.4
"""Real wall-clock sleep, not scripted: the interrupt has to land while an
actual ``asyncio.to_thread`` call is still outstanding, which needs the tool
to still be running when the scenario checks for it."""


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def slow_carrier_lookup(order_id: str) -> str:
        """Check a shipment's status with a slow carrier API."""
        time.sleep(_TOOL_SLEEP_SECONDS)
        return f"{order_id}: in transit"

    return registry


def _spec() -> AgentSpec:
    return AgentSpec(
        name="support",
        instructions="Look up shipments for the customer.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="slow_carrier_lookup"),),
    )


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    scope = psych_runtime.Scope(tenant="acme", principal="user-1")

    spec = _spec()
    version = await psych_runtime.publish(store, spec)

    await emit("dispatch", "dispatching the first Run: 'where is order A1?'")
    first = await psych_runtime.dispatch(
        store, version, scope, input={"message": "where is order A1?"}
    )

    # One shared Worker (and so one shared Runtime and model) claims whatever
    # is runnable, which will be both Runs before this is done -- so this
    # scripts two turns, not one: the first Run's single call before it is
    # interrupted mid-tool-call, and the second Run's own first call once it
    # is dispatched to the same running Worker.
    model = (
        FakeModel()
        .turn(
            text="Checking on that now.",
            tool_calls=[("slow_carrier_lookup", {"order_id": "A1"})],
        )
        .turn(text="Checking on A2 instead.")
    )
    runtime = Runtime(store=store, model=model, registry=_registry())
    worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    worker_task = asyncio.create_task(worker.run())

    try:
        await emit("wait", "waiting for the tool call to actually start")
        saw_started = await _wait_for_record_type(
            store, first.run_id, "tool_call_started", timeout=5.0
        )
        checks.require(
            "the tool call was genuinely in flight before the interrupt landed",
            saw_started,
            "tool_call_started never appeared before the timeout"
            if not saw_started
            else "observed in the log while the tool was still sleeping",
        )

        await emit("interrupt", "interrupting the first Run while the tool call is still running")
        await psych_runtime.interrupt(
            store, first.run_id, reason="the user pressed stop mid-tool-call"
        )

        await emit("steer", "dispatching a follow-up message immediately, before the first settles")
        second = await psych_runtime.dispatch(
            store, version, scope, input={"message": "actually, check order A2 instead"}
        )

        await emit("settle", "waiting for both Runs to reach a terminal state")
        first_settled = await _await_settled(store, first.run_id, timeout=10.0)
        second_settled = await _await_settled(store, second.run_id, timeout=10.0)
    finally:
        worker.stop()
        await asyncio.wait_for(worker_task, timeout=10.0)

    first_log = await psych_runtime.records(store, first.run_id)
    second_log = await psych_runtime.records(store, second.run_id)
    first_kinds = [r.type for r in first_log]
    second_kinds = [r.type for r in second_log]

    checks.require(
        "the interrupted Run reached a terminal record",
        first_settled and first_kinds[-1] == "run_settled",
        f"first Run's record kinds: {first_kinds}",
    )
    checks.require(
        "abort_requested precedes run_settled in the interrupted Run's log",
        "abort_requested" in first_kinds
        and first_kinds.index("abort_requested") < first_kinds.index("run_settled"),
        f"first Run's record kinds in order: {first_kinds}",
    )

    # A Run still in flight has no terminal state. Reading .value unguarded
    # would raise AttributeError and tell a reader nothing about what went
    # wrong -- report the assertion as failed instead.
    first_report = await psych_runtime.report(store, first.run_id)
    first_terminal = first_report.terminal_state.value if first_report.terminal_state else None
    checks.require(
        "the interrupted Run never reached a normal completion",
        first_report.terminal_state is not None
        and first_report.terminal_state is not TerminalState.COMPLETED,
        f"terminal_state={first_terminal!r} "
        "(FAILED here means the still-running Attempt's own write lost the race "
        "to the interrupt's -- see this module's docstring)",
    )

    second_report = await psych_runtime.report(store, second.run_id)
    second_terminal = (
        second_report.terminal_state.value if second_report.terminal_state is not None else None
    )
    checks.require(
        "the follow-up message started and completed an entirely separate Run",
        second_settled
        and second.run_id != first.run_id
        and second_report.terminal_state is TerminalState.COMPLETED
        and second_kinds[-1] == "run_settled",
        f"second run_id={second.run_id}, terminal_state={second_terminal!r}",
    )

    return checks.result(
        "the interrupt stopped the first Run mid-tool-call and the follow-up "
        "message ran to completion as its own Run",
        run_ids=[str(first.run_id), str(second.run_id)],
        report={
            "first_run": {"report": report_dict(first_report), "records": records_dict(first_log)},
            "second_run": {
                "report": report_dict(second_report),
                "records": records_dict(second_log),
            },
        },
    )


async def _wait_for_record_type(
    store: Store, run_id: RunId, record_type: str, *, timeout: float
) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        log = await store.read(run_id)
        if any(r.type == record_type for r in log):
            return True
        await asyncio.sleep(0.01)
    return False


async def _await_settled(store: Store, run_id: RunId, *, timeout: float) -> bool:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        state = await psych_runtime.state(store, run_id)
        if state.settled:
            return True
        await asyncio.sleep(0.02)
    return False
