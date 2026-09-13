"""The record log: the spine of the system.

DESIGN.md §6. A Run's state is not stored, it is derived. The Run owns an
append-only log of Records and a pure reducer folds the log into current state.

## The five rules

1. Append-only. Records are never updated or deleted.
2. Sequenced. Every Record carries ``(run_id, seq)`` with ``seq`` gapless from 1.
3. Single writer. Exactly one Attempt holds the lease and may append.
4. Conditional write. Appending Record ``n`` asserts ``n`` does not yet exist.
5. Contradiction is refused, never repaired.

## What the log gives you for free

The run report, the resumable stream, step memoisation and the whole trace are
projections over this log rather than subsystems beside it. Do not build parallel
storage for any of them: a second copy is a second thing that can disagree.

## Why every record is frozen and carries a Scope

Frozen because rule 1 is not a convention. A Record that can be mutated after it
is appended makes "append-only" a comment rather than a property.

Scope-stamped because every store query is filtered by tenancy (§14). Stamping at
write time means a later reader cannot forget to filter, and a record that
somehow reached the wrong Run's log is visibly foreign rather than silently
readable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from psych_runtime.core.components import Component
from psych_runtime.core.ids import (
    AttemptId,
    RunId,
    StepId,
    ToolCallId,
    VersionHash,
    WorkerId,
)
from psych_runtime.core.questions import AskedQuestion
from psych_runtime.core.scope import Scope
from psych_runtime.core.tasks import Task
from psych_runtime.core.usage import Cost, Usage

__all__ = [
    "RECORD_ADAPTER",
    "AbortRequested",
    "AttemptStarted",
    "CompactionApplied",
    "ComponentShown",
    "ModelCallFailed",
    "ModelCallFinished",
    "ModelCallStarted",
    "ModelTimings",
    "QueueCancelled",
    "QueueConsumed",
    "QueueEnqueued",
    "QueueKind",
    "Record",
    "ResultAttachment",
    "Resumed",
    "RunAdmitted",
    "RunSettled",
    "StepCompleted",
    "StepStarted",
    "SubagentFinished",
    "SubagentMessaged",
    "SubagentSpawned",
    "SuspendReason",
    "Suspended",
    "TerminalState",
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolFailure",
    "ToolOutcome",
    "TurnStarted",
]


class QueueKind(StrEnum):
    """Where input that arrived mid-Run is parked (DESIGN.md §9).

    Three queues rather than one flag, because "stop mid-response and
    immediately send another request" is three different intentions and
    collapsing them produces the race that breaks streaming after an interrupt.
    """

    STEER = "steer"
    """Inject into the current turn. Refused after an abort."""

    FOLLOW_UP = "follow_up"
    """Deliver after the current turn settles. Refused after an abort."""

    NEXT_RUN = "next_run"
    """Deliver to the next Run. Explicitly legal after an abort: this is what
    makes "stop and send another message" a modelled transition rather than an
    accident."""


class TerminalState(StrEnum):
    """How a Run ended. Every Run reaches exactly one of these."""

    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    ABANDONED = "abandoned"
    """A suspension waited past its expiry (DESIGN.md §11)."""
    FORCE_SETTLED = "force_settled"
    """The supervisor wrote a terminal record over work that would not unwind
    inside the grace period, and orphaned it (DESIGN.md §8.4)."""


class SuspendReason(StrEnum):
    """Why a Run is waiting (DESIGN.md §11)."""

    APPROVAL = "approval"
    QUESTION = "question"
    EXTERNAL = "external"
    CHILDREN = "children"
    """Waiting on subagents it spawned in the background, with no work of its
    own left to do (DESIGN.md §17 by way of §11).

    A fifth reason rather than a reuse of ``EXTERNAL``, and the difference is
    worth the enum member. Every other suspension waits on something outside
    Psych entirely -- a person, a webhook, a clock -- so nothing in the library
    can say when it will end or whether it ever will. This one waits on Runs
    Psych admitted, can name, and can read the state of, which makes two things
    possible that ``EXTERNAL`` could not offer: a parent about to suspend
    reconciles its children first and does not suspend at all if they have
    already finished, and a reader can be told what it is waiting for by name.

    The alternative was to let the parent hold its lease and poll. That is
    exactly the shape DESIGN.md §11 exists to remove: a Run waiting on something
    slow must persist, release its lease and wait, or a fan-out of four children
    pins four Workers doing nothing while their own children queue behind
    them."""


class ToolOutcome(StrEnum):
    """How a tool call ended."""

    OK = "ok"
    ERROR = "error"
    ABORTED = "aborted"
    """Cancelled by an interrupt. Only an ``interruptible`` tool reaches this."""
    UNKNOWN = "unknown"
    """The Attempt died mid-call and a later Attempt settled it without knowing
    whether the side effect happened. Recorded honestly rather than guessed:
    DESIGN.md §8.4 requires an orphaned call to be visibly incomplete rather than
    ambiguous, and 'unknown' is the only truthful value."""


class ToolFailure(BaseModel):
    """A tool failure, as data the model can read.

    DESIGN.md §18 states this for code execution and it holds for every tool: a
    traceback is returned so the model can fix its approach, not raised so it
    kills the turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, max_length=128)
    """A short classifier: ``timeout``, ``http_500``, ``validation``. Used by the
    failure-streak guard, so it is a stable token rather than prose."""
    message: str = Field(max_length=8192)
    traceback: str | None = Field(default=None, max_length=65_536)
    """Kept for whoever operates the platform: a report, a trace, an alert. Not
    shown to the model unless ``traceback_is_for_the_model`` says so."""
    transient: bool = False
    """Whether the classifier judged this worth retrying (DESIGN.md §8.6)."""
    traceback_is_for_the_model: bool = False
    """Whether ``traceback`` may be replayed into the conversation.

    DESIGN.md §18 wants a *sandboxed program's* traceback handed back as data,
    because the model wrote that program and can fix the line. A host tool's
    traceback is a different thing: it is the consumer's own code, and it
    carries their filesystem paths, module names and whatever the exception
    message holds -- an asyncpg error embeds the DSN, a botocore error the
    bucket, an httpx error the internal URL it called. Replaying that into the
    prompt puts it in the model's context, in the provider's logs, and one
    prompt-injection away from being repeated to a user.

    So the default is False and the log still keeps the traceback. Only the
    code-execution path sets this True, which is exactly the case the design
    asks for."""


class ResultAttachment(BaseModel):
    """One named stream or file a tool call produced beside its result.

    A ``run_code`` call produces several things at once: what the program
    printed, what it printed to stderr, what it returned, and the files it
    wrote. The model is shown a compact preview of each; the full bytes are
    kept here, inline when small enough and in the Runtime's ``BlobStore`` when
    not (DESIGN.md §10.8, the same offload rule a plain large result follows).
    Each attachment is addressable through ``read_tool_output`` by its own
    handle, which the reducer registers exactly like ``result_handle``, so a
    model can page through a megabyte of stdout without any of it re-entering
    the prompt whole.

    Attributes:
        name: what this is: ``stdout``, ``stderr``, ``value``, or
            ``file:<relative path>`` for a workspace artifact. Never a host path.
        handle: the string the model passes to ``read_tool_output``.
        content_type: how to read ``data`` or the blob back.
        size_bytes: the size of what was captured.
        observed_bytes: how much the program actually produced, which is more
            than ``size_bytes`` when the backend stopped capturing at its cap.
        truncated: ``observed_bytes > size_bytes``, stated rather than derived
            so a reader does not have to compare two numbers to learn it.
        data: the captured bytes, when they were small enough to keep in the
            record. ``None`` when they live in the ``BlobStore`` or were not
            kept at all; ``stored`` says which.
        stored: ``inline`` (in ``data``), ``blob`` (in the ``BlobStore`` under
            a key derived from this Run and call, never from this record), or
            ``preview_only`` (the bytes beyond the preview were not kept, by
            policy or because no ``BlobStore`` was wired; the preview itself
            is in the call's result).
        sha256: a hex digest of the captured bytes, so a reader fetching a blob
            can check it got what was written.
        readable: whether the model may read this attachment through
            ``read_tool_output``. ``False`` for bookkeeping a person reads in a
            report but the model has no use for (the execution's enforcement
            report), so its presence does not cost the prompt a tool
            definition on every later turn.
    """

    # base64 on the wire, not utf-8: an attachment is arbitrary bytes (a
    # program's binary stdout, an image it wrote), and pydantic's default
    # bytes encoding refuses anything that is not valid UTF-8 at the moment a
    # store serialises the record, which would fail the write of a legitimate
    # result.
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    name: str = Field(min_length=1, max_length=512)
    handle: str = Field(min_length=1, max_length=256)
    content_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    observed_bytes: int = Field(ge=0)
    truncated: bool = False
    data: bytes | None = None
    stored: Literal["inline", "blob", "preview_only"] = "inline"
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    readable: bool = True

    @model_validator(mode="after")
    def _storage_is_consistent(self) -> Self:
        if self.stored == "inline" and self.data is None:
            raise ValueError("an inline ResultAttachment must carry its data")
        if self.stored != "inline" and self.data is not None:
            raise ValueError(
                "a ResultAttachment stored in a blob or as a preview only must not also "
                "carry inline data"
            )
        if self.observed_bytes < self.size_bytes:
            raise ValueError("observed_bytes cannot be smaller than what was captured")
        return self


class ModelTimings(BaseModel):
    """Where the time went in one model call (DESIGN.md §13.3).

    Separate from the record's own timestamp because the gap between a Run's
    wall-clock and the sum of its parts is the interesting number, and it cannot
    be computed if the parts were never measured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    queue_wait_seconds: float = Field(default=0.0, ge=0)
    time_to_first_token_seconds: float | None = Field(default=None, ge=0)
    stream_duration_seconds: float = Field(default=0.0, ge=0)


class _RecordBase(BaseModel):
    """Fields every Record carries.

    Attributes:
        run_id: whose log this belongs to.
        seq: position in that log. Gapless from 1; the reducer refuses a gap.
        at: when the writer produced it. Wall-clock, for reports and latency.
            The reducer never reads it, because the reducer is pure.
        scope: the tenancy stamp (DESIGN.md §14).
        attempt_id: which execution pass wrote it. Present on everything written
            by a Worker so a log that interleaved two Attempts is visible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    seq: int = Field(ge=1)
    at: datetime
    scope: Scope
    attempt_id: AttemptId | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class RunAdmitted(_RecordBase):
    """Record 1 of every Run. Nothing precedes it.

    Pins the Version for the Run's whole life, so editing an agent never mutates
    a Run in flight (DESIGN.md §4).
    """

    type: Literal["run_admitted"] = "run_admitted"
    version_hash: VersionHash
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=256)
    deadline_at: datetime
    """Every Run has one (DESIGN.md §8.4). There is no 'no deadline'."""
    parent_run_id: RunId | None = None
    delegation_depth: int = Field(default=0, ge=0)
    """Monotone: runtime options may deepen it but never lower it. A resumed
    child arriving with fresh options must not be counted from zero, or it
    delegates as though it were top-level and the recursion budget is
    defeated (DESIGN.md §17)."""
    continues_run_id: RunId | None = None
    """The immediate predecessor Run in a conversation thread, when this Run
    was admitted by ``psych_runtime.dispatch(..., continues=...)`` rather than as a
    fresh start.

    Deliberately not ``parent_run_id``. That field is delegation: a
    subagent's link to the Run that spawned it as a nested step, present the
    moment the child is admitted, bounded by ``max_delegation_depth``
    (DESIGN.md §17). This field is a chat thread's link to the Run whose
    conversation it continues -- two otherwise-unrelated top-level Runs, no
    delegation between them, chained because a person or a job sent a second
    message after the first Run had already settled (DESIGN.md §23.3).
    Conflating the two would make a delegation depth check start counting a
    chat thread's length, and would make a report walking a delegation tree
    wander into an unrelated conversation.

    Continuity is derived from this field alone, never from a side table: see
    ``psych_runtime.runtime.thread.load_thread_history``, which walks it back through
    the chain to rebuild what an earlier Attempt would have sent, and refuses
    to cross a Scope boundary while doing so (DESIGN.md §14)."""


class AttemptStarted(_RecordBase):
    """A Worker claimed the lease and began an execution pass.

    Written on every claim including reclaims, so the log shows how many times a
    Run was picked up and by whom.
    """

    type: Literal["attempt_started"] = "attempt_started"
    worker_id: WorkerId
    attempt_number: int = Field(ge=1)
    """Increments by one per claim. A gap means a claim was not recorded."""
    reclaimed_expired_lease: bool = False
    """True when this Attempt took over from one whose lease expired, which is
    the crash-recovery path rather than a fresh dispatch."""


class RunSettled(_RecordBase):
    """The terminal record. Nothing follows it, and the reducer refuses anything
    that does."""

    type: Literal["run_settled"] = "run_settled"
    state: TerminalState
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    orphaned_attempt_id: AttemptId | None = None
    """Set when the supervisor force-settled over work that would not unwind.
    Names what was abandoned, so a later reader knows the difference between a
    clean failure and work that may still have been running."""


# ---------------------------------------------------------------------------
# Turns and model calls
# ---------------------------------------------------------------------------


class TurnStarted(_RecordBase):
    """One model call plus the tool calls it produced. Agents loop over these."""

    type: Literal["turn_started"] = "turn_started"
    turn: int = Field(ge=1)
    step_id: StepId | None = None
    """Set when this turn belongs to a workflow step rather than a bare agent."""


class ModelCallStarted(_RecordBase):
    """Recorded before the call goes out.

    Before, not after, so a crash mid-call leaves a visibly incomplete pair
    rather than no evidence that a call was ever made.
    """

    type: Literal["model_call_started"] = "model_call_started"
    turn: int = Field(ge=1)
    model: str = Field(min_length=1)
    prompt_tokens_estimate: int | None = Field(default=None, ge=0)
    tool_names: tuple[str, ...] = ()
    """The tool set as resolved for this turn. Recorded because the set is
    resolved per turn (DESIGN.md §10.2) and a report six months later should say
    what the model was actually offered, not what the Spec granted."""
    system_prompt: str = ""
    """The system prompt as sent, for this turn, in full.

    Recorded rather than reconstructed. ``psych_runtime.report`` used to rebuild it
    from the pinned Spec and said so in its own docstring, admitting that
    durable memories and runtime advisories were missing because neither was
    in the log. That was tolerable while advisories were rare. It stopped
    being tolerable once they carried meaning a reader needs: a withheld tool,
    an unreachable optional server, a deferred catalogue, and what each
    connected system is for. A reconstruction shows something the model never
    saw while looking authoritative, which is the worst way to be wrong.

    Every other question a person asks of a trace is already answerable from
    the log. "What was it told" was not. Empty on a record written before this
    field existed, which reads as "not recorded" rather than "the prompt was
    empty": a Run with no system prompt at all has nothing for the report to
    show either way."""


class ModelCallFinished(_RecordBase):
    """The call returned. Carries the usage and the cost computed at the time.

    ``cost`` is ``None`` when the model has no known price. Never zero: a silent
    zero makes metering look correct and be wrong (DESIGN.md §13.2).
    """

    type: Literal["model_call_finished"] = "model_call_finished"
    turn: int = Field(ge=1)
    model: str = Field(min_length=1)
    usage: Usage
    usage_reported: bool = True
    """False when the provider omitted usage from its response.

    Defaulted to true so records written before this distinction existed keep
    their original meaning. New adapters set it explicitly.
    """
    cost: Cost | None
    timings: ModelTimings
    finish_reason: str = Field(min_length=1)
    text: str = ""
    tool_calls: tuple[ToolCallId, ...] = ()
    """Ids the model asked to invoke. The reducer checks every one of these gets
    a result, because a provider rejects an assistant message whose tool calls
    have no answers and the conversation becomes unrecoverable (DESIGN.md §9)."""


class ModelCallFailed(_RecordBase):
    """The call did not return usable output."""

    type: Literal["model_call_failed"] = "model_call_failed"
    turn: int = Field(ge=1)
    model: str = Field(min_length=1)
    failure: ToolFailure
    will_retry: bool = False
    """Whether the transient budget allowed another attempt. Recorded so a report
    distinguishes 'gave up' from 'still trying'."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolCallStarted(_RecordBase):
    """Recorded before the tool executes.

    DESIGN.md §8.4: every tool call is recorded before execution and its result
    after, so an orphaned call is visibly incomplete rather than ambiguous. This
    ordering is the whole reason force-settlement is safe.
    """

    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: ToolCallId
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    turn: int = Field(ge=1)
    step_id: StepId | None = None
    interruptible: bool = True
    """From the Spec. A non-interruptible tool is allowed to finish on abort;
    an interruptible one is cancelled. The difference between a stopped agent
    and a half-issued refund (DESIGN.md §9)."""
    safe_to_retry: bool = False
    """Whether a later Attempt may re-execute this call after a crash rather than
    recording an unknown outcome. Defaults False: assuming a side effect is
    repeatable is how double refunds happen."""
    parent_call_id: ToolCallId | None = None
    """The ``run_code`` call whose program made this one, when a program did.

    ``None`` for everything the model called itself, which is every call
    written before this field existed, so an old log reads exactly as it did.

    A program that loops over forty orders produces forty of these records. The
    model's own context holds one ``run_code`` result -- that is the point of
    code execution -- so without this a report and a trace would show forty
    calls with no visible cause and one call with no visible effect. It is a
    tree, and this is the edge."""


class ToolCallFinished(_RecordBase):
    """The call ended, one way or another.

    The full result lives here, or a reference to it does. When it exceeds the
    large-result threshold the model's view is elided to ``result_handle`` plus
    ``preview``, but the whole result is still durably held: inline in ``result``
    below the offload threshold, or in a ``psych_runtime.store.blob.BlobStore`` above it
    (DESIGN.md §10.8 and §7; see ``psych_runtime.tools.large_results`` for why both are
    true even though a single DynamoDB item cannot hold an arbitrarily large
    payload).
    """

    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: ToolCallId
    outcome: ToolOutcome
    result: Any = None
    failure: ToolFailure | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    result_handle: str | None = None
    """Set when the result was elided for the model. ``read_tool_output`` reads
    the stored result by this handle, and the reducer refuses a handle that names
    no stored result or one from another Run."""
    preview: str | None = None
    result_bytes: int = Field(default=0, ge=0)
    result_blob_key: str | None = None
    """Set when ``result`` was offloaded to a BlobStore instead of stored inline.
    Carries ``str(BlobKey)`` for a reader's benefit; ``read_tool_output`` does not
    parse it back into an address, it rebuilds the key from this Run's own scope
    and run id (DESIGN.md §14: a blob key is never trusted from data, only derived
    from state the reducer already validated). ``None`` on every record written
    before this field existed and on every record whose result was small enough
    to stay inline; both cases read identically because the absence of a blob key
    means exactly "read ``result``"."""
    result_content_type: str | None = None
    """How to decode the bytes named by ``result_blob_key``. Always set together
    with it, never independently: one says where the bytes are, the other says
    how to read them back, and one without the other is unusable."""
    attachments: tuple[ResultAttachment, ...] = ()
    """Named streams and files this call produced beside ``result``, each
    readable through ``read_tool_output`` by its own handle. Empty for every
    ordinary tool; ``run_code`` fills it. See ``ResultAttachment``."""

    @model_validator(mode="after")
    def _offload_fields_are_consistent(self) -> Self:
        if (self.result_blob_key is None) != (self.result_content_type is None):
            raise ValueError(
                "result_blob_key and result_content_type must be set together: a "
                "record naming a blob has to say how to decode it, and one with a "
                "content type but no blob has nothing to apply it to"
            )
        if self.result_blob_key is not None and self.result is not None:
            raise ValueError(
                "a ToolCallFinished cannot carry both an inline result and a "
                "result_blob_key: an offloaded result's payload lives in the "
                "BlobStore, not in the log, so 'result' must be None whenever "
                "'result_blob_key' is set"
            )
        handles = [attachment.handle for attachment in self.attachments]
        if len(set(handles)) != len(handles):
            raise ValueError("a ToolCallFinished's attachments must have distinct handles")
        if self.result_handle is not None and self.result_handle in handles:
            raise ValueError("an attachment handle cannot repeat the call's own result_handle")
        return self


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class StepStarted(_RecordBase):
    """A checkpointed unit of work began."""

    type: Literal["step_started"] = "step_started"
    step_id: StepId
    name: str = Field(min_length=1)
    kind: Literal["agent", "tool", "workflow", "subagent"]
    attempt_number: int = Field(default=1, ge=1)
    """Retries of one step increment this by exactly one. The reducer refuses a
    gap as ``non_consecutive_attempt``."""
    input: dict[str, Any] = Field(default_factory=dict)


class StepCompleted(_RecordBase):
    """A step finished, and its result is memoised.

    On resume, a step whose result is already here returns from the log and is
    not re-executed (DESIGN.md §5). This record is what makes a crashed workflow
    continue where it stopped instead of starting over.
    """

    type: Literal["step_completed"] = "step_completed"
    step_id: StepId
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    child_run_id: RunId | None = None
    """Set when the step was a nested Run, so the report can walk into it."""


# ---------------------------------------------------------------------------
# Interrupts, steering, suspension
# ---------------------------------------------------------------------------


class AbortRequested(_RecordBase):
    """An abort is a Record, not a flag.

    This is the whole of DESIGN.md §9. Because the abort has a sequence number,
    "what arrived after the stop" is a question the log answers rather than a
    race the runtime has to win.
    """

    type: Literal["abort_requested"] = "abort_requested"
    reason: str = Field(default="", max_length=1024)
    requested_by: str | None = Field(default=None, max_length=256)


class QueueEnqueued(_RecordBase):
    """Input that arrived while the Run was executing."""

    type: Literal["queue_enqueued"] = "queue_enqueued"
    queue: QueueKind
    entry_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


class QueueCancelled(_RecordBase):
    """A pending queue entry was withdrawn before it was consumed."""

    type: Literal["queue_cancelled"] = "queue_cancelled"
    entry_id: str = Field(min_length=1, max_length=128)


class QueueConsumed(_RecordBase):
    """A pending queue entry was delivered into the conversation.

    Separate from the enqueue so the log shows both when input arrived and when
    it actually reached the model, which are different moments and both matter in
    a report.
    """

    type: Literal["queue_consumed"] = "queue_consumed"
    entry_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Composed subagents (DESIGN.md §17)
# ---------------------------------------------------------------------------


class SubagentSpawned(_RecordBase):
    """A parent composed a child Spec at run time and admitted its Run.

    The whole tree is reconstructable from the log alone, and this is the record
    that makes that true: it names the child Run, the Version hash the child
    pinned, and the tools and model that survived narrowing. A reader six months
    later can fetch that Version and see exactly the Spec the model wrote, rather
    than a prompt that was never stored.

    Written by the parent's own Attempt, immediately after the child Run is
    admitted. A spawn recorded before admission would name a Run that might not
    exist; recorded after, a crash in between leaves a child Run nobody
    references, which is an orphan the supervisor's deadline settles rather than
    a dangling id in the parent's log.
    """

    type: Literal["subagent_spawned"] = "subagent_spawned"
    child_run_id: RunId
    name: str = Field(min_length=1, max_length=128)
    """What the parent called this child. Addresses it in every later tool call,
    so it is unique within a Run and the reducer says so."""
    call_id: ToolCallId
    """The ``spawn_subagent`` call that asked for it, so the spawn and its
    result are one thing in a report rather than two that have to be joined by
    timestamp."""
    child_version_hash: VersionHash
    """What the child pinned. The composed Spec is published like any other
    Version, which is what makes a reclaiming Worker resume the same child
    rather than one composed from a prompt written a second time."""
    purpose: str = Field(max_length=2048)
    task: str = Field(max_length=65_536)
    deliverable: str = Field(max_length=4096)
    """The three halves of the brief, as the model wrote them. Kept apart rather
    than concatenated because they answer different questions in a report: what
    was this for, what was it told, and what was it asked to hand back."""
    tools: tuple[str, ...] = ()
    """What the child actually holds after narrowing, not what was asked for. A
    report showing the request would describe a child that never existed."""
    model: str = Field(min_length=1, max_length=256)
    delegation_depth: int = Field(ge=0)
    background: bool = True
    """Whether the parent kept working. False is reserved for a caller that
    wants the composed path with blocking semantics; the tool always spawns in
    the background today, and the field exists so a log written now stays
    readable if that changes."""


class SubagentMessaged(_RecordBase):
    """The parent sent a message into a running child.

    A Record rather than a side effect, so "what did the parent tell it, and
    when" is answerable from the log alone. The message itself lands in the
    child's own log as a ``queue_enqueued`` entry: this is the parent's half,
    and the child's half is the child's.

    ## What happens to a message that arrives mid-tool-call

    It is queued for the child's next turn boundary, and that is the whole
    answer. Psych already models this: a steering entry is drained at the start
    of the next turn (``psych_runtime.runtime.agent._turn``), never injected into a turn
    already running, because there is no mid-turn mutation (DESIGN.md §10.2).
    Interrupting a tool call whose side effect has already happened is strictly
    worse than a message arriving one turn later: the refund went out either
    way, and now the log cannot say whether it did. A parent that genuinely
    wants the child stopped has ``psych_runtime.interrupt``, which is a different
    intention and is recorded as one.
    """

    type: Literal["subagent_messaged"] = "subagent_messaged"
    child_run_id: RunId
    name: str = Field(min_length=1, max_length=128)
    call_id: ToolCallId
    entry_id: str = Field(min_length=1, max_length=128)
    """The queue entry this became in the child's log, so the two halves join."""
    message: str = Field(max_length=65_536)


class SubagentFinished(_RecordBase):
    """A child Run reached a terminal state, told to the parent as a Record.

    The notification is a Record and never a callback. That is not a style
    preference: the parent may be suspended, on another machine, or not running
    at all when its child finishes, and the only thing all three have in common
    is the log. A callback would also make the reducer impure, since the tree's
    state would then depend on whether a process happened to be listening.

    Appended by whichever Attempt settled the child (``psych_runtime.runtime.notify``),
    which is why it is in ``psych_runtime.runtime.journal.EXTERNAL_RECORD_TYPES``: it is
    the one record a parent's own Attempt does not write and must tolerate
    arriving underneath it, exactly like an interrupt or a steer.

    Carries the child's usage and cost so a parent's report can roll a branch up
    without reading every descendant's log, and so the numbers survive a child
    whose log is later pruned. They are a copy of what the child's own log says,
    and the child's log stays the authority: ``psych_runtime.report`` walks into it when
    asked for depth.
    """

    type: Literal["subagent_finished"] = "subagent_finished"
    child_run_id: RunId
    name: str = Field(min_length=1, max_length=128)
    state: TerminalState
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    usage: Usage = Field(default_factory=Usage)
    unreported_usage_calls: int = Field(default=0, ge=0)
    """How many finished child calls had no provider usage counters."""
    cost: Cost | None = None
    """``None`` when the child's models had no known price. Never zero: a silent
    zero makes a branch's metering look correct and be wrong (DESIGN.md §13.2)."""
    unpriced_model_calls: int = Field(default=0, ge=0)
    """How many of the child's calls could not be priced, so a parent's rollup
    can say its total is incomplete rather than quietly understating it."""


class Suspended(_RecordBase):
    """The Run persisted, released its lease, and is waiting."""

    type: Literal["suspended"] = "suspended"
    reason: SuspendReason
    payload_schema: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime
    """Suspensions expire. A Run suspended past this is settled as abandoned
    rather than waiting forever on a user who left (DESIGN.md §11)."""
    pending_call_id: ToolCallId | None = None
    """Set for an approval or a question, naming the call awaiting an answer."""
    question: str | None = Field(default=None, max_length=8192)
    questions: tuple[AskedQuestion, ...] = ()
    """What was asked, structured, when the model used ``ask_question``.

    ``question`` above stays the one-line rendering of the same thing, so a log
    reader, a notification or an approval (which has no structure of its own)
    all keep working unchanged. Defaulted to empty so every Record written
    before this field existed still loads.

    Options are suggestions rather than an enumeration: a person answers in
    their own words whatever these say. See ``psych_runtime.core.questions``.
    """


class Resumed(_RecordBase):
    """A decision or payload arrived and the Run became runnable again."""

    type: Literal["resumed"] = "resumed"
    payload: dict[str, Any] = Field(default_factory=dict)
    approved: bool | None = None
    """Set for an approval resume. ``False`` denies the pending call, which is a
    settlement rather than a failure."""
    resumed_by: str | None = Field(default=None, max_length=256)
    """Who decided, as the consumer identifies people. Psych does not
    interpret it (DESIGN.md §14 keeps identity out of the library), but an
    approval of a destructive call whose log cannot say who approved it is not
    an audit trail, so there has to be somewhere to put the answer."""


class CompactionApplied(_RecordBase):
    """Conversation history was compacted to fit the context window.

    Records the range it replaced so the reducer can rebuild the conversation the
    model actually saw. The replaced records stay in the log: compaction changes
    what the model sees next, never what happened.

    Carries what the summary cost, because writing one is a model call. It is
    not a turn -- no ``turn_started``, no ``model_call_started``, and it is
    deliberately not counted in ``RunStateView.model_calls`` -- so a pair of
    model-call records could not describe it without either inventing a turn
    the model never took or leaving a ``model_call_finished`` outside any turn,
    which the reducer refuses. Putting the usage and the cost on this record
    instead keeps the tokens in the Run's totals and keeps them attributable to
    the thing that spent them. See ``psych_runtime.runtime.compaction``.
    """

    type: Literal["compaction_applied"] = "compaction_applied"
    reason: Literal["threshold", "manual", "overflow"]
    timings: ModelTimings = Field(default_factory=ModelTimings)
    """Where the time went in the summarising call.

    Measured for the same reason a turn's call is: §13.3 wants a latency
    breakdown that accounts for the Run's wall clock, and a summarising call
    that takes eight seconds is eight seconds of that clock. Without this it
    landed in ``unaccounted_seconds`` beside "time between turns", so a person
    reading the breakdown saw an unexplained gap and had nothing to attribute
    it to.

    Defaulted so a record written before compaction was timed still loads, and
    reads as a call that took no measurable time -- which understates rather
    than invents, and is the only honest thing to say about a duration nobody
    recorded."""
    replaced_from_seq: int = Field(ge=1)
    replaced_to_seq: int = Field(ge=1)
    summary: str = Field(min_length=1)
    model: str = ""
    """Which model wrote the summary. Empty for a record written before this
    field existed."""
    usage: Usage = Field(default_factory=Usage)
    """What the summarising call spent. Empty for a record written before this
    field existed, which reads as "not recorded" rather than as "free"; a call
    that really did report nothing is the same shape, and neither can be
    invented after the fact."""
    usage_reported: bool = False
    """Whether the summarising provider returned the usage counters.

    False by default because old compaction records used an empty ``Usage`` to
    mean "not recorded".
    """
    cost: Cost | None = None
    """``None`` when the summarising model had no known price, exactly as on
    ``ModelCallFinished``. Never zero: a silent zero makes metering look
    correct and be wrong (DESIGN.md §13.2)."""


class TaskListUpdated(_RecordBase):
    """The model rewrote its plan.

    Carries the **whole list** every time rather than a delta. A model that has
    to sequence add/update/remove calls gets it wrong eventually, and a partial
    update that half-applied would leave a plan nobody wrote. Replacing is
    idempotent: replaying this record twice produces the same list, which is
    the property the reducer needs anyway.

    Opt-in per Spec and off by default. See ``psych_runtime.core.tasks`` for why a task
    list is in Psych at all, given how much §1 refuses.
    """

    type: Literal["task_list_updated"] = "task_list_updated"
    tasks: tuple[Task, ...]
    # No call id: the record is appended by the tool body, so it lands
    # immediately after its own `tool_call_started` in the log. `seq` already
    # places it beside the turn that wrote it, and a second identifier would be
    # one more thing that can disagree with the log's own order.


class ComponentShown(_RecordBase):
    """The model showed something structured: a card, a chart, a timeline.

    **Accumulates rather than replaces**, which is the one way this record
    differs from ``TaskListUpdated`` beside it. A plan is a single thing that
    is revised, so the newest one is the answer; components are several things
    that were shown, in the order they were shown, and folding them the same
    way would leave a Run that displayed an order confirmation and then a
    delivery timeline showing only the timeline. Order matters for the same
    reason: "here is the flight, then the hotel, then the itinerary" is a
    sequence, and re-sorting it or keeping only the last would be a different
    answer.

    Carries exactly one component. A tool call that wanted to show three makes
    three calls, so each lands beside the turn that produced it and a partial
    failure loses one rather than all three.

    Opt-in per Spec and off by default. See ``psych_runtime.core.components`` for why
    a payload like this is in a library that refuses to render anything.
    """

    type: Literal["component_shown"] = "component_shown"
    component: Component
    # No call id, for the reason ``TaskListUpdated`` gives: the record is
    # appended by the tool body and `seq` already places it beside its own
    # `tool_call_started`.


type Record = Annotated[
    RunAdmitted
    | AttemptStarted
    | RunSettled
    | TurnStarted
    | ModelCallStarted
    | ModelCallFinished
    | ModelCallFailed
    | ToolCallStarted
    | ToolCallFinished
    | StepStarted
    | StepCompleted
    | AbortRequested
    | QueueEnqueued
    | QueueCancelled
    | QueueConsumed
    | Suspended
    | Resumed
    | CompactionApplied
    | TaskListUpdated
    | ComponentShown
    | SubagentSpawned
    | SubagentMessaged
    | SubagentFinished,
    Field(discriminator="type"),
]
"""Everything that happens in a Run. There is no other way for state to change."""

RECORD_ADAPTER: TypeAdapter[Record] = TypeAdapter(Record)
"""For adapters deserialising a stored row back into a Record."""
