"""The reducer.

DESIGN.md §6 and §22 name this the highest-value unit-test target in the
codebase, and the test shape matters as much as the coverage:

- **Property tests over generated logs.** Every prefix of a legal sequence must
  fold cleanly. That is the operational definition of "recoverable", and stating
  it as a property covers every truncation rather than the handful someone
  thought to write down.
- **One targeted test per corruption reason**, each mutating a valid log into an
  impossible one. A reason with no test asserting it fires is a reason that will
  quietly stop firing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.ids import RunId, StepId, ToolCallId
from psych_runtime.core.records import (
    QueueKind,
    Record,
    SuspendReason,
    TerminalState,
    ToolOutcome,
)
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def valid_run() -> list[Record]:
    """A complete, legal Run: admit, claim, one turn, one tool call, settle."""
    return (
        LogBuilder()
        .admitted()
        .attempt()
        .turn()
        .model_started()
        .model_finished()
        .tool_started("call-1", "refund")
        .tool_finished("call-1")
        .turn()
        .model_started()
        .model_finished()
        .settled()
        .records
    )


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


class TestPurity:
    def test_folding_the_same_log_twice_gives_identical_state(self) -> None:
        log = valid_run()
        assert dataclasses.asdict(reduce(log)) == dataclasses.asdict(reduce(log))

    def test_the_fold_does_not_mutate_the_records(self) -> None:
        log = valid_run()
        before = [record.model_dump_json() for record in log]
        reduce(log)
        assert [record.model_dump_json() for record in log] == before

    def test_mutating_the_returned_state_does_not_affect_a_later_fold(self) -> None:
        log = valid_run()
        first = reduce(log)
        first.failure_streaks["refund"] = 99
        assert reduce(log).failure_streaks == {}


# ---------------------------------------------------------------------------
# Recoverable prefixes
# ---------------------------------------------------------------------------


def scripts() -> dict[str, list[Record]]:
    """Legal sequences whose every prefix must fold cleanly."""
    return {
        "simple run": valid_run(),
        "crash and reclaim": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1")
            .attempt(reclaimed=True)
            .tool_finished("call-1", ToolOutcome.UNKNOWN)
            .settled()
            .records
        ),
        "abort during a tool call": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1")
            .abort()
            .tool_finished("call-1", ToolOutcome.ABORTED)
            .settled(TerminalState.ABORTED)
            .records
        ),
        "stop then immediately send another message": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .abort()
            .enqueued("entry-1", QueueKind.NEXT_RUN)
            .settled(TerminalState.ABORTED)
            .records
        ),
        "steer enqueued then consumed": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .enqueued("entry-1", QueueKind.STEER)
            .consumed("entry-1")
            .model_started()
            .model_finished()
            .settled()
            .records
        ),
        "steer enqueued then cancelled": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .enqueued("entry-1", QueueKind.STEER)
            .cancelled("entry-1")
            .model_started()
            .model_finished()
            .settled()
            .records
        ),
        "suspend for approval then resume": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1")
            .suspended(SuspendReason.APPROVAL, pending_call_id="call-1")
            .resumed(approved=True)
            .tool_finished("call-1")
            .settled()
            .records
        ),
        "workflow step retried then completed": (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1", attempt_number=1)
            .step_started("stp_1", attempt_number=2)
            .step_completed("stp_1", output={"ok": True})
            .settled()
            .records
        ),
        "compaction then another turn": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .compacted(3, 5)
            .turn()
            .model_started()
            .model_finished()
            .settled()
            .records
        ),
        "model failure then retry": (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_failed(will_retry=True)
            .turn()
            .model_started()
            .model_finished()
            .settled()
            .records
        ),
    }


class TestRecoverablePrefixes:
    """Any prefix of a sequence the protocol could produce folds cleanly.

    This is the property that defines the line between "crashed" and "corrupt",
    and it is checked at every truncation point of every script rather than at a
    few hand-picked ones.
    """

    @pytest.mark.parametrize("name", list(scripts()))
    def test_every_prefix_of_a_legal_script_folds(self, name: str) -> None:
        log = scripts()[name]
        for length in range(1, len(log) + 1):
            prefix = log[:length]
            try:
                reduce(prefix)
            except CorruptLog as err:  # pragma: no cover - only on a real failure
                pytest.fail(
                    f"{name} truncated after {length} of {len(log)} records was rejected "
                    f"as {err.reason.value}, but truncation of a legal sequence is "
                    f"always recoverable: {err.detail}"
                )

    def test_a_log_ending_mid_tool_call_reports_the_call_rather_than_repairing(self) -> None:
        log = LogBuilder().admitted().attempt().turn().tool_started("call-1", "refund").records
        state = reduce(log)
        assert state.has_dangling_tool_calls
        assert state.open_tool_calls[ToolCallId("call-1")].tool == "refund"
        assert not state.settled

    def test_a_dangling_call_records_whether_it_is_safe_to_retry(self) -> None:
        """A reclaiming Worker decides between re-executing and recording an
        unknown outcome from this, so it has to survive the fold."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("safe", "lookup", safe_to_retry=True)
            .tool_started("unsafe", "refund", safe_to_retry=False)
            .records
        )
        state = reduce(log)
        assert state.open_tool_calls[ToolCallId("safe")].safe_to_retry
        assert not state.open_tool_calls[ToolCallId("unsafe")].safe_to_retry

    def test_an_empty_log_is_a_programming_error_rather_than_an_empty_state(self) -> None:
        """Every Run has run_admitted at seq 1, so an empty log describes no Run.
        A Run that does not exist is the store's RunNotFound."""
        with pytest.raises(ValueError, match="no records and no prior state"):
            reduce([], run_id=RunId("run_x"))


# ---------------------------------------------------------------------------
# Corruption: one test per reason
# ---------------------------------------------------------------------------


def expect_corruption(log: list[Record], reason: CorruptionReason) -> CorruptLog:
    with pytest.raises(CorruptLog) as caught:
        reduce(log)
    assert caught.value.reason is reason, (
        f"expected {reason.value}, got {caught.value.reason.value}: {caught.value.detail}"
    )
    return caught.value


class TestCorruption:
    def test_non_consecutive_seq(self) -> None:
        """A gap means a write was lost, or a partial range is being folded as
        though it were whole. Reading past it would silently reduce a log that is
        missing its middle."""
        log = valid_run()
        del log[2]
        expect_corruption(log, CorruptionReason.NON_CONSECUTIVE_SEQ)

    def test_record_after_finish(self) -> None:
        builder = LogBuilder().admitted().attempt().turn().model_started().model_finished()
        builder.settled()
        log = [*builder.records, LogBuilder().admitted().records[0].model_copy(update={"seq": 7})]
        expect_corruption(log, CorruptionReason.RECORD_AFTER_FINISH)

    def test_multiple_open_operations_from_a_second_admission(self) -> None:
        log = LogBuilder().admitted().admitted().records
        expect_corruption(log, CorruptionReason.MULTIPLE_OPEN_OPERATIONS)

    def test_multiple_open_operations_from_two_open_turns(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        builder.turn()
        expect_corruption(builder.records, CorruptionReason.MULTIPLE_OPEN_OPERATIONS)

    def test_unknown_operation_when_an_attempt_precedes_admission(self) -> None:
        log = LogBuilder().attempt().records
        expect_corruption(log, CorruptionReason.UNKNOWN_OPERATION)

    def test_unknown_operation_when_a_model_call_runs_outside_a_turn(self) -> None:
        log = LogBuilder().admitted().attempt().model_started().records
        expect_corruption(log, CorruptionReason.UNKNOWN_OPERATION)

    def test_unknown_operation_when_a_run_resumes_without_suspending(self) -> None:
        log = LogBuilder().admitted().attempt().resumed().records
        expect_corruption(log, CorruptionReason.UNKNOWN_OPERATION)

    def test_non_consecutive_attempt(self) -> None:
        builder = LogBuilder().admitted()
        builder.attempt()
        skipped = builder.records[-1].model_copy(update={"attempt_number": 3})
        expect_corruption([builder.records[0], skipped], CorruptionReason.NON_CONSECUTIVE_ATTEMPT)

    def test_queue_after_abort(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .abort()
            .enqueued("entry-1", QueueKind.STEER)
            .records
        )
        error = expect_corruption(log, CorruptionReason.QUEUE_AFTER_ABORT)
        assert "Only next_run may follow an abort" in error.detail

    def test_next_run_is_exempt_from_queue_after_abort(self) -> None:
        """The exemption is the point. "Stop mid-response and immediately send
        another request" is exactly this, and it is correct (DESIGN.md §9)."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .abort()
            .enqueued("entry-1", QueueKind.NEXT_RUN)
            .records
        )
        state = reduce(log)
        assert len(state.pending_next_run) == 1
        assert state.aborted

    def test_follow_up_is_not_exempt(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .abort()
            .enqueued("entry-1", QueueKind.FOLLOW_UP)
            .records
        )
        expect_corruption(log, CorruptionReason.QUEUE_AFTER_ABORT)

    def test_invalid_queue_cancellation_with_no_enqueue(self) -> None:
        log = LogBuilder().admitted().attempt().cancelled("entry-1").records
        expect_corruption(log, CorruptionReason.INVALID_QUEUE_CANCELLATION)

    def test_invalid_queue_cancellation_after_the_entry_was_consumed(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .enqueued("entry-1")
            .consumed("entry-1")
            .cancelled("entry-1")
            .records
        )
        error = expect_corruption(log, CorruptionReason.INVALID_QUEUE_CANCELLATION)
        assert "already consumed" in error.detail

    def test_invalid_queue_cancellation_twice(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .enqueued("entry-1")
            .cancelled("entry-1")
            .cancelled("entry-1")
            .records
        )
        expect_corruption(log, CorruptionReason.INVALID_QUEUE_CANCELLATION)

    def test_inconsistent_step_completing_one_that_never_started(self) -> None:
        log = LogBuilder().admitted().attempt().step_completed("stp_ghost").records
        expect_corruption(log, CorruptionReason.INCONSISTENT_STEP)

    def test_inconsistent_step_completed_twice(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1")
            .step_completed("stp_1")
            .step_completed("stp_1")
            .records
        )
        error = expect_corruption(log, CorruptionReason.INCONSISTENT_STEP)
        assert "written once and read thereafter" in error.detail

    def test_inconsistent_step_restarted_after_completing(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1")
            .step_completed("stp_1")
            .step_started("stp_1", attempt_number=2)
            .records
        )
        expect_corruption(log, CorruptionReason.INCONSISTENT_STEP)

    def test_tool_call_mismatch_for_a_result_with_no_start(self) -> None:
        log = LogBuilder().admitted().attempt().turn().tool_finished("ghost").records
        expect_corruption(log, CorruptionReason.TOOL_CALL_MISMATCH)

    def test_tool_call_mismatch_for_a_second_result(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call-1")
            .tool_finished("call-1")
            .tool_finished("call-1")
            .records
        )
        error = expect_corruption(log, CorruptionReason.TOOL_CALL_MISMATCH)
        assert "already has a result" in error.detail

    def test_duplicate_tool_invocation(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call-1")
            .tool_started("call-1")
            .records
        )
        expect_corruption(log, CorruptionReason.DUPLICATE_TOOL_INVOCATION)

    def test_provisioned_entry_mismatch_from_a_double_enqueue(self) -> None:
        log = LogBuilder().admitted().attempt().enqueued("entry-1").enqueued("entry-1").records
        expect_corruption(log, CorruptionReason.PROVISIONED_ENTRY_MISMATCH)

    def test_provisioned_entry_mismatch_from_consuming_twice(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .enqueued("entry-1")
            .consumed("entry-1")
            .consumed("entry-1")
            .records
        )
        expect_corruption(log, CorruptionReason.PROVISIONED_ENTRY_MISMATCH)

    def test_invalid_deferred_handle(self) -> None:
        """A handle that resolved outside its own Run would be a cross-tenant
        read dressed up as a tool call."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started(
                "call-1", "read_tool_output", arguments={"handle": "res_from_another_run"}
            )
            .records
        )
        expect_corruption(log, CorruptionReason.INVALID_DEFERRED_HANDLE)

    def test_a_handle_this_run_issued_resolves(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call-1", "search")
            .tool_finished("call-1", result_handle="res_1", preview="first 200 bytes")
            .tool_started("call-2", "read_tool_output", arguments={"handle": "res_1"})
            .records
        )
        assert reduce(log).result_handles == {"res_1": "call-1"}

    def test_invalid_compaction_reason_for_a_backwards_range(self) -> None:
        log = LogBuilder().admitted().attempt().turn().compacted(5, 2).records
        expect_corruption(log, CorruptionReason.INVALID_COMPACTION_REASON)

    def test_invalid_compaction_reason_for_replacing_the_future(self) -> None:
        log = LogBuilder().admitted().attempt().turn().compacted(1, 99).records
        expect_corruption(log, CorruptionReason.INVALID_COMPACTION_REASON)

    def test_records_from_another_run_are_refused(self) -> None:
        log = [*LogBuilder("run_a").admitted().records, *LogBuilder("run_b").attempt().records]
        expect_corruption(log, CorruptionReason.UNKNOWN_OPERATION)

    def test_a_suspension_awaiting_an_unopened_call_is_refused(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .suspended(SuspendReason.APPROVAL, pending_call_id="never-started")
            .records
        )
        expect_corruption(log, CorruptionReason.TOOL_CALL_MISMATCH)

    def test_inconsistent_cost_when_two_calls_are_priced_in_different_currencies(
        self,
    ) -> None:
        """A Run whose calls cannot be summed into one number is a resolver bug,
        and folding it anyway would state a total that is not one."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(cost=Cost(amount=Decimal("0.01"), currency="USD", model="gpt-4o"))
            .turn()
            .model_started()
            .model_finished(cost=Cost(amount=Decimal("0.02"), currency="EUR", model="gpt-4o"))
            .records
        )
        with pytest.raises(CorruptLog) as caught:
            reduce(log)
        assert caught.value.reason is CorruptionReason.INCONSISTENT_COST

    def test_a_model_call_finishing_with_no_start_is_refused(self) -> None:
        log = LogBuilder().admitted().attempt().turn().model_finished().records
        with pytest.raises(CorruptLog) as caught:
            reduce(log)
        assert caught.value.reason is CorruptionReason.UNKNOWN_OPERATION

    def test_two_open_model_calls_in_one_turn_are_refused(self) -> None:
        log = LogBuilder().admitted().attempt().turn().model_started().model_started().records
        with pytest.raises(CorruptLog) as caught:
            reduce(log)
        assert caught.value.reason is CorruptionReason.MULTIPLE_OPEN_OPERATIONS

    def test_every_corruption_reason_has_a_test(self) -> None:
        """A reason with no test asserting it fires is a reason that will quietly
        stop firing. This asserts the suite above stays exhaustive."""
        source = __import__("pathlib").Path(__file__).read_text(encoding="utf-8")
        missing = [
            reason.name
            for reason in CorruptionReason
            if f"CorruptionReason.{reason.name}" not in source
        ]
        assert not missing, f"corruption reasons with no test: {missing}"


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_usage_accumulates_across_model_calls(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(usage=Usage(input=100, cache_read=50))
            .turn()
            .model_started()
            .model_finished(usage=Usage(input=10, output=20))
            .records
        )
        state = reduce(log)
        assert state.usage == Usage(input=110, output=20, cache_read=50)
        assert state.model_calls == 2

    def test_an_unpriced_call_is_counted_rather_than_added_as_zero(self) -> None:
        """A report can then say the total is incomplete instead of quietly
        understating the bill."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(cost=Cost(amount=Decimal("1.50"), model="gpt-4o"))
            .turn()
            .model_started()
            .model_finished(model="mystery", cost=None)
            .records
        )
        state = reduce(log)
        assert state.cost is not None
        assert state.cost.amount == Decimal("1.50")
        assert state.unpriced_model_calls == 1

    def test_a_failure_streak_counts_consecutive_failures_of_one_tool(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        for index in range(3):
            builder.tool_started(f"call-{index}", "flaky").tool_finished(
                f"call-{index}", ToolOutcome.ERROR
            )
        assert reduce(builder.records).failure_streaks == {"flaky": 3}

    def test_a_success_resets_the_streak(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("a", "flaky")
            .tool_finished("a", ToolOutcome.ERROR)
            .tool_started("b", "flaky")
            .tool_finished("b", ToolOutcome.OK)
        )
        assert reduce(builder.records).failure_streaks == {}

    def test_an_abort_does_not_count_as_a_tool_failure(self) -> None:
        """The tool did not fail, the Run was stopped. Counting it would push the
        guard toward tripping on the user's own interrupt."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("a", "refund")
            .abort()
            .tool_finished("a", ToolOutcome.ABORTED)
        )
        assert reduce(builder.records).failure_streaks == {}

    def test_streaks_are_per_tool(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("a", "one")
            .tool_finished("a", ToolOutcome.ERROR)
            .tool_started("b", "two")
            .tool_finished("b", ToolOutcome.ERROR)
        )
        assert reduce(builder.records).failure_streaks == {"one": 1, "two": 1}

    def test_a_streak_survives_suspend_and_resume(self) -> None:
        """It is derived from the log rather than held in a process, so this is
        free. The test exists because it stops being free the moment someone
        moves the counter into the loop."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("a", "flaky")
            .tool_finished("a", ToolOutcome.ERROR)
            .suspended(SuspendReason.EXTERNAL)
            .resumed()
            .tool_started("b", "flaky")
            .tool_finished("b", ToolOutcome.ERROR)
        )
        assert reduce(builder.records).failure_streaks == {"flaky": 2}

    def test_step_results_are_memoised_for_resume(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1", name="fetch")
            .step_completed("stp_1", output={"rows": 3})
            .records
        )
        step = reduce(log).steps[StepId("stp_1")]
        assert step.completed
        assert step.output == {"rows": 3}

    def test_the_queues_stay_separate(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .enqueued("s", QueueKind.STEER)
            .enqueued("f", QueueKind.FOLLOW_UP)
            .enqueued("n", QueueKind.NEXT_RUN)
            .records
        )
        state = reduce(log)
        assert [entry.entry_id for entry in state.pending_steer] == ["s"]
        assert [entry.entry_id for entry in state.pending_follow_up] == ["f"]
        assert [entry.entry_id for entry in state.pending_next_run] == ["n"]

    def test_consuming_an_entry_removes_it_from_pending(self) -> None:
        log = LogBuilder().admitted().attempt().enqueued("entry-1").consumed("entry-1").records
        assert reduce(log).pending_steer == []

    def test_the_abort_sequence_is_recorded_not_just_a_flag(self) -> None:
        """'After' is the actual question, so the answer has to be a number."""
        log = LogBuilder().admitted().attempt().turn().abort().records
        assert reduce(log).abort_seq == 4

    def test_a_second_abort_does_not_move_the_boundary(self) -> None:
        log = LogBuilder().admitted().attempt().abort().abort().records
        assert reduce(log).abort_seq == 3

    def test_the_delegation_depth_comes_from_admission(self) -> None:
        log = LogBuilder().admitted(delegation_depth=2, parent_run_id="run_parent").records
        state = reduce(log)
        assert state.delegation_depth == 2
        assert state.parent_run_id == "run_parent"

    def test_the_terminal_state_and_output_are_derived(self) -> None:
        log = LogBuilder().admitted().attempt().settled(output={"answer": 42}).records
        state = reduce(log)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output == {"answer": 42}
        assert not state.is_runnable

    def test_a_compaction_moves_the_boundary_without_removing_records(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .compacted(3, 5, summary="they asked about a refund")
            .records
        )
        state = reduce(log)
        assert state.compaction_boundary_seq == 5
        assert state.compaction_summaries == ["they asked about a refund"]
        assert state.head_seq == 6

    def test_a_compaction_s_own_model_call_is_metered_but_is_not_a_turn(self) -> None:
        """Writing the summary is a model call and its tokens are the
        Run's tokens, but it took no turn. Counting it in `model_calls` would
        put a call in the report that no `turn_started` explains."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(usage=Usage(input=100, output=20))
            .compacted(
                3,
                5,
                summary="they asked about a refund",
                model="cheap-model",
                usage=Usage(input=80, output=30),
                cost=Cost(amount=Decimal("0.01"), currency="USD", model="cheap-model"),
            )
            .records
        )
        state = reduce(log)
        assert state.model_calls == 1
        assert state.compaction_calls == 1
        assert state.usage == Usage(input=180, output=50)
        assert state.cost is not None
        assert state.cost.amount == Decimal("0.01")

    def test_a_summary_written_by_a_model_with_no_price_is_never_costed_at_zero(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(cost=Cost(amount=Decimal("0.02"), currency="USD", model="gpt-4o"))
            .compacted(3, 5, usage=Usage(input=80, output=30))
            .records
        )
        state = reduce(log)
        assert state.unpriced_model_calls == 1
        assert state.cost is not None
        assert state.cost.amount == Decimal("0.02")

    def test_the_last_prompt_size_is_what_the_provider_counted(self) -> None:
        """The compaction trigger reads this, so it has to be the input side of
        the last *turn's* call: cached input included, because it occupies the
        window, and the summariser's own call excluded, because it measured a
        different prompt."""
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(usage=Usage(input=100, output=20, cache_read=400, cache_write=50))
            .compacted(3, 5, usage=Usage(input=9_000, output=100))
            .records
        )
        assert reduce(log).last_prompt_tokens == 550


# ---------------------------------------------------------------------------
# Properties over generated logs
# ---------------------------------------------------------------------------

Action = Callable[[LogBuilder], LogBuilder]


def _tool_pair(index: int, outcome: ToolOutcome) -> Action:
    def action(builder: LogBuilder) -> LogBuilder:
        return builder.tool_started(f"call-{index}", "work").tool_finished(f"call-{index}", outcome)

    return action


def _turn_with_model(builder: LogBuilder) -> LogBuilder:
    return builder.turn().model_started().model_finished()


def _queue_cycle(index: int) -> Action:
    def action(builder: LogBuilder) -> LogBuilder:
        return builder.enqueued(f"entry-{index}", QueueKind.FOLLOW_UP).consumed(f"entry-{index}")

    return action


@st.composite
def legal_logs(draw: st.DrawFn) -> list[Record]:
    """Generate logs the protocol could genuinely have produced."""
    builder = LogBuilder().admitted().attempt()
    count = draw(st.integers(min_value=0, max_value=6))
    for index in range(count):
        choice = draw(st.sampled_from(["turn", "tool_ok", "tool_error", "queue", "step"]))
        match choice:
            case "turn":
                _turn_with_model(builder)
            case "tool_ok":
                _turn_with_model(builder)
                _tool_pair(index, ToolOutcome.OK)(builder)
            case "tool_error":
                _turn_with_model(builder)
                _tool_pair(index, ToolOutcome.ERROR)(builder)
            case "queue":
                _queue_cycle(index)(builder)
            case "step":
                builder.step_started(f"stp_{index}").step_completed(f"stp_{index}")
    if draw(st.booleans()):
        builder.settled()
    return builder.records


class TestProperties:
    @given(legal_logs())
    @settings(max_examples=200, deadline=None)
    def test_every_generated_log_folds_without_corruption(self, log: list[Record]) -> None:
        reduce(log)

    @given(legal_logs())
    @settings(max_examples=100, deadline=None)
    def test_every_prefix_of_every_generated_log_folds(self, log: list[Record]) -> None:
        """The recoverable-prefix property, over generated input rather than
        hand-written scripts."""
        for length in range(1, len(log) + 1):
            reduce(log[:length])

    @given(legal_logs())
    @settings(max_examples=100, deadline=None)
    def test_head_seq_always_equals_the_record_count(self, log: list[Record]) -> None:
        assert reduce(log).head_seq == len(log)

    @given(legal_logs())
    @settings(max_examples=100, deadline=None)
    def test_folding_is_deterministic(self, log: list[Record]) -> None:
        assert dataclasses.asdict(reduce(log)) == dataclasses.asdict(reduce(log))

    @given(legal_logs(), st.integers(min_value=1, max_value=40))
    @settings(max_examples=100, deadline=None)
    def test_deleting_any_record_from_the_middle_is_caught(
        self, log: list[Record], index: int
    ) -> None:
        """A lost write must never fold quietly. Deleting a record always leaves
        a sequence gap, and the gap is always noticed."""
        if len(log) < 2:
            return
        position = index % (len(log) - 1)
        mutated = [*log[:position], *log[position + 1 :]]
        with pytest.raises(CorruptLog) as caught:
            reduce(mutated)
        assert caught.value.reason is CorruptionReason.NON_CONSECUTIVE_SEQ


class TestScopeAndIdentity:
    def test_the_scope_comes_from_the_first_record(self) -> None:
        scope = Scope(tenant="beta", principal="user-9", labels={"team": "support"})
        log = LogBuilder(scope=scope).admitted().records
        assert reduce(log).scope == scope

    def test_an_explicit_run_id_that_disagrees_with_the_log_is_refused(self) -> None:
        log = LogBuilder("run_a").admitted().records
        with pytest.raises(CorruptLog) as caught:
            reduce(log, run_id=RunId("run_b"))
        assert caught.value.reason is CorruptionReason.UNKNOWN_OPERATION

    def test_folding_an_empty_list_is_a_programming_error(self) -> None:
        """Not corruption: nothing is wrong with any log. The caller asked for
        state about a Run that has no records, which cannot exist."""
        with pytest.raises(ValueError, match="no records and no prior state"):
            reduce([])


class TestIncrementalFolding:
    """A Worker folds once on claim, then appends. Re-folding the whole log per
    append would be quadratic, so continuing from prior state has to give exactly
    the same answer as folding the lot."""

    def test_folding_in_two_halves_matches_folding_the_whole_log(self) -> None:
        log = valid_run()
        for split in range(1, len(log)):
            first = reduce(log[:split])
            continued = reduce(log[split:], prior=first)
            assert dataclasses.asdict(continued) == dataclasses.asdict(reduce(log)), (
                f"splitting after {split} records changed the derived state"
            )

    def test_continuing_does_not_mutate_the_prior_state(self) -> None:
        """Folding into the caller's own state object would make reduce impure
        in the one way that matters: calling it twice would answer differently."""
        log = valid_run()
        prior = reduce(log[:6])
        snapshot = dataclasses.asdict(prior)
        reduce(log[6:], prior=prior)
        assert dataclasses.asdict(prior) == snapshot

    def test_a_slice_folded_without_prior_state_is_refused_as_a_gap(self) -> None:
        """The whole point. A partial slice folded as though it were whole would
        silently derive state from a log missing its middle."""
        log = valid_run()
        with pytest.raises(CorruptLog) as caught:
            reduce(log[4:], run_id=log[0].run_id)
        assert caught.value.reason is CorruptionReason.NON_CONSECUTIVE_SEQ

    def test_continuing_with_a_gap_is_still_refused(self) -> None:
        log = valid_run()
        prior = reduce(log[:5])
        with pytest.raises(CorruptLog) as caught:
            reduce(log[6:], prior=prior)
        assert caught.value.reason is CorruptionReason.NON_CONSECUTIVE_SEQ

    def test_continuing_with_another_runs_state_is_refused(self) -> None:
        prior = reduce(LogBuilder("run_a").admitted().records)
        with pytest.raises(ValueError, match="prior state is for run"):
            reduce(LogBuilder("run_b").attempt().records, run_id=RunId("run_b"), prior=prior)


class TestDeadline:
    def test_the_deadline_is_carried_from_admission(self) -> None:
        deadline = datetime(2026, 6, 1, tzinfo=UTC)
        log = LogBuilder().admitted(deadline_at=deadline).records
        assert reduce(log).deadline_at == deadline

    def test_a_suspension_carries_its_expiry(self) -> None:
        expires = datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=1)
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .suspended(SuspendReason.QUESTION, expires_at=expires, question="which order?")
            .records
        )
        state = reduce(log)
        assert state.suspended
        assert state.suspend_expires_at == expires
        assert state.suspend_reason is SuspendReason.QUESTION
        assert not state.is_runnable


class TestComposedSubagents:
    """The tree is reconstructable from the log alone (DESIGN.md §17).

    Nothing about a running tree lives in a process: which children exist, which
    are still alive, what each one cost and how many times its parent steered it
    are all folded from records, which is what lets a reclaiming Worker pick up a
    parent mid-flight.
    """

    def base(self) -> LogBuilder:
        return LogBuilder().admitted().attempt().turn().model_started().model_finished()

    def test_a_spawn_registers_a_live_child(self) -> None:
        state = reduce(self.base().spawned("run_child", name="alpha").records)
        child = state.children[RunId("run_child")]
        assert child.name == "alpha"
        assert child.alive
        assert [c.name for c in state.live_children] == ["alpha"]

    def test_a_finish_carries_the_childs_own_totals(self) -> None:
        log = (
            self.base()
            .spawned("run_child")
            .child_finished(
                "run_child",
                output={"text": "done"},
                usage=Usage(input=10, output=3),
                unpriced_model_calls=1,
            )
            .records
        )
        child = reduce(log).children[RunId("run_child")]
        assert not child.alive
        assert child.terminal_state is TerminalState.COMPLETED
        assert child.usage.input == 10
        assert child.unpriced_model_calls == 1
        assert child.cost is None  # never a silent zero

    def test_the_fanout_cap_counts_children_still_alive(self) -> None:
        """A background child outlives the turn that spawned it, so what a cap
        has to count is the ones still running rather than the ones started
        here."""
        log = (
            self.base()
            .spawned("run_a", name="alpha")
            .spawned("run_b", name="beta")
            .child_finished("run_a", name="alpha")
            .records
        )
        state = reduce(log)
        assert len(state.children) == 2
        assert [c.name for c in state.live_children] == ["beta"]

    def test_two_live_children_cannot_share_a_name(self) -> None:
        """The name is how the parent addresses a child in every later call."""
        log = self.base().spawned("run_a", name="alpha").spawned("run_b", name="alpha").records
        with pytest.raises(CorruptLog) as err:
            reduce(log)
        assert err.value.reason is CorruptionReason.MULTIPLE_OPEN_OPERATIONS

    def test_a_name_is_reusable_once_its_holder_has_finished(self) -> None:
        log = (
            self.base()
            .spawned("run_a", name="alpha")
            .child_finished("run_a", name="alpha")
            .spawned("run_b", name="alpha")
            .records
        )
        state = reduce(log)
        assert [c.name for c in state.live_children] == ["alpha"]

    def test_one_child_cannot_be_spawned_twice(self) -> None:
        log = self.base().spawned("run_a", name="alpha").spawned("run_a", name="beta").records
        with pytest.raises(CorruptLog, match="spawned twice"):
            reduce(log)

    def test_a_child_cannot_finish_twice(self) -> None:
        """A Run reaches exactly one terminal state, so a second notification
        means the notifier wrote without reading what was already there."""
        log = self.base().spawned("run_a").child_finished("run_a").child_finished("run_a").records
        with pytest.raises(CorruptLog, match="finished twice"):
            reduce(log)

    def test_a_notification_for_a_child_this_run_never_spawned_is_corrupt(self) -> None:
        log = self.base().child_finished("run_stranger").records
        with pytest.raises(CorruptLog) as err:
            reduce(log)
        assert err.value.reason is CorruptionReason.UNKNOWN_OPERATION

    def test_a_message_to_a_child_this_run_never_spawned_is_corrupt(self) -> None:
        log = self.base().messaged_child("run_stranger").records
        with pytest.raises(CorruptLog) as err:
            reduce(log)
        assert err.value.reason is CorruptionReason.UNKNOWN_OPERATION

    def test_a_message_that_lost_the_race_with_a_finish_folds_cleanly(self) -> None:
        """The parent checks a child is alive before messaging it, but the check
        and the write are not one operation: the child's own Worker can take the
        sequence in between. Refusing here would turn an ordinary race into a
        corrupt log for a parent that did nothing wrong."""
        log = self.base().spawned("run_a").child_finished("run_a").messaged_child("run_a").records
        child = reduce(log).children[RunId("run_a")]
        assert child.messages_sent == 1
        assert not child.alive

    def test_a_parent_can_suspend_waiting_on_its_children(self) -> None:
        log = self.base().spawned("run_a").suspended(reason=SuspendReason.CHILDREN).records
        state = reduce(log)
        assert state.suspended
        assert state.suspend_reason is SuspendReason.CHILDREN
        assert state.pending_approval_call_id is None

    def test_continuing_a_fold_gives_the_same_children(self) -> None:
        """The property the Journal rests on, over the new state too."""
        log = self.base().spawned("run_a").child_finished("run_a").records
        whole = reduce(log)
        split = reduce(log[4:], prior=reduce(log[:4]))
        assert dataclasses.asdict(whole) == dataclasses.asdict(split)


class TestHowAnAttemptBegan:
    """`resumed_since_attempt` tells a clean wake from a reclaimed lease.

    `attempt_count` cannot: a second attempt happens both when the previous
    Worker died holding the lease and when the Run suspended and something woke
    it. Reading the count alone made every resumed Run announce itself as
    recovered from a crash, which is a false positive on the one field an
    operator reads to find real crashes.
    """

    def test_a_fresh_run_has_not_resumed(self) -> None:
        state = reduce(LogBuilder().admitted().attempt().records)
        assert state.resumed_since_attempt is False

    def test_a_resume_marks_the_next_attempt_as_a_clean_wake(self) -> None:
        state = reduce(LogBuilder().admitted().attempt().suspended().resumed().records)
        assert state.resumed_since_attempt is True

    def test_the_flag_belongs_to_one_attempt_and_not_to_the_run(self) -> None:
        """A Run that suspended, resumed and later died mid-attempt is a real
        reclaim on the attempt after that, so the flag has to clear."""
        state = reduce(LogBuilder().admitted().attempt().suspended().resumed().attempt().records)
        assert state.resumed_since_attempt is False
