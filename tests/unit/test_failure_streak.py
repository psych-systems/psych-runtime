"""The failure-streak guard.

DESIGN.md §10.6 and §23 item ten: three consecutive failures of one tool stop the
model repeating it.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.records import ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.testing.logs import LogBuilder
from psych_runtime.tools.failure_streak import assess, withheld_tools

pytestmark = pytest.mark.unit


class TestThresholds:
    def test_one_failure_withholds_nothing(self) -> None:
        """A tool that failed once is a tool that might work."""
        verdict = assess("flaky", 1)
        assert not verdict.withhold
        assert verdict.advisory is None

    def test_two_failures_still_withhold_nothing(self) -> None:
        assert not assess("flaky", 2).withhold

    def test_the_third_failure_withholds_the_tool(self) -> None:
        verdict = assess("flaky", 3)
        assert verdict.withhold
        assert not verdict.fail_turn
        assert verdict.advisory is not None

    def test_the_hard_stop_fails_the_turn(self) -> None:
        verdict = assess("flaky", 6)
        assert verdict.fail_turn
        assert verdict.withhold

    def test_the_thresholds_are_configurable(self) -> None:
        assert assess("flaky", 2, threshold=2).withhold
        assert not assess("flaky", 2, threshold=5).withhold


class TestAdvisory:
    def test_the_advisory_names_the_tool_and_the_count(self) -> None:
        advisory = assess("refund", 3).advisory
        assert advisory is not None
        assert "'refund'" in advisory
        assert "3 times" in advisory

    def test_the_advisory_says_what_to_do_instead(self) -> None:
        """A message that only says stop leaves the model to guess, and the usual
        guess is the same tool with slightly different arguments."""
        advisory = assess("refund", 3).advisory
        assert advisory is not None
        assert "ask how they" in advisory
        assert "Do not look for another way to call it" in advisory


class TestWithheldSet:
    def test_only_tripped_tools_are_withheld(self) -> None:
        assert withheld_tools({"a": 3, "b": 1, "c": 7}) == {"a", "c"}

    def test_an_empty_streak_map_withholds_nothing(self) -> None:
        assert withheld_tools({}) == set()


class TestAgainstTheReducer:
    """The counter and the policy have to agree, so the guard is exercised
    against streaks the reducer actually derives rather than hand-made numbers."""

    def test_three_failures_in_a_row_withhold_the_tool(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        for index in range(3):
            builder.tool_started(f"c{index}", "flaky").tool_finished(f"c{index}", ToolOutcome.ERROR)
        state = reduce(builder.records)
        assert withheld_tools(state.failure_streaks) == {"flaky"}

    def test_a_success_between_failures_keeps_the_tool_available(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        outcomes = [
            ToolOutcome.ERROR,
            ToolOutcome.ERROR,
            ToolOutcome.OK,
            ToolOutcome.ERROR,
            ToolOutcome.ERROR,
        ]
        for index, outcome in enumerate(outcomes):
            builder.tool_started(f"c{index}", "flaky").tool_finished(f"c{index}", outcome)
        state = reduce(builder.records)
        assert state.failure_streaks == {"flaky": 2}
        assert withheld_tools(state.failure_streaks) == set()

    def test_failures_spread_across_turns_still_count(self) -> None:
        """The count is per Run, not per turn. A tool failing three times across
        three turns is exactly as broken as one failing three times inside one,
        and resetting per turn lets a model launder a broken tool by taking a
        turn off."""
        builder = LogBuilder().admitted().attempt()
        for index in range(3):
            builder.turn().model_started().model_finished()
            builder.tool_started(f"c{index}", "flaky").tool_finished(f"c{index}", ToolOutcome.ERROR)
        state = reduce(builder.records)
        assert state.failure_streaks == {"flaky": 3}

    def test_an_interrupt_does_not_trip_the_guard(self) -> None:
        """The tool did not fail, the Run was stopped. Counting it would push the
        guard toward tripping on the user's own interrupt."""
        builder = LogBuilder().admitted().attempt().turn()
        for index in range(4):
            builder.tool_started(f"c{index}", "slow").tool_finished(
                f"c{index}", ToolOutcome.ABORTED
            )
        assert withheld_tools(reduce(builder.records).failure_streaks) == set()

    def test_an_unknown_outcome_after_a_crash_counts_as_a_failure(self) -> None:
        """A call the Worker died inside did not succeed, and a Run that keeps
        crashing on one tool is exactly what the guard is for."""
        builder = LogBuilder().admitted().attempt().turn()
        for index in range(3):
            builder.tool_started(f"c{index}", "crasher").tool_finished(
                f"c{index}", ToolOutcome.UNKNOWN
            )
        assert withheld_tools(reduce(builder.records).failure_streaks) == {"crasher"}

    def test_streaks_are_per_tool_not_across_tools(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        for index, tool in enumerate(["a", "b", "a", "b", "a"]):
            builder.tool_started(f"c{index}", tool).tool_finished(f"c{index}", ToolOutcome.ERROR)
        state = reduce(builder.records)
        assert state.failure_streaks == {"a": 3, "b": 2}
        assert withheld_tools(state.failure_streaks) == {"a"}

    def test_the_streak_survives_suspend_and_resume(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("a", "flaky")
            .tool_finished("a", ToolOutcome.ERROR)
            .tool_started("b", "flaky")
            .tool_finished("b", ToolOutcome.ERROR)
            .suspended()
            .resumed()
            .tool_started("c", "flaky")
            .tool_finished("c", ToolOutcome.ERROR)
        )
        assert withheld_tools(reduce(builder.records).failure_streaks) == {"flaky"}
