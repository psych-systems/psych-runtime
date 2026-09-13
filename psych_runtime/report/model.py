"""The typed shape of a run report.

DESIGN.md §13.4. ``psych_runtime.report()`` returns one of these: a typed object, not a
dict, so a consumer's IDE and their static checker both know what a report
contains instead of trusting a key name to be spelled the same way twice.

Every model here is frozen for the same reason every Record is: a report is a
snapshot of a log at the moment it was read, and a report that could be mutated
after the fact would invite someone to "fix" a number instead of re-reading the
log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from psych_runtime.core.components import Component
from psych_runtime.core.ids import AttemptId, RunId, StepId, ToolCallId, VersionHash
from psych_runtime.core.records import (
    ModelTimings,
    ResultAttachment,
    SuspendReason,
    TerminalState,
    ToolFailure,
    ToolOutcome,
)
from psych_runtime.core.scope import Scope
from psych_runtime.core.tasks import Task
from psych_runtime.core.usage import Cost, Usage

__all__ = [
    "CompactionReport",
    "FailureStreakTrip",
    "LatencyReport",
    "ModelCallReport",
    "RunReport",
    "StepReport",
    "SubagentReport",
    "SubtreeTotals",
    "SuspensionReport",
    "ToolCallReport",
    "TotalsReport",
]


class ModelCallReport(BaseModel):
    """One model call: what was asked, what came back, what it cost.

    Attributes:
        turn: the Turn this call belongs to. Unique within a Run: DESIGN.md §5's
            agent loop opens a fresh Turn for every model call, retries included,
            so this is a stable key rather than a display counter.
        step_id: set when the Turn ran inside a workflow step.
        usage: ``None`` only when the call never finished, either because it
            failed (see ``failure``) or because the Attempt that made it died
            mid-stream (see ``dangling``). A finished call always carries usage,
            even when its cost could not be computed.
        cost: the cost recorded at call time, or ``None`` when the model had no
            known price. Never a silent zero (DESIGN.md §13.2): read
            ``RunReport.totals.cost_is_incomplete`` before summing these.
        will_retry: whether the transient budget allowed another attempt after
            this one failed. Only meaningful when ``failure`` is set.
        dangling: the call started and the log ends before it settled. The
            Attempt that made it died mid-call; a later Attempt does not retry
            the model call itself, only the turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: int
    step_id: StepId | None
    model: str
    system_prompt: str
    """The prompt this turn was sent, as sent. Read from the log rather than
    rebuilt, so a turn whose advisories differed from its neighbour's shows
    its own text. Empty for a Run recorded before this was captured."""
    tool_names: tuple[str, ...]
    """What this turn was offered, resolved per turn (DESIGN.md §10.2)."""
    usage: Usage | None
    usage_reported: bool | None
    """True when the finished provider response carried usage, false when it
    omitted it, and ``None`` for a failed or dangling call."""
    cost: Cost | None
    timings: ModelTimings | None
    finish_reason: str | None
    text: str
    tool_call_ids: tuple[ToolCallId, ...]
    started_at: datetime
    finished_at: datetime | None
    failure: ToolFailure | None
    will_retry: bool
    dangling: bool


class ToolCallReport(BaseModel):
    """One tool call: what it was asked to do, and how it ended.

    Attributes:
        outcome: ``None`` only when the call is dangling: started, and the log
            ends before a result was recorded (the same crash window the
            reducer's ``has_dangling_tool_calls`` reports on the live Run).
        result: the recorded result, whole. A large result was elided for the
            model at call time (DESIGN.md §10.8); the log, and so this report,
            always holds the full value.
        duration_seconds: ``None`` for a dangling call, since none was measured.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: ToolCallId
    tool: str
    turn: int
    step_id: StepId | None
    arguments: dict[str, Any]
    outcome: ToolOutcome | None
    result: Any
    failure: ToolFailure | None
    duration_seconds: float | None
    result_handle: str | None
    preview: str | None
    result_bytes: int
    attachments: tuple[ResultAttachment, ...] = ()
    """Named streams and files recorded beside ``result`` (a ``run_code``
    call's stdout, stderr, returned value and workspace files). Reported with
    their sizes, storage and handles, never with a host path."""
    started_at: datetime
    finished_at: datetime | None
    interruptible: bool
    safe_to_retry: bool
    parent_call_id: ToolCallId | None = None
    """The ``run_code`` call whose program made this one, or ``None``.

    A program that loops over forty records produces forty rows here and one
    ``run_code`` row. Without the edge between them a report shows forty calls
    nobody asked for and one call that appears to have done nothing -- which is
    exactly backwards, since the program is the reason all forty happened."""


class StepReport(BaseModel):
    """One workflow step: one checkpointed, memoised unit of work.

    Populated from ``StepStarted``/``StepCompleted`` pairs, which only exist for
    Steps a workflow wraps. A bare agent Run's Turns and tool calls are reported
    through ``model_calls`` and ``tool_calls`` directly and never appear here,
    because nothing wrapped them in a Step.

    A step_id retried after a failure appears once per attempt, in the order
    each attempt started, so a report shows the whole story rather than only
    the attempt that stuck.

    Attributes:
        completed: ``False`` when the step started and the log ends before it
            completed, the workflow analogue of a dangling tool call.
        child_run_id: set when this step was a nested Run (DESIGN.md §5: a
            workflow step may itself be an agent or a nested workflow). See
            ``child`` for how it is walked.
        child: the nested Run's own report, present only when the caller asked
            ``build_report`` to include children and the depth bound allowed it.
            ``None`` here does not mean there is no child; check
            ``child_run_id`` for that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: StepId
    name: str
    kind: str
    attempt_number: int
    input: dict[str, Any]
    completed: bool
    output: dict[str, Any] | None
    failure: ToolFailure | None
    child_run_id: RunId | None
    child: RunReport | None = None


class FailureStreakTrip(BaseModel):
    """The moment one tool's consecutive-failure count crossed the guard's
    threshold (DESIGN.md §10.6, §23 item 10).

    Recorded once per streak, at the call that tipped it over, rather than once
    per failure afterwards: the guard changes the model's advisory exactly once
    per streak, and that is the event worth naming. The next success resets the
    streak; a later run of failures on the same tool trips again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    call_id: ToolCallId
    streak: int
    threshold: int
    at: datetime


class SuspensionReport(BaseModel):
    """One wait: why the Run paused, and how it ended.

    Attributes:
        resumed_at: ``None`` while still waiting, or when the log ends before a
            ``resumed`` record arrived (an abandoned suspension past its expiry
            settles the Run directly; see ``RunReport.terminal_state``).
        approved: set only for an ``approval`` suspension. ``False`` denies the
            pending call, which DESIGN.md §11 treats as a settlement rather than
            a failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: SuspendReason
    question: str | None
    pending_call_id: ToolCallId | None
    suspended_at: datetime
    expires_at: datetime
    resumed_at: datetime | None
    approved: bool | None
    payload: dict[str, Any] | None


class CompactionReport(BaseModel):
    """One compaction: what it replaced, what it said, and what it cost.

    The replaced Records are still in the log and still in this report's own
    ``tool_calls`` and ``model_calls``. Compaction changes what the model sees
    next, never what happened, so this row says which stretch of the
    conversation stopped being sent rather than which stretch stopped existing.

    Attributes:
        seq: where the compaction itself sits in the log, so a reader can place
            it against the records above and below it.
        reason: ``threshold`` when the Spec's trigger was reached, ``overflow``
            when the provider refused the prompt for being too long, ``manual``
            when something outside the loop asked.
        model: which model wrote the summary, which is not necessarily the
            agent's own (``CompactionPolicy.model``). Empty for a compaction
            recorded before this was written down.
        usage: what the summarising call spent. Already inside
            ``TotalsReport.usage``; here so the spend is attributable to the
            compaction that caused it rather than only visible in the total.
        cost: ``None`` when that model had no known price. Never zero
            (DESIGN.md §13.2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    reason: Literal["threshold", "manual", "overflow"]
    replaced_from_seq: int
    replaced_to_seq: int
    summary: str
    model: str
    usage: Usage
    usage_reported: bool
    cost: Cost | None
    at: datetime


class LatencyReport(BaseModel):
    """DESIGN.md §13.3: wall-clock, the sum of the measured parts, and the gap.

    Attributes:
        wall_clock_seconds: from the Run's first record to its last. Includes
            everything, including a suspension's wait: the gap is meant to be
            visible, not hidden because it happened to be long.
        model_seconds: summed ``queue_wait_seconds + stream_duration_seconds``
            over every model call that finished. A failed or dangling call
            carries no measured duration and contributes nothing here.
        tool_seconds: summed ``duration_seconds`` over every tool call that
            finished. A dangling call contributes nothing.
        compaction_seconds: the same sum over every summarising call. Counted
            apart from ``model_seconds`` for the reason
            ``TotalsReport.compaction_calls`` is counted apart from
            ``model_calls``: it is time the Run spent fitting itself into the
            window rather than doing the work, and a breakdown that blended the
            two could not tell anyone whether compaction was worth it. It was
            in neither before, so it landed in the gap below and read as time
            nobody could explain.
        unaccounted_seconds: ``wall_clock_seconds - model_seconds -
            tool_seconds - compaction_seconds``, computed and reported rather
            than silently dropped.
            Ordinarily positive: time between Turns, suspension waits, and
            anything else the runtime does not attribute to a model or a tool
            all land here. Not clamped at zero, because a negative value is a
            real signal too (clock skew between Workers, say) and clamping would
            hide exactly what this field exists to show.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    wall_clock_seconds: float
    model_seconds: float
    tool_seconds: float
    compaction_seconds: float = 0.0
    """Defaulted so a report built before compaction was timed still validates."""
    unaccounted_seconds: float


class TotalsReport(BaseModel):
    """Run-wide totals. DESIGN.md §13.4: "totals for tokens, cost and duration".

    Attributes:
        usage: every model call's usage summed with ``Usage.__add__``, which
            keeps ``cache_read`` and ``cache_write`` disjoint from ``input`` and
            keeps ``cache_write_1h`` a subset of ``cache_write`` rather than a
            second addend. Never re-derive this by hand from ``model_calls``.
        cost: the sum of every *priced* call's cost. ``None`` only when not one
            call in the Run had a known price. Its ``source`` says where the
            figures came from, and is ``"mixed"`` when the Run used both a
            provider-reported cost and a locally computed one.
        unpriced_model_calls: how many finished calls had no known price and so
            are absent from ``cost``. The one number that keeps "this run cost
            $0.00" and "I could not price N of these calls" from looking the
            same (DESIGN.md §13.2).
        cost_is_incomplete: ``unpriced_model_calls > 0``, spelled out as its own
            field so a consumer does not have to remember which comparison
            means "the total is honest but partial".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage: Usage
    usage_source: Literal["provider", "partial", "unknown"] = "provider"
    """Whether the aggregate token counters all came from provider responses.

    ``partial`` means at least one finished call omitted usage; ``unknown``
    means every finished call omitted it (or there were no finished calls).
    """
    unreported_usage_calls: int = 0
    """Finished model and compaction calls whose provider returned no usage.

    Their zero-valued counters are excluded from any claim of completeness;
    this count lets readers distinguish measured zero from missing metering.
    """
    cost: Cost | None
    unpriced_model_calls: int
    cost_is_incomplete: bool
    provider_reported_costs: int = 0
    """How many priced calls carried a figure the provider reported rather than
    one Psych computed. ``cost.source`` says which kind the total is: `provider`
    when every priced call came from the provider, `computed` when every one was
    derived locally, and `mixed` when the total is a sum of both.

    Named rather than left to be inferred, because reconciling against an
    invoice needs to know which rows came from where, and a total that blended
    the two without saying so would look like one number and be two."""
    model_calls: int
    failed_model_calls: int
    tool_calls: int
    latency: LatencyReport
    compaction_calls: int = 0
    """Summarising model calls, counted apart from ``model_calls`` because a
    compaction is not a turn. Their tokens and cost are inside ``usage`` and
    ``cost`` above: the bill is the bill whichever half of the loop ran it, and
    this is what lets a reader see how much of it was spent fitting the
    conversation into the window."""


class SubtreeTotals(BaseModel):
    """One Run plus every descendant it spawned, added up.

    ``RunReport.totals`` deliberately covers one Run alone and says so, because a
    workflow orchestrating several expensive children and one cheap one is a
    different report from a flat sum. This is the flat sum, kept beside it rather
    than folded into it, so a reader picks the number they meant instead of
    discovering which one they got.

    Attributes:
        complete: whether every descendant was actually counted. ``False`` when
            a child was still running, or when ``child_depth`` stopped the walk
            before the bottom of the tree. A total that could not say this would
            look authoritative and be partial, which is the same failure
            ``cost_is_incomplete`` exists to prevent one level down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage: Usage
    unreported_usage_calls: int = 0
    cost: Cost | None
    unpriced_model_calls: int
    cost_is_incomplete: bool
    runs: int
    """How many Runs are in this total, this one included. A branch that cost
    nothing and a branch that was never walked look identical without it."""
    complete: bool


class SubagentReport(BaseModel):
    """One subagent a Run composed and started (DESIGN.md §17).

    The composed path only. A ``SubagentRef`` delegated to inline is a nested Run
    reached through ``StepReport.child_run_id`` and stays there: the two are
    different mechanisms with different lifetimes, and a report that merged them
    would have to lie about one of them to describe the other.

    Attributes:
        child_version_hash: the Version the child pinned. The composed Spec is
            published like any other, so a reader can fetch it and see exactly
            the agent the model wrote.
        tools: what the child actually held after narrowing, not what was asked
            for.
        terminal_state: ``None`` while it is still running -- or while its
            ending has not reached this parent's log, which from the parent's
            side is the same thing and is why the field is read from the log
            rather than from the child.
        report: the child's own report, present only when ``child_depth``
            allowed the walk. ``None`` does not mean there is nothing there.
        subtree: this child and its own descendants, added up.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    child_run_id: RunId
    call_id: ToolCallId
    child_version_hash: VersionHash
    purpose: str
    task: str
    deliverable: str
    tools: tuple[str, ...]
    model: str
    delegation_depth: int
    background: bool
    spawned_at: datetime
    finished_at: datetime | None
    terminal_state: TerminalState | None
    output: dict[str, Any] | None
    failure: ToolFailure | None
    messages_sent: int
    """How many times the parent steered this child while it ran."""
    usage: Usage
    unreported_usage_calls: int = 0
    cost: Cost | None
    unpriced_model_calls: int
    report: RunReport | None = None
    subtree: SubtreeTotals | None = None


class RunReport(BaseModel):
    """The whole report for one Run. DESIGN.md §13.4.

    Attributes:
        version_hash: the Version this Run pinned at admission (DESIGN.md §4).
        spec_name: the pinned Spec's name, for a report that reads on its own.
        system_prompt: the system prompt as sent, read from the log. The most
            recent turn's, which is the one a reader means by "the prompt";
            every turn's own is on its ``ModelCallReport``, because a turn
            whose advisories changed was genuinely told something different.

            This used to be reconstructed from the pinned Spec, which could not
            know what the runtime added at assembly time -- a withheld tool, an
            unreachable optional server, a deferred catalogue, what each
            connected system is for -- and so showed something the model never
            saw while looking authoritative. A Run recorded before the prompt
            was captured still falls back to that reconstruction, because there
            a guess beats an empty box.

            Empty for a ``WorkflowSpec`` Run, which has no system prompt of its
            own; a nested agent step's own prompt is on its child report.
        steps: workflow steps, in the order each attempt started. Empty for a
            bare agent Run.
        tool_calls: every tool call, in the order each started.
        model_calls: every model call, in the order each started.
        suspensions: every suspend/resume cycle, in order.
        failure_streak_trips: every time a tool's consecutive-failure count
            crossed the guard's threshold, in order.
        terminal_state: ``None`` while the Run has not settled yet.
        orphaned_attempt_id: set only for a ``force_settled`` terminal state:
            the Attempt the supervisor gave up waiting on (DESIGN.md §8.4). Its
            dangling tool calls, if any, still show up in ``tool_calls`` with
            ``outcome=None``; this names whose work was abandoned.
        totals: token, cost and latency totals over this Run alone. A child's
            totals are not folded in; sum them yourself if that is what a caller
            wants, since a workflow orchestrating several expensive children and
            one cheap one is a different report from a flat sum either way.
        continues_run_id: the Run this one continues, when it was admitted by
            ``psych_runtime.dispatch(continues=...)`` as a later message in the same
            conversation. ``None`` for a thread's first Run. Read
            straight off ``RunAdmitted.continues_run_id`` -- the same field
            ``psych_runtime.runtime.thread.load_thread_history`` walks to build the
            conversation a Worker actually sent -- so this report and that
            conversation can never disagree about which Runs make up the
            thread. Totals above still cover this Run alone; an ancestor's own
            cost is on its own report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    scope: Scope
    version_hash: VersionHash
    spec_name: str
    system_prompt: str
    steps: tuple[StepReport, ...]
    tool_calls: tuple[ToolCallReport, ...]
    model_calls: tuple[ModelCallReport, ...]
    suspensions: tuple[SuspensionReport, ...]
    failure_streak_trips: tuple[FailureStreakTrip, ...]
    terminal_state: TerminalState | None
    output: dict[str, Any] | None
    failure: ToolFailure | None
    orphaned_attempt_id: AttemptId | None
    admitted_at: datetime
    settled_at: datetime | None
    totals: TotalsReport
    components: tuple[Component, ...] = ()
    """Everything the agent showed, oldest first.

    Every one of them, unlike ``tasks`` just below, which keeps only the final
    plan. A plan is one thing revised; components are several things shown, and
    a report that kept only the last would say the Run showed one card when it
    showed four."""
    subagents: tuple[SubagentReport, ...] = ()
    """Subagents this Run composed at run time and ran in the background, in
    spawn order (DESIGN.md §17).

    Empty for every Run that composed none, which is every Run whose Spec
    carries no ``SpawnEnvelope``. A blocking ``delegate`` to a subagent its
    author wrote is not here: that is a nested Run and appears through
    ``steps``, because the two have different lifetimes and merging them would
    mean lying about one to describe the other."""
    subtree: SubtreeTotals | None = None
    """This Run and every descendant, added up, or ``None`` when it spawned
    none.

    ``totals`` above stays this Run alone and keeps saying so. Both are here
    because both are asked for: "what did this agent cost" and "what did this
    request cost" are different questions the moment a tree exists, and a
    report that answered only one of them would have every consumer summing the
    other by hand, differently (DESIGN.md §13.4)."""
    tasks: tuple[Task, ...] = ()
    """The plan as the model last left it, for an agent given the task tool.

    The final state rather than every revision: a reader wants to know what the
    Run set out to do and how far it got, and the intermediate lists are in the
    log for anyone who needs them. Empty for every agent that was not offered
    the tool, which is the default."""
    compactions: tuple[CompactionReport, ...] = ()
    """Every time this Run replaced part of its conversation with a summary,
    in order. Empty for every agent that did not ask for compaction, which is
    the default. The replaced Records are still in ``tool_calls`` and
    ``model_calls``: what a compaction changes is what the model is sent
    next."""
    continues_run_id: RunId | None = None


# Resolves the forward references StepReport.child and SubagentReport.report,
# both created by the mutual reference between those models and RunReport above.
StepReport.model_rebuild()
SubagentReport.model_rebuild()
