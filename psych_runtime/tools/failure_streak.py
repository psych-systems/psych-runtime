"""The failure-streak guard.

DESIGN.md §10.6. A counter tracks consecutive failures of the same tool within a
Run. At the threshold the loop stops handing the model that tool and tells it
what failed and why, forcing a change of approach. At a higher threshold the turn
fails rather than burning the step budget on a loop.

This is a guard against a broken tool, not against a broken model. A model
emitting empty or repeated output is a separate problem with a separate fix, and
conflating the two would have one threshold answering two questions.

## What counts, and why

**Per Run, not per turn.** DESIGN.md §10.6 says "within a Run" and "surviving
suspend and resume", so the count carries across turns. A tool failing three
times over three turns is exactly as broken as one failing three times inside
one, and resetting per turn lets a model launder a broken tool by taking a turn
off. It follows for free from the counter living in the reducer.

**Keyed by tool name only, not by tool and arguments.** Two calls with wildly
different arguments share a counter, and a success with any arguments clears it.
Per-argument bucketing sounds more precise and is worse: a model retrying a
broken tool varies its arguments each time, which is precisely the loop this
guard exists to break.

**An aborted call is not a failure.** The tool did not fail, the Run was stopped.
Counting it would push the guard toward tripping on the user's own interrupt.

**A Policy denial is not a failure either.** The consumer's authorization saying
no is not the tool misbehaving, and letting it feed the streak would mean a
correctly-denied tool eventually gets withdrawn for a reason nobody intended.

**The advisory rides in the tool result, not in a system message.** The reason is
prompt caching: appending a system message mid-run invalidates the provider's
cached prefix, while an extra field on a tool result the model was already going
to read costs nothing (DESIGN.md §19).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["StreakVerdict", "advisory_message", "assess", "withheld_tools"]


@dataclass(frozen=True, slots=True)
class StreakVerdict:
    """What the guard says about one tool right now.

    Attributes:
        tool: the tool in question.
        streak: consecutive failures, as derived from the log.
        withhold: stop offering this tool to the model. True at or above the
            advisory threshold.
        fail_turn: stop the turn entirely. True at or above the hard stop.
        advisory: text to hand the model, or None when there is nothing to say.
    """

    tool: str
    streak: int
    withhold: bool
    fail_turn: bool
    advisory: str | None


def assess(
    tool: str,
    streak: int,
    *,
    threshold: int = 3,
    hard_stop: int = 6,
) -> StreakVerdict:
    """Judge one tool's streak.

    Args:
        tool: the tool name.
        streak: consecutive failures, from ``RunStateView.failure_streaks``.
        threshold: withhold the tool at or above this many failures.
        hard_stop: fail the turn at or above this many.

    Returns:
        The verdict. A streak below ``threshold`` withholds nothing and says
        nothing, because a tool that failed once is a tool that might work.
    """
    if streak >= hard_stop:
        return StreakVerdict(
            tool=tool,
            streak=streak,
            withhold=True,
            fail_turn=True,
            advisory=(
                f"{tool!r} failed {streak} times in a row. The turn is being stopped "
                "rather than spending more of the step budget on it."
            ),
        )
    if streak >= threshold:
        return StreakVerdict(
            tool=tool,
            streak=streak,
            withhold=True,
            fail_turn=False,
            advisory=advisory_message(tool, streak),
        )
    return StreakVerdict(tool=tool, streak=streak, withhold=False, fail_turn=False, advisory=None)


def advisory_message(tool: str, streak: int) -> str:
    """What the model is told when a tool is withdrawn.

    Says what happened, that the tool is gone, and what to do instead. A message
    that only says "stop" leaves the model to guess, and the usual guess is to
    call the same tool with slightly different arguments.
    """
    return (
        f"The tool {tool!r} has now failed {streak} times in a row, so it has been "
        "removed from the tools available to you for the rest of this run. Do not "
        "look for another way to call it. Either solve the problem with the tools "
        "you still have, or explain to the user what is broken and ask how they "
        "would like to proceed."
    )


def withheld_tools(
    streaks: Mapping[str, int],
    *,
    threshold: int = 3,
    hard_stop: int = 6,
) -> set[str]:
    """Every tool the guard is currently withholding.

    Passed to ``psych_runtime.tools.narrowing.narrow`` as part of ``callable_now``, which
    is why narrowing takes that argument at all: withholding a tool is a live
    restriction rather than a change to what the Spec granted, and it must
    narrow through the same one function as everything else so the validator and
    the runtime cannot disagree.
    """
    return {
        tool
        for tool, streak in streaks.items()
        if assess(tool, streak, threshold=threshold, hard_stop=hard_stop).withhold
    }
