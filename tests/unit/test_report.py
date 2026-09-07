"""The run report's arithmetic.

DESIGN.md §13.4. Built from ``psych_runtime.testing.logs.LogBuilder`` logs stored in a
``InMemoryStore``, which is in-process dict bookkeeping rather than real IO, so
this stays a unit test of the projection's arithmetic and not a test of a
store adapter.

The three things most likely to be got wrong each get a
test that pins the exact number, not just "it ran without raising":

- token totals split by cache state must not double count (cache reads are
  disjoint from input; cache_write_1h is a subset of cache_write, not a
  sibling addend);
- a model with no known price must never be summed into the cost total as
  zero;
- the latency breakdown must expose the gap between wall-clock and the sum of
  its measured parts, not silently let the parts fail to add up.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from psych_runtime.core.records import ModelTimings, SuspendReason, TerminalState, ToolOutcome
from psych_runtime.core.spec import AgentSpec, Limits, ModelRef
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.core.version import Version, publish
from psych_runtime.report.build import build_report
from psych_runtime.report.model import RunReport
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def _spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer.",
        "model": ModelRef(model="fake-standard"),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def _store_with(spec: AgentSpec) -> tuple[InMemoryStore, Version]:
    store = InMemoryStore()
    version = publish(spec)
    await store.put_version(version)
    return store, version


async def _report(log: LogBuilder, store: InMemoryStore, **kwargs: Any) -> RunReport:
    for record in log.records:
        await store.append(log.run_id, record.seq, record)
    return await build_report(store, log.run_id, **kwargs)


class TestUsageTotalsDoNotDoubleCount:
    async def test_cache_reads_and_writes_sum_across_calls_without_double_counting(
        self,
    ) -> None:
        """Two calls, each with a different mix of cache reads and writes. The
        total must be the disjoint sum, and cache_write_1h -- a subset of
        cache_write -- must not also land in the total as if it were separate.
        """
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(
                usage=Usage(input=100, output=20, cache_write=50, cache_write_1h=20),
                cost=Cost(amount=Decimal("1.5"), model="fake-standard"),
            )
            .turn()
            .model_started()
            .model_finished(
                usage=Usage(input=200, output=40, cache_read=10, cache_write=30, cache_write_1h=30),
                cost=Cost(amount=Decimal("2.75"), model="fake-standard"),
            )
            .settled()
        )

        report = await _report(log, store)
        totals = report.totals.usage

        assert totals.input == 300
        assert totals.output == 60
        assert totals.cache_read == 10
        assert totals.cache_write == 80
        assert totals.cache_write_1h == 50
        # cache_write_1h is a subset of cache_write, never a second addend.
        assert totals.total_billable == 300 + 60 + 10 + 80

        assert report.totals.cost == Cost(amount=Decimal("4.25"), model="fake-standard")
        assert report.totals.unpriced_model_calls == 0
        assert not report.totals.cost_is_incomplete


class TestUnpricedCallsKeepTheCostHonest:
    async def test_an_unpriced_call_is_not_summed_as_zero(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(
                usage=Usage(input=100, output=20),
                cost=Cost(amount=Decimal("1.5"), model="fake-standard"),
            )
            .turn()
            .model_started(model="model-nobody-priced")
            .model_finished(
                model="model-nobody-priced", usage=Usage(input=50, output=10), cost=None
            )
            .settled()
        )

        report = await _report(log, store)

        # The priced call's cost, and only the priced call's cost.
        assert report.totals.cost == Cost(amount=Decimal("1.5"), model="fake-standard")
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete
        # Usage still totals across both calls: usage is always known, unlike cost.
        assert report.totals.usage.input == 150

    async def test_every_call_unpriced_leaves_cost_none_rather_than_zero(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started(model="model-nobody-priced")
            .model_finished(model="model-nobody-priced", cost=None)
            .settled()
        )

        report = await _report(log, store)

        assert report.totals.cost is None
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete


class TestLatencyAccountsForTheWholeWallClock:
    async def test_the_gap_between_wall_clock_and_measured_parts_is_reported_explicitly(
        self,
    ) -> None:
        """LogBuilder advances the clock by exactly one second per record, so the
        wall-clock total is predictable from the record count alone, and the
        measured parts are pinned by the timings this test passes explicitly.
        """
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()  # seq 1, at +0s
            .attempt()  # seq 2, at +1s
            .turn()  # seq 3, at +2s
            .model_started()  # seq 4, at +3s
            .model_finished(  # seq 5, at +4s
                timings=ModelTimings(queue_wait_seconds=0.5, stream_duration_seconds=2.0)
            )
            .tool_started("call-1")  # seq 6, at +5s
            .tool_finished("call-1", duration_seconds=1.5)  # seq 7, at +6s
            .turn()  # seq 8, at +7s
            .model_started()  # seq 9, at +8s
            .model_finished(  # seq 10, at +9s
                timings=ModelTimings(queue_wait_seconds=0.25, stream_duration_seconds=1.25)
            )
            .settled()  # seq 11, at +10s
        )

        report = await _report(log, store)
        latency = report.totals.latency

        assert latency.wall_clock_seconds == pytest.approx(10.0)
        assert latency.model_seconds == pytest.approx((0.5 + 2.0) + (0.25 + 1.25))
        assert latency.tool_seconds == pytest.approx(1.5)
        expected_unaccounted = 10.0 - (2.5 + 1.5) - 1.5
        assert latency.unaccounted_seconds == pytest.approx(expected_unaccounted)
        assert latency.unaccounted_seconds > 0

    async def test_the_gap_is_not_clamped_when_measured_parts_exceed_wall_clock(self) -> None:
        """An adjacent pair of records is one second apart by construction, but a
        model call's own measured duration is independent of that and can be
        made to exceed it. The gap must be reported as it is computed, negative
        included, rather than floored at zero and hiding the mismatch.
        """
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(timings=ModelTimings(stream_duration_seconds=1_000.0))
            .settled()
        )

        report = await _report(log, store)

        assert report.totals.latency.unaccounted_seconds < 0


class TestOrdering:
    async def test_tool_calls_are_ordered_by_when_each_started_not_when_it_finished(
        self,
    ) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", tool="first")
            .tool_started("call-2", tool="second")
            .tool_finished("call-2")
            .tool_finished("call-1")
            .settled()
        )

        report = await _report(log, store)

        assert [call.call_id for call in report.tool_calls] == ["call-1", "call-2"]

    async def test_steps_are_ordered_by_when_each_attempt_started(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .step_started("s1", name="first")
            .step_started("s2", name="second")
            .step_completed("s2", output={"ok": True})
            .step_completed("s1", output={"ok": True})
            .settled()
        )

        report = await _report(log, store)

        assert [step.step_id for step in report.steps] == ["s1", "s2"]


class TestSuspendAndResume:
    async def test_a_suspension_and_its_resume_are_reported_together(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", tool="refund")
            .suspended(
                reason=SuspendReason.APPROVAL,
                pending_call_id="call-1",
                question="Approve the refund?",
            )
            .resumed(approved=True, payload={"note": "approved by a manager"})
            .tool_finished("call-1", outcome=ToolOutcome.OK, result={"refunded": True})
            .settled()
        )

        report = await _report(log, store)

        assert len(report.suspensions) == 1
        suspension = report.suspensions[0]
        assert suspension.reason is SuspendReason.APPROVAL
        assert suspension.pending_call_id == "call-1"
        assert suspension.question == "Approve the refund?"
        assert suspension.resumed_at is not None
        assert suspension.approved is True
        assert suspension.payload == {"note": "approved by a manager"}
        assert report.terminal_state is TerminalState.COMPLETED


class TestAbortedRun:
    async def test_an_aborted_run_has_no_turns_and_the_right_terminal_state(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .abort(reason="user pressed stop")
            .settled(state=TerminalState.ABORTED)
        )

        report = await _report(log, store)

        assert report.terminal_state is TerminalState.ABORTED
        assert report.model_calls == ()
        assert report.tool_calls == ()


class TestForceSettledRunWithAnOrphanedCall:
    async def test_the_dangling_call_and_the_orphaned_attempt_are_both_visible(self) -> None:
        store, version = await _store_with(_spec())
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(finish_reason="tool_calls", tool_calls=("call-1",))
            .tool_started("call-1", tool="refund", safe_to_retry=False)
            .settled(state=TerminalState.FORCE_SETTLED, orphaned_attempt_id="att_1")
        )

        report = await _report(log, store)

        assert report.terminal_state is TerminalState.FORCE_SETTLED
        assert report.orphaned_attempt_id == "att_1"
        assert len(report.tool_calls) == 1
        dangling = report.tool_calls[0]
        assert dangling.outcome is None
        assert dangling.finished_at is None
        assert dangling.result is None


class TestFailureStreakTrips:
    async def test_a_trip_is_recorded_once_the_streak_crosses_the_threshold(self) -> None:
        store, version = await _store_with(_spec(limits=Limits(failure_streak_threshold=2)))
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", tool="lookup")
            .tool_finished("call-1", outcome=ToolOutcome.ERROR)
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-2", tool="lookup")
            .tool_finished("call-2", outcome=ToolOutcome.ERROR)
            .turn()
            .model_started()
            .model_finished(text="giving up on that tool")
            .settled()
        )

        report = await _report(log, store)

        assert len(report.failure_streak_trips) == 1
        trip = report.failure_streak_trips[0]
        assert trip.tool == "lookup"
        assert trip.call_id == "call-2"
        assert trip.streak == 2
        assert trip.threshold == 2

    async def test_a_success_resets_the_streak_so_a_later_run_of_failures_trips_again(
        self,
    ) -> None:
        store, version = await _store_with(_spec(limits=Limits(failure_streak_threshold=2)))
        log = (
            LogBuilder(version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", tool="lookup")
            .tool_finished("call-1", outcome=ToolOutcome.ERROR)
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-2", tool="lookup")
            .tool_finished("call-2", outcome=ToolOutcome.OK, result={"ok": True})
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-3", tool="lookup")
            .tool_finished("call-3", outcome=ToolOutcome.ERROR)
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-4", tool="lookup")
            .tool_finished("call-4", outcome=ToolOutcome.ERROR)
            .settled()
        )

        report = await _report(log, store)

        assert [trip.call_id for trip in report.failure_streak_trips] == ["call-4"]


class TestChildRuns:
    async def test_a_step_s_child_is_absent_by_default_and_present_when_asked_for(self) -> None:
        store, version = await _store_with(_spec())

        child_log = (
            LogBuilder(run_id="run_child", version_hash=version.hash)
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="child done")
            .settled(output={"text": "child done"})
        )
        for record in child_log.records:
            await store.append(child_log.run_id, record.seq, record)

        parent_log = (
            LogBuilder(run_id="run_parent", version_hash=version.hash)
            .admitted()
            .attempt()
            .step_started("s1", name="delegate", kind="subagent")
            .step_completed("s1", child_run_id="run_child")
            .settled()
        )

        default_report = await _report(parent_log, store)
        assert default_report.steps[0].child_run_id == "run_child"
        assert default_report.steps[0].child is None

        deep_report = await build_report(store, parent_log.run_id, child_depth=1)
        child_report = deep_report.steps[0].child
        assert child_report is not None
        assert child_report.run_id == "run_child"
        assert child_report.output == {"text": "child done"}
