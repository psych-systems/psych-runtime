"""An answer has two parts: the work, and the point.

A Run that took six turns is six blocks of model text and tool calls, in the
order they happened. The person who asked "where is order A1" has to read the
agent's whole working process to find the sentence that answers them. This
splits the two so a reader gets the answer and can open the work if they want
it.

## The split is derived, never decided

The tempting design is to ask the model: is this simple or complex, and answer
accordingly. That classification is unreliable, it costs a call, and when it
guesses wrong you get either a "hi" wrapped in ceremony or a genuinely complex
answer whose reasoning was thrown away.

It is also unnecessary, because the log already knows:

- **The answer** is the model's last message, the one it finished on with
  nothing left to call. DESIGN.md §5 makes that the loop's single success
  condition, so it is not a heuristic: it is the turn the runtime itself
  treated as the end.
- **The work** is every turn before it.

For "hi" there are no earlier turns, so the work is empty and there is nothing
to collapse. For an order lookup there are four, and they collapse behind one
line. No classification anywhere, and the answer cannot drift from what the
Run actually did.

A Run that failed, aborted or is still going has no finishing turn yet, so
``text`` is empty and every turn is work. That is the honest shape: there is
no answer, and a reader should be shown why instead (``psych_runtime.status()``'s
``failure_message``), not an empty box where a sentence should be.

## What this is not

Not a second store and not a rewrite. Every field here is read from records
the log already holds, and the flat transcript stays exactly where it was
(``psych_runtime.thread()``). A consumer who wants the whole conversation in order
still gets it; this is the other view, for the reader who wants the outcome.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from psych_runtime.core.ids import RunId, ToolCallId
from psych_runtime.core.records import (
    ModelCallFailed,
    ModelCallFinished,
    Record,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutcome,
)

__all__ = ["AnswerView", "ToolCallView", "WorkTurn", "split_answer"]


class ToolCallView(BaseModel):
    """One tool call, as a reader opening the work section sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: ToolCallId
    tool: str
    arguments: dict[str, Any]
    outcome: ToolOutcome | None = None
    """``None`` while the call is still running, or when the Attempt died
    before recording a result. Not the same as a failure, and a reader is
    told which."""
    result: Any = None
    failure_message: str | None = None
    """Already written for a person by ``psych_runtime.tools.guidance`` at the moment
    the failure was recorded. Rendered as prose; the traceback stays in the
    log unless the sandbox marked it as the model's to read."""
    duration_seconds: float | None = None
    started_at: datetime
    finished_at: datetime | None = None


class WorkTurn(BaseModel):
    """One turn of working: what the model said on the way, and what it ran."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    turn: int
    text: str = ""
    """What the model said while working. Usually empty: a turn that calls
    tools often says nothing. Kept because when it is there it is the model
    narrating its own plan, which is the most useful line in the section."""
    tool_calls: tuple[ToolCallView, ...] = ()
    failure_message: str | None = None
    """Set when the model call itself failed rather than a tool it ran."""
    at: datetime


class AnswerView(BaseModel):
    """A Run split into what it concluded and how it got there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    text: str = ""
    """The answer. Empty when the Run has not reached one: still running,
    failed, or stopped. A reader shown an empty answer needs the reason, which
    is on ``psych_runtime.status()``, not a blank space."""
    work: tuple[WorkTurn, ...] = ()
    """Every turn before the answer, oldest first. Empty for a Run that
    answered on its first turn, which is what "hi" looks like."""
    finished: bool = False
    """Whether the model reached a finishing turn. Distinguishes "the answer
    is empty because it said nothing" from "there is no answer yet"."""

    @property
    def tool_call_count(self) -> int:
        return sum(len(turn.tool_calls) for turn in self.work)

    def summary(self) -> str:
        """One line for the collapsed section, or ``""`` when there is no work.

        Deliberately counts rather than characterises. "Looked up the order"
        would be a guess about what the tools did; "2 tool calls over 3 turns"
        is what happened, and the reader opening the section is about to see
        the rest anyway.
        """
        if not self.work:
            return ""
        turns = len(self.work)
        calls = self.tool_call_count
        parts = [f"{turns} turn" + ("" if turns == 1 else "s")]
        if calls:
            parts.append(f"{calls} tool call" + ("" if calls == 1 else "s"))
        return ", ".join(parts)


def split_answer(records: Iterable[Record]) -> AnswerView:
    """Split one Run's log into its answer and the work behind it.

    Pure, like every projection in this package: same records in, same view
    out, no IO and no clock.
    """
    ordered = list(records)
    if not ordered:
        raise ValueError(
            "split_answer() was given no records. Every Run has a run_admitted "
            "record at seq 1, so an empty log describes no Run."
        )

    run_id = ordered[0].run_id
    calls = _tool_calls(ordered)
    answer_turn = _finishing_turn(ordered)

    work: list[WorkTurn] = []
    text = ""
    finished = answer_turn is not None

    for record in ordered:
        if isinstance(record, ModelCallFinished):
            if answer_turn is not None and record.turn == answer_turn:
                text = record.text
                continue
            work.append(
                WorkTurn(
                    turn=record.turn,
                    text=record.text,
                    tool_calls=tuple(
                        calls[call_id] for call_id in record.tool_calls if call_id in calls
                    ),
                    at=record.at,
                )
            )
        elif isinstance(record, ModelCallFailed):
            # A failed call is work too, and saying so is the point: a reader
            # asking why an answer took four turns is usually looking at a
            # retry.
            work.append(
                WorkTurn(
                    turn=record.turn,
                    failure_message=record.failure.message,
                    at=record.at,
                )
            )

    return AnswerView(run_id=run_id, text=text, work=tuple(work), finished=finished)


def _finishing_turn(records: Sequence[Record]) -> int | None:
    """The turn the loop treated as the end, or ``None`` if it never got there.

    The last model call that finished with no tool calls. DESIGN.md §5 names
    that the agent loop's one success condition, so this reads the runtime's
    own decision rather than guessing at one.
    """
    for record in reversed(records):
        if isinstance(record, ModelCallFinished) and not record.tool_calls:
            return record.turn
    return None


def _tool_calls(records: Sequence[Record]) -> dict[ToolCallId, ToolCallView]:
    """Every tool call, paired with its result where one was recorded."""
    started: dict[ToolCallId, ToolCallStarted] = {}
    views: dict[ToolCallId, ToolCallView] = {}

    for record in records:
        if isinstance(record, ToolCallStarted):
            started[record.call_id] = record
            views[record.call_id] = ToolCallView(
                call_id=record.call_id,
                tool=record.tool,
                arguments=dict(record.arguments),
                started_at=record.at,
            )
        elif isinstance(record, ToolCallFinished) and record.call_id in views:
            began = started[record.call_id]
            views[record.call_id] = ToolCallView(
                call_id=record.call_id,
                tool=began.tool,
                arguments=dict(began.arguments),
                outcome=record.outcome,
                result=record.result,
                failure_message=record.failure.message if record.failure is not None else None,
                duration_seconds=record.duration_seconds,
                started_at=began.at,
                finished_at=record.at,
            )
    return views


AnswerStyle = Literal["concise"]
"""How an agent is asked to shape its final answer.

One value today, and an alias rather than an enum so adding a second is not a
breaking change to anything that stores the field. ``None`` on the Spec means
Psych adds nothing at all, which is what keeps every existing Version
assembling byte-identically.
"""
