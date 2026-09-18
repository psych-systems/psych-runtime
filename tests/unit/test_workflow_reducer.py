"""What the fold derives for a workflow: retries, state, step answers.

Every property here is one the engine leans on. A retry is the one case a
completed step may start again; the workflow state is the fold of ``set_state``
completions; and an answer to a suspended step is filed under that step with
its log position, so a step that waits twice reads the right answer each time.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.corruption import CorruptionReason
from psych_runtime.core.ids import StepId
from psych_runtime.core.records import SuspendReason, ToolFailure
from psych_runtime.core.reducer import reduce
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit

FAILURE = ToolFailure(kind="boom", message="it broke")


def _corruption(log: object) -> CorruptionReason:
    from psych_runtime.core.corruption import CorruptLog

    with pytest.raises(CorruptLog) as caught:
        reduce(log)  # type: ignore[arg-type]
    return caught.value.reason


class TestRetries:
    def test_a_completion_marked_will_retry_admits_the_next_attempt(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1", name="flaky")
            .step_completed("stp_1", failure=FAILURE, will_retry=True)
            .step_started("stp_1", name="flaky", attempt_number=2)
            .step_completed("stp_1", output={"ok": True})
            .records
        )
        step = reduce(log).steps[StepId("stp_1")]
        assert step.attempt_number == 2
        assert step.settled
        assert step.output == {"ok": True}
        assert reduce(log).step_starts == 2

    def test_a_retrying_completion_is_not_settled(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1")
            .step_completed("stp_1", failure=FAILURE, will_retry=True)
            .records
        )
        step = reduce(log).steps[StepId("stp_1")]
        assert step.completed
        assert not step.settled

    def test_a_settled_step_still_refuses_a_restart(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1")
            .step_completed("stp_1", failure=FAILURE)
            .step_started("stp_1", attempt_number=2)
            .records
        )
        assert _corruption(log) is CorruptionReason.INCONSISTENT_STEP

    def test_will_retry_needs_a_failure(self) -> None:
        with pytest.raises(ValueError, match="will_retry"):
            LogBuilder().admitted().attempt().step_started("stp_1").step_completed(
                "stp_1", will_retry=True
            )

    def test_a_skipped_step_carries_nothing(self) -> None:
        with pytest.raises(ValueError, match="skipped"):
            LogBuilder().admitted().attempt().step_started("stp_1").step_completed(
                "stp_1", skipped=True, output={"x": 1}
            )
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1")
            .step_completed("stp_1", skipped=True)
            .records
        )
        assert reduce(log).steps[StepId("stp_1")].skipped


class TestWorkflowState:
    def test_set_state_completions_fold_in_order(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1", kind="set_state")
            .step_completed("stp_1", output={"a": 1, "b": 1})
            .step_started("stp_2", kind="set_state")
            .step_completed("stp_2", output={"b": 2})
            .step_started("stp_3", kind="tool")
            .step_completed("stp_3", output={"b": 99})
            .records
        )
        assert reduce(log).workflow_state_updates == {"a": 1, "b": 2}

    def test_a_failed_or_skipped_set_state_writes_nothing(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_1", kind="set_state")
            .step_completed("stp_1", failure=FAILURE)
            .step_started("stp_2", kind="set_state")
            .step_completed("stp_2", skipped=True)
            .records
        )
        assert reduce(log).workflow_state_updates == {}


class TestStepAnswers:
    def test_a_resume_is_filed_under_the_suspended_step_with_its_position(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_w", kind="wait")
            .suspended(SuspendReason.EXTERNAL, step_id=StepId("stp_w"), event="paid")
            .resumed(payload={"amount": 5})
            .records
        )
        state = reduce(log)
        (answer,) = state.step_resumes[StepId("stp_w")]
        assert answer.reason is SuspendReason.EXTERNAL
        assert answer.payload == {"amount": 5}
        assert answer.seq == 5
        assert not state.suspended
        assert state.suspended_step_id is None

    def test_the_suspension_names_its_step_event_and_wake_time(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .step_started("stp_z", kind="sleep")
            .suspended(SuspendReason.TIMER, step_id=StepId("stp_z"))
        )
        state = reduce(builder.records)
        assert state.suspended_step_id == StepId("stp_z")
        assert state.suspend_reason is SuspendReason.TIMER

    def test_two_waits_on_one_step_keep_two_answers(self) -> None:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .suspended(SuspendReason.BREAKPOINT, step_id=StepId("stp_x"))
            .resumed(payload={"step_mode": False})
            .step_started("stp_x", kind="human")
            .suspended(SuspendReason.QUESTION, step_id=StepId("stp_x"))
            .resumed(payload={"answer": "yes"})
            .records
        )
        state = reduce(log)
        reasons = [a.reason for a in state.step_resumes[StepId("stp_x")]]
        assert reasons == [SuspendReason.BREAKPOINT, SuspendReason.QUESTION]
        assert state.step_mode is False

    def test_a_breakpoint_resume_may_turn_step_mode_on(self) -> None:
        log = (
            LogBuilder()
            .admitted(step_mode=False, breakpoints=("b",))
            .attempt()
            .suspended(SuspendReason.BREAKPOINT, step_id=StepId("stp_b"))
            .resumed(payload={"step_mode": True})
            .records
        )
        state = reduce(log)
        assert state.breakpoints == ("b",)
        assert state.step_mode is True

    def test_admission_carries_the_replay_source(self) -> None:
        log = LogBuilder().admitted(replays_run_id="run_src", replay_from_step="two").records
        state = reduce(log)
        assert state.replays_run_id == "run_src"
        assert state.replay_from_step == "two"
