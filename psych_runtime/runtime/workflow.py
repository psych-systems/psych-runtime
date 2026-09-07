"""The workflow engine: sequence steps, remember what finished.

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
willing to repeat.

## Why the step id cannot be random

If it were, a resumed Run would compute new ids, find no memoised results, and
re-execute completed steps. Every one of the properties above rests on
``derive_step_id`` being a pure function of the Run id and the step's path.
"""

from __future__ import annotations

import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from psych_runtime.core.ids import derive_step_id
from psych_runtime.core.records import TerminalState, ToolFailure
from psych_runtime.core.reducer import StepRecord
from psych_runtime.core.spec import AgentSpec, AgentStep, ToolStep, WorkflowSpec, WorkflowStepRef
from psych_runtime.runtime.journal import Journal
from psych_runtime.tools.guidance import failure_guidance

__all__ = ["StepOutcome", "WorkflowEngine", "WorkflowRunner"]


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """What one step produced."""

    name: str
    output: dict[str, Any] | None
    failure: ToolFailure | None
    memoised: bool
    """True when the result came from the log rather than from executing. What a
    test asserts to prove a resumed workflow did not re-run completed work."""


AgentStepRunner = Callable[[Journal, AgentSpec, dict[str, Any]], Awaitable[dict[str, Any] | None]]
"""Runs an agent step. Injected so the workflow engine does not import the agent
loop and the two can be tested apart."""

ToolStepRunner = Callable[[Journal, str, dict[str, Any]], Awaitable[Any]]
"""Runs a tool step."""


class WorkflowEngine:
    """Executes a workflow's steps in order, memoising each one."""

    def __init__(
        self,
        journal: Journal,
        agent_runner: AgentStepRunner,
        tool_runner: ToolStepRunner,
    ) -> None:
        self._journal = journal
        self._agent_runner = agent_runner
        self._tool_runner = tool_runner

    async def run(
        self, spec: WorkflowSpec, *, path: tuple[str | int, ...] = ()
    ) -> tuple[TerminalState, dict[str, Any]]:
        """Run every step in order, stopping at the first failure.

        Args:
            spec: the workflow.
            path: this workflow's position, for a nested one. The root passes
                nothing. Included in every step id below it, so two nested
                workflows with the same step names do not collide.

        Returns:
            The terminal state and the accumulated outputs, keyed by step name.
        """
        outputs: dict[str, Any] = {}

        for index, step in enumerate(spec.steps):
            step_path = (*path, spec.name, index, step.name)
            outcome = await self._step(step, step_path, outputs)
            if outcome.failure is not None:
                return TerminalState.FAILED, outputs
            outputs[step.name] = outcome.output
            if self._journal.state.aborted:
                return TerminalState.ABORTED, outputs
            if self._journal.state.suspended:
                # A step suspended. The Run persists and waits; the outputs so
                # far are already memoised, so resuming continues from here.
                return TerminalState.COMPLETED, outputs

        return TerminalState.COMPLETED, outputs

    async def _step(
        self,
        step: AgentStep | ToolStep | WorkflowStepRef,
        path: tuple[str | int, ...],
        prior_outputs: dict[str, Any],
    ) -> StepOutcome:
        step_id = derive_step_id(self._journal.run_id, path)

        memoised = self._journal.state.steps.get(step_id)
        if memoised is not None and memoised.completed:
            # The whole point of memoisation. This step ran on an earlier
            # attempt and its result is in the log, so it is not run again.
            return StepOutcome(
                name=step.name,
                output=memoised.output,
                failure=memoised.failure,
                memoised=True,
            )

        attempt_number = _next_attempt(memoised)
        step_input = _step_input(step, prior_outputs)

        await self._journal.append(
            type="step_started",
            step_id=step_id,
            name=step.name,
            kind=_kind_of(step),
            attempt_number=attempt_number,
            input=step_input,
        )

        try:
            output = await self._execute(step, path, step_input)
        except Exception as err:
            failure = ToolFailure(
                kind=type(err).__name__,
                message=failure_guidance(type(err).__name__, str(err)),
                traceback=traceback.format_exc(),
            )
            await self._journal.append(type="step_completed", step_id=step_id, failure=failure)
            return StepOutcome(name=step.name, output=None, failure=failure, memoised=False)

        await self._journal.append(type="step_completed", step_id=step_id, output=output)
        return StepOutcome(name=step.name, output=output, failure=None, memoised=False)

    async def _execute(
        self,
        step: AgentStep | ToolStep | WorkflowStepRef,
        path: tuple[str | int, ...],
        step_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        match step:
            case AgentStep():
                return await self._agent_runner(self._journal, step.spec, step_input)
            case ToolStep():
                result = await self._tool_runner(self._journal, step.tool, step.arguments)
                return {"result": result}
            case WorkflowStepRef():
                # A nested workflow shares the parent's journal and log. One Run,
                # one log, one resume path: nesting does not fork durability.
                _, nested_outputs = await self.run(step.spec, path=path)
                return nested_outputs


def _kind_of(step: AgentStep | ToolStep | WorkflowStepRef) -> str:
    match step:
        case AgentStep():
            return "agent"
        case ToolStep():
            return "tool"
        case WorkflowStepRef():
            return "workflow"


def _next_attempt(memoised: StepRecord | None) -> int:
    """A step that started and did not finish is retried at the next number.

    The reducer refuses a gap here, so this is the only correct way to number a
    retry and getting it wrong fails loudly rather than quietly.
    """
    return 1 if memoised is None else memoised.attempt_number + 1


def _step_input(
    step: AgentStep | ToolStep | WorkflowStepRef, prior_outputs: dict[str, Any]
) -> dict[str, Any]:
    """What a step receives.

    Every prior step's output, keyed by step name. Deliberately not a template
    language: a workflow that needs to transform a value between steps uses a
    tool step to do it, where the transformation is visible in the log like
    everything else. A templating layer here would be a second execution model
    hiding inside the first.
    """
    if isinstance(step, ToolStep):
        return {"arguments": step.arguments, "prior": prior_outputs}
    return {"prior": prior_outputs}


class WorkflowRunner:
    """Adapts a workflow to the ``AttemptRunner`` a Worker expects.

    Keeps the Worker from knowing whether it claimed an agent or a workflow: it
    claims a Run, and what the Version says decides which engine drives it.
    """

    def __init__(self, engine_for: Callable[[Journal], WorkflowEngine]) -> None:
        self._engine_for = engine_for

    async def __call__(self, journal: Journal, spec: WorkflowSpec) -> None:
        engine = self._engine_for(journal)
        state, outputs = await engine.run(spec)
        if journal.state.settled or journal.state.suspended:
            return
        await journal.append(type="run_settled", state=state, output=outputs)
