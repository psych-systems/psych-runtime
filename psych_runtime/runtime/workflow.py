"""The workflow engine: walk the step tree, remember what finished.

DESIGN.md §5. A Workflow is a Step that sequences other Steps deterministically.
A workflow step may be an Agent; an Agent's tool may be a Workflow; a Step may be
a nested Run. Recursion falls out, and there is one durability implementation
rather than two.

## Memoisation, not replay

Each Step has a deterministic id derived from its position in the Version plus
the Run id, and its result is written to the log. On resume, a Step whose result
is already in the log returns from the log and is not re-executed.

DESIGN.md §5 chooses this over Temporal-style deterministic replay deliberately.
Replay imposes determinism rules on surrounding code that consumers will violate:
a step that reads the clock, or a dict that iterates in a different order, breaks
a replay engine in a way that is very hard to debug. Memoisation gives the
property that actually matters, which is that a crashed Run continues where it
stopped, and asks nothing of the consumer's code.

The cost is honest: memoisation cannot re-derive a result it never wrote, so a
step that crashed halfway through re-runs from its start. That is why a step is
the unit of restart and why steps should be the size of a thing you would be
willing to repeat. The same rule covers a parallel branch whose sibling
suspended: the sibling's in-flight steps are cancelled and re-run from their
own starts when the Run wakes, while everything they had already completed
stays completed.

## The tree

Composite steps (parallel, branch, foreach, loop, nested workflow) are steps
too: each opens its own ``step_started``, runs its children with paths that
extend its own, and closes with ``step_completed`` carrying the children's
combined output. So the log alone reconstructs the tree, and the view in
``psych_runtime.core.workflow_view`` does exactly that without the Spec.

## Waiting without a Worker

A step that waits -- a ``sleep``, a retry backoff, a ``wait`` for an event, a
``human`` question, a tool step's approval, a breakpoint -- suspends the Run
(DESIGN.md §11) with the step named on the ``suspended`` record. The engine
returns, the Worker releases the lease, and whichever Worker claims the Run
next re-enters this walk: memoised steps return from the log, the walk reaches
the waiting step, finds the ``resumed`` that answered it in the reducer's
``step_resumes``, and continues the same attempt. Nothing about a wait lives in
a process.

## Why the step id cannot be random

If it were, a resumed Run would compute new ids, find no memoised results, and
re-execute completed steps. Every one of the properties above rests on
``derive_step_id`` being a pure function of the Run id and the step's path.
"""

from __future__ import annotations

import asyncio
import contextlib
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import StepId, derive_step_id
from psych_runtime.core.questions import render_questions
from psych_runtime.core.records import SuspendReason, TerminalState, ToolFailure
from psych_runtime.core.reducer import RunStateView, StepRecord, StepResume
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    BranchStep,
    ForEachStep,
    HumanStep,
    LoopStep,
    MapStep,
    ParallelStep,
    RetryPolicy,
    SetStateStep,
    SleepStep,
    SuspensionPolicy,
    ToolStep,
    WaitStep,
    WorkflowSpec,
    WorkflowStep,
    WorkflowStepRef,
)
from psych_runtime.core.workflow_values import (
    SchemaViolation,
    UnresolvedPath,
    WorkflowScope,
    evaluate_condition,
    resolve_mapping,
    resolve_path,
    validate_schema,
)
from psych_runtime.runtime.abort import AbortReason, AbortSignal
from psych_runtime.runtime.journal import Journal
from psych_runtime.telemetry.port import NOOP_TELEMETRY, SpanStatus, Telemetry
from psych_runtime.tools.guidance import failure_guidance
from psych_runtime.tools.policy import Decision

__all__ = [
    "AgentStepRunner",
    "Parker",
    "StepContext",
    "StepFailed",
    "StepOutcome",
    "StepStatus",
    "ToolGate",
    "ToolStepRunner",
    "WorkflowEngine",
    "WorkflowOutcome",
]

_COMPOSITE: Final = (ParallelStep, BranchStep, ForEachStep, LoopStep, WorkflowStepRef)
"""Step kinds whose work is their children's."""

_TIMER_GRACE: Final = timedelta(days=7)
"""How long past its wake time a parked Run may go unclaimed before the
suspension is treated as expired. A timer needs no answer, so the expiry is
only a backstop against a fleet that never comes back."""


class StepFailed(Exception):
    """A workflow step's agent ended in a failure, carried whole.

    The engine records ``ToolFailure(kind=type(err).__name__, ...)`` for a step
    that raised, so re-raising a nested agent's failure as a bare
    ``RuntimeError`` renamed every kind to ``RuntimeError`` and dropped the
    traceback the agent had already built. This carries the original so the
    step's record says what actually failed.
    """

    def __init__(self, failure: ToolFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class StepStatus(StrEnum):
    """How one step's execution ended, as the engine sees it."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    SUSPENDED = "suspended"
    """The step is waiting and the Run must release its lease."""
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What one step produced."""

    name: str
    status: StepStatus
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    memoised: bool = False
    """True when the result came from the log rather than from executing. What a
    test asserts to prove a resumed workflow did not re-run completed work."""

    @property
    def stops_sequence(self) -> bool:
        return self.status in (StepStatus.SUSPENDED, StepStatus.ABORTED)


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    """How a whole workflow walk ended.

    ``state`` is ``None`` when the Run suspended: it has not ended, it is
    waiting, and the caller must not settle it.
    """

    state: TerminalState | None
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a runner needs to know about the step it runs inside: the id to
    stamp on the records it writes, and the span to open its own under."""

    step_id: StepId
    telemetry: Telemetry


AgentStepRunner = Callable[
    [Journal, AgentSpec, dict[str, Any], StepContext], Awaitable[dict[str, Any] | None]
]
"""Runs an agent step. Injected so the workflow engine does not import the agent
loop and the two can be tested apart."""

ToolStepRunner = Callable[[Journal, str, dict[str, Any]], Awaitable[Any]]
"""Runs a tool step's call, after the gate allowed it."""

ToolGate = Callable[[str, dict[str, Any]], Awaitable[Decision]]
"""Asks the consumer's Policy about a tool step's call."""

Parker = Callable[[datetime], Awaitable[None]]
"""Parks the Run until a time (``Store.set_runnable_at``). Injected because the
engine owns no store handle; the Journal is its only write path."""


@dataclass(frozen=True, slots=True)
class _Frame:
    """Where the walk is: the scope a step reads from and its place in the tree."""

    scope: WorkflowScope
    path: tuple[str | int, ...]
    parent_step_id: StepId | None
    iteration: int | None = None
    telemetry: Telemetry = NOOP_TELEMETRY
    """Where this step's span opens: the attempt span at the top, the parent
    step's span below it, so the trace nests the way the log does."""


@dataclass(frozen=True, slots=True)
class _SequenceResult:
    outcome: StepOutcome | None
    """The outcome that stopped the sequence early, or ``None`` when it ran
    through."""
    scope: WorkflowScope
    outputs: dict[str, Any]


class WorkflowEngine:
    """Executes a workflow's steps, memoising each one."""

    def __init__(
        self,
        journal: Journal,
        agent_runner: AgentStepRunner,
        tool_runner: ToolStepRunner,
        *,
        tool_gate: ToolGate | None = None,
        park: Parker | None = None,
        abort: AbortSignal | None = None,
        now: Callable[[], datetime] | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._journal = journal
        self._agent_runner = agent_runner
        self._tool_runner = tool_runner
        self._tool_gate = tool_gate
        self._park = park
        self._abort = abort
        self._telemetry = telemetry if telemetry is not None else NOOP_TELEMETRY
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._root: WorkflowSpec | None = None
        self._initial_state: dict[str, Any] = {}

    # -- entry --------------------------------------------------------------

    async def run(
        self, spec: WorkflowSpec, *, path: tuple[str | int, ...] = ()
    ) -> tuple[TerminalState, dict[str, Any]]:
        """Run the workflow. Kept for callers that only want the ending.

        Returns ``COMPLETED`` with the outputs while the Run is suspended, the
        way the earlier engine did; ``execute`` tells the two apart.
        """
        outcome = await self.execute(spec, path=path)
        return (outcome.state or TerminalState.COMPLETED, outcome.output or {})

    async def execute(
        self, spec: WorkflowSpec, *, path: tuple[str | int, ...] = ()
    ) -> WorkflowOutcome:
        """Walk the whole workflow from wherever the log says it stopped.

        Args:
            spec: the workflow.
            path: this workflow's position, for a caller driving a nested one
                directly. The root passes nothing.
        """
        self._root = spec
        state = self._journal.state
        self._initial_state = dict(spec.initial_state)

        if spec.input_schema is not None:
            try:
                validate_schema(state.run_input, spec.input_schema)
            except SchemaViolation as err:
                return WorkflowOutcome(
                    TerminalState.FAILED,
                    failure=_failure(
                        "schema_violation", f"the Run's input does not satisfy input_schema: {err}"
                    ),
                )

        frame = _Frame(
            scope=WorkflowScope(input=dict(state.run_input), state=self._current_state()),
            path=path,
            parent_step_id=None,
            telemetry=self._telemetry,
        )
        result = await self._sequence(spec, frame)
        if result.outcome is not None:
            match result.outcome.status:
                case StepStatus.SUSPENDED:
                    return WorkflowOutcome(None, output=result.outputs)
                case StepStatus.ABORTED:
                    return WorkflowOutcome(TerminalState.ABORTED, output=result.outputs)
                case _:
                    return WorkflowOutcome(
                        TerminalState.FAILED,
                        output=result.outputs,
                        failure=result.outcome.failure,
                    )

        output = result.outputs
        if spec.output is not None:
            try:
                output = resolve_mapping(result.scope, spec.output)
            except UnresolvedPath as err:
                return WorkflowOutcome(
                    TerminalState.FAILED,
                    output=result.outputs,
                    failure=_failure(
                        "unresolved_path", f"the workflow's output mapping did not resolve: {err}"
                    ),
                )
        return WorkflowOutcome(TerminalState.COMPLETED, output=output)

    # -- sequences ----------------------------------------------------------

    async def _sequence(self, spec: WorkflowSpec, frame: _Frame) -> _SequenceResult:
        """Run ``spec.steps`` in order under ``frame``, stopping at the first
        outcome that cannot be continued past."""
        scope = frame.scope
        outputs: dict[str, Any] = {}
        for index, step in enumerate(spec.steps):
            stopped = await self._stopped()
            if stopped is not None:
                return _SequenceResult(stopped, scope, outputs)
            child = replace(frame, scope=scope, path=(*frame.path, spec.name, index, step.name))
            outcome = await self._step(step, child)
            scope = self._after(scope, step, outcome)
            outputs[step.name] = outcome.output
            if outcome.stops_sequence:
                return _SequenceResult(outcome, scope, outputs)
            if outcome.status is StepStatus.FAILED and step.on_failure == "fail":
                return _SequenceResult(outcome, scope, outputs)
            if step.ends_workflow and outcome.status is StepStatus.COMPLETED:
                # An early, successful exit. The steps after it are not
                # skipped-and-recorded: they were never considered, and the
                # view shows them pending, which is what happened.
                return _SequenceResult(None, scope, outputs)
        return _SequenceResult(None, scope, outputs)

    def _after(
        self, scope: WorkflowScope, step: WorkflowStep, outcome: StepOutcome
    ) -> WorkflowScope:
        """The scope the next step reads, with this one's result in it."""
        entry = {
            "output": outcome.output,
            "failure": outcome.failure.model_dump(mode="json") if outcome.failure else None,
            "skipped": outcome.status is StepStatus.SKIPPED,
            "status": outcome.status.value,
        }
        scope = scope.with_step(step.name, entry)
        # The state is read back from the log, never tracked here: a set_state
        # inside a parallel branch, a loop body or a branch arm is folded by
        # the reducer the moment its completion lands, so every reader sees
        # the same state a reclaiming Worker would derive.
        return scope.with_state(self._current_state())

    def _current_state(self) -> dict[str, Any]:
        """The workflow state right now: ``initial_state`` under every
        completed ``set_state`` step, as the reducer folded them."""
        return {**self._initial_state, **self._journal.state.workflow_state_updates}

    async def _stopped(self) -> StepOutcome | None:
        """An abort, if one has landed. Refreshes the journal so an interrupt
        written by the consumer is seen before the next step starts rather than
        at the next append."""
        await self._journal.refresh()
        if self._journal.state.aborted or (
            self._abort is not None
            and self._abort.is_set()
            and self._abort.reason is not AbortReason.DEADLINE
        ):
            return StepOutcome(name="", status=StepStatus.ABORTED)
        return None

    # -- one step -----------------------------------------------------------

    async def _step(  # noqa: PLR0911 - one return per state the log can be in
        self, step: WorkflowStep, frame: _Frame
    ) -> StepOutcome:
        """Run one step, or return what the log already says about it."""
        step_id = derive_step_id(self._journal.run_id, frame.path)
        memo = self._journal.state.steps.get(step_id)

        if memo is not None and memo.settled:
            # The whole point of memoisation. This step ran on an earlier
            # attempt and its result is in the log, so it is not run again.
            return StepOutcome(
                name=step.name,
                status=_status_of(memo),
                output=memo.output,
                failure=memo.failure,
                memoised=True,
            )

        if memo is not None and memo.completed and memo.will_retry:
            # A failed attempt whose retry is due, or whose backoff has not
            # passed. The backoff parks the Run; a resume that woke it (its
            # own timer, or a person impatient with the timer) is in the log.
            if memo.retry_at is not None and self._now() < memo.retry_at:
                woken = _resume_after(self._journal.state, step_id, memo.completed_seq or 0)
                if woken is None:
                    return await self._suspend_timer(step, step_id, memo.retry_at)
            return await self._attempt(step, frame, step_id, memo.attempt_number + 1)

        if memo is not None and not memo.completed:
            # Started and not finished. Either this Attempt's predecessor died
            # mid-step, in which case the step re-runs from its start as a new
            # attempt, or the step suspended and has now been answered, in
            # which case the same attempt continues where it stopped.
            if _resume_after(self._journal.state, step_id, memo.started_seq) is not None:
                return await self._continue(step, frame, step_id, memo)
            return await self._attempt(step, frame, step_id, memo.attempt_number + 1)

        # Never started. A breakpoint, if one is set here, comes before the
        # step's own start record so a paused step reads as "about to run".
        paused = await self._breakpoint(step, step_id)
        if paused is not None:
            return paused
        return await self._attempt(step, frame, step_id, 1)

    async def _attempt(
        self, step: WorkflowStep, frame: _Frame, step_id: StepId, attempt_number: int
    ) -> StepOutcome:
        """Start ``attempt_number`` of ``step`` and drive it to an outcome."""
        limits = self._root.limits if self._root is not None else None
        if limits is not None and self._journal.state.step_starts >= limits.max_steps:
            failure = _failure(
                "budget_exhausted",
                f"the workflow has started {self._journal.state.step_starts} steps, which is "
                f"its Limits.max_steps of {limits.max_steps}. Loops and foreach bodies count "
                "once per iteration; raise the limit or bound the loop.",
            )
            # Recorded as this step's failure so the report says where the
            # budget ran out, without a start record that would push past it.
            await self._journal.append(
                type="step_started",
                step_id=step_id,
                name=step.name,
                kind=step.kind,
                attempt_number=attempt_number,
                input={},
                path=frame.path,
                parent_step_id=frame.parent_step_id,
                iteration=frame.iteration,
            )
            await self._journal.append(type="step_completed", step_id=step_id, failure=failure)
            return StepOutcome(name=step.name, status=StepStatus.FAILED, failure=failure)

        if step.when is not None:
            try:
                holds = evaluate_condition(frame.scope, step.when)
            except UnresolvedPath as err:
                return await self._record_failure(
                    step,
                    frame,
                    step_id,
                    attempt_number,
                    step_input={},
                    failure=_failure(
                        "unresolved_path", f"the step's `when` did not resolve: {err}"
                    ),
                )
            if not holds:
                await self._journal.append(
                    type="step_started",
                    step_id=step_id,
                    name=step.name,
                    kind=step.kind,
                    attempt_number=attempt_number,
                    input={},
                    path=frame.path,
                    parent_step_id=frame.parent_step_id,
                    iteration=frame.iteration,
                )
                await self._journal.append(type="step_completed", step_id=step_id, skipped=True)
                return StepOutcome(name=step.name, status=StepStatus.SKIPPED)

        try:
            step_input = _step_input(step, frame.scope)
        except UnresolvedPath as err:
            return await self._record_failure(
                step,
                frame,
                step_id,
                attempt_number,
                step_input={},
                failure=_failure("unresolved_path", f"the step's input did not resolve: {err}"),
            )

        await self._journal.append(
            type="step_started",
            step_id=step_id,
            name=step.name,
            kind=step.kind,
            attempt_number=attempt_number,
            input=step_input,
            path=frame.path,
            parent_step_id=frame.parent_step_id,
            iteration=frame.iteration,
        )
        return await self._drive(step, frame, step_id, attempt_number, step_input)

    async def _continue(
        self, step: WorkflowStep, frame: _Frame, step_id: StepId, memo: StepRecord
    ) -> StepOutcome:
        """Pick up an attempt that suspended and has been answered."""
        return await self._drive(step, frame, step_id, memo.attempt_number, memo.input)

    async def _drive(
        self,
        step: WorkflowStep,
        frame: _Frame,
        step_id: StepId,
        attempt_number: int,
        step_input: dict[str, Any],
    ) -> StepOutcome:
        """Execute a started attempt, record how it ended, and retry if allowed."""
        async with frame.telemetry.start_span(
            "psych.step",
            attributes={
                "psych.step.id": step_id,
                "psych.step.name": step.name,
                "psych.step.kind": step.kind,
                "psych.step.attempt_number": attempt_number,
            },
        ) as span:
            outcome = await self._drive_in_span(
                step, replace(frame, telemetry=span), step_id, attempt_number, step_input
            )
            span.set_attributes({"psych.step.outcome": _span_outcome(outcome)})
            if outcome.status is StepStatus.FAILED and outcome.failure is not None:
                span.set_status(SpanStatus.ERROR, outcome.failure.kind)
            return outcome

    async def _drive_in_span(  # noqa: PLR0911 - one return per way an attempt ends
        self,
        step: WorkflowStep,
        frame: _Frame,
        step_id: StepId,
        attempt_number: int,
        step_input: dict[str, Any],
    ) -> StepOutcome:
        try:
            result = await self._execute(step, frame, step_id, step_input)
        except StepFailed as err:
            # The nested agent's own failure: its kind, its message, its
            # traceback. Nothing about this frame is what the reader needs.
            return await self._failed(step, frame, step_id, attempt_number, err.failure)
        except TimeoutError:
            failure = _failure(
                "step_timeout",
                f"step {step.name!r} ran past its timeout of {step.timeout_seconds} seconds "
                "and was stopped.",
            )
            return await self._failed(step, frame, step_id, attempt_number, failure)
        except UnresolvedPath as err:
            return await self._failed(
                step, frame, step_id, attempt_number, _failure("unresolved_path", str(err))
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            failure = ToolFailure(
                kind=_kind_of_exception(err),
                message=failure_guidance(_kind_of_exception(err), str(err)),
                traceback=traceback.format_exc(),
            )
            return await self._failed(step, frame, step_id, attempt_number, failure)

        if result.status is not StepStatus.COMPLETED:
            # Suspended or aborted inside: the start record stays open and the
            # same attempt continues when the Run is claimed again.
            if result.status is StepStatus.FAILED:
                if isinstance(step, _COMPOSITE):
                    # A composite is not retried as a whole: its failed child
                    # is already settled in the log, so another attempt would
                    # find it memoised and fail again for nothing. Retries
                    # belong to the child that failed.
                    await self._journal.append(
                        type="step_completed", step_id=step_id, failure=result.failure
                    )
                    return StepOutcome(
                        name=step.name, status=StepStatus.FAILED, failure=result.failure
                    )
                return await self._failed(step, frame, step_id, attempt_number, result.failure)
            return result

        if step.output_schema is not None and result.output is not None:
            try:
                validate_schema(result.output, step.output_schema)
            except SchemaViolation as err:
                failure = _failure(
                    "schema_violation",
                    f"step {step.name!r} produced output that does not satisfy its "
                    f"output_schema: {err}",
                )
                return await self._failed(step, frame, step_id, attempt_number, failure)

        await self._journal.append(type="step_completed", step_id=step_id, output=result.output)
        return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=result.output)

    async def _failed(
        self,
        step: WorkflowStep,
        frame: _Frame,
        step_id: StepId,
        attempt_number: int,
        failure: ToolFailure | None,
    ) -> StepOutcome:
        """Record a failed attempt, and retry it when the policy allows."""
        failure = failure or _failure("step_failed", f"step {step.name!r} failed")
        policy = self._retry_policy(step)
        retrying = (
            attempt_number < policy.max_attempts
            and (not policy.retry_on or failure.kind in policy.retry_on)
            # A person said no. Asking again is not a retry, it is nagging.
            and failure.kind != "denied"
        )
        if not retrying:
            await self._journal.append(type="step_completed", step_id=step_id, failure=failure)
            return StepOutcome(name=step.name, status=StepStatus.FAILED, failure=failure)

        delay = policy.delay_before(attempt_number + 1)
        retry_at = self._now() + timedelta(seconds=delay)
        await self._journal.append(
            type="step_completed",
            step_id=step_id,
            failure=failure,
            will_retry=True,
            retry_at=retry_at,
        )
        if delay > 0:
            return await self._suspend_timer(step, step_id, retry_at)
        stopped = await self._stopped()
        if stopped is not None:
            return stopped
        return await self._attempt(step, frame, step_id, attempt_number + 1)

    async def _record_failure(
        self,
        step: WorkflowStep,
        frame: _Frame,
        step_id: StepId,
        attempt_number: int,
        *,
        step_input: dict[str, Any],
        failure: ToolFailure,
    ) -> StepOutcome:
        """A failure found before the step could start: recorded as a started
        and completed attempt so the log shows the step was reached."""
        await self._journal.append(
            type="step_started",
            step_id=step_id,
            name=step.name,
            kind=step.kind,
            attempt_number=attempt_number,
            input=step_input,
            path=frame.path,
            parent_step_id=frame.parent_step_id,
            iteration=frame.iteration,
        )
        return await self._failed(step, frame, step_id, attempt_number, failure)

    def _retry_policy(self, step: WorkflowStep) -> RetryPolicy:
        if step.retry is not None:
            return step.retry
        return self._root.retry if self._root is not None else RetryPolicy()

    # -- execution by kind --------------------------------------------------

    async def _execute(  # noqa: PLR0911 - one return per step kind
        self, step: WorkflowStep, frame: _Frame, step_id: StepId, step_input: dict[str, Any]
    ) -> StepOutcome:
        """Run a started step. Returns COMPLETED with its output, or the
        suspension/abort/failure that stopped it."""
        inner = replace(frame, parent_step_id=step_id)
        match step:
            case ToolStep():
                return await self._tool(step, step_id, step_input)
            case AgentStep():
                output = await self._timed(
                    step,
                    self._agent_runner(
                        self._journal,
                        step.spec,
                        step_input,
                        StepContext(step_id=step_id, telemetry=frame.telemetry),
                    ),
                )
                return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=output)
            case WorkflowStepRef():
                return await self._nested(step, inner, step_input)
            case ParallelStep():
                return await self._parallel(step, inner)
            case BranchStep():
                return await self._branch(step, inner)
            case ForEachStep():
                return await self._foreach(step, inner)
            case LoopStep():
                return await self._loop(step, inner)
            case MapStep():
                return StepOutcome(
                    name=step.name,
                    status=StepStatus.COMPLETED,
                    output=resolve_mapping(frame.scope, step.output),
                )
            case SetStateStep():
                return StepOutcome(
                    name=step.name,
                    status=StepStatus.COMPLETED,
                    output=resolve_mapping(frame.scope, step.values),
                )
            case SleepStep():
                return await self._sleep(step, frame, step_id)
            case WaitStep():
                return await self._wait(step, step_id)
            case HumanStep():
                return await self._human(step, step_id)

    async def _timed[T](self, step: WorkflowStep, work: Awaitable[T]) -> T:
        if step.timeout_seconds is None:
            return await work
        return await asyncio.wait_for(work, timeout=step.timeout_seconds)

    async def _tool(
        self, step: ToolStep, step_id: StepId, step_input: dict[str, Any]
    ) -> StepOutcome:
        """A tool step, gated exactly like a model's tool call.

        A policy that asks for a human decision suspends the Run on this step,
        and the decision arrives through ``psych_runtime.resume(approved=...)``
        keyed to the step. A denial is a failure of kind ``denied``.
        """
        arguments = dict(step_input.get("arguments", {}))
        if self._tool_gate is not None:
            decision = await self._tool_gate(step.tool, arguments)
            if decision.requires_approval:
                answer = _resume_after(
                    self._journal.state,
                    step_id,
                    self._journal.state.steps[step_id].started_seq,
                    reason=SuspendReason.APPROVAL,
                )
                if answer is None:
                    return await self._suspend(
                        step,
                        step_id,
                        SuspendReason.APPROVAL,
                        expires_in=self._suspension().approval_expires_seconds,
                        question=(
                            decision.reason
                            or f"Approve the call to {step.tool!r} with arguments {arguments!r}?"
                        ),
                    )
                if answer.approved is not True:
                    raise StepFailed(
                        _failure(
                            "denied",
                            f"the call to {step.tool!r} was not approved"
                            + (
                                f" by {answer.payload.get('by')}"
                                if answer.payload.get("by")
                                else ""
                            )
                            + ".",
                        )
                    )
            elif not decision.allowed:
                raise StepFailed(
                    _failure("denied", decision.reason or f"the policy refused tool {step.tool!r}")
                )
        result = await self._timed(step, self._tool_runner(self._journal, step.tool, arguments))
        return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output={"result": result})

    async def _nested(
        self, step: WorkflowStepRef, frame: _Frame, step_input: dict[str, Any]
    ) -> StepOutcome:
        """A nested workflow shares the parent's journal and log. One Run, one
        log, one resume path: nesting does not fork durability. Its steps are
        a fresh namespace fed by the step's ``input`` mapping; the workflow
        state is shared, so a nested ``set_state`` is visible to its parent."""
        inner_input = step_input.get("input", frame.scope.input)
        nested_frame = replace(
            frame,
            scope=WorkflowScope(input=dict(inner_input), state=self._current_state()),
            path=frame.path,
        )
        result = await self._sequence(step.spec, nested_frame)
        if result.outcome is not None:
            if result.outcome.status is StepStatus.FAILED:
                return StepOutcome(
                    name=step.name, status=StepStatus.FAILED, failure=result.outcome.failure
                )
            return result.outcome
        output = result.outputs
        if step.spec.output is not None:
            output = resolve_mapping(result.scope, step.spec.output)
        return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=output)

    async def _parallel(self, step: ParallelStep, frame: _Frame) -> StepOutcome:
        frames = [
            replace(frame, path=(*frame.path, index, branch.name))
            for index, branch in enumerate(step.branches)
        ]
        children: list[WorkflowStep] = list(step.branches)
        outcomes = await self._concurrently(
            [(branch, child) for branch, child in zip(step.branches, frames, strict=True)],
            limit=len(step.branches),
            fail_fast=step.on_branch_failure == "fail_fast",
        )
        stopped = _first_stop(outcomes)
        if stopped is not None:
            return stopped
        failed = _blocking_failure(children, outcomes)
        if failed is not None:
            return StepOutcome(name=step.name, status=StepStatus.FAILED, failure=failed.failure)
        return StepOutcome(
            name=step.name,
            status=StepStatus.COMPLETED,
            output={
                branch.name: o.output for branch, o in zip(step.branches, outcomes, strict=True)
            },
        )

    async def _branch(self, step: BranchStep, frame: _Frame) -> StepOutcome:
        chosen: list[tuple[WorkflowStep, _Frame]] = []
        skipped: list[tuple[WorkflowStep, _Frame]] = []
        for index, case in enumerate(step.cases):
            child = replace(frame, path=(*frame.path, index, case.step.name))
            holds = evaluate_condition(frame.scope, case.when)
            if holds and (step.mode == "all" or not chosen):
                chosen.append((case.step, child))
            else:
                skipped.append((case.step, child))
        if not chosen and step.otherwise is not None:
            chosen.append(
                (
                    step.otherwise,
                    replace(frame, path=(*frame.path, "otherwise", step.otherwise.name)),
                )
            )
        elif step.otherwise is not None:
            skipped.append(
                (
                    step.otherwise,
                    replace(frame, path=(*frame.path, "otherwise", step.otherwise.name)),
                )
            )

        # A case that was not chosen is still recorded, as skipped, so the
        # log says the branch considered it rather than leaving a reader to
        # infer that from a name missing under `chosen`.
        for inner, child in skipped:
            await self._skip(inner, child)

        children = [inner for inner, _ in chosen]
        outcomes = await self._concurrently(chosen, limit=max(len(chosen), 1), fail_fast=True)
        stopped = _first_stop(outcomes)
        if stopped is not None:
            return stopped
        failed = _blocking_failure(children, outcomes)
        if failed is not None:
            return StepOutcome(name=step.name, status=StepStatus.FAILED, failure=failed.failure)
        output: dict[str, Any] = {"chosen": [inner.name for inner, _ in chosen]}
        for (inner, _), outcome in zip(chosen, outcomes, strict=True):
            output[inner.name] = outcome.output
        return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=output)

    async def _skip(self, step: WorkflowStep, frame: _Frame) -> None:
        step_id = derive_step_id(self._journal.run_id, frame.path)
        memo = self._journal.state.steps.get(step_id)
        if memo is not None and memo.completed:
            return
        await self._journal.append(
            type="step_started",
            step_id=step_id,
            name=step.name,
            kind=step.kind,
            attempt_number=1 if memo is None else memo.attempt_number + 1,
            input={},
            path=frame.path,
            parent_step_id=frame.parent_step_id,
            iteration=frame.iteration,
        )
        await self._journal.append(type="step_completed", step_id=step_id, skipped=True)

    async def _foreach(self, step: ForEachStep, frame: _Frame) -> StepOutcome:
        items = resolve_path(frame.scope, step.items.path)
        if not isinstance(items, list | tuple):
            raise UnresolvedPath(
                step.items.path, f"a foreach needs a list; got {type(items).__name__}"
            )
        work = [
            (
                step.body,
                replace(
                    frame,
                    scope=frame.scope.inside(item=item, index=index),
                    path=(*frame.path, index, step.body.name),
                    iteration=index,
                ),
            )
            for index, item in enumerate(items)
        ]
        children = [step.body for _ in work]
        outcomes = await self._concurrently(
            work, limit=step.concurrency, fail_fast=step.on_item_failure == "fail_fast"
        )
        stopped = _first_stop(outcomes)
        if stopped is not None:
            return stopped
        failed = _blocking_failure(children, outcomes)
        if failed is not None:
            return StepOutcome(name=step.name, status=StepStatus.FAILED, failure=failed.failure)
        return StepOutcome(
            name=step.name,
            status=StepStatus.COMPLETED,
            output={"items": [o.output for o in outcomes], "count": len(outcomes)},
        )

    async def _loop(self, step: LoopStep, frame: _Frame) -> StepOutcome:
        scope = frame.scope
        last: dict[str, Any] | None = None
        for iteration in range(1, step.max_iterations + 1):
            stopped = await self._stopped()
            if stopped is not None:
                return stopped
            child = replace(
                frame,
                scope=scope.inside(iteration=iteration),
                path=(*frame.path, iteration, step.body.name),
                iteration=iteration,
            )
            outcome = await self._step(step.body, child)
            scope = self._after(scope, step.body, outcome)
            if outcome.stops_sequence:
                return outcome
            if outcome.status is StepStatus.FAILED and step.body.on_failure == "fail":
                return StepOutcome(
                    name=step.name, status=StepStatus.FAILED, failure=outcome.failure
                )
            last = outcome.output
            positioned = scope.inside(iteration=iteration)
            if step.until is not None and evaluate_condition(positioned, step.until):
                return StepOutcome(
                    name=step.name,
                    status=StepStatus.COMPLETED,
                    output={"iterations": iteration, "last": last},
                )
            if step.while_ is not None and not evaluate_condition(positioned, step.while_):
                return StepOutcome(
                    name=step.name,
                    status=StepStatus.COMPLETED,
                    output={"iterations": iteration, "last": last},
                )
        return StepOutcome(
            name=step.name,
            status=StepStatus.FAILED,
            failure=_failure(
                "loop_exhausted",
                f"loop {step.name!r} ran its max_iterations of {step.max_iterations} without "
                "its exit condition holding.",
            ),
        )

    async def _concurrently(
        self,
        work: Sequence[tuple[WorkflowStep, _Frame]],
        *,
        limit: int,
        fail_fast: bool,
    ) -> list[StepOutcome]:
        """Run several steps at once, ``limit`` at a time, sharing the journal.

        A suspension or an abort in any one of them cancels the rest: the Run
        is about to release its lease, and a sibling still appending after
        that would be a writer with no lease. Its in-flight steps re-run from
        their starts when the Run wakes; its completed ones are memoised. With
        ``fail_fast`` a failure cancels the rest the same way.
        """
        if not work:
            return []
        gate = asyncio.Semaphore(limit)
        results: dict[int, StepOutcome] = {}
        stop = asyncio.Event()

        async def one(position: int, step: WorkflowStep, frame: _Frame) -> None:
            async with gate:
                if stop.is_set():
                    return
                outcome = await self._step(step, frame)
                results[position] = outcome
                if outcome.stops_sequence or (
                    fail_fast and outcome.status is StepStatus.FAILED and step.on_failure == "fail"
                ):
                    stop.set()

        tasks = [
            asyncio.create_task(one(position, step, frame))
            for position, (step, frame) in enumerate(work)
        ]
        stopper = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
            while not stop.is_set() and not all(task.done() for task in tasks):
                await asyncio.wait(
                    [*[t for t in tasks if not t.done()], stopper],
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            stopper.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for task in tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error
        # A sibling cancelled because another child suspended or aborted is
        # reported as that, so the composite propagates the real stop. One
        # cancelled because a sibling failed is a failure of its own kind: it
        # never ran to an end, and calling it aborted would settle the Run as
        # stopped when nobody stopped it.
        real_stop = next((o for o in results.values() if o.stops_sequence), None)
        cancelled_status = real_stop.status if real_stop is not None else StepStatus.FAILED
        return [
            results.get(
                position,
                StepOutcome(
                    name=step.name,
                    status=cancelled_status,
                    failure=(
                        _failure(
                            "cancelled",
                            f"step {step.name!r} was cancelled because a sibling failed first.",
                        )
                        if cancelled_status is StepStatus.FAILED
                        else None
                    ),
                ),
            )
            for position, (step, _) in enumerate(work)
        ]

    # -- waiting ------------------------------------------------------------

    async def _sleep(self, step: SleepStep, frame: _Frame, step_id: StepId) -> StepOutcome:
        started = self._journal.state.steps[step_id]
        woken = _resume_after(self._journal.state, step_id, started.started_seq)
        if woken is not None:
            return StepOutcome(
                name=step.name,
                status=StepStatus.COMPLETED,
                output={"woken_by": woken.payload.get("woken_by", "resume")},
            )
        if step.seconds is not None:
            wake_at = self._now() + timedelta(seconds=step.seconds)
        else:
            assert step.until is not None  # the Spec validator guarantees one of the two
            raw = resolve_path(frame.scope, step.until.path)
            if not isinstance(raw, str):
                raise UnresolvedPath(
                    step.until.path, "a sleep's `until` must be an ISO 8601 string"
                )
            try:
                wake_at = datetime.fromisoformat(raw)
            except ValueError as err:
                raise UnresolvedPath(step.until.path, f"not an ISO 8601 timestamp: {err}") from err
            if wake_at.tzinfo is None:
                wake_at = wake_at.replace(tzinfo=UTC)
        if wake_at <= self._now():
            return StepOutcome(
                name=step.name, status=StepStatus.COMPLETED, output={"woken_by": "elapsed"}
            )
        return await self._suspend_timer(step, step_id, wake_at)

    async def _wait(self, step: WaitStep, step_id: StepId) -> StepOutcome:
        started = self._journal.state.steps[step_id]
        answer = _resume_after(
            self._journal.state, step_id, started.started_seq, reason=SuspendReason.EXTERNAL
        )
        if answer is not None:
            if step.payload_schema:
                try:
                    validate_schema(answer.payload, step.payload_schema)
                except SchemaViolation as err:
                    raise StepFailed(
                        _failure(
                            "schema_violation",
                            f"the payload delivered for event {step.event!r} does not satisfy "
                            f"payload_schema: {err}",
                        )
                    ) from err
            return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=answer.payload)
        return await self._suspend(
            step,
            step_id,
            SuspendReason.EXTERNAL,
            expires_in=step.timeout_seconds or self._suspension().external_expires_seconds,
            event=step.event,
            payload_schema=step.payload_schema,
            question=f"waiting for event {step.event!r}",
        )

    async def _human(self, step: HumanStep, step_id: StepId) -> StepOutcome:
        started = self._journal.state.steps[step_id]
        answer = _resume_after(
            self._journal.state, step_id, started.started_seq, reason=SuspendReason.QUESTION
        )
        if answer is not None:
            return StepOutcome(name=step.name, status=StepStatus.COMPLETED, output=answer.payload)
        return await self._suspend(
            step,
            step_id,
            SuspendReason.QUESTION,
            expires_in=step.expires_seconds or self._suspension().question_expires_seconds,
            question=step.prompt if not step.questions else render_questions(step.questions),
            questions=step.questions,
        )

    async def _breakpoint(self, step: WorkflowStep, step_id: StepId) -> StepOutcome | None:
        """Pause before ``step`` if this Run asked to. ``None`` to go on."""
        state = self._journal.state
        if not (state.step_mode or step.name in state.breakpoints):
            return None
        # Answered already: the resume for this step's breakpoint is in the
        # log, so this is the re-entry after the pause.
        answers = state.step_resumes.get(step_id, ())
        if any(a.reason is SuspendReason.BREAKPOINT for a in answers):
            return None
        return await self._suspend(
            step,
            step_id,
            SuspendReason.BREAKPOINT,
            expires_in=self._suspension().question_expires_seconds,
            question=f"paused before step {step.name!r}",
        )

    async def _suspend_timer(
        self, step: WorkflowStep, step_id: StepId, wake_at: datetime
    ) -> StepOutcome:
        if self._park is None:
            # No store to park in (a caller driving the engine directly): wait
            # in place, holding this coroutine and nothing else. No suspension
            # is recorded, because none happened: the lease was never released.
            await asyncio.sleep(max((wake_at - self._now()).total_seconds(), 0))
            if isinstance(step, SleepStep):
                return StepOutcome(
                    name=step.name, status=StepStatus.COMPLETED, output={"woken_by": "elapsed"}
                )
            return await self._step_again(step, step_id)
        await self._journal.append(
            type="suspended",
            reason=SuspendReason.TIMER,
            step_id=step_id,
            step_name=step.name,
            wake_at=wake_at,
            expires_at=wake_at + _TIMER_GRACE,
            question=f"step {step.name!r} waits until {wake_at.isoformat()}",
        )
        await self._park(wake_at)
        return StepOutcome(name=step.name, status=StepStatus.SUSPENDED)

    async def _step_again(self, step: WorkflowStep, step_id: StepId) -> StepOutcome:
        """After an in-process timer wait for a retry backoff, run the next
        attempt. Only reached without a parker."""
        memo = self._journal.state.steps[step_id]
        frame = _Frame(
            scope=WorkflowScope(
                input=dict(self._journal.state.run_input), state=self._current_state()
            ),
            path=memo.path,
            parent_step_id=memo.parent_step_id,
            iteration=memo.iteration,
        )
        return await self._attempt(step, frame, step_id, memo.attempt_number + 1)

    async def _suspend(
        self,
        step: WorkflowStep,
        step_id: StepId,
        reason: SuspendReason,
        *,
        expires_in: float,
        question: str,
        event: str | None = None,
        payload_schema: dict[str, Any] | None = None,
        questions: tuple[Any, ...] = (),
    ) -> StepOutcome:
        await self._journal.append(
            type="suspended",
            reason=reason,
            step_id=step_id,
            step_name=step.name,
            event=event,
            payload_schema=payload_schema or {},
            expires_at=self._now() + timedelta(seconds=expires_in),
            question=question,
            questions=questions,
        )
        return StepOutcome(name=step.name, status=StepStatus.SUSPENDED)

    def _suspension(self) -> SuspensionPolicy:
        return self._root.suspension if self._root is not None else SuspensionPolicy()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _failure(kind: str, message: str) -> ToolFailure:
    return ToolFailure(kind=kind, message=failure_guidance(kind, message))


def _kind_of_exception(err: Exception) -> str:
    if isinstance(err, AccessDenied):
        return "denied"
    return type(err).__name__


def _span_outcome(outcome: StepOutcome) -> str:
    match outcome.status:
        case StepStatus.COMPLETED:
            return "succeeded"
        case StepStatus.FAILED:
            return "failed"
        case StepStatus.SKIPPED:
            return "skipped"
        case StepStatus.SUSPENDED:
            return "suspended"
        case StepStatus.ABORTED:
            return "aborted"


def _status_of(memo: StepRecord) -> StepStatus:
    if memo.skipped:
        return StepStatus.SKIPPED
    if memo.failure is not None:
        return StepStatus.FAILED
    return StepStatus.COMPLETED


def _blocking_failure(
    children: Sequence[WorkflowStep], outcomes: Sequence[StepOutcome]
) -> StepOutcome | None:
    """The failure that fails a composite, if any.

    A child whose ``on_failure`` is ``continue`` failed on its own terms and
    the composite carries ``None`` for it. Among the rest, a real failure
    outranks a sibling that was merely cancelled because of it.
    """
    blocking = [
        outcome
        for child, outcome in zip(children, outcomes, strict=True)
        if outcome.status is StepStatus.FAILED and child.on_failure == "fail"
    ]
    if not blocking:
        return None
    return next(
        (o for o in blocking if o.failure is None or o.failure.kind != "cancelled"), blocking[0]
    )


def _first_stop(outcomes: Sequence[StepOutcome]) -> StepOutcome | None:
    for status in (StepStatus.ABORTED, StepStatus.SUSPENDED):
        for outcome in outcomes:
            if outcome.status is status:
                return outcome
    return None


def _resume_after(
    state: RunStateView, step_id: StepId, seq: int, *, reason: SuspendReason | None = None
) -> StepResume | None:
    """The answer delivered to ``step_id`` after log position ``seq``, if any.

    Matching on position rather than presence is what lets one step suspend
    more than once: a breakpoint before it and then the event it waits for
    both leave an answer under the same step, and each attempt reads only the
    answers that came after it began.
    """
    for answer in state.step_resumes.get(step_id, ()):
        if answer.seq > seq and (reason is None or answer.reason is reason):
            return answer
    return None


def _step_input(step: WorkflowStep, scope: WorkflowScope) -> dict[str, Any]:
    """What a step receives, resolved, so the log records what it was given.

    Every step's record carries the scope it could read from in summary form
    (``prior``: each completed step's output by name), and its own resolved
    inputs beside that. Deliberately not a template language: a workflow that
    needs to compute a value between steps uses a ``map`` step, where the
    shaping is visible in the log like everything else.
    """
    prior = {name: entry.get("output") for name, entry in scope.steps.items()}
    match step:
        case ToolStep():
            arguments = {**step.arguments, **resolve_mapping(scope, step.arguments_from)}
            return {"tool": step.tool, "arguments": arguments, "prior": prior}
        case AgentStep() | WorkflowStepRef():
            resolved = resolve_mapping(scope, step.input) if step.input else None
            return {"input": resolved, "prior": prior} if resolved is not None else {"prior": prior}
        case ForEachStep():
            return {"items": resolve_path(scope, step.items.path), "prior": prior}
        case _:
            return {"prior": prior}
