"""What a Run is doing right now, shaped for a person to be shown.

``RunStateView`` (``psych_runtime.core.reducer``) is the fold's own working object: a
mutable dataclass carrying everything the *next* fold needs cheaply --
``open_tool_calls``, ``repeat_counts``, ``approval_decisions``,
``result_handles`` -- none of which a UI wants and none of which is JSON.

So every consumer with a UI writes a projection of it, and the one in this
repository's own example had to decide, field by field, what a status even is:
which lifecycle values exist, whether "suspended" is a state or a flag, what a
UI needs to render an approval prompt. Getting that wrong is not dangerous, but
having every consumer get it differently is exactly the "each will get the
arithmetic wrong in a different way" DESIGN.md §13.4 gives as the reason the
report ships with Psych. The same argument applies here, one size down.

This is that projection: frozen, JSON-serialisable, and derived from the same
fold a Worker uses to decide what to do next, so a UI and the runtime can never
disagree about whether a Run is waiting.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from psych_runtime.core.components import Component
from psych_runtime.core.ids import AttemptId, RunId, ToolCallId, VersionHash
from psych_runtime.core.questions import AskedQuestion
from psych_runtime.core.records import SuspendReason, TerminalState
from psych_runtime.core.reducer import RunStateView
from psych_runtime.core.scope import Scope
from psych_runtime.core.tasks import Task

__all__ = ["SETTLED_LIFECYCLE", "Lifecycle", "PendingApproval", "RunStatus", "status_of"]


class Lifecycle(StrEnum):
    """Where a Run is, in the words a person would use.

    Coarser than ``TerminalState`` and finer than the store's ``RunState``:
    those answer "how did it end" and "is it claimable", and this answers "what
    should the screen say".
    """

    QUEUED = "queued"
    """Admitted, no Attempt has started yet."""
    RUNNING = "running"
    WAITING = "waiting"
    """Suspended: an approval, a question, an external event, a time."""
    STOPPING = "stopping"
    """An abort is in the log and the terminal record has not landed yet. A UI
    that has no word for this shows "running" and offers a stop button that
    does nothing, which is how a person concludes stop is broken."""
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"
    """Aborted, abandoned, or force-settled: it ended without finishing, and
    the distinction between those three is in ``terminal_state`` for whoever
    needs it."""


class PendingApproval(BaseModel):
    """The call a suspended Run is waiting on a decision for.

    Everything an approval prompt needs, so a UI does not parse it back out of
    the suspension's question text: the tool, the exact arguments that will run
    if approved, and when waiting stops being useful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: ToolCallId
    tool: str
    arguments: dict[str, Any]
    question: str | None
    expires_at: datetime


class PendingQuestion(BaseModel):
    """A Run stopped because the agent asked something.

    `questions` carries the structure a console renders: labels to click, and
    a description of what each one means. `summary` is the same thing as one
    line, for a notification or a log that will not render options.

    The options are suggestions and nothing validates against them. A person
    may answer in their own words, which is what makes offering them safe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: ToolCallId
    questions: tuple[AskedQuestion, ...]
    summary: str
    expires_at: datetime


class RunStatus(BaseModel):
    """One Run's current state, as a UI would render it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    scope: Scope
    version_hash: VersionHash
    lifecycle: Lifecycle
    terminal_state: TerminalState | None
    """``None`` until the Run settles. ``Lifecycle`` is what to show; this is
    the exact ending, for a caller that needs to tell abandoned from aborted."""
    output: dict[str, Any] | None
    failure_message: str | None
    """The terminal failure's guidance text, already written for a person to
    read (``psych_runtime.tools.guidance``). The traceback stays in the log."""
    failure_kind: str | None

    suspend_reason: SuspendReason | None
    suspend_expires_at: datetime | None
    pending_approval: PendingApproval | None
    pending_question: PendingQuestion | None
    components: tuple[Component, ...]
    """Everything the agent has shown so far, oldest first. Here rather than
    only in the report for the reason the plan is: a card the agent produced on
    turn two should appear on turn two, not once the Run has finished."""
    tasks: tuple[Task, ...]
    """The agent's plan as it stands right now, for an agent given the task
    tool. On the status rather than only in the report because the point of a
    plan is to be readable *while* the Run works: a plan that appears once the
    Run has finished tells somebody what they already watched happen."""
    """Set when the Run is waiting on `ask_question`. The sibling of
    `pending_approval`: both are a Run stopped for a person, and a console
    renders one or the other rather than guessing from `suspend_reason`."""

    turn: int
    tool_calls: int
    model_calls: int
    head_seq: int
    """The highest sequence written. What a client reconnects a stream with."""
    attempt_count: int
    current_attempt: AttemptId | None
    deadline_at: datetime | None
    """The effective deadline: the admission deadline pushed back by however
    long the Run has spent suspended, since waiting for a human is not the Run
    running too long."""
    continues_run_id: RunId | None
    parent_run_id: RunId | None
    has_dangling_tool_calls: bool
    """A crash left calls open and no Attempt has settled them yet."""


def status_of(state: RunStateView) -> RunStatus:
    """Project one fold into a ``RunStatus``. Pure, like the fold itself."""
    return RunStatus(
        run_id=state.run_id,
        scope=state.scope,
        version_hash=state.version_hash,
        lifecycle=_lifecycle(state),
        terminal_state=state.terminal_state,
        output=state.output,
        failure_message=state.failure.message if state.failure is not None else None,
        failure_kind=state.failure.kind if state.failure is not None else None,
        suspend_reason=state.suspend_reason,
        suspend_expires_at=state.suspend_expires_at,
        pending_approval=_pending_approval(state),
        pending_question=_pending_question(state),
        components=state.components,
        tasks=state.tasks,
        turn=state.turn,
        tool_calls=len(state.tool_results),
        model_calls=state.model_calls,
        head_seq=state.head_seq,
        attempt_count=state.attempt_count,
        current_attempt=state.current_attempt,
        deadline_at=state.effective_deadline_at,
        continues_run_id=state.continues_run_id,
        parent_run_id=state.parent_run_id,
        has_dangling_tool_calls=state.has_dangling_tool_calls,
    )


SETTLED_LIFECYCLE: Final = {
    TerminalState.COMPLETED: Lifecycle.DONE,
    TerminalState.FAILED: Lifecycle.FAILED,
    TerminalState.ABORTED: Lifecycle.STOPPED,
    TerminalState.ABANDONED: Lifecycle.STOPPED,
    TerminalState.FORCE_SETTLED: Lifecycle.STOPPED,
}
"""Every ``TerminalState``, so a new one added there without a lifecycle here
is a KeyError at the one call site rather than a silent "stopped".

Public because a consumer projecting a tree of Runs needs the same mapping and
a second copy of it drifts: the console's subagent tree renders a child's
ending with this rather than with an ``if`` chain of its own."""


def _lifecycle(state: RunStateView) -> Lifecycle:
    if state.settled and state.terminal_state is not None:
        return SETTLED_LIFECYCLE[state.terminal_state]
    if state.aborted:
        return Lifecycle.STOPPING
    if state.suspended:
        return Lifecycle.WAITING
    if state.attempt_count == 0:
        return Lifecycle.QUEUED
    return Lifecycle.RUNNING


def _pending_question(state: RunStateView) -> PendingQuestion | None:
    """What the Run is waiting to be told, or `None`.

    Separate from `pending_approval` even though both come from one suspension,
    because they are answered differently: an approval takes yes or no, a
    question takes words. A console that had to tell them apart by reading
    `suspend_reason` would be one `if` away from offering the wrong control.
    """
    if state.suspend_reason is not SuspendReason.QUESTION:
        return None
    call = state.pending_call
    if call is None or state.suspend_expires_at is None:
        return None
    return PendingQuestion(
        call_id=call.call_id,
        questions=state.suspend_questions,
        summary=state.suspend_question or "",
        expires_at=state.suspend_expires_at,
    )


def _pending_approval(state: RunStateView) -> PendingApproval | None:
    """The call waiting on a yes or no, or `None`.

    Checks the reason, which it did not have to before `ask_question` existed:
    a question also parks the Run with a pending call, and without this a
    console would offer Approve and Deny for something that needs words.
    """
    if state.suspend_reason is not SuspendReason.APPROVAL:
        return None
    call = state.pending_call
    if call is None or state.suspend_expires_at is None:
        return None
    return PendingApproval(
        call_id=call.call_id,
        tool=call.tool,
        arguments=dict(call.arguments),
        question=state.suspend_question,
        expires_at=state.suspend_expires_at,
    )
