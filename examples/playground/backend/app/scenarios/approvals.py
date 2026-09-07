"""Approvals: a destructive tool suspends the Run, releases its lease, and a
human decision resumes it from another process.

DESIGN.md §11 (suspend and resume) and §10.9 (the approval selector). The
property this checks is that suspension holds nothing in memory: the pending
call, the reason and the expiry all live in the log, so the Worker that
resumes a suspended Run need never be the Worker that suspended it -- this
scenario resumes with a completely fresh ``Runtime`` and journal, the way a
human clicking "approve" in a separate process actually would.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import SuspendReason, TerminalState, ToolOutcome
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="approvals",
    title="Approvals",
    proves=(
        "A destructive tool call suspends the Run and releases its lease "
        "rather than blocking a Worker; a human decision made afterward, in "
        "a wholly separate Runtime, resumes it and either runs the call or "
        "records a clean denial."
    ),
    design_ref="§11, §10.9",
    requires=(),
)


def _registry(effects: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"destructive"})
    def refund(order_id: str) -> str:
        """Refund an order."""
        effects.append(f"refunded {order_id}")
        return f"refunded {order_id}"

    return registry


def _spec() -> AgentSpec:
    return AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="refund", interruptible=False),),
    )


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def _drive_attempt(
    store: InMemoryStore,
    run_id: RunId,
    model: FakeModel,
    registry: ToolRegistry,
    *,
    worker_id: str,
    attempt_number: int,
    approval_selectors: tuple[str, ...] = (),
) -> None:
    """One Attempt, in its own fresh journal and Runtime -- exactly the shape
    a separate process resuming a suspended Run would use, never sharing
    state with whichever Attempt suspended it."""
    journal = await Journal.open(store, run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id=worker_id, attempt_number=attempt_number)
    runtime = Runtime(
        store=store, model=model, registry=registry, approval_selectors=approval_selectors
    )
    header = await store.get_run(run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    effects: list[str] = []
    registry = _registry(effects)
    spec = _spec()

    await emit("dispatch", "dispatching a Run whose only move is a destructive tool call")
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(
        store, version, SCOPE_A, input={"message": "refund A1"}
    )
    await _drive_attempt(
        store,
        dispatched.run_id,
        FakeModel().turn(tool_calls=[("refund", {"order_id": "A1"})]),
        registry,
        worker_id="wrk_1",
        attempt_number=1,
        approval_selectors=("@destructive",),
    )

    state = await psych_runtime.state(store, dispatched.run_id)
    checks.require(
        "the Run suspended rather than running the destructive call or failing",
        state.suspended and state.suspend_reason is SuspendReason.APPROVAL,
        f"suspended={state.suspended}, suspend_reason={state.suspend_reason}",
    )
    checks.require(
        "nothing happened while the Run waited -- the tool never ran",
        effects == [],
        f"effects={effects!r}",
    )

    await emit("approve", "a human approves, from a wholly separate process, and resume it")
    await psych_runtime.resume(store, dispatched.run_id, approved=True)
    await _drive_attempt(
        store,
        dispatched.run_id,
        FakeModel().turn(text="Refund issued."),
        registry,
        worker_id="wrk_2",
        attempt_number=2,
    )

    report = await psych_runtime.report(store, dispatched.run_id)
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the approved call actually ran, on the second Worker, and the Run completed",
        effects == ["refunded A1"] and report.terminal_state is TerminalState.COMPLETED,
        f"effects={effects!r}, terminal_state={terminal!r}",
    )
    checks.require(
        "the report shows exactly one suspension, and it is recorded as approved",
        len(report.suspensions) == 1 and report.suspensions[0].approved is True,
        f"suspensions={report.suspensions!r}",
    )

    await emit("deny", "a second Run, denied instead of approved, for the other outcome")
    denied = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "refund A2"})
    await _drive_attempt(
        store,
        denied.run_id,
        FakeModel().turn(tool_calls=[("refund", {"order_id": "A2"})]),
        registry,
        worker_id="wrk_3",
        attempt_number=1,
        approval_selectors=("@destructive",),
    )
    await psych_runtime.resume(store, denied.run_id, approved=False)
    await _drive_attempt(
        store,
        denied.run_id,
        FakeModel().turn(text="That refund was not approved."),
        registry,
        worker_id="wrk_4",
        attempt_number=2,
    )

    denied_state = await psych_runtime.state(store, denied.run_id)
    denial = denied_state.tool_results[0]
    denial_kind = denial.failure.kind if denial.failure else None
    checks.require(
        "a denied call is recorded as a clean denial, not a crash, and never runs",
        denial.outcome is ToolOutcome.ERROR
        and denial_kind == "denied"
        and effects == ["refunded A1"],  # unchanged: A2 never actually refunded
        f"outcome={denial.outcome}, failure_kind={denial_kind!r}, effects={effects!r}",
    )

    log = await psych_runtime.records(store, dispatched.run_id)
    deny_log = await psych_runtime.records(store, denied.run_id)
    return checks.result(
        "an approved refund ran on a later Worker and a denied one recorded a clean "
        "denial, both resumed from outside the process that suspended them",
        run_ids=[str(dispatched.run_id), str(denied.run_id)],
        report={
            "approved_run": {"report": report_dict(report), "records": records_dict(log)},
            "denied_run": {"records": records_dict(deny_log)},
        },
    )
