"""The guard for a model that repeats a call which *works*.

DESIGN.md §10.6's failure-streak guard counts consecutive failures, which is
the right answer to a broken tool. It is no answer at all to the other way a
Run burns its budget: a model that calls a working tool over and over, gets
the same answer every time, and never moves on.

Observed against a real provider: one tool, one question, and thirty-two calls
to ``lookup_order`` with byte-identical arguments, every one returning ``ok``.
The Run ended ``failed`` with ``budget_exhausted`` after 31,120 input tokens
for a question a single call answers. The failure-streak guard never advanced,
correctly, because nothing failed.

## What counts as degenerate

Repetition on its own is ordinary and useful. A model polls a job until it
finishes, pages through a list, and looks up ten different orders with the same
tool. Withdrawing a tool for being called twice would break all three.

What is degenerate is a repeated call that *taught the model nothing*: the same
tool, the same arguments, **and the same result**. Asking an identical question
and getting an identical answer leaves the model exactly where it was, so doing
it again is the definition of not making progress.

That third term is what keeps the legitimate cases out. Pagination changes its
arguments. Ten orders change their arguments. Polling changes its *result* the
moment the thing it waits on moves, and until then a model polling in a tight
loop with no delay is not waiting, it is spinning, and telling it so is right.

## What happens at the threshold

The repeated *call* is refused, not the tool. A model looping on
``lookup_order("A1")`` may still legitimately need ``lookup_order("B2")``, so
withholding the whole tool the way the failure-streak guard does would break
the Run to fix the loop. The narrower refusal is also why this does not go
through ``psych_runtime.tools.narrowing``: that answers which tool *names* are callable,
and this is about one argument set.
"""

from __future__ import annotations

from dataclasses import dataclass

from psych_runtime.core.reducer import call_digest

__all__ = [
    "RepetitionVerdict",
    "advisory_message",
    "assess",
    "call_digest",
]

# `call_digest` is defined in `psych_runtime.core.reducer` and re-exported here so a
# reader finds the whole guard in one place. It cannot live in this module: the
# reducer needs it to build the counts, and `psych_runtime.core` imports nothing from
# the other packages (import-linter enforces the direction). The same split
# already exists for the failure-streak guard, whose counting is in the reducer
# and whose policy is here.


@dataclass(frozen=True)
class RepetitionVerdict:
    """What to do about one repeated call."""

    digest: str
    repeats: int
    refuse: bool
    """Refuse this exact call. The tool stays available for other arguments."""
    fail_turn: bool
    """Stop the turn. The model has been told and carried on regardless."""
    advisory: str | None


def assess(
    digest: str,
    repeats: int,
    *,
    threshold: int = 3,
    hard_stop: int = 6,
) -> RepetitionVerdict:
    """Judge how many times one identical call has already been answered.

    Args:
        digest: from ``call_digest``.
        repeats: how many times this call has already completed identically,
            counting the one about to be made.
        threshold: refuse the call at or above this many.
        hard_stop: stop the turn at or above this many.

    Returns:
        The verdict. Below ``threshold`` nothing happens and nothing is said: a
        second identical call is often a model double-checking, which is
        reasonable, and a guard that fires on it would be noise.
    """
    if repeats >= hard_stop:
        return RepetitionVerdict(
            digest=digest,
            repeats=repeats,
            refuse=True,
            fail_turn=True,
            advisory=(
                f"The same call has now been made {repeats} times with the same "
                "arguments and the same result. The turn is being stopped rather "
                "than spending more of the budget repeating it."
            ),
        )
    if repeats >= threshold:
        return RepetitionVerdict(
            digest=digest,
            repeats=repeats,
            refuse=True,
            fail_turn=False,
            advisory=advisory_message(repeats),
        )
    return RepetitionVerdict(
        digest=digest, repeats=repeats, refuse=False, fail_turn=False, advisory=None
    )


def advisory_message(repeats: int) -> str:
    """What the model is told when an identical call is refused.

    Names the specific thing that is wrong, which is not "you called a tool too
    often" but "you already have this answer". A model told only to stop tends
    to try the same call with cosmetically different arguments; a model told it
    already holds the result tends to use it.
    """
    return (
        f"You have already made this exact call {repeats} times and received the "
        "same result every time, so it has not been run again. You already have "
        "this answer: use it. If it does not contain what you need, call "
        "something different or change the arguments meaningfully. If neither "
        "will help, tell the user what is missing rather than repeating this."
    )
