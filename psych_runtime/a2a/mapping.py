"""Psych's own concepts, expressed as A2A's. Pure, and the whole point.

DESIGN.md §1 refuses "an HTTP server, a route table, or any transport", and
A2A *is* a transport. What is left when the transport is removed is this: a
total function from what a Run is to what a Task is. It reads only projections
Psych already owns -- ``RunStatus``, ``AnswerView``, ``Record`` -- and returns
only protocol objects, so every mapping in this file is unit-testable without
a socket, and the routes that use it (``examples/playground/backend/app/a2a``)
contain no protocol decisions at all.

## The correspondence, and where it is exact

Two models built by different people for different reasons agree here more
than they had to, and the places they agree are the places to be careful not
to fudge:

- **``RunId`` is ``taskId``.** §3.4.2 requires the server to generate task ids
  and forbids a client minting one ("Client-provided ``taskId`` values for
  creating new tasks is NOT supported"). Psych's ``dispatch`` already mints
  Run ids server-side, so there is nothing to reconcile: a client that sends a
  ``taskId`` is naming an existing Run or is wrong, and ``resolve_message`` is
  where that is decided.
- **The ``continues_run_id`` chain is ``contextId``.** §3.4.1 defines a
  context as what "logically groups multiple related Task and Message
  objects", which is exactly what that continuation chain is: separate
  top-level Runs, no delegation between them, linked because a second message
  followed the first. The context id is the *root* of that chain, so every
  Run in one conversation reports the same one and a client can list them
  (``ListTasks(contextId=...)``) without Psych keeping a second index.
  Deliberately not ``parent_run_id``: that is delegation, and a subagent's
  Run is a step inside its parent's Task rather than a sibling Task in a
  conversation.
- **``SuspendReason.QUESTION`` is ``TASK_STATE_INPUT_REQUIRED``.** Both mean:
  stopped, not broken, waiting on words from whoever asked. §3.4.3 even
  describes the resumption the same way Psych implements it ("The client
  continues the interaction by sending a new message with the same ``taskId``
  and ``contextId``").
- **An authorization wait is ``TASK_STATE_AUTH_REQUIRED``.** In Psych that is
  ``SuspendReason.EXTERNAL``: the Run is parked until something outside it
  happens, and the case that reaches a caller is an OAuth authorization the
  *client* must complete before the Run can go on. ``AUTH_REQUIRED`` is the
  state A2A defines for exactly that, and mapping it to ``INPUT_REQUIRED``
  instead would tell a client to send a message when what it has to do is
  authorize.
- **A parent waiting on its subagents is ``TASK_STATE_WORKING``.**
  ``SuspendReason.CHILDREN`` means the Run released its lease while children it
  spawned carry on, which is an implementation detail of how Psych schedules
  work rather than anything a caller can act on. From outside, the task is
  progressing.

Two places the models do **not** line up, stated rather than smoothed over:

- **``TASK_STATE_REJECTED`` is never produced.** It means the agent decided
  not to do the work. Psych makes that decision at publish time
  (``psych_runtime.publish``) or at admission, before any Run exists to have a state,
  so no Run can be in it. The value is still handled on the way *in*, because
  a peer can send it.
- **``SuspendReason.APPROVAL`` is ``INPUT_REQUIRED``, not a state of its own.**
  A2A has no "waiting for a human to approve a tool call" state. Input is what
  the task needs and input is what the client can supply, so the honest
  mapping is ``INPUT_REQUIRED`` with the approval prompt carried in
  ``TaskStatus.message`` -- which is the thing a client actually renders.

## Why the tables are module-level and overridable

``LIFECYCLE_STATES``, ``TERMINAL_TASK_STATES`` and ``SUSPEND_TASK_STATES`` are
exhaustive over Psych's own enums, so adding a ``Lifecycle`` value without
deciding its Task state is a ``KeyError`` at the one call site rather than a
silent "working". ``task_state_of`` takes the suspension table as an argument
because a consumer whose platform uses ``SuspendReason.EXTERNAL`` for
something that is *not* an authorization wait -- a webhook from a batch job,
say -- should be able to say so without forking this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from psych_runtime.a2a.errors import InvalidParamsError, TaskNotFoundError
from psych_runtime.a2a.models import (
    Artifact,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from psych_runtime.core.answer import AnswerView
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import (
    AttemptStarted,
    Record,
    Resumed,
    RunAdmitted,
    RunSettled,
    Suspended,
    SuspendReason,
    TerminalState,
)
from psych_runtime.core.status import Lifecycle, RunStatus
from psych_runtime.core.thread_view import MessageView

__all__ = [
    "ANSWER_ARTIFACT_ID",
    "LIFECYCLE_STATES",
    "SUSPEND_TASK_STATES",
    "TERMINAL_TASK_STATES",
    "MessageIntent",
    "ResolvedMessage",
    "artifact_of",
    "context_id_of",
    "history_of",
    "resolve_message",
    "status_message_of",
    "stream_events",
    "task_of",
    "task_state_of",
]

ANSWER_ARTIFACT_ID: Final = "answer"
"""The artifact id Psych's own answer is published under.

Stable rather than random: §4.2.2's ``append`` and ``last_chunk`` identify a
growing artifact by id, so a streaming implementation that minted a fresh id
per chunk would be sending unrelated artifacts. One Run has one answer, and it
has this id.
"""

TERMINAL_TASK_STATES: Final[Mapping[TerminalState, TaskState]] = {
    TerminalState.COMPLETED: TaskState.COMPLETED,
    TerminalState.FAILED: TaskState.FAILED,
    # An interrupt is a client-visible stop, which is what CANCELED means.
    TerminalState.ABORTED: TaskState.CANCELED,
    # Abandoned is a suspension that expired: nobody canceled it, it simply
    # never got the answer it was waiting for. That is a failure to complete,
    # not a cancellation, and calling it CANCELED would tell a client somebody
    # made a decision when nobody did.
    TerminalState.ABANDONED: TaskState.FAILED,
    TerminalState.FORCE_SETTLED: TaskState.FAILED,
}
"""Every ``TerminalState``, so a new one added there is a ``KeyError`` here."""

SUSPEND_TASK_STATES: Final[Mapping[SuspendReason, TaskState]] = {
    SuspendReason.QUESTION: TaskState.INPUT_REQUIRED,
    SuspendReason.APPROVAL: TaskState.INPUT_REQUIRED,
    SuspendReason.EXTERNAL: TaskState.AUTH_REQUIRED,
    # A timer is nobody's turn to act. Telling a client INPUT_REQUIRED for a
    # Run that is merely sleeping would have it prompt a person for input the
    # Run will never read.
    # A parent waiting on its own children is working, not stuck. Nothing is
    # being asked of the caller: the work is going on, in Runs this Psych
    # admitted and will resume the parent from. INPUT_REQUIRED would have a
    # client prompt somebody for an answer nobody wants, and AUTH_REQUIRED
    # would send them to an authorization that does not exist.
    SuspendReason.CHILDREN: TaskState.WORKING,
}

LIFECYCLE_STATES: Final[Mapping[Lifecycle, TaskState]] = {
    Lifecycle.QUEUED: TaskState.SUBMITTED,
    Lifecycle.RUNNING: TaskState.WORKING,
    # An abort is in the log and the terminal record has not landed. The task
    # is still running until it is not; reporting CANCELED here would have a
    # client stop watching a stream that is about to carry the real ending.
    Lifecycle.STOPPING: TaskState.WORKING,
    Lifecycle.WAITING: TaskState.INPUT_REQUIRED,
    Lifecycle.DONE: TaskState.COMPLETED,
    Lifecycle.FAILED: TaskState.FAILED,
    Lifecycle.STOPPED: TaskState.CANCELED,
}
"""The fallback when there is no more specific answer. ``WAITING`` and the
three settled lifecycles are refined by ``task_state_of`` from the suspension
reason and the terminal state, which carry more."""


def task_state_of(
    status: RunStatus,
    *,
    suspend_states: Mapping[SuspendReason, TaskState] = SUSPEND_TASK_STATES,
) -> TaskState:
    """The A2A state of one Run, from the projection a UI already reads.

    Reads ``RunStatus`` rather than the reducer's working dataclass on
    purpose: ``status_of`` is the projection the runtime and every UI already
    agree on, so an A2A client and the console cannot disagree about whether a
    Run is waiting.
    """
    if status.terminal_state is not None:
        return TERMINAL_TASK_STATES[status.terminal_state]
    if status.lifecycle is Lifecycle.WAITING and status.suspend_reason is not None:
        return suspend_states[status.suspend_reason]
    return LIFECYCLE_STATES[status.lifecycle]


def context_id_of(run_id: RunId, continues: Mapping[RunId, RunId | None]) -> str:
    """The ``contextId`` for a Run: the root of its continuation chain.

    Args:
        run_id: the Run whose context is wanted.
        continues: each Run's ``continues_run_id``, for as much of the chain
            as the caller has loaded. A Run missing from the mapping ends the
            walk, so a caller that loaded only part of a long thread gets the
            oldest Run it knows about rather than an exception -- the id stays
            stable as long as the caller loads the chain the same way, which
            ``psych_runtime.thread`` already does.

    A cycle is impossible in a well-formed log (a Run can only continue one
    admitted earlier) but not impossible in a corrupt one, so the walk stops
    on a repeat rather than looping forever. Consistent with DESIGN.md's rule
    about never repairing a contradictory log: this does not fix anything, it
    refuses to hang.
    """
    seen: set[RunId] = set()
    current = run_id
    while True:
        if current in seen:
            return str(current)
        seen.add(current)
        parent = continues.get(current)
        if parent is None:
            return str(current)
        current = parent


def status_message_of(status: RunStatus, *, context_id: str) -> Message | None:
    """The agent message that belongs on a ``TaskStatus``, if any.

    §3.7: "Agents attach Messages to status update events to inform clients
    about task progress, request additional input, or provide informational
    updates". The three cases that carry something a client must act on are a
    question, an approval and a failure; a Run merely working has nothing to
    say that is not already in its state.

    The message id is derived from the Run and what it is waiting on rather
    than random, so re-reading the same suspended task twice produces the same
    message rather than a new one each poll.
    """
    text: str | None = None
    suffix = "status"
    if status.pending_question is not None:
        text = status.pending_question.summary
        suffix = f"question-{status.pending_question.call_id}"
    elif status.pending_approval is not None:
        approval = status.pending_approval
        text = approval.question or f"Approve calling {approval.tool!r}?"
        suffix = f"approval-{approval.call_id}"
    elif status.failure_message is not None:
        text = status.failure_message
        suffix = "failure"
    if not text:
        return None
    return Message(
        message_id=f"{status.run_id}-{suffix}",
        context_id=context_id,
        task_id=str(status.run_id),
        role=Role.AGENT,
        parts=(Part.from_text(text),),
    )


def artifact_of(answer: AnswerView) -> Artifact | None:
    """The Run's answer as an ``Artifact``, or ``None`` when there is not one.

    §3.7 is explicit that outputs belong in Artifacts and not in Messages
    ("Messages SHOULD NOT be used to deliver task outputs"), which lines up
    with ``psych_runtime.answer()``'s own split: ``text`` is the answer and ``work``
    is how it got there. Only the answer becomes an Artifact. The work stays
    reachable through Psych's own report, because a peer agent asked a
    question and does not need six turns of tool calls to read it.

    ``None`` for a Run that never reached a finishing turn: an empty Artifact
    would look like an agent that answered with nothing, which is a different
    and more misleading thing than no artifact at all.
    """
    if not answer.finished or not answer.text:
        return None
    return Artifact(
        artifact_id=ANSWER_ARTIFACT_ID,
        name="answer",
        description="The agent's final answer.",
        parts=(Part.from_text(answer.text, media_type="text/plain"),),
    )


def history_of(messages: Sequence[MessageView], *, context_id: str) -> tuple[Message, ...]:
    """A Psych conversation as A2A ``Message``s (``Task.history``).

    Tool results are dropped. §3.7 defines Task history as "Messages exchanged
    during task execution" between client and agent, and a tool result is
    neither: it is the agent's internal working, which belongs to the report.
    Sending it would also leak whatever a tool returned to whoever can read
    the task, which is a wider audience than the Run's own operator.
    """
    out: list[Message] = []
    for view in messages:
        if view.role not in ("user", "assistant"):
            continue
        if not view.content:
            continue
        out.append(
            Message(
                message_id=f"{view.run_id}-{view.seq}",
                context_id=context_id,
                task_id=str(view.run_id),
                role=Role.USER if view.role == "user" else Role.AGENT,
                parts=(Part.from_text(view.content),),
            )
        )
    return tuple(out)


def task_of(
    status: RunStatus,
    *,
    context_id: str,
    answer: AnswerView | None = None,
    history: Sequence[MessageView] = (),
    history_length: int | None = None,
    include_artifacts: bool = True,
    timestamp: datetime | None = None,
    suspend_states: Mapping[SuspendReason, TaskState] = SUSPEND_TASK_STATES,
) -> Task:
    """One Run as one ``Task``.

    Args:
        status: from ``psych_runtime.status()``.
        context_id: from ``context_id_of``.
        answer: from ``psych_runtime.answer()``. Omitted means no artifacts, which is
            what a caller listing tasks wants: §3.1.4's ``include_artifacts``
            defaults to false "to reduce payload size".
        history: from ``psych_runtime.thread()`` or ``message_views``.
        history_length: §3.2.4. ``None`` means no limit, ``0`` means no
            messages, and any other value takes that many of the **most
            recent** -- the proto says "the maximum number of most recent
            messages", so truncation is from the front.
        include_artifacts: §3.1.4's flag, honoured here so both bindings get
            the same behaviour from one place.
        timestamp: what to stamp the status with. Passed in rather than read
            from the clock, because this module is pure and a caller that
            wants the Run's own last-change time has it in the log.
    """
    messages = history_of(history, context_id=context_id)
    if history_length is not None:
        messages = messages[len(messages) - history_length :] if history_length else ()
    artifact = artifact_of(answer) if answer is not None and include_artifacts else None
    return Task(
        id=str(status.run_id),
        context_id=context_id,
        status=TaskStatus(
            state=task_state_of(status, suspend_states=suspend_states),
            message=status_message_of(status, context_id=context_id),
            timestamp=timestamp,
        ),
        artifacts=(artifact,) if artifact is not None else (),
        history=messages,
    )


# ---------------------------------------------------------------------------
# Records to stream events (§3.5.2, §4.2)
# ---------------------------------------------------------------------------


def stream_events(
    records: Iterable[Record],
    *,
    context_id: str,
    answer: AnswerView | None = None,
    suspend_states: Mapping[SuspendReason, TaskState] = SUSPEND_TASK_STATES,
) -> tuple[StreamResponse, ...]:
    """The status and artifact events one Run's log implies, in log order.

    §3.5.2 requires that "All implementations MUST deliver events in the order
    they were generated. Events MUST NOT be reordered during transmission".
    Psych gets that for free and it is worth saying why: the log is already a
    gapless, totally-ordered sequence, and this function is a ``for`` loop over
    it. There is no queue, no fan-in and no second source of events that could
    interleave, so ordering is not something the streaming endpoint has to
    maintain -- it is something it cannot lose.

    Not every Record is an event. A Task's state changes on admission, on the
    first Attempt, on suspension, on resumption and on settlement; a turn
    starting or a tool finishing is progress *within* ``WORKING`` and would
    only tell a client the same state again. Emitting one event per Record
    would also hand a peer a turn-by-turn view of another organisation's
    agent internals, which the protocol never asks for.
    """
    events: list[StreamResponse] = []
    state: TaskState | None = None

    def emit(task_id: str, new_state: TaskState, at: datetime, message: Message | None) -> None:
        nonlocal state
        if new_state is state and message is None:
            return
        state = new_state
        events.append(
            StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=new_state, message=message, timestamp=at),
                )
            )
        )

    for record in records:
        task_id = str(record.run_id)
        if isinstance(record, RunAdmitted):
            emit(task_id, TaskState.SUBMITTED, record.at, None)
        elif isinstance(record, AttemptStarted):
            emit(task_id, TaskState.WORKING, record.at, None)
        elif isinstance(record, Suspended):
            emit(
                task_id,
                suspend_states[record.reason],
                record.at,
                _suspension_message(record, task_id=task_id, context_id=context_id),
            )
        elif isinstance(record, Resumed):
            emit(task_id, TaskState.WORKING, record.at, None)
        elif isinstance(record, RunSettled):
            if answer is not None:
                artifact = artifact_of(answer)
                if artifact is not None:
                    events.append(
                        StreamResponse(
                            artifact_update=_artifact_event(
                                artifact, task_id=task_id, context_id=context_id
                            )
                        )
                    )
            emit(
                task_id,
                TERMINAL_TASK_STATES[record.state],
                record.at,
                _failure_message(record, task_id=task_id, context_id=context_id),
            )
    return tuple(events)


def _artifact_event(
    artifact: Artifact, *, task_id: str, context_id: str
) -> TaskArtifactUpdateEvent:
    # `append=False, last_chunk=True`: Psych publishes the answer once, whole,
    # when the Run settles. Nothing streams it in pieces today, and claiming
    # otherwise would have a client wait for a continuation that never comes.
    return TaskArtifactUpdateEvent(
        task_id=task_id,
        context_id=context_id,
        artifact=artifact,
        append=False,
        last_chunk=True,
    )


def _suspension_message(record: Suspended, *, task_id: str, context_id: str) -> Message | None:
    if not record.question:
        return None
    return Message(
        message_id=f"{task_id}-{record.seq}",
        context_id=context_id,
        task_id=task_id,
        role=Role.AGENT,
        parts=(Part.from_text(record.question),),
    )


def _failure_message(record: RunSettled, *, task_id: str, context_id: str) -> Message | None:
    if record.failure is None:
        return None
    return Message(
        message_id=f"{task_id}-{record.seq}",
        context_id=context_id,
        task_id=task_id,
        role=Role.AGENT,
        parts=(Part.from_text(record.failure.message),),
    )


# ---------------------------------------------------------------------------
# Inbound: what one SendMessage means (§3.4)
# ---------------------------------------------------------------------------


class MessageIntent(StrEnum):
    """What a server must do with an incoming ``SendMessage`` (§3.4.3)."""

    NEW_CONTEXT = "new_context"
    """No ids: start a conversation, and a Task in it."""
    NEW_TASK_IN_CONTEXT = "new_task_in_context"
    """A ``contextId`` without a ``taskId``: §3.4.3's "Clients MAY use
    ``contextId`` without ``taskId`` to start a new task within an existing
    conversation context". In Psych that is
    ``dispatch(continues=<newest Run in the context>)``."""
    CONTINUE_TASK = "continue_task"
    """A ``taskId``: the same Task goes on. In Psych that is ``resume`` when
    the Run is waiting, which is what makes ``INPUT_REQUIRED`` round-trip
    without minting a second task id."""


@dataclass(frozen=True, slots=True)
class ResolvedMessage:
    """A validated ``SendMessage``, reduced to the decision it implies.

    Attributes:
        intent: what to do.
        task_id: the Task to continue, for ``CONTINUE_TASK``.
        context_id: the conversation, when the client named one.
        text: the message's text parts, joined -- what Psych dispatches or
            resumes with.
        message: the message itself, for a caller that needs the parts a text
            agent ignored.
    """

    intent: MessageIntent
    task_id: RunId | None
    context_id: str | None
    text: str
    message: Message


def resolve_message(
    request: SendMessageRequest,
    *,
    known_context_of: Mapping[RunId, str] | None = None,
) -> ResolvedMessage:
    """Apply §3.4's identifier rules to one request, before anything runs.

    The three rules that have to hold, and what happens when they do not:

    - A ``taskId`` "MUST reference an existing task", and a client may not
      mint one for a new task (§3.4.2). This function cannot know which Runs
      exist, so it reports ``CONTINUE_TASK`` and the caller raises
      ``TaskNotFoundError`` if the Run is not there. What it *can* refuse
      here is a task id that is not a well-formed Run id at all.
    - "Agents MUST infer ``contextId`` from the task if only ``taskId`` is
      provided" (§3.4.3), so a request naming only a task carries no context
      requirement.
    - "Agents MUST reject messages containing mismatching ``contextId`` and
      ``taskId``" (§3.4.3). With ``known_context_of`` supplied, that check
      happens here rather than in a route, because a route that forgets it
      lets a client read one conversation's task under another's context id.

    Raises:
        InvalidParamsError: the message has no text content, or the
            ``contextId``/``taskId`` pair contradicts itself.
        TaskNotFoundError: ``known_context_of`` was supplied and does not know
            the named task. §13.1 makes "not found" the answer for "exists but
            is not yours" too, which is why this is not an access error.
    """
    message = request.message
    task_id = RunId(message.task_id) if message.task_id else None
    context_id = message.context_id or None

    if task_id is not None and known_context_of is not None:
        actual = known_context_of.get(task_id)
        if actual is None:
            raise TaskNotFoundError(
                f"task {task_id!r} does not exist or is not accessible",
                metadata={"taskId": str(task_id)},
            )
        if context_id is not None and context_id != actual:
            raise InvalidParamsError(
                f"contextId {context_id!r} does not match task {task_id!r}, which belongs "
                f"to context {actual!r}",
                metadata={"taskId": str(task_id), "contextId": context_id},
            )
        context_id = actual

    text = message.text
    if not text.strip():
        raise InvalidParamsError(
            "the message carries no text content; this agent reads text parts and the "
            "request had none"
        )

    if task_id is not None:
        intent = MessageIntent.CONTINUE_TASK
    elif context_id is not None:
        intent = MessageIntent.NEW_TASK_IN_CONTEXT
    else:
        intent = MessageIntent.NEW_CONTEXT

    return ResolvedMessage(
        intent=intent,
        task_id=task_id,
        context_id=context_id,
        text=text,
        message=message,
    )
