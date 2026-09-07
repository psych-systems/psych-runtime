"""One conversation, positioned: what a chat UI renders.

``build_conversation`` (``psych_runtime.core.conversation``) answers what the *model*
sees, which is the right answer for the prompt and the wrong one for a person
scrolling back. It returns bare ``Message``s: no sequence numbers, no
timestamps, no Run ids, because a prompt needs none of those.

A consumer rendering a chat needs all three, and the shape of that need is not
hypothetical. The example platform in this repository derived them by
re-implementing ``build_conversation``'s emission rules in a second function,
walking the same records under the same conditions to recover *which record*
produced each message, then zipping the two lists positionally and asserting
their lengths matched -- with a comment saying the assertion existed because
the two could drift. That is the shape of a projection the library should
have provided, so this is that projection.

## Why the position comes from the same walk, not a parallel one

Each message is emitted here, at the record that produced it, so there is
nothing to zip and nothing to keep in sync. ``build_conversation`` stays
exactly as it is -- the prompt path must not change to serve a UI -- and this
walks the same records with the same rules, returning what it emitted along
with where it came from.

## A thread is a chain of Runs

``psych_runtime.runtime.thread`` explains why a conversation is several Runs rather
than one long-lived one. So a view of a conversation spans Runs, and ``seq``
is unique only within one Run's log: every entry carries its ``run_id`` for
that reason, and the ordering across Runs is the chain's own order, oldest
first.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.ids import RunId, ToolCallId
from psych_runtime.core.messages import AssistantMessage, ToolResultMessage, UserMessage
from psych_runtime.core.records import (
    CompactionApplied,
    ModelCallFinished,
    QueueConsumed,
    QueueEnqueued,
    Record,
    RunAdmitted,
    ToolCallFinished,
    ToolOutcome,
)

__all__ = ["MessageView", "ThreadView", "message_views"]


class MessageView(BaseModel):
    """One message, and where in the log it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant", "tool"]
    content: str
    run_id: RunId
    seq: int
    """Position in that Run's log. Unique within one Run, never across a
    thread, which is why ``run_id`` is here too."""
    at: datetime
    tool_name: str | None = None
    """Set for a tool result, so a UI can label it without looking the call up."""
    tool_call_id: ToolCallId | None = None
    is_error: bool = False
    """A failed tool result. The content is already the failure as the model
    read it (``psych_runtime.core.conversation``), so a UI renders the same text and
    only styles it differently."""


class ThreadView(BaseModel):
    """One conversation, across every Run in its chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_ids: tuple[RunId, ...]
    """Oldest first, ending at the Run that was asked for. What a history list
    needs to collapse several Runs into one conversation."""
    messages: tuple[MessageView, ...]


def message_views(records: Iterable[Record]) -> list[MessageView]:
    """One Run's conversation, each message carrying its own position.

    The same messages ``build_conversation`` produces, in the same order, for
    the same records -- checked by a test that compares the two directly, so
    this cannot quietly start showing a person something the model never saw.
    """
    ordered = list(records)
    if not ordered:
        return []
    run_id = ordered[0].run_id
    boundary, compactions = _compaction(ordered)

    views: list[MessageView] = []
    if compactions:
        # ``build_conversation`` prepends exactly one synthetic message joining
        # every summary. It belongs to no single record, so it is dated at the
        # most recent compaction: the moment the summary a reader sees became
        # the current one.
        last = compactions[-1]
        views.append(
            MessageView(
                role="user",
                content=_compaction_text(compactions),
                run_id=run_id,
                seq=last.seq,
                at=last.at,
            )
        )

    queued = {r.entry_id: r.payload for r in ordered if isinstance(r, QueueEnqueued)}
    tool_names: dict[ToolCallId, str] = {}

    for record in ordered:
        if record.seq <= boundary:
            continue
        match record:
            case RunAdmitted():
                text = _input_text(record.input)
                if text:
                    views.append(
                        MessageView(
                            role="user", content=text, run_id=run_id, seq=record.seq, at=record.at
                        )
                    )
            case QueueConsumed():
                text = _input_text(queued.get(record.entry_id, {}))
                if text:
                    views.append(
                        MessageView(
                            role="user", content=text, run_id=run_id, seq=record.seq, at=record.at
                        )
                    )
            case ModelCallFinished():
                if record.text or record.tool_calls:
                    views.append(
                        MessageView(
                            role="assistant",
                            content=record.text,
                            run_id=run_id,
                            seq=record.seq,
                            at=record.at,
                        )
                    )
            case ToolCallFinished():
                views.append(
                    MessageView(
                        role="tool",
                        content=_tool_content(ordered, record),
                        run_id=run_id,
                        seq=record.seq,
                        at=record.at,
                        tool_name=tool_names.get(record.call_id) or _tool_name(ordered, record),
                        tool_call_id=record.call_id,
                        is_error=record.outcome is not ToolOutcome.OK,
                    )
                )
            case _:
                continue

    return views


def _compaction(records: Sequence[Record]) -> tuple[int, list[CompactionApplied]]:
    boundary = 0
    applied: list[CompactionApplied] = []
    for record in records:
        if isinstance(record, CompactionApplied):
            boundary = max(boundary, record.replaced_to_seq)
            applied.append(record)
    return boundary, applied


def _compaction_text(applied: Sequence[CompactionApplied]) -> str:
    joined = "\n\n".join(record.summary for record in applied)
    return (
        "Summary of the earlier conversation, which has been compacted "
        f"to fit the context window:\n\n{joined}"
    )


def _input_text(payload: dict[str, object]) -> str:
    """Delegates to ``build_conversation``'s own rule by calling it.

    Rather than restating "a ``message`` key wins, then ``input``, then...",
    which is exactly the duplication this module exists to remove, this builds
    the one record that rule applies to and reads back what it produced.
    """
    from psych_runtime.core.conversation import _input_text as rule  # noqa: PLC0415

    return rule(dict(payload))


def _tool_name(records: Sequence[Record], record: ToolCallFinished) -> str:
    from psych_runtime.core.conversation import _tool_name as rule  # noqa: PLC0415

    return rule(records, record.call_id)


def _tool_content(records: Sequence[Record], record: ToolCallFinished) -> str:
    """Exactly what the model was shown for this result.

    Read from ``build_conversation``'s own renderer, so a person and the model
    see the same text: the same elision for a large result, the same fixed
    wording for an aborted or unknown one, the same failure message.
    """
    from psych_runtime.core.conversation import _result_text as rule  # noqa: PLC0415

    _ = records
    return rule(record)


def _same_messages(records: Sequence[Record]) -> bool:
    """Whether this module and ``build_conversation`` agree, for a test to call.

    Not used in production. Exported for ``tests/unit/test_thread_view.py``,
    which asserts the two projections stay identical rather than trusting a
    comment that says they should.
    """
    mine = [(view.role, view.content) for view in message_views(records)]
    theirs = [(_role_of(message), message.content) for message in build_conversation(records)]
    return mine == theirs


def _role_of(message: object) -> str:
    if isinstance(message, UserMessage):
        return "user"
    if isinstance(message, AssistantMessage):
        return "assistant"
    if isinstance(message, ToolResultMessage):
        return "tool"
    raise AssertionError(f"unhandled message type: {type(message).__name__}")
