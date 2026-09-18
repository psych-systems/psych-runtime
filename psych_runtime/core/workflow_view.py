"""What a workflow Run looks like right now, as a tree a person can read.

``RunStateView.steps`` is a flat map of step id to memoised result, which is
what the engine needs and not what anyone debugging a workflow wants to look
at. This projects it back into the shape the Spec has: every step in the
workflow, in order, with its status, its attempts, what it was given and what
it produced, its children (branches, loop iterations, foreach items, a nested
workflow's own steps), and which step the Run is waiting on if it is waiting.

Pure, like ``status_of``: Spec plus fold in, tree out. The same view a UI
renders and a test asserts on, so the two cannot disagree about whether a
branch was taken.

## Static shape, dynamic children

The Spec fixes most of the tree, so a step that has not run yet is still here,
as ``pending``, which is what makes a graph show where the Run is going and
not only where it has been. Two step kinds have children the Spec cannot
count -- a ``foreach`` has as many as the list had elements, a ``loop`` as
many as it iterated -- so those are read from the log, by parent id.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from psych_runtime.core.ids import RunId, StepId, derive_step_id
from psych_runtime.core.records import SuspendReason, TerminalState, ToolFailure
from psych_runtime.core.reducer import RunStateView, StepRecord
from psych_runtime.core.spec import (
    BranchStep,
    ForEachStep,
    LoopStep,
    ParallelStep,
    WorkflowSpec,
    WorkflowStep,
    WorkflowStepRef,
)

__all__ = ["StepView", "StepViewStatus", "WaitingStep", "WorkflowView", "workflow_view"]


class StepViewStatus(StrEnum):
    """Where one step is, in the words a graph node would use."""

    PENDING = "pending"
    """Not reached yet."""
    RUNNING = "running"
    """Started, and the log ends before it completed. For a Run still
    executing that means in flight; for a Run whose Worker died, dangling."""
    WAITING = "waiting"
    """The Run is suspended on this step."""
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    """Its last attempt failed and another is allowed."""
    SKIPPED = "skipped"
    REPLAYED = "replayed"
    """Copied from an earlier Run by ``psych_runtime.replay()`` rather than run here."""


class StepView(BaseModel):
    """One step in the tree, with every attempt it has made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: StepId
    name: str
    kind: str
    path: tuple[str | int, ...]
    status: StepViewStatus
    attempts: int
    """Attempts started so far, retries included. 0 while pending."""
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    failure: ToolFailure | None
    started_at: datetime | None
    completed_at: datetime | None
    retry_at: datetime | None
    iteration: int | None
    replayed_from: RunId | None
    child_run_id: RunId | None
    children: tuple[StepView, ...] = ()
    """Branches, iterations, items, or a nested workflow's steps."""
    cases: tuple[str, ...] = ()
    """For a ``branch`` step: its case names in order, so a graph can label the
    arms before any is chosen."""


class WaitingStep(BaseModel):
    """The step the Run is suspended on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: StepId
    name: str
    reason: SuspendReason
    question: str | None
    event: str | None
    wake_at: datetime | None
    expires_at: datetime | None


class WorkflowView(BaseModel):
    """A workflow Run as a tree of steps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    workflow: str
    steps: tuple[StepView, ...]
    waiting: WaitingStep | None
    state: dict[str, Any]
    """The workflow state: ``initial_state`` under every completed
    ``set_state`` step."""
    input: dict[str, Any]
    output: dict[str, Any] | None
    terminal_state: TerminalState | None
    step_starts: int
    """Every step start so far, against ``Limits.max_steps``."""
    completed: int
    failed: int
    replays_run_id: RunId | None
    replay_from_step: str | None
    breakpoints: tuple[str, ...]
    step_mode: bool


def workflow_view(spec: WorkflowSpec, state: RunStateView) -> WorkflowView:
    """Project one fold into the workflow's tree. Pure."""
    by_parent: dict[StepId | None, list[StepRecord]] = {}
    for record in state.steps.values():
        by_parent.setdefault(record.parent_step_id, []).append(record)
    for children in by_parent.values():
        children.sort(key=lambda record: record.started_seq)

    builder = _Builder(state, by_parent)
    steps = builder.sequence(spec, path=())
    waiting = _waiting(state)
    settled = [record for record in state.steps.values() if record.settled]
    return WorkflowView(
        run_id=state.run_id,
        workflow=spec.name,
        steps=steps,
        waiting=waiting,
        state={**spec.initial_state, **state.workflow_state_updates},
        input=dict(state.run_input),
        output=state.output,
        terminal_state=state.terminal_state,
        step_starts=state.step_starts,
        completed=sum(1 for record in settled if record.failure is None and not record.skipped),
        failed=sum(1 for record in settled if record.failure is not None),
        replays_run_id=state.replays_run_id,
        replay_from_step=state.replay_from_step,
        breakpoints=state.breakpoints,
        step_mode=state.step_mode,
    )


class _Builder:
    def __init__(
        self, state: RunStateView, by_parent: dict[StepId | None, list[StepRecord]]
    ) -> None:
        self._state = state
        self._by_parent = by_parent

    def sequence(self, spec: WorkflowSpec, path: tuple[str | int, ...]) -> tuple[StepView, ...]:
        return tuple(
            self.step(step, (*path, spec.name, index, step.name))
            for index, step in enumerate(spec.steps)
        )

    def step(self, step: WorkflowStep, path: tuple[str | int, ...]) -> StepView:
        step_id = derive_step_id(self._state.run_id, path)
        record = self._state.steps.get(step_id)
        children: tuple[StepView, ...] = ()
        cases: tuple[str, ...] = ()
        match step:
            case ParallelStep():
                children = tuple(
                    self.step(branch, (*path, index, branch.name))
                    for index, branch in enumerate(step.branches)
                )
            case BranchStep():
                cases = tuple(case.name for case in step.cases)
                arms = [
                    self.step(case.step, (*path, index, case.step.name))
                    for index, case in enumerate(step.cases)
                ]
                if step.otherwise is not None:
                    arms.append(
                        self.step(step.otherwise, (*path, "otherwise", step.otherwise.name))
                    )
                children = tuple(arms)
            case ForEachStep() | LoopStep():
                # As many children as the log shows: the Spec cannot count
                # them. Each is the body at its own iteration.
                children = tuple(
                    self.step(step.body, child.path) for child in self._by_parent.get(step_id, ())
                )
            case WorkflowStepRef():
                children = self.sequence(step.spec, path)
            case _:
                pass
        return _view(step, step_id, path, record, children, cases, self._state)


def _view(  # noqa: PLR0917 - a projection with one argument per source
    step: WorkflowStep,
    step_id: StepId,
    path: tuple[str | int, ...],
    record: StepRecord | None,
    children: tuple[StepView, ...],
    cases: tuple[str, ...],
    state: RunStateView,
) -> StepView:
    if record is None:
        # A breakpoint pauses before the step's own start record, so a step
        # with no record can still be the one the Run is waiting on.
        paused = state.suspended and state.suspended_step_id == step_id
        return StepView(
            step_id=step_id,
            name=step.name,
            kind=step.kind,
            path=path,
            status=StepViewStatus.WAITING if paused else StepViewStatus.PENDING,
            attempts=0,
            input=None,
            output=None,
            failure=None,
            started_at=None,
            completed_at=None,
            retry_at=None,
            iteration=None,
            replayed_from=None,
            child_run_id=None,
            children=children,
            cases=cases,
        )
    return StepView(
        step_id=step_id,
        name=record.name,
        kind=record.kind,
        path=record.path or path,
        status=_status(record, state),
        attempts=record.attempt_number,
        input=record.input,
        output=record.output,
        failure=record.failure,
        started_at=record.started_at,
        completed_at=record.completed_at,
        retry_at=record.retry_at,
        iteration=record.iteration,
        replayed_from=record.replayed_from,
        child_run_id=record.child_run_id,
        children=children,
        cases=cases,
    )


def _status(record: StepRecord, state: RunStateView) -> StepViewStatus:  # noqa: PLR0911 - one per status
    if state.suspended and state.suspended_step_id == record.step_id:
        return StepViewStatus.WAITING
    if not record.completed:
        return StepViewStatus.RUNNING
    if record.replayed_from is not None:
        return StepViewStatus.REPLAYED
    if record.skipped:
        return StepViewStatus.SKIPPED
    if record.will_retry:
        return StepViewStatus.RETRYING
    if record.failure is not None:
        return StepViewStatus.FAILED
    return StepViewStatus.COMPLETED


def _waiting(state: RunStateView) -> WaitingStep | None:
    if not state.suspended or state.suspended_step_id is None or state.suspend_reason is None:
        return None
    record = state.steps.get(state.suspended_step_id)
    return WaitingStep(
        step_id=state.suspended_step_id,
        name=state.suspended_step_name or (record.name if record is not None else ""),
        reason=state.suspend_reason,
        question=state.suspend_question,
        event=state.suspend_event,
        wake_at=state.suspend_wake_at,
        expires_at=state.suspend_expires_at,
    )


StepView.model_rebuild()
