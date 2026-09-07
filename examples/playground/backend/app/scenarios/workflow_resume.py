"""DESIGN.md §23 item 5: a workflow resumes without repeating a side effect.

A workflow step's result is memoised in the log the moment it completes
(DESIGN.md §9), so a Worker that crashes between two steps and a fresh one
that picks the Run back up must produce the same side effects a Worker that
never crashed would -- each step's body executed exactly once, not zero and
not twice. Proving that from the *outside* (asserting the workflow finishes)
would pass even if a completed step quietly re-ran; this scenario proves it
from a side-effect counter instead, cancelling the driving task only after
the log shows two steps genuinely completed (not merely started -- cancelling
between a tool running and its completion record landing is a real crash in
the gap, and that step correctly re-runs, so the cut has to land past the
write, not before it).

Mirrors ``tests/e2e/test_regression_gate.py``'s
``test_6_a_workflow_resumes_without_repeating_a_side_effect``, reached
through ``psych_runtime.runtime.workflow.WorkflowEngine`` directly for the first
half (so it can be interrupted mid-flight) and through a real ``Worker`` for
the resume, the same way a consumer's own crash recovery would look.
"""

from __future__ import annotations

import asyncio

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, await_settled, records_dict, report_dict
from psych_runtime.core.ids import RunId, WorkerId
from psych_runtime.core.records import TerminalState
from psych_runtime.core.spec import AgentSpec, AgentStep, CodeTool, ModelRef, ToolStep, WorkflowSpec
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="workflow-resume",
    title="Workflow resume",
    proves=(
        "A workflow with a nested agent resumes from its last completed step "
        "after a crash and does not re-execute completed steps, proven by a "
        "side-effect counter rather than by the workflow merely finishing."
    ),
    design_ref="§23.5",
    requires=(),
)

_POLL_ATTEMPTS = 600
_POLL_INTERVAL = 0.005


def _registry(effects: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def once(label: str) -> str:
        """A step whose executions are counted."""
        effects.append(f"once:{label}")
        return f"{label} done"

    return registry


def _workflow() -> WorkflowSpec:
    return WorkflowSpec(
        name="nightly-workflow",
        tools=(CodeTool(name="once"),),
        steps=(
            ToolStep(name="first", tool="once", arguments={"label": "first"}),
            ToolStep(name="second", tool="once", arguments={"label": "second"}),
            AgentStep(
                name="wrap",
                spec=AgentSpec(
                    name="wrapper",
                    instructions="Summarise what happened.",
                    model=ModelRef(model="fake-standard"),
                    tools=(),
                ),
            ),
        ),
    )


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def _drive_until_crash(
    store: InMemoryStore,
    run_id: RunId,
    workflow: WorkflowSpec,
    registry: ToolRegistry,
    *,
    checks: Checks,
    emit: ProgressFn,
) -> None:
    """Runs the workflow directly through ``WorkflowEngine`` (not a
    ``Worker``, so it can be cancelled mid-flight) until the log shows two
    steps genuinely completed, then simulates a crash by cancelling it."""
    await emit("drive", "running the workflow directly, with the agent step stalled")
    journal = await Journal.open(store, run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    stalling = Runtime(store=store, model=FakeModel().stalls(seconds=30), registry=registry)
    engine = stalling.engine_for(journal)
    attempt = asyncio.create_task(engine.run(workflow))

    await emit("wait", "waiting for the log to show two steps genuinely completed")
    saw_two_completed = False
    for _ in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_INTERVAL)
        state = await psych_runtime.state(store, run_id)
        if len([s for s in state.steps.values() if s.completed]) == 2:
            saw_two_completed = True
            break
    checks.require(
        "the first two steps completed (in the log) before the crash was simulated",
        saw_two_completed,
        "both steps' completion records were observed in the log"
        if saw_two_completed
        else f"never saw two completed steps within {_POLL_ATTEMPTS * _POLL_INTERVAL:.1f}s",
    )

    await emit("crash", "cancelling the driving task mid-third-step, like a killed Worker")
    attempt.cancel()
    cancelled_cleanly = False
    try:
        await attempt
    except asyncio.CancelledError:
        cancelled_cleanly = True
    checks.require(
        "the simulated crash actually interrupted the workflow",
        cancelled_cleanly,
        f"asyncio.CancelledError raised: {cancelled_cleanly}",
    )


async def _resume_after_crash(
    store: InMemoryStore, run_id: RunId, registry: ToolRegistry, *, checks: Checks, emit: ProgressFn
) -> None:
    await emit("resume", "releasing the lease and resuming with a fresh Worker")
    await store.release(run_id, WorkerId("wrk_1"), RunState.RUNNABLE)
    runtime = Runtime(store=store, model=FakeModel().turn(text="wrapped"), registry=registry)
    worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    worker_task = asyncio.create_task(worker.run())
    try:
        settled = await await_settled(store, run_id, timeout=10.0)
    finally:
        worker.stop()
        await asyncio.wait_for(worker_task, timeout=10.0)
    checks.require(
        "the resumed Run reached a settled state", settled, f"settled within 10s: {settled}"
    )


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    effects: list[str] = []
    registry = _registry(effects)

    await emit("publish", "publishing a workflow: two tool steps, then a nested agent")
    workflow = _workflow()
    version = await psych_runtime.publish(store, workflow)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE_A)
    run_id = dispatched.run_id

    await _drive_until_crash(store, run_id, workflow, registry, checks=checks, emit=emit)
    checks.require(
        "each tool step's side effect ran exactly once before the crash",
        list(effects) == ["once:first", "once:second"],
        f"effects={effects!r}",
    )

    await _resume_after_crash(store, run_id, registry, checks=checks, emit=emit)
    checks.require(
        "neither completed step's side effect ran again across the crash",
        effects == ["once:first", "once:second"],
        f"effects after resume={effects!r}",
    )

    report = await psych_runtime.report(store, run_id)
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the workflow reached COMPLETED after resuming",
        report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={terminal!r}",
    )

    log = await psych_runtime.records(store, run_id)
    return checks.result(
        "the workflow resumed past two already-completed steps and neither re-ran",
        run_ids=[str(run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
