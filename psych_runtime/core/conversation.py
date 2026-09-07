"""The conversation, projected from the log.

DESIGN.md §6: the conversation the model sees is a projection over the record
log, not a second thing stored beside it. Building it here, purely, means the
prompt a Worker sends after a crash is byte-identical to the one the crashed
Worker was about to send, which is what makes resume invisible to the model.

## Elision happens here, and only to the model's view

A tool result over the threshold is written to the log in full and replaced in
the model's context by a handle plus a preview (DESIGN.md §10.8). The record
already carries both, so this function does no truncating of its own: it reads
what the writer decided. Deciding again here would let the log and the
conversation disagree about what the model was shown.

## Compaction is a boundary, not a deletion

A compaction record replaces a range of the conversation with a summary. The
replaced records stay in the log and stay in the report; only what goes back to
the model changes. So this walks the whole log and skips the compacted range,
rather than the log being rewritten.

## What the model is told about a failed or interrupted call

``_result_text`` below relays ``ToolCallFinished.failure.message`` for an error
outcome rather than reformatting it, on the same principle this module already
states for elision: the writer decided what the model sees, and deciding again
here would let the log and the conversation disagree. That message is expected
to already say what happened, whether retrying is worth it and what to do
instead -- ``psych_runtime.tools.guidance`` is where the write side
(``psych_runtime.runtime``) builds it, and is not imported here because ``psych_runtime.core``
may not depend on ``psych_runtime.tools`` (DESIGN.md §21, enforced by import-linter's
layering contract).

Two outcomes are the exception, and are handled directly below rather than
through a write-time message: a call cancelled by an interrupt
(``ToolOutcome.ABORTED``) and a call whose fate is unknown after a crash
(``ToolOutcome.UNKNOWN``). Neither needs tool-specific detail to describe, and
DESIGN.md §8.4 wants one fixed wording for "unknown" regardless of which of
several write sites produced it, so this module fixes the wording once here
rather than trusting every write site to agree. ``psych_runtime.tools.guidance`` keeps
its own copy of both strings so a test can catch the two copies drifting apart;
see that module's docstring for the full reasoning.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from psych_runtime.core.messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from psych_runtime.core.records import (
    CompactionApplied,
    ModelCallFinished,
    QueueConsumed,
    QueueEnqueued,
    Record,
    RunAdmitted,
    SubagentFinished,
    ToolCallFinished,
    ToolCallStarted,
    ToolFailure,
    ToolOutcome,
)

__all__ = ["build_conversation"]


def build_conversation(records: Iterable[Record]) -> list[Message]:
    """Project a record log into the messages the model should receive.

    Excludes the system message, which ``psych_runtime.model.prompt.assemble`` owns and
    rebuilds every turn from the pinned Spec.

    Args:
        records: the Run's log, ascending by seq. Assumed already validated by
            the reducer; this function derives and does not check.

    Returns:
        Messages in order, with any compacted range replaced by its summary.
    """
    ordered = list(records)
    boundary, summaries = _compaction(ordered)

    messages: list[Message] = []
    if summaries:
        messages.append(
            UserMessage(
                content=(
                    "Summary of the earlier conversation, which has been compacted "
                    "to fit the context window:\n\n" + "\n\n".join(summaries)
                )
            )
        )

    calls: dict[str, ToolCallStarted] = {}

    for record in ordered:
        if record.seq <= boundary:
            continue

        match record:
            case RunAdmitted():
                text = _input_text(record.input)
                if text:
                    messages.append(UserMessage(content=text))

            case QueueConsumed():
                # The payload of a consumed queue entry is what the user sent
                # mid-Run. It reaches the model here, at the point it was
                # actually delivered rather than the point it arrived, which is
                # what makes a steer visible in the right place in a report.
                payload = _queue_payload(ordered, record.entry_id)
                if payload:
                    messages.append(UserMessage(content=payload))

            case ModelCallFinished():
                if record.text or record.tool_calls:
                    messages.append(
                        AssistantMessage(
                            content=record.text,
                            tool_calls=tuple(
                                ToolCall(
                                    id=call_id,
                                    name=_tool_name(ordered, call_id),
                                    arguments=_tool_arguments(ordered, call_id),
                                )
                                for call_id in record.tool_calls
                            ),
                        )
                    )

            case SubagentFinished():
                # A child finished. It reaches the model as a user-role message
                # rather than as a tool result, and that is deliberate: the
                # `spawn_subagent` call that started this child was answered
                # turns ago, with the child's id and nothing else, because the
                # spawn was the thing that succeeded. Answering it a second time
                # here would give one tool call two results, which providers
                # reject and the reducer refuses to write. A background child
                # reporting back is new information arriving mid-conversation,
                # which is exactly what a user message is.
                messages.append(UserMessage(content=_child_finished_text(record)))

            case ToolCallStarted():
                calls[record.call_id] = record

            case ToolCallFinished():
                started = calls.get(record.call_id)
                messages.append(
                    ToolResultMessage(
                        tool_call_id=record.call_id,
                        name=started.tool if started is not None else "unknown",
                        content=_result_text(record),
                        is_error=record.outcome is not ToolOutcome.OK,
                    )
                )

    return messages


def _compaction(records: Sequence[Record]) -> tuple[int, list[str]]:
    """The highest compacted sequence, and every summary in order."""
    boundary = 0
    summaries: list[str] = []
    for record in records:
        if isinstance(record, CompactionApplied):
            boundary = max(boundary, record.replaced_to_seq)
            summaries.append(record.summary)
    return boundary, summaries


def _input_text(payload: dict[str, Any]) -> str:
    """The Run's input, as text for the model.

    A ``message`` or ``input`` key is used directly, because that is what a
    consumer dispatching a chat turn will send. Anything else is serialised as
    JSON rather than dropped: an agent given structured input should see it, and
    silently ignoring a payload the caller provided is worse than showing them
    JSON.
    """
    for key in ("message", "input", "text", "prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if not payload:
        return ""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _queue_payload(records: Sequence[Record], entry_id: str) -> str:
    for record in records:
        if isinstance(record, QueueEnqueued) and record.entry_id == entry_id:
            return _input_text(record.payload)
    return ""


def _tool_name(records: Sequence[Record], call_id: str) -> str:
    for record in records:
        if isinstance(record, ToolCallStarted) and record.call_id == call_id:
            return record.tool
    # A model call recorded a tool call that was never started, which the reducer
    # allows only as a truncated prefix: the Attempt died between recording the
    # model's answer and starting the call. Naming it honestly beats inventing.
    return "unknown"


def _tool_arguments(records: Sequence[Record], call_id: str) -> dict[str, Any]:
    for record in records:
        if isinstance(record, ToolCallStarted) and record.call_id == call_id:
            return dict(record.arguments)
    return {}


_ABORTED_GUIDANCE = "This call was cancelled because the run was interrupted."
"""Byte-identical to ``psych_runtime.tools.guidance.ABORTED_GUIDANCE``. See this
module's docstring for why that is a separate copy rather than an import, and
``tests/unit/test_guidance.py`` for the test that keeps the two in sync."""

_UNKNOWN_OUTCOME_GUIDANCE = (
    "The outcome of this call is unknown: the worker running it stopped before "
    "recording a result. It may or may not have taken effect. Do not assume "
    "either way; check before retrying anything with a side effect."
)
"""Byte-identical to ``psych_runtime.tools.guidance.UNKNOWN_OUTCOME_GUIDANCE``. Used
unconditionally for ``ToolOutcome.UNKNOWN``, regardless of what either write
site (``psych_runtime.runtime.agent``'s crash recovery or ``psych_runtime.runtime.worker``'s
force-settlement) put in ``record.failure.message`` -- that is what makes this
one wording rather than two. See this module's docstring and
``tests/unit/test_guidance.py``."""


def _result_text(record: ToolCallFinished) -> str:
    """What the model sees for one tool result.

    Dispatches on ``outcome`` first, matching ``ToolOutcome`` exhaustively so a
    fifth member added to that enum without a case here is a ``mypy --strict``
    failure rather than a silently blank message.
    """
    match record.outcome:
        case ToolOutcome.ABORTED:
            return _ABORTED_GUIDANCE
        case ToolOutcome.UNKNOWN:
            return _UNKNOWN_OUTCOME_GUIDANCE
        case ToolOutcome.ERROR:
            return _failure_text(record.failure)
        case ToolOutcome.OK:
            if record.result_handle is not None:
                preview = record.preview or ""
                return (
                    f"This result is {record.result_bytes} bytes, too large to "
                    f"include in full. It is stored under handle "
                    f"{record.result_handle!r}. Call `read_tool_output` with that "
                    f"handle to read it, with an offset or a search pattern.\n\n"
                    f"First part of the result:\n{preview}"
                )
            return _stringify(record.result)


def _failure_text(failure: ToolFailure | None) -> str:
    """Relay a write-time-built guidance message, honestly.

    ``failure`` is ``None`` here only if a ``ToolCallFinished`` reaches this
    projection with ``outcome=ERROR`` and no failure attached, which no current
    write site produces (every one of them pairs an error outcome with a
    failure) and which the reducer does not itself forbid. Rather than raising
    out of a pure projection over an already-validated log, this says so
    plainly; a corrupt log that reaches this state is a bug for the writer that
    produced it; this function's job is only to never lie about it.
    """
    if failure is None:
        return "This call ended in an error, but no failure detail was recorded."
    text = failure.message
    if failure.traceback and failure.traceback_is_for_the_model:
        # Only where the writer said so. A sandboxed program's traceback is
        # the model's own to read and fix (DESIGN.md §18); a host tool's is
        # the consumer's code, and carries their paths, module names and
        # whatever the exception message embedded -- a DSN, a bucket, an
        # internal URL. Those stay in the log for an operator and out of the
        # prompt. See ``ToolFailure.traceback_is_for_the_model``.
        text = f"{text}\n\n{failure.traceback}"
    return text


def _stringify(result: Any) -> str:
    """A tool result, as text for the model.

    A structure Psych serialises itself costs nothing to render
    compactly, so this uses ``separators=(",", ":")`` rather than the default
    ``json.dumps`` spacing -- every space and newline that spacing inserts is
    otherwise billed on every turn the result stays in context, for
    formatting that carries no information. A ``str`` result is returned
    untouched by the check directly below instead: a tool chose that exact
    text -- a code snippet, a Markdown table, whitespace that is itself
    meaningful -- and rewriting it would damage the result rather than just
    its bytes. Normalising a structure Psych serialises itself is safe;
    rewriting a string the tool chose to return is not.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(
            result, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        # A result that will not serialise still has to reach the model as
        # something. repr is honest about what it is.
        return repr(result)


def _child_finished_text(record: SubagentFinished) -> str:
    """What the parent's model is told when a subagent it spawned finishes.

    Names the child the way the parent named it, because the parent addresses it
    by name in ``check_subagent`` and ``message_subagent`` and a notification
    using a different label would make the model guess which child this was. The
    run id is included as well: it is what a person reading a trace needs, and
    it costs one line.
    """
    head = f"Subagent {record.name!r} (run {record.child_run_id}) finished: {record.state.value}."
    if record.failure is not None:
        return f"{head}\n\n{record.failure.message}"
    body = _stringify(record.output) if record.output else ""
    return f"{head}\n\n{body}" if body else head
