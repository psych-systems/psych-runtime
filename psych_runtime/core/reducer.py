"""The reducer: a pure function from a record log to run state.

DESIGN.md §6. No IO, no clock, no randomness. The same log always folds to the
same state, which is what makes crash recovery and the report trustworthy.

## Validation is not separable from derivation

``reduce`` validates as it folds rather than offering a separate ``validate``
anyone could forget to call. A state derived from a log nobody checked is a state
that quietly encodes a writer bug.

## Recoverable against impossible

The line, stated as a property rather than a list of cases:

> Any prefix of a sequence the protocol could legitimately produce folds
> cleanly. Anything requiring an illegal write to have happened raises,
> wherever the truncation lands.

A log ending mid-tool-call is a crash. The fold hands it back with that call
open, and the Worker settles it explicitly before continuing (§9). A log with
two starts for one call id is not a crash: no legal caller could write it, so it
raises and the Run fails loudly.

## What this deliberately does not do

It does not repair. It does not fill a gap, close a dangling call, or drop a
record it cannot explain. Every one of those would hide the writer bug that
produced the state, which is exactly the bug worth finding.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from psych_runtime.core.components import Component
from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.ids import AttemptId, RunId, StepId, ToolCallId, VersionHash
from psych_runtime.core.questions import AskedQuestion
from psych_runtime.core.records import (
    AbortRequested,
    AttemptStarted,
    CompactionApplied,
    ComponentShown,
    ModelCallFailed,
    ModelCallFinished,
    ModelCallStarted,
    QueueCancelled,
    QueueConsumed,
    QueueEnqueued,
    QueueKind,
    Record,
    ResultAttachment,
    Resumed,
    RunAdmitted,
    RunSettled,
    StepCompleted,
    StepStarted,
    SubagentFinished,
    SubagentMessaged,
    SubagentSpawned,
    Suspended,
    SuspendReason,
    TaskListUpdated,
    TerminalState,
    ToolCallFinished,
    ToolCallStarted,
    ToolFailure,
    ToolOutcome,
    TurnStarted,
)
from psych_runtime.core.scope import Scope
from psych_runtime.core.tasks import Task
from psych_runtime.core.usage import Cost, Usage

__all__ = [
    "ChildRun",
    "OpenToolCall",
    "PendingQueueEntry",
    "RunStateView",
    "StepRecord",
    "reduce",
]


@dataclass(frozen=True, slots=True)
class OpenToolCall:
    """A tool call that started and has no result.

    Mid-fold this is normal: the log is being read while the call is running. At
    the end of a fold it means the Attempt died mid-call, and the reclaiming
    Worker must settle it explicitly before continuing. A model must never
    receive an assistant message whose tool calls have no results; providers
    reject it and the conversation is unrecoverable (DESIGN.md §9).
    """

    call_id: ToolCallId
    tool: str
    arguments: dict[str, Any]
    turn: int
    step_id: StepId | None
    interruptible: bool
    safe_to_retry: bool
    started_at: datetime


@dataclass(frozen=True, slots=True)
class PendingQueueEntry:
    """Input that arrived mid-Run and has not been delivered yet."""

    entry_id: str
    queue: QueueKind
    payload: dict[str, Any]
    enqueued_seq: int


@dataclass(frozen=True, slots=True)
class ChildRun:
    """One subagent this Run spawned, as the log describes it.

    Derived rather than remembered, like every other piece of state here, which
    is what lets a reclaiming Worker pick up a tree mid-flight: the children a
    dead Attempt spawned are in the log, so the Worker that replaces it knows
    what it is waiting for without anything having been handed over.

    Attributes:
        terminal_state: ``None`` while the child is still running. This is the
            field the fan-out cap counts and the field a parent about to suspend
            reads.
        usage, cost: the child's own totals as its ``subagent_finished`` record
            reported them, so a branch can be rolled up without reading every
            descendant's log. ``cost`` is ``None`` when nothing in the child was
            priced, never zero.
        messages_sent: how many messages the parent put into this child.
    """

    run_id: RunId
    name: str
    call_id: ToolCallId
    version_hash: VersionHash
    depth: int
    background: bool
    tools: tuple[str, ...]
    model: str
    spawned_seq: int
    terminal_state: TerminalState | None = None
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    usage: Usage = field(default_factory=Usage)
    unreported_usage_calls: int = 0
    cost: Cost | None = None
    unpriced_model_calls: int = 0
    messages_sent: int = 0
    finished_seq: int | None = None

    @property
    def alive(self) -> bool:
        return self.terminal_state is None


@dataclass(frozen=True, slots=True)
class StepRecord:
    """A step's memoised result.

    On resume, a step whose result is here returns from the log rather than
    re-executing (DESIGN.md §5). This is the whole of step memoisation.
    """

    step_id: StepId
    name: str
    kind: str
    attempt_number: int
    input: dict[str, Any]
    completed: bool
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    child_run_id: RunId | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A settled tool call, in log order."""

    call_id: ToolCallId
    tool: str
    outcome: ToolOutcome
    result: Any
    failure: ToolFailure | None
    duration_seconds: float
    result_handle: str | None
    preview: str | None
    turn: int
    blob_key: str | None = None
    """``str(BlobKey)`` when ``result`` was offloaded rather than stored inline.
    ``None`` for every result small enough to stay in the log, which is every
    result the reducer folded before ``psych_runtime.store.blob`` existed. Kept as a
    reference only: reading the bytes back rebuilds the address from this
    Run's own scope and run id rather than trusting this string (DESIGN.md
    §14)."""
    content_type: str | None = None
    """How to decode the bytes at ``blob_key``. Set exactly when ``blob_key``
    is, enforced by ``ToolCallFinished``'s own validator, so this dataclass
    does not repeat that check."""
    attachments: tuple[ResultAttachment, ...] = ()
    """Named streams and files recorded beside the result, each with its own
    handle in ``RunStateView.result_handles``. See ``ResultAttachment``."""


@dataclass
class RunStateView:
    """Everything derivable from a Run's log.

    Mutable during the fold and returned as-is: making it frozen would mean
    rebuilding it once per record, and the fold is the only thing that ever
    writes to it. Callers treat it as read-only.
    """

    run_id: RunId
    scope: Scope
    version_hash: VersionHash

    head_seq: int = 0
    """The highest sequence folded. What a stream client passes as ``after``."""

    admitted: bool = False
    settled: bool = False
    terminal_state: TerminalState | None = None
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None

    deadline_at: datetime | None = None
    parent_run_id: RunId | None = None
    delegation_depth: int = 0
    continues_run_id: RunId | None = None
    """The Run this one continues, if any (DESIGN.md §23.3). See
    ``RunAdmitted.continues_run_id`` for why this is a separate field from
    ``parent_run_id`` rather than an overload of it."""
    idempotency_key: str | None = None
    run_input: dict[str, Any] = field(default_factory=dict)

    attempt_count: int = 0
    current_attempt: AttemptId | None = None

    turn: int = 0
    """Turns started. The current turn while one is open."""

    turn_open: bool = False
    """Whether the current turn is still awaiting its model result.

    Part of the state rather than a fold-local flag because a Worker folds the
    log on claim and then keeps appending inside the same turn. A continued fold
    that reset this would disagree with a whole-log fold, and the two agreeing is
    the property incremental folding rests on."""

    model_call_open: bool = False
    """Whether a ``model_call_started`` is awaiting its finish or failure. A
    result arriving with no start is refused rather than summed: the report
    pairs the two, and a pair with no first half is a writer bug."""

    abort_seq: int | None = None
    """Sequence of the abort record, or None. Anything that must not follow an
    abort is compared against this rather than against a boolean, because
    "after" is the actual question (DESIGN.md §9)."""

    suspended: bool = False
    suspend_reason: SuspendReason | None = None
    suspend_expires_at: datetime | None = None
    suspended_at: datetime | None = None
    pending_approval_call_id: ToolCallId | None = None
    suspend_question: str | None = None
    suspend_questions: tuple[AskedQuestion, ...] = ()
    """What ``ask_question`` asked, structured. Empty for an approval, which
    has only the rendered line above."""
    resumed_since_attempt: bool = False
    """Whether a ``resumed`` record has landed since the last
    ``attempt_started``.

    What ``reclaimed_expired_lease`` needs and ``attempt_count`` cannot give.
    A second attempt happens for two unrelated reasons: the previous Worker
    died holding the lease, or the Run suspended cleanly and something woke it.
    Counting attempts cannot tell those apart, so every resumed Run reported
    itself as recovered from a dead Worker, and a console told a person their
    answer had been picked up after a crash when nothing had crashed. Waiting
    on a subagent made that constant rather than occasional (DESIGN.md §17),
    which is how it was noticed.

    Cleared by ``attempt_started`` rather than accumulated, so it describes the
    handover into *this* attempt: a Run that suspended, resumed, and later died
    mid-attempt is correctly a reclaim on the attempt after that.
    """
    suspended_seconds: float = 0.0
    """Wall-clock seconds this Run has spent suspended, summed over every
    suspend/resume pair so far. Derived from record timestamps rather than a
    clock, so the fold stays pure. ``effective_deadline_at`` adds it to the
    admission deadline: a Run waiting a day for an approval has not spent a
    day of its deadline, and treating it as though it had would abort every
    approved Run the moment it was claimed again (DESIGN.md §8.4, §11)."""

    resume_payloads: dict[ToolCallId, dict[str, Any]] = field(default_factory=dict)
    """What a ``resumed`` record delivered for the call a suspension was
    waiting on: a human's answer to an ``ask_user`` question, a webhook's
    body. Keyed by the pending call so the reclaiming Worker can record it as
    that call's result without needing to be the Worker that asked."""

    approval_decisions: dict[ToolCallId, bool] = field(default_factory=dict)
    """Decisions already made on tool calls that suspended for approval.

    Derived rather than remembered so the answer survives the Worker that asked
    the question. A reclaiming Worker reads the decision out of the log and
    either executes the call or records the denial, without needing to know which
    Worker was waiting or for how long."""

    open_tool_calls: dict[ToolCallId, OpenToolCall] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)

    steps: dict[StepId, StepRecord] = field(default_factory=dict)

    pending_steer: list[PendingQueueEntry] = field(default_factory=list)
    pending_follow_up: list[PendingQueueEntry] = field(default_factory=list)
    pending_next_run: list[PendingQueueEntry] = field(default_factory=list)

    tasks: tuple[Task, ...] = ()
    """The model's current plan, when the agent was given the task tool. Empty
    for every agent that was not, which is the default."""

    components: tuple[Component, ...] = ()
    """Everything the model has shown, oldest first.

    A list rather than a single slot because showing is cumulative: an agent
    that showed the order and then the delivery timeline showed both, and the
    order they arrived in is part of the answer."""

    usage: Usage = field(default_factory=Usage)
    unreported_usage_calls: int = 0
    cost: Cost | None = None
    unpriced_model_calls: int = 0
    """Model calls whose model had no known price. Surfaced rather than folded
    into the total as zero, so a report can say "this total is incomplete"
    instead of quietly understating the bill (DESIGN.md §13.2)."""
    provider_reported_costs: int = 0
    """Priced calls whose figure came from the provider rather than from a
    local price table.

    Counted so a total can say what it is made of. A provider-reported figure
    comes from the party doing the billing; a computed one is as good as
    whatever table was in force. Reconciling against an invoice needs to know
    which rows came from where, and a total that blended them silently would
    look like one number and be two."""

    model_calls: int = 0
    failed_model_calls: int = 0
    transient_retries_used: int = 0

    last_prompt_tokens: int = 0
    """How many input-side tokens the provider counted for the most recent
    finished model call: ``input + cache_read + cache_write``.

    The measure the compaction trigger reads (``CompactionPolicy``). A number
    the provider reported about a call that really happened, rather than one a
    tokenizer guessed about a call that has not: Psych takes no tokenizer
    dependency, and an estimate that looks like a measurement is worse than no
    number. Zero until the first call finishes, and unchanged by a compaction's
    own summarising call, which measures a different prompt.
    """

    failure_streaks: dict[str, int] = field(default_factory=dict)
    repeat_counts: dict[str, RepeatTally] = field(default_factory=dict)
    """Per call, how many times running it changed nothing.

    Keyed by ``call_digest(tool, arguments)``, so the loop can look a call up
    *before* running it, which is the whole point: the cost being avoided is
    running it again. The tally carries the answer that call kept returning, so
    a different result resets the count rather than accumulating across two
    genuinely different answers.

    Counts successes that told the model nothing new, which is the loop
    ``failure_streaks`` cannot see because nothing failed."""
    """Consecutive failures per tool, surviving suspend and resume because it is
    derived from the log rather than held in a process (DESIGN.md §10.6)."""

    compaction_boundary_seq: int = 0
    """Records at or below this were replaced by a compaction summary. The
    records remain in the log; this only changes what the model sees next."""
    compaction_summaries: list[str] = field(default_factory=list)
    compaction_calls: int = 0
    """Summarising model calls this Run has made. Counted apart from
    ``model_calls`` because a compaction is not a turn: their tokens and cost
    are already inside ``usage`` and ``cost``, and this is what lets a report
    say which part of the bill was the loop and which part was fitting it into
    the window."""

    children: dict[RunId, ChildRun] = field(default_factory=dict)
    """Subagents this Run composed and spawned, in spawn order (dicts preserve
    insertion order and the fold only ever appends), keyed by the child's Run id.

    The whole tree is reconstructable from this plus each child's own log, which
    is the property DESIGN.md §17 asks for and the reason a notification is a
    Record: nothing about a running tree lives in a process."""

    result_handles: dict[str, ToolCallId] = field(default_factory=dict)
    """Large-result handles this Run issued, so a handle from another Run is
    visibly invalid rather than silently readable."""

    @property
    def has_dangling_tool_calls(self) -> bool:
        """True when the log ends with calls started and unanswered.

        The reclaiming Worker settles these before continuing, either by
        re-executing the ones recorded ``safe_to_retry`` or by recording an
        unknown outcome for the rest.
        """
        return bool(self.open_tool_calls)

    @property
    def aborted(self) -> bool:
        return self.abort_seq is not None

    @property
    def effective_deadline_at(self) -> datetime | None:
        """The admission deadline, pushed back by every second spent suspended."""
        if self.deadline_at is None:
            return None
        return self.deadline_at + timedelta(seconds=self.suspended_seconds)

    @property
    def pending_call(self) -> OpenToolCall | None:
        """The tool call a suspension is waiting on, with its arguments.

        What an approval UI renders: the tool name and the exact arguments
        that will run if approved, read from the open call rather than parsed
        back out of the suspension's question text.
        """
        if self.pending_approval_call_id is None:
            return None
        return self.open_tool_calls.get(self.pending_approval_call_id)

    @property
    def live_children(self) -> list[ChildRun]:
        """Children spawned and not yet finished.

        What the fan-out cap counts. A cap counting only the children spawned in
        this turn would let a parent hold any number open by spawning a few a
        turn, and a background child outlives the turn that spawned it by
        design."""
        return [child for child in self.children.values() if child.alive]

    @property
    def is_runnable(self) -> bool:
        return self.admitted and not self.settled and not self.suspended


def reduce(  # noqa: PLR0912, PLR0915
    records: Iterable[Record],
    *,
    run_id: RunId | None = None,
    prior: RunStateView | None = None,
) -> RunStateView:
    """Fold a record log into run state, refusing anything impossible.

    Args:
        records: the log, ascending by ``seq``. A prefix that stops mid-work is
            fine and is the normal case during crash recovery.
        run_id: the Run these records must belong to. Inferred from the first
            record when omitted. Passing it explicitly turns "these records came
            from another Run" from an invisible bug into a raised error.
        prior: state from folding this Run's earlier records, to continue from.
            Omitted means ``records`` starts at seq 1 and is the whole log.

            This exists because a Worker folds the log once on claim and then
            appends: re-folding thousands of records per append would be
            quadratic. Passing a slice *without* ``prior`` is refused as a
            sequence gap, which is the point. A partial slice folded as though it
            were whole would silently derive state from a log missing its middle.

    Returns:
        The derived state. Dangling tool calls and open turns are reported, not
        repaired.

    Raises:
        CorruptLog: the log could not have been produced by the protocol.
        ValueError: ``records`` is empty and no ``prior`` was given. A Run always
            has ``run_admitted`` at seq 1, so an empty log describes no Run; a
            missing Run is the store's ``RunNotFound``, not a fold result.
    """
    ordered = list(records)

    if prior is not None:
        state = _continue_from(prior)
        resolved_run_id = state.run_id
        if run_id is not None and run_id != resolved_run_id:
            raise ValueError(
                f"prior state is for run {resolved_run_id} but run_id={run_id} was passed"
            )
        expected_seq = prior.head_seq + 1
    else:
        if not ordered:
            raise ValueError(
                "reduce() was given no records and no prior state. Every Run has a "
                "run_admitted record at seq 1, so an empty log describes no Run. A "
                "Run that does not exist is the store's RunNotFound."
            )
        first = ordered[0]
        resolved_run_id = run_id if run_id is not None else first.run_id
        state = RunStateView(
            run_id=resolved_run_id,
            scope=first.scope,
            version_hash=VersionHash(""),
        )
        expected_seq = 1

    # Every tool call id ever started, so a second start is distinguishable from
    # a result arriving for a call that already settled.
    started_call_ids: set[ToolCallId] = set(state.open_tool_calls)
    settled_call_ids: set[ToolCallId] = {result.call_id for result in state.tool_results}
    started_call_ids |= settled_call_ids
    enqueued: dict[str, PendingQueueEntry] = {
        entry.entry_id: entry
        for queue in (state.pending_steer, state.pending_follow_up, state.pending_next_run)
        for entry in queue
    }
    cancelled_entry_ids: set[str] = set()
    consumed_entry_ids: set[str] = set()

    for record in ordered:
        _check_belongs(record, resolved_run_id)
        _check_sequence(record, expected_seq, resolved_run_id)
        expected_seq = record.seq + 1

        if state.settled and not isinstance(record, RunSettled):
            raise CorruptLog(
                CorruptionReason.RECORD_AFTER_FINISH,
                resolved_run_id,
                record.seq,
                f"a {record.type} record follows the terminal settlement at "
                f"seq {state.head_seq}. The Run is over; nothing may be appended.",
            )

        state.head_seq = record.seq

        match record:
            case RunAdmitted():
                if state.admitted:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        "a second run_admitted record. A Run is admitted exactly once.",
                    )
                state.admitted = True
                state.version_hash = record.version_hash
                state.deadline_at = record.deadline_at
                state.parent_run_id = record.parent_run_id
                state.delegation_depth = record.delegation_depth
                state.continues_run_id = record.continues_run_id
                state.idempotency_key = record.idempotency_key
                state.run_input = dict(record.input)

            case AttemptStarted():
                if not state.admitted:
                    raise CorruptLog(
                        CorruptionReason.UNKNOWN_OPERATION,
                        resolved_run_id,
                        record.seq,
                        "an attempt started before the Run was admitted.",
                    )
                if record.attempt_number != state.attempt_count + 1:
                    raise CorruptLog(
                        CorruptionReason.NON_CONSECUTIVE_ATTEMPT,
                        resolved_run_id,
                        record.seq,
                        f"attempt {record.attempt_number} follows attempt "
                        f"{state.attempt_count}. Attempt numbers increment by one, so "
                        "a claim went unrecorded.",
                    )
                # Cleared here rather than on the next resume: the flag answers
                # "how did this attempt begin", so it belongs to the attempt
                # that is starting and not to the whole Run.
                state.resumed_since_attempt = False
                state.attempt_count = record.attempt_number
                state.current_attempt = record.attempt_id
                # A new Attempt means the previous one is gone. Its turn is not
                # open any more; whether its tool calls are settled is a separate
                # question the reclaiming Worker answers.
                state.turn_open = False
                state.model_call_open = False

            case TurnStarted():
                if state.turn_open:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        f"turn {record.turn} started while turn {state.turn} is still "
                        "open. One Attempt runs one turn at a time.",
                    )
                if record.turn != state.turn + 1:
                    raise CorruptLog(
                        CorruptionReason.NON_CONSECUTIVE_ATTEMPT,
                        resolved_run_id,
                        record.seq,
                        f"turn {record.turn} follows turn {state.turn}. Turns increment by one.",
                    )
                state.turn = record.turn
                state.turn_open = True

            case ModelCallStarted():
                if not state.turn_open:
                    raise CorruptLog(
                        CorruptionReason.UNKNOWN_OPERATION,
                        resolved_run_id,
                        record.seq,
                        "a model call started outside any turn.",
                    )
                if state.model_call_open:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        f"a model call started while turn {state.turn}'s model call is "
                        "still open. One turn makes one model call at a time.",
                    )
                state.model_call_open = True

            case ModelCallFinished():
                _require_open_model_call(state, record.seq, resolved_run_id, "finished")
                state.model_calls += 1
                state.usage = state.usage + record.usage
                if not record.usage_reported:
                    state.unreported_usage_calls += 1
                # The prompt this call was actually charged for, kept so the
                # compaction trigger can read a measured size rather than
                # estimate one. Cached input counts: it is billed differently,
                # not sent for free, and it occupies the window either way.
                state.last_prompt_tokens = (
                    record.usage.input + record.usage.cache_read + record.usage.cache_write
                )
                _fold_cost(state, record.cost, record.seq, resolved_run_id)
                state.model_call_open = False
                state.turn_open = False

            case ModelCallFailed():
                _require_open_model_call(state, record.seq, resolved_run_id, "failed")
                state.failed_model_calls += 1
                if record.will_retry:
                    state.transient_retries_used += 1
                state.model_call_open = False
                state.turn_open = False

            case ToolCallStarted():
                if record.call_id in started_call_ids:
                    raise CorruptLog(
                        CorruptionReason.DUPLICATE_TOOL_INVOCATION,
                        resolved_run_id,
                        record.seq,
                        f"tool call {record.call_id} was started twice. The id addresses "
                        "the call in the model's context, so two starts leave the model "
                        "unable to tell the results apart.",
                    )
                started_call_ids.add(record.call_id)
                _check_deferred_handle(state, record, resolved_run_id)
                state.open_tool_calls[record.call_id] = OpenToolCall(
                    call_id=record.call_id,
                    tool=record.tool,
                    arguments=dict(record.arguments),
                    turn=record.turn,
                    step_id=record.step_id,
                    interruptible=record.interruptible,
                    safe_to_retry=record.safe_to_retry,
                    started_at=record.at,
                )

            case ToolCallFinished():
                open_call = state.open_tool_calls.pop(record.call_id, None)
                if open_call is None:
                    reason = (
                        "it already has a result"
                        if record.call_id in settled_call_ids
                        else "no start was recorded for it"
                    )
                    raise CorruptLog(
                        CorruptionReason.TOOL_CALL_MISMATCH,
                        resolved_run_id,
                        record.seq,
                        f"a result arrived for tool call {record.call_id} but {reason}.",
                    )
                settled_call_ids.add(record.call_id)
                if record.result_handle is not None:
                    state.result_handles[record.result_handle] = record.call_id
                for attachment in record.attachments:
                    # An attachment's handle resolves to its call exactly like
                    # the result's own, so read_tool_output has one lookup for
                    # both and a forged handle fails the same structural check.
                    # One the model may not read is not registered, so its
                    # presence alone never puts read_tool_output in the prompt.
                    if attachment.readable:
                        state.result_handles[attachment.handle] = record.call_id
                state.tool_results.append(
                    ToolResult(
                        call_id=record.call_id,
                        tool=open_call.tool,
                        outcome=record.outcome,
                        result=record.result,
                        failure=record.failure,
                        duration_seconds=record.duration_seconds,
                        result_handle=record.result_handle,
                        preview=record.preview,
                        turn=open_call.turn,
                        blob_key=record.result_blob_key,
                        content_type=record.result_content_type,
                        attachments=record.attachments,
                    )
                )
                _update_failure_streak(state, open_call.tool, record.outcome)
                _update_repeat_count(
                    state, open_call.tool, open_call.arguments, record, record.outcome
                )

            case StepStarted():
                existing = state.steps.get(record.step_id)
                if existing is not None:
                    if existing.completed:
                        raise CorruptLog(
                            CorruptionReason.INCONSISTENT_STEP,
                            resolved_run_id,
                            record.seq,
                            f"step {record.step_id} started again after it completed. A "
                            "memoised step is returned from the log, never re-run.",
                        )
                    if record.attempt_number != existing.attempt_number + 1:
                        raise CorruptLog(
                            CorruptionReason.NON_CONSECUTIVE_ATTEMPT,
                            resolved_run_id,
                            record.seq,
                            f"step {record.step_id} attempt {record.attempt_number} follows "
                            f"attempt {existing.attempt_number}. Step attempts increment "
                            "by one.",
                        )
                elif record.attempt_number != 1:
                    raise CorruptLog(
                        CorruptionReason.NON_CONSECUTIVE_ATTEMPT,
                        resolved_run_id,
                        record.seq,
                        f"step {record.step_id} first appears at attempt "
                        f"{record.attempt_number}; a step's first attempt is 1.",
                    )
                state.steps[record.step_id] = StepRecord(
                    step_id=record.step_id,
                    name=record.name,
                    kind=record.kind,
                    attempt_number=record.attempt_number,
                    input=dict(record.input),
                    completed=False,
                )

            case StepCompleted():
                started = state.steps.get(record.step_id)
                if started is None:
                    raise CorruptLog(
                        CorruptionReason.INCONSISTENT_STEP,
                        resolved_run_id,
                        record.seq,
                        f"step {record.step_id} completed but never started.",
                    )
                if started.completed:
                    raise CorruptLog(
                        CorruptionReason.INCONSISTENT_STEP,
                        resolved_run_id,
                        record.seq,
                        f"step {record.step_id} completed twice. A memoised result is "
                        "written once and read thereafter.",
                    )
                state.steps[record.step_id] = StepRecord(
                    step_id=started.step_id,
                    name=started.name,
                    kind=started.kind,
                    attempt_number=started.attempt_number,
                    input=started.input,
                    completed=True,
                    output=record.output,
                    failure=record.failure,
                    child_run_id=record.child_run_id,
                )

            case AbortRequested():
                if state.abort_seq is None:
                    state.abort_seq = record.seq

            case QueueEnqueued():
                # next_run is exempt from the after-abort rule and the exemption
                # is the point: "stop mid-response and immediately send another
                # request" is exactly a next_run enqueue after an abort, and it
                # is correct rather than a race (DESIGN.md §9).
                if (
                    record.queue is not QueueKind.NEXT_RUN
                    and state.abort_seq is not None
                    and record.seq > state.abort_seq
                ):
                    raise CorruptLog(
                        CorruptionReason.QUEUE_AFTER_ABORT,
                        resolved_run_id,
                        record.seq,
                        f"a {record.queue.value} entry was enqueued after the abort at "
                        f"seq {state.abort_seq}. Only next_run may follow an abort.",
                    )
                if record.entry_id in enqueued:
                    raise CorruptLog(
                        CorruptionReason.PROVISIONED_ENTRY_MISMATCH,
                        resolved_run_id,
                        record.seq,
                        f"queue entry {record.entry_id} was enqueued twice.",
                    )
                entry = PendingQueueEntry(
                    entry_id=record.entry_id,
                    queue=record.queue,
                    payload=dict(record.payload),
                    enqueued_seq=record.seq,
                )
                enqueued[record.entry_id] = entry
                _queue_for(state, record.queue).append(entry)

            case QueueCancelled():
                origin = enqueued.get(record.entry_id)
                if (
                    origin is None
                    or origin.enqueued_seq >= record.seq
                    or record.entry_id in cancelled_entry_ids
                    or record.entry_id in consumed_entry_ids
                ):
                    raise CorruptLog(
                        CorruptionReason.INVALID_QUEUE_CANCELLATION,
                        resolved_run_id,
                        record.seq,
                        f"queue entry {record.entry_id} cannot be cancelled: "
                        + _cancellation_detail(
                            origin,
                            record.seq,
                            record.entry_id,
                            cancelled_entry_ids,
                            consumed_entry_ids,
                        ),
                    )
                cancelled_entry_ids.add(record.entry_id)
                _remove_entry(state, origin)

            case QueueConsumed():
                origin = enqueued.get(record.entry_id)
                if origin is None or record.entry_id in cancelled_entry_ids:
                    raise CorruptLog(
                        CorruptionReason.PROVISIONED_ENTRY_MISMATCH,
                        resolved_run_id,
                        record.seq,
                        f"queue entry {record.entry_id} was consumed but was "
                        + ("cancelled first." if origin is not None else "never enqueued."),
                    )
                if record.entry_id in consumed_entry_ids:
                    raise CorruptLog(
                        CorruptionReason.PROVISIONED_ENTRY_MISMATCH,
                        resolved_run_id,
                        record.seq,
                        f"queue entry {record.entry_id} was consumed twice.",
                    )
                consumed_entry_ids.add(record.entry_id)
                _remove_entry(state, origin)

            case SubagentSpawned():
                if record.child_run_id in state.children:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        f"child run {record.child_run_id} was spawned twice. A Run id is "
                        "minted once per admission, so two spawns naming one child means "
                        "a writer replayed a spawn instead of recording a new one.",
                    )
                if any(
                    child.name == record.name and child.alive for child in state.children.values()
                ):
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        f"a second live child is named {record.name!r}. The name is how "
                        "the parent addresses a child in every later call, so two live "
                        "children sharing one would make `message_subagent` ambiguous.",
                    )
                state.children[record.child_run_id] = ChildRun(
                    run_id=record.child_run_id,
                    name=record.name,
                    call_id=record.call_id,
                    version_hash=record.child_version_hash,
                    depth=record.delegation_depth,
                    background=record.background,
                    tools=record.tools,
                    model=record.model,
                    spawned_seq=record.seq,
                )

            case SubagentMessaged():
                messaged = state.children.get(record.child_run_id)
                if messaged is None:
                    raise CorruptLog(
                        CorruptionReason.UNKNOWN_OPERATION,
                        resolved_run_id,
                        record.seq,
                        f"a message was sent to child run {record.child_run_id}, which "
                        "this Run never spawned.",
                    )
                # A message recorded *after* the child finished is legal, and
                # this is the one place that has to be said out loud. The
                # runtime checks the child is alive before writing, but the
                # check and the write are not one operation: a
                # `subagent_finished` appended by the child's own Worker can
                # take the sequence in between, and the Journal then absorbs it
                # and retries the message one sequence later. Refusing here
                # would turn that race into a corrupt log for a parent that did
                # nothing wrong. The message is counted; nothing consumes it,
                # because the child is gone.
                state.children[record.child_run_id] = dataclasses.replace(
                    messaged, messages_sent=messaged.messages_sent + 1
                )

            case SubagentFinished():
                finished = state.children.get(record.child_run_id)
                if finished is None:
                    raise CorruptLog(
                        CorruptionReason.UNKNOWN_OPERATION,
                        resolved_run_id,
                        record.seq,
                        f"child run {record.child_run_id} finished, but this Run never "
                        "spawned it. A notification reached the wrong parent.",
                    )
                if not finished.alive:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        f"child run {record.child_run_id} finished twice. A Run reaches "
                        "exactly one terminal state, so a second notification means the "
                        "notifier ran without reading what was already in the log.",
                    )
                state.children[record.child_run_id] = dataclasses.replace(
                    finished,
                    terminal_state=record.state,
                    output=record.output,
                    failure=record.failure,
                    usage=record.usage,
                    unreported_usage_calls=record.unreported_usage_calls,
                    cost=record.cost,
                    unpriced_model_calls=record.unpriced_model_calls,
                    finished_seq=record.seq,
                )

            case Suspended():
                if state.suspended:
                    raise CorruptLog(
                        CorruptionReason.MULTIPLE_OPEN_OPERATIONS,
                        resolved_run_id,
                        record.seq,
                        "the Run suspended while already suspended.",
                    )
                if (
                    record.reason is SuspendReason.APPROVAL
                    and record.pending_call_id is not None
                    and record.pending_call_id not in state.open_tool_calls
                ):
                    raise CorruptLog(
                        CorruptionReason.TOOL_CALL_MISMATCH,
                        resolved_run_id,
                        record.seq,
                        f"suspension awaits approval for tool call "
                        f"{record.pending_call_id}, which is not an open call.",
                    )
                state.suspended = True
                state.suspend_reason = record.reason
                state.suspend_expires_at = record.expires_at
                state.suspended_at = record.at
                state.pending_approval_call_id = record.pending_call_id
                state.suspend_question = record.question
                state.suspend_questions = record.questions
                state.turn_open = False
                state.model_call_open = False

            case Resumed():
                if not state.suspended:
                    raise CorruptLog(
                        CorruptionReason.UNKNOWN_OPERATION,
                        resolved_run_id,
                        record.seq,
                        "the Run resumed without being suspended.",
                    )
                if state.pending_approval_call_id is not None and record.approved is not None:
                    state.approval_decisions[state.pending_approval_call_id] = record.approved
                if state.pending_approval_call_id is not None and record.payload:
                    state.resume_payloads[state.pending_approval_call_id] = dict(record.payload)
                if state.suspended_at is not None:
                    waited = (record.at - state.suspended_at).total_seconds()
                    state.suspended_seconds += max(waited, 0.0)
                state.suspended = False
                state.resumed_since_attempt = True
                state.suspend_reason = None
                state.suspend_expires_at = None
                state.suspended_at = None
                state.pending_approval_call_id = None
                state.suspend_question = None
                state.suspend_questions = ()

            case TaskListUpdated():
                # Replaced wholesale. The record carries the whole list for
                # exactly this reason: folding a delta would make the answer
                # depend on having seen every earlier record, and a replay from
                # a compaction boundary would silently produce a shorter plan.
                state.tasks = record.tasks

            case ComponentShown():
                # Appended, never replaced. See `ComponentShown` for why this
                # is the opposite fold from the plan directly above it.
                state.components = (*state.components, record.component)

            case CompactionApplied():
                if record.replaced_to_seq < record.replaced_from_seq:
                    raise CorruptLog(
                        CorruptionReason.INVALID_COMPACTION_REASON,
                        resolved_run_id,
                        record.seq,
                        f"compaction replaces seq {record.replaced_from_seq}.."
                        f"{record.replaced_to_seq}, which runs backwards.",
                    )
                if record.replaced_to_seq >= record.seq:
                    raise CorruptLog(
                        CorruptionReason.INVALID_COMPACTION_REASON,
                        resolved_run_id,
                        record.seq,
                        f"compaction at seq {record.seq} claims to replace up to seq "
                        f"{record.replaced_to_seq}, which is not yet written.",
                    )
                state.compaction_boundary_seq = max(
                    state.compaction_boundary_seq, record.replaced_to_seq
                )
                state.compaction_summaries.append(record.summary)
                # Writing the summary was a model call. It is counted apart
                # from `model_calls`, which counts turns, and its tokens join
                # the Run's usage through the same arithmetic every other call
                # uses: money spent is money spent, whichever half of the loop
                # spent it (DESIGN.md §13.2).
                state.compaction_calls += 1
                state.usage = state.usage + record.usage
                if not record.usage_reported:
                    state.unreported_usage_calls += 1
                _fold_cost(state, record.cost, record.seq, resolved_run_id)

            case RunSettled():
                state.settled = True
                state.terminal_state = record.state
                state.output = record.output
                state.failure = record.failure
                state.turn_open = False
                state.model_call_open = False

    return state


def _continue_from(prior: RunStateView) -> RunStateView:
    """A deep-enough copy of ``prior`` to fold onto without mutating the caller's.

    The mutable collections are copied; everything else is immutable or a scalar.
    Folding into the caller's own state object would make ``reduce`` impure in
    the one way that matters: calling it twice would give different answers.
    """
    return dataclasses.replace(
        prior,
        open_tool_calls=dict(prior.open_tool_calls),
        tool_results=list(prior.tool_results),
        steps=dict(prior.steps),
        pending_steer=list(prior.pending_steer),
        pending_follow_up=list(prior.pending_follow_up),
        pending_next_run=list(prior.pending_next_run),
        failure_streaks=dict(prior.failure_streaks),
        repeat_counts=dict(prior.repeat_counts),
        compaction_summaries=list(prior.compaction_summaries),
        result_handles=dict(prior.result_handles),
        children=dict(prior.children),
        resume_payloads=dict(prior.resume_payloads),
        run_input=dict(prior.run_input),
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_belongs(record: Record, run_id: RunId) -> None:
    if record.run_id != run_id:
        raise CorruptLog(
            CorruptionReason.UNKNOWN_OPERATION,
            run_id,
            record.seq,
            f"this record belongs to run {record.run_id}. A log holds one Run's "
            "records, and mixing two would let one tenant's state derive from "
            "another's records.",
        )


def _check_sequence(record: Record, expected: int, run_id: RunId) -> None:
    """Rule 2: seq is gapless.

    Checked here rather than at write admission. The writer only ever sees the
    record in front of it; this fold sees the whole log as the store returned
    it, so it is the last layer that can still notice a gap. Reading past one
    would silently reduce a log that is missing its middle.
    """
    if record.seq != expected:
        raise CorruptLog(
            CorruptionReason.NON_CONSECUTIVE_SEQ,
            run_id,
            record.seq,
            f"expected seq {expected}. Either a write was lost, or a partial range "
            "is being folded as though it were whole.",
        )


def _fold_cost(state: RunStateView, cost: Cost | None, seq: int, run_id: RunId) -> None:
    """Add one call's cost to the Run's running total.

    One function because two record kinds carry a cost now -- a turn's model
    call and a compaction's summarising call -- and two copies of this
    arithmetic would eventually disagree about which of them counts as
    unpriced. ``None`` is never folded in as zero: it is counted, so a total
    can say it is incomplete instead of quietly understating the bill
    (DESIGN.md §13.2). A ``CompactionApplied`` written before the record
    carried a cost at all reads as unpriced here, which is the truth about it:
    that call's cost is not known.
    """
    if cost is None:
        state.unpriced_model_calls += 1
        return
    if cost.source == "provider":
        state.provider_reported_costs += 1
    if state.cost is None:
        state.cost = cost
    elif state.cost.currency != cost.currency:
        raise CorruptLog(
            CorruptionReason.INCONSISTENT_COST,
            run_id,
            seq,
            f"this call is priced in {cost.currency} but earlier calls "
            f"were priced in {state.cost.currency}; the two cannot be summed "
            "into one total.",
        )
    else:
        state.cost = state.cost + cost


def _require_open_model_call(state: RunStateView, seq: int, run_id: RunId, what: str) -> None:
    if not state.model_call_open:
        raise CorruptLog(
            CorruptionReason.UNKNOWN_OPERATION,
            run_id,
            seq,
            f"a model call {what} but none was started. The report pairs every finish "
            "with its start, and a finish with no start is a writer bug.",
        )


def _check_deferred_handle(state: RunStateView, record: ToolCallStarted, run_id: RunId) -> None:
    """A handle must name a result this Run stored *before* the read.

    Checked as the call starts, against the handles issued so far, rather
    than against the final set once the whole log is folded: a read recorded
    before its handle existed is just as impossible as one naming a handle
    that never existed. A handle that resolved across Runs would be a
    cross-tenant read dressed up as a tool call, which is why this is
    corruption rather than a not-found.
    """
    handle = record.arguments.get("handle")
    if record.tool != "read_tool_output" or not isinstance(handle, str):
        return
    if handle not in state.result_handles:
        raise CorruptLog(
            CorruptionReason.INVALID_DEFERRED_HANDLE,
            run_id,
            record.seq,
            f"handle {handle!r} names no result this Run had stored when the read "
            "started. A handle that resolved outside its own Run would be a "
            "cross-tenant read.",
        )


def _cancellation_detail(
    origin: PendingQueueEntry | None,
    seq: int,
    entry_id: str,
    cancelled: set[str],
    consumed: set[str],
) -> str:
    if origin is None:
        return "no enqueue was ever recorded for it."
    if entry_id in consumed:
        return "it was already consumed, so there is nothing pending to withdraw."
    if entry_id in cancelled:
        return "it was already cancelled."
    return f"its enqueue at seq {origin.enqueued_seq} is not strictly before seq {seq}."


# ---------------------------------------------------------------------------
# Derivation helpers
# ---------------------------------------------------------------------------


def _queue_for(state: RunStateView, queue: QueueKind) -> list[PendingQueueEntry]:
    match queue:
        case QueueKind.STEER:
            return state.pending_steer
        case QueueKind.FOLLOW_UP:
            return state.pending_follow_up
        case QueueKind.NEXT_RUN:
            return state.pending_next_run


def _remove_entry(state: RunStateView, entry: PendingQueueEntry) -> None:
    queue = _queue_for(state, entry.queue)
    for index, pending in enumerate(queue):
        if pending.entry_id == entry.entry_id:
            del queue[index]
            return


@dataclass
class RepeatTally:
    """How many times one exact call has come back with the same answer."""

    result_digest: str
    count: int


_NO_RESULT: Final = object()
"""Distinguishes "no result supplied" from a result that is genuinely ``None``,
which a tool returning nothing produces and which must key differently from the
call on its own."""

_DIGEST_LENGTH: Final = 16
"""Enough hex that an accidental collision is irrelevant, short enough to read
in a log line. A collision merges two distinct calls into one counter, which at
worst trips the repetition guard early on a Run already making no progress."""


def call_digest(tool: str, arguments: Any, result: Any = _NO_RESULT) -> str:
    """A stable key for "this exact call, answered this exact way".

    The counting half of the repetition guard; the policy that reads these
    counts is ``psych_runtime.tools.repetition``, which re-exports this name.

    Called two ways. With a result it keys "this call, answered this way", which
    is how a changed answer resets a tally. Without one it keys the call alone,
    which is what the loop can compute before running anything.

    Canonical JSON with sorted keys, so two calls a model wrote with its
    arguments in a different order count as the same call: the model chooses
    that order and it carries no meaning. ``default=str`` keeps a result holding
    something JSON cannot express from raising here, because a guard that throws
    on an unusual result is worse than one that occasionally treats two odd
    results as distinct.
    """
    body: dict[str, Any] = {"tool": tool, "arguments": arguments}
    if result is not _NO_RESULT:
        body["result"] = result
    payload = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def _update_repeat_count(
    state: RunStateView,
    tool: str,
    arguments: Any,
    record: ToolCallFinished,
    outcome: ToolOutcome,
) -> None:
    """Count identical calls that produced identical results.

    Only ``OK`` counts. A failed call is the failure-streak guard's business,
    and an aborted one is the user's interrupt rather than the model's choice;
    counting either here would make two guards fire on one event.

    An offloaded result is keyed by its blob reference rather than its bytes.
    The record's ``result`` is ``None`` once the payload moves out of the log
    (DESIGN.md §10.8), so hashing it would make every large result look
    identical to every other one and trip the guard on calls that returned
    completely different megabytes.

    In the reducer rather than the loop for the same reason as its sibling: it
    is derived from the log, so it survives suspend, resume and a Worker being
    replaced without anything holding it in a process.
    """
    if outcome is not ToolOutcome.OK:
        return
    answer: Any = record.result if record.result_blob_key is None else record.result_blob_key
    call_key = call_digest(tool, arguments)
    answer_key = call_digest(tool, arguments, answer)
    tally = state.repeat_counts.get(call_key)
    if tally is None or tally.result_digest != answer_key:
        # First time, or the answer changed. A changed answer is the model
        # learning something, which is what polling a job until it finishes
        # looks like, so the count starts over rather than carrying forward.
        state.repeat_counts[call_key] = RepeatTally(result_digest=answer_key, count=1)
    else:
        tally.count += 1


def _update_failure_streak(state: RunStateView, tool: str, outcome: ToolOutcome) -> None:
    """Count consecutive failures of one tool (DESIGN.md §10.6).

    A success resets the streak. An abort does not count as a failure: the tool
    did not fail, the Run was stopped, and counting it would push a guard toward
    tripping on the user's own interrupt.

    This lives in the reducer rather than in the loop so it survives suspend and
    resume for free: it is derived from the log, not held in a process.
    """
    match outcome:
        case ToolOutcome.OK:
            state.failure_streaks.pop(tool, None)
        case ToolOutcome.ERROR | ToolOutcome.UNKNOWN:
            state.failure_streaks[tool] = state.failure_streaks.get(tool, 0) + 1
        case ToolOutcome.ABORTED:
            pass
