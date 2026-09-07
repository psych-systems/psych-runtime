"""Building a run report: a pure projection over one Run's log.

DESIGN.md §6: the report is a projection over the log, never parallel storage.
Every value here is read from Records the reducer already validated, or derived
from them with the same arithmetic the reducer itself uses (``Usage.__add__``,
the log's own ``cost`` fields, the log's own timings). This module invents no
new numbers; it only arranges the ones the log already carries.

The one exception is the failure-streak trip, which needs one fact the log does
not carry on its own: the Spec's ``failure_streak_threshold``. That is why this
reads the pinned Version, not just the log, and why it is the only derivation
here that depends on anything but the Records themselves.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psych_runtime.core.errors import RunNotFound, VersionNotFound
from psych_runtime.core.ids import AttemptId, RunId, StepId, ToolCallId
from psych_runtime.core.records import (
    CompactionApplied,
    ModelCallFailed,
    ModelCallFinished,
    ModelCallStarted,
    Record,
    Resumed,
    RunAdmitted,
    RunSettled,
    StepCompleted,
    StepStarted,
    SubagentFinished,
    SubagentSpawned,
    Suspended,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutcome,
    TurnStarted,
)
from psych_runtime.core.reducer import ChildRun, RunStateView, reduce
from psych_runtime.core.spec import AgentSpec
from psych_runtime.model.prompt import system_prompt
from psych_runtime.report.model import (
    CompactionReport,
    FailureStreakTrip,
    LatencyReport,
    ModelCallReport,
    RunReport,
    StepReport,
    SubagentReport,
    SubtreeTotals,
    SuspensionReport,
    ToolCallReport,
    TotalsReport,
)
from psych_runtime.store.port import Store

__all__ = ["build_report"]


async def build_report(store: Store, run_id: RunId, *, child_depth: int = 0) -> RunReport:
    """Project one Run's log, and its pinned Version, into a typed report.

    Args:
        store: where the log and the pinned Version live.
        run_id: the Run to report on.
        child_depth: how many levels of nested Run to walk into and attach as
            ``StepReport.child`` and ``SubagentReport.report``. ``0``, the
            default, reports this Run alone: a deep delegation tree is not
            pulled into memory unless a caller asks for it. Each level costs one
            more log read per step carrying a ``child_run_id`` and per subagent
            spawned, so this bounds that cost rather than switching a feature on
            or off.

    Returns:
        The report. Totals reconcile exactly with the sum of the Records they
        are drawn from, because they are read from the same reducer state a
        live Worker uses to decide what to do next, not recomputed by hand.

    Raises:
        RunNotFound: no such Run.
        VersionNotFound: the Run's pinned Version is missing from the store. A
            report cannot reconstruct a system prompt or a failure-streak
            threshold without the Spec that produced them.
        CorruptLog: the log could not have been produced by the protocol
            (DESIGN.md §6). Propagated rather than reported around: a report
            built over a log the reducer refused would be describing a state
            that never legitimately existed.
    """
    records = await store.read(run_id)
    if not records:
        raise RunNotFound(run_id)

    state = reduce(records, run_id=run_id)

    version = await store.get_version(state.version_hash)
    if version is None:
        raise VersionNotFound(state.version_hash)
    spec = version.spec

    # Read from the log, not rebuilt from the Spec. A reconstruction cannot
    # know what the runtime added at the turn it was assembled -- a withheld
    # tool, an unreachable server, a deferred catalogue, what each connected
    # system is for -- so it showed something the model never saw while looking
    # authoritative. The last turn's prompt is the report's headline; every
    # turn's is on its own ModelCallReport.
    #
    # Falls back to the reconstruction for a Run recorded before the field
    # existed, which is the only case where a guess beats an empty box. A
    # WorkflowSpec has no system prompt of its own either way; a nested agent
    # step's prompt lives on that step's own child report.
    recorded = [r.system_prompt for r in records if isinstance(r, ModelCallStarted)]
    prompt = next(
        (text for text in reversed(recorded) if text),
        system_prompt(spec).render() if isinstance(spec, AgentSpec) else "",
    )

    admitted = next(r for r in records if isinstance(r, RunAdmitted))
    settled = next((r for r in records if isinstance(r, RunSettled)), None)
    orphaned_attempt_id: AttemptId | None = (
        settled.orphaned_attempt_id if settled is not None else None
    )

    subagents = await _subagents(store, records, state, child_depth)
    totals = _totals(state, records)

    return RunReport(
        run_id=run_id,
        scope=state.scope,
        version_hash=state.version_hash,
        spec_name=spec.name,
        system_prompt=prompt,
        steps=await _steps(store, records, child_depth),
        tool_calls=_tool_calls(records),
        model_calls=_model_calls(records),
        suspensions=_suspensions(records),
        failure_streak_trips=_failure_streak_trips(records, spec.limits.failure_streak_threshold),
        terminal_state=state.terminal_state,
        output=state.output,
        failure=state.failure,
        orphaned_attempt_id=orphaned_attempt_id,
        admitted_at=admitted.at,
        settled_at=settled.at if settled is not None else None,
        totals=totals,
        components=state.components,
        subagents=subagents,
        subtree=_subtree(totals, subagents),
        tasks=state.tasks,
        compactions=_compactions(records),
        continues_run_id=state.continues_run_id,
    )


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------


def _model_calls(records: Sequence[Record]) -> tuple[ModelCallReport, ...]:
    """Pair each ``ModelCallStarted`` with its ``ModelCallFinished`` or
    ``ModelCallFailed``, keyed by ``turn``.

    A turn number is used by exactly one model call: DESIGN.md §5's agent loop
    opens a fresh Turn for every model call, retries included, so ``turn`` is a
    safe join key rather than a coincidence this function relies on. One left
    unpaired at the end of the log is ``dangling``: the Attempt that made it
    died mid-stream, the same crash window ``RunStateView.turn_open`` reports
    on for the live Run.
    """
    turn_step_id: dict[int, StepId | None] = {}
    rows: list[dict[str, Any]] = []
    index_by_turn: dict[int, int] = {}

    for record in records:
        if isinstance(record, TurnStarted):
            turn_step_id[record.turn] = record.step_id

        elif isinstance(record, ModelCallStarted):
            index_by_turn[record.turn] = len(rows)
            rows.append(
                {
                    "turn": record.turn,
                    "step_id": turn_step_id.get(record.turn),
                    "model": record.model,
                    "system_prompt": record.system_prompt,
                    "tool_names": record.tool_names,
                    "usage": None,
                    "cost": None,
                    "timings": None,
                    "finish_reason": None,
                    "text": "",
                    "tool_call_ids": (),
                    "started_at": record.at,
                    "finished_at": None,
                    "failure": None,
                    "will_retry": False,
                    "dangling": True,
                }
            )

        elif isinstance(record, ModelCallFinished):
            row = rows[index_by_turn[record.turn]]
            row.update(
                usage=record.usage,
                cost=record.cost,
                timings=record.timings,
                finish_reason=record.finish_reason,
                text=record.text,
                tool_call_ids=record.tool_calls,
                finished_at=record.at,
                dangling=False,
            )

        elif isinstance(record, ModelCallFailed):
            row = rows[index_by_turn[record.turn]]
            row.update(
                finished_at=record.at,
                failure=record.failure,
                will_retry=record.will_retry,
                dangling=False,
            )

    return tuple(ModelCallReport(**row) for row in rows)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _compactions(records: Sequence[Record]) -> tuple[CompactionReport, ...]:
    """One row per compaction, read straight off the Record.

    Nothing is paired and nothing is joined: a compaction is a single record
    that already carries the range it replaced, the summary that replaced it
    and what writing that summary cost.
    """
    return tuple(
        CompactionReport(
            seq=record.seq,
            reason=record.reason,
            replaced_from_seq=record.replaced_from_seq,
            replaced_to_seq=record.replaced_to_seq,
            summary=record.summary,
            model=record.model,
            usage=record.usage,
            cost=record.cost,
            at=record.at,
        )
        for record in records
        if isinstance(record, CompactionApplied)
    )


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def _tool_calls(records: Sequence[Record]) -> tuple[ToolCallReport, ...]:
    """Pair each ``ToolCallStarted`` with its ``ToolCallFinished``, keyed by
    ``call_id``. One left unpaired is dangling, the crash window
    ``RunStateView.has_dangling_tool_calls`` reports on for the live Run."""
    rows: list[dict[str, Any]] = []
    index_by_call: dict[ToolCallId, int] = {}

    for record in records:
        if isinstance(record, ToolCallStarted):
            index_by_call[record.call_id] = len(rows)
            rows.append(
                {
                    "call_id": record.call_id,
                    "tool": record.tool,
                    "turn": record.turn,
                    "step_id": record.step_id,
                    "arguments": record.arguments,
                    "outcome": None,
                    "result": None,
                    "failure": None,
                    "duration_seconds": None,
                    "result_handle": None,
                    "preview": None,
                    "result_bytes": 0,
                    "started_at": record.at,
                    "finished_at": None,
                    "interruptible": record.interruptible,
                    "safe_to_retry": record.safe_to_retry,
                }
            )

        elif isinstance(record, ToolCallFinished):
            row = rows[index_by_call[record.call_id]]
            row.update(
                outcome=record.outcome,
                result=record.result,
                failure=record.failure,
                duration_seconds=record.duration_seconds,
                result_handle=record.result_handle,
                preview=record.preview,
                result_bytes=record.result_bytes,
                finished_at=record.at,
            )

    return tuple(ToolCallReport(**row) for row in rows)


# ---------------------------------------------------------------------------
# Workflow steps
# ---------------------------------------------------------------------------


async def _steps(
    store: Store, records: Sequence[Record], child_depth: int
) -> tuple[StepReport, ...]:
    """Pair each ``StepStarted`` with its ``StepCompleted``, keyed by
    ``step_id``, one row per attempt in the order each attempt started.

    A retried step reuses its ``step_id``: the reducer only allows a second
    ``StepStarted`` for one after the first has completed, so popping the
    mapping on completion and re-adding it on the next start keeps each attempt
    as its own row instead of the later one overwriting the earlier.
    """
    rows: list[dict[str, Any]] = []
    index_by_step: dict[StepId, int] = {}

    for record in records:
        if isinstance(record, StepStarted):
            index_by_step[record.step_id] = len(rows)
            rows.append(
                {
                    "step_id": record.step_id,
                    "name": record.name,
                    "kind": record.kind,
                    "attempt_number": record.attempt_number,
                    "input": record.input,
                    "completed": False,
                    "output": None,
                    "failure": None,
                    "child_run_id": None,
                    "child": None,
                }
            )

        elif isinstance(record, StepCompleted):
            row = rows[index_by_step.pop(record.step_id)]
            row.update(
                completed=True,
                output=record.output,
                failure=record.failure,
                child_run_id=record.child_run_id,
            )

    if child_depth > 0:
        for row in rows:
            child_run_id = row["child_run_id"]
            if child_run_id is not None:
                row["child"] = await build_report(store, child_run_id, child_depth=child_depth - 1)

    return tuple(StepReport(**row) for row in rows)


# ---------------------------------------------------------------------------
# Composed subagents
# ---------------------------------------------------------------------------


async def _subagents(
    store: Store, records: Sequence[Record], state: RunStateView, child_depth: int
) -> tuple[SubagentReport, ...]:
    """Every composed subagent, in spawn order, with its branch rolled up.

    Built from the parent's own log rather than by asking the store which Runs
    name this one as a parent. The log is the authority on what this Run
    spawned, it is already read, and a query by parent would find a child whose
    spawn record was lost -- which is a corruption worth noticing rather than
    papering over.
    """
    spawned = {
        record.child_run_id: record for record in records if isinstance(record, SubagentSpawned)
    }
    finished_at = {
        record.child_run_id: record.at for record in records if isinstance(record, SubagentFinished)
    }

    rows: list[SubagentReport] = []
    for run_id, child in state.children.items():
        spawn = spawned.get(run_id)
        if spawn is None:  # pragma: no cover - the reducer refuses this log
            continue
        report = (
            await build_report(store, run_id, child_depth=child_depth - 1)
            if child_depth > 0
            else None
        )
        rows.append(
            SubagentReport(
                name=child.name,
                child_run_id=run_id,
                call_id=child.call_id,
                child_version_hash=child.version_hash,
                purpose=spawn.purpose,
                task=spawn.task,
                deliverable=spawn.deliverable,
                tools=child.tools,
                model=child.model,
                delegation_depth=child.depth,
                background=child.background,
                spawned_at=spawn.at,
                finished_at=finished_at.get(run_id),
                terminal_state=child.terminal_state,
                output=child.output,
                failure=child.failure,
                messages_sent=child.messages_sent,
                usage=child.usage,
                cost=child.cost,
                unpriced_model_calls=child.unpriced_model_calls,
                report=report,
                subtree=_child_subtree(child, report),
            )
        )
    return tuple(rows)


def _child_subtree(child: ChildRun, report: RunReport | None) -> SubtreeTotals:
    """One child's branch, added up.

    Two sources, and which one is used matters. When the child's own report was
    walked, its numbers and its own children's are used, because they are read
    from the child's log and are current. When it was not, the figures the
    child's ``subagent_finished`` record carried are used instead -- correct for
    the child itself, and knowingly missing its descendants, which is what
    ``complete`` says out loud.
    """
    if report is not None:
        return report.subtree if report.subtree is not None else _own_subtree(report)
    return SubtreeTotals(
        usage=child.usage,
        cost=child.cost,
        unpriced_model_calls=child.unpriced_model_calls,
        cost_is_incomplete=child.unpriced_model_calls > 0,
        runs=1,
        # A child still running has not reported its totals, and one that has
        # may itself have had children this walk never reached.
        complete=not child.alive,
    )


def _own_subtree(report: RunReport) -> SubtreeTotals:
    """A leaf's subtree: itself.

    ``complete`` follows whether that Run has settled. A total for something
    still working is a total that will change, and saying so is the difference
    between a number and a number somebody quotes.
    """
    return SubtreeTotals(
        usage=report.totals.usage,
        cost=report.totals.cost,
        unpriced_model_calls=report.totals.unpriced_model_calls,
        cost_is_incomplete=report.totals.cost_is_incomplete,
        runs=1,
        complete=report.terminal_state is not None,
    )


def _subtree(totals: TotalsReport, subagents: Sequence[SubagentReport]) -> SubtreeTotals | None:
    """This Run plus every child's branch. ``None`` when it spawned none.

    Summed with ``Usage.__add__`` and ``Cost.__add__`` rather than by hand, so a
    rollup obeys the same rules a single Run's totals do: cache reads stay
    disjoint from input, an unpriced call stays out of the sum rather than
    entering it as zero, and two currencies raise instead of quietly adding
    (DESIGN.md §13.2).
    """
    if not subagents:
        return None

    usage = totals.usage
    cost = totals.cost
    unpriced = totals.unpriced_model_calls
    runs = 1
    complete = True

    for child in subagents:
        branch = child.subtree
        if branch is None:  # pragma: no cover - every row is built with one
            continue
        usage = usage + branch.usage
        cost = branch.cost if cost is None else cost if branch.cost is None else cost + branch.cost
        unpriced += branch.unpriced_model_calls
        runs += branch.runs
        complete = complete and branch.complete

    return SubtreeTotals(
        usage=usage,
        cost=cost,
        unpriced_model_calls=unpriced,
        cost_is_incomplete=unpriced > 0,
        runs=runs,
        complete=complete,
    )


# ---------------------------------------------------------------------------
# Suspensions
# ---------------------------------------------------------------------------


def _suspensions(records: Sequence[Record]) -> tuple[SuspensionReport, ...]:
    """Pair each ``Suspended`` with its ``Resumed``, in order.

    The reducer refuses a second suspension while one is open and refuses a
    resume without one open (DESIGN.md §6), so at most one suspension is ever
    open at a time and a single pending index is enough to pair them; no stack
    is needed.
    """
    rows: list[dict[str, Any]] = []
    open_index: int | None = None

    for record in records:
        if isinstance(record, Suspended):
            open_index = len(rows)
            rows.append(
                {
                    "reason": record.reason,
                    "question": record.question,
                    "pending_call_id": record.pending_call_id,
                    "suspended_at": record.at,
                    "expires_at": record.expires_at,
                    "resumed_at": None,
                    "approved": None,
                    "payload": None,
                }
            )

        elif isinstance(record, Resumed) and open_index is not None:
            rows[open_index].update(
                resumed_at=record.at, approved=record.approved, payload=record.payload
            )
            open_index = None

    return tuple(SuspensionReport(**row) for row in rows)


# ---------------------------------------------------------------------------
# Failure-streak trips
# ---------------------------------------------------------------------------


def _failure_streak_trips(
    records: Sequence[Record], threshold: int
) -> tuple[FailureStreakTrip, ...]:
    """Every time one tool's consecutive-failure count first reached
    ``threshold``, in order.

    Mirrors ``psych_runtime.core.reducer._update_failure_streak`` exactly, so a trip
    reported here is a trip the live guard actually saw: a success resets the
    streak, an abort does not count as a failure (the tool did not fail, the
    Run was stopped), and only the call that tips the streak over the
    threshold is recorded, not every failure after it.
    """
    trips: list[FailureStreakTrip] = []
    streaks: dict[str, int] = {}
    call_tool: dict[ToolCallId, str] = {}

    for record in records:
        if isinstance(record, ToolCallStarted):
            call_tool[record.call_id] = record.tool
            continue
        if not isinstance(record, ToolCallFinished):
            continue

        tool = call_tool.get(record.call_id, "unknown")
        if record.outcome is ToolOutcome.OK:
            streaks.pop(tool, None)
        elif record.outcome in (ToolOutcome.ERROR, ToolOutcome.UNKNOWN):
            streak = streaks.get(tool, 0) + 1
            streaks[tool] = streak
            if streak == threshold:
                trips.append(
                    FailureStreakTrip(
                        tool=tool,
                        call_id=record.call_id,
                        streak=streak,
                        threshold=threshold,
                        at=record.at,
                    )
                )
        # ToolOutcome.ABORTED: no change, matching the reducer.

    return tuple(trips)


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def _totals(state: RunStateView, records: Sequence[Record]) -> TotalsReport:
    """Token, cost and latency totals.

    Usage and cost come straight off ``RunStateView``: the reducer already
    sums usage with ``Usage.__add__`` (keeping cache reads disjoint from input
    and cache_write_1h a subset of cache_write rather than a second addend) and
    already keeps an unpriced call out of the cost sum rather than folding it
    in as zero (DESIGN.md §13.2). Recomputing either by hand here would risk
    disagreeing with the state a live Worker used to decide what to do next.
    """
    wall_clock = (records[-1].at - records[0].at).total_seconds() if records else 0.0

    model_seconds = sum(
        (
            record.timings.queue_wait_seconds + record.timings.stream_duration_seconds
            for record in records
            if isinstance(record, ModelCallFinished)
        ),
        start=0.0,
    )
    tool_seconds = sum(
        (record.duration_seconds for record in records if isinstance(record, ToolCallFinished)),
        start=0.0,
    )
    # Summed the same way as a turn's call and reported separately, for the
    # reason the call count is: this is time spent fitting the conversation
    # into the window rather than doing the work. Before it was measured it
    # fell into the gap below, where it read as time nobody could explain.
    compaction_seconds = sum(
        (
            record.timings.queue_wait_seconds + record.timings.stream_duration_seconds
            for record in records
            if isinstance(record, CompactionApplied)
        ),
        start=0.0,
    )

    latency = LatencyReport(
        wall_clock_seconds=wall_clock,
        model_seconds=model_seconds,
        tool_seconds=tool_seconds,
        compaction_seconds=compaction_seconds,
        unaccounted_seconds=wall_clock - model_seconds - tool_seconds - compaction_seconds,
    )

    return TotalsReport(
        usage=state.usage,
        cost=state.cost,
        unpriced_model_calls=state.unpriced_model_calls,
        cost_is_incomplete=state.unpriced_model_calls > 0,
        provider_reported_costs=state.provider_reported_costs,
        model_calls=state.model_calls,
        failed_model_calls=state.failed_model_calls,
        tool_calls=len(state.tool_results),
        latency=latency,
        compaction_calls=state.compaction_calls,
    )
