"""Corruption: states the protocol cannot produce.

DESIGN.md §6 rule 6. A log that could not have been produced by following the
protocol raises a typed error and the Run fails loudly. It is never repaired,
because silent repair hides the writer bug that produced it and turns a
five-minute fix into a month of confusing reports.

## The line between recoverable and corrupt

The suite states this as a property rather than a list of examples: it generates
one case per prefix length of a legal action sequence and asserts every
truncation validates cleanly. A list of examples only ever covers the
truncations someone thought of.

So the rule is: **any prefix of a sequence the protocol could legitimately
produce is recoverable. Anything requiring an illegal write to have happened is
corrupt, wherever the truncation lands.**

A log ending mid-tool-call is a crash, and the reducer hands it back with that
call open so the Worker can settle it. A log with two open operations is not a
crash; no legal caller could have written it.

## Why thirteen reasons where DESIGN.md §6 lists eleven

A reason earns its place when a writer bug can put the log in the state it names.
Two beyond the design's list clear that bar:

- ``non_consecutive_attempt`` checks that retry attempt numbers increment by one
  within a step. That is a different invariant from ``non_consecutive_seq``,
  which is gaplessness over the whole log, and neither check catches the other's
  bug. The reducer folds the entire log rather than a bounded recovery slice, so
  it is the one place both can be checked at once.
- ``invalid_compaction_reason`` fires when a step record's step and compaction
  reason disagree. Psych keeps compaction, so that contradiction is reachable.

DESIGN.md decides what Psych builds. Where it is silent about a state the
protocol can reach, the state still needs a name.
"""

from __future__ import annotations

from enum import StrEnum

from psych_runtime.core.errors import PsychError

__all__ = ["CorruptLog", "CorruptionReason"]


class CorruptionReason(StrEnum):
    """Why a log is impossible. Each member is a distinct writer bug."""

    MULTIPLE_OPEN_OPERATIONS = "multiple_open_operations"
    """Two operations are open at once on one Run. The single-writer rule makes
    this unreachable: one Attempt holds the lease, and it finishes what it starts
    before it starts another."""

    UNKNOWN_OPERATION = "unknown_operation"
    """A record refers to an operation the log never started. Either a write
    landed out of order or it belongs to a different Run."""

    RECORD_AFTER_FINISH = "record_after_finish"
    """A record follows the terminal settlement. The Run is over; nothing may be
    appended after the record that says so."""

    NON_CONSECUTIVE_SEQ = "non_consecutive_seq"
    """Sequence numbers are not gapless from 1. A gap means a write was lost or
    a reader is being handed a partial range and treating it as whole. Reading
    past it would silently reduce a log that is missing its middle."""

    NON_CONSECUTIVE_ATTEMPT = "non_consecutive_attempt"
    """Retry attempt numbers within one step do not increment by one. Distinct
    from ``non_consecutive_seq``: a log can be gapless overall and still number
    its retries wrongly, so the sequence check never sees this bug."""

    QUEUE_AFTER_ABORT = "queue_after_abort"
    """Something was enqueued for this Run after its abort was recorded.

    The exemption matters as much as the rule: ``next_run`` is exempt, because it
    is not tied to the aborting Run. "Stop mid-response and immediately send
    another request" (DESIGN.md §9) is exactly a ``next_run`` enqueue after an
    abort, and it is correct rather than corrupt. A steer or follow-up in that
    position is a race the protocol does not permit."""

    INVALID_QUEUE_CANCELLATION = "invalid_queue_cancellation"
    """A cancellation with no pending enqueue to cancel. Four ways to get here:
    no matching enqueue was ever recorded, the enqueue is not strictly earlier
    than the cancellation, the two disagree about which Run they belong to, or
    the target was already consumed."""

    INCONSISTENT_STEP = "inconsistent_step"
    """A step record contradicts the step series it belongs to: a completion for
    a step that never started, or two completions for one step id."""

    TOOL_CALL_MISMATCH = "tool_call_mismatch"
    """A tool result whose call id matches no recorded start. A model must never
    receive an assistant message with tool calls whose results are missing, and
    the inverse is just as impossible: a result from a call nobody made."""

    DUPLICATE_TOOL_INVOCATION = "duplicate_tool_invocation"
    """Two starts for one tool call id. The id addresses the call in the model's
    context, so two starts means the model cannot tell the results apart."""

    PROVISIONED_ENTRY_MISMATCH = "provisioned_entry_mismatch"
    """A record claimed a log entry that does not match what was provisioned for
    it: a different id, a different kind, or one already claimed."""

    INVALID_DEFERRED_HANDLE = "invalid_deferred_handle"
    """A handle for a large tool result or deferred tool that names no stored
    result, or names one belonging to a different Run. A handle that resolves
    across Runs would be a cross-tenant read."""

    INVALID_COMPACTION_REASON = "invalid_compaction_reason"
    """A compaction record whose stated reason and content disagree: a
    threshold compaction with no replaced range, or a replaced range with no
    reason. Not in DESIGN.md §6's list, but compaction ships, so the
    contradiction is reachable and needs a name."""

    INCONSISTENT_COST = "inconsistent_cost"
    """Two model calls in one Run were priced in different currencies. A
    ``PriceResolver`` answers for one deployment and one currency; a log whose
    costs cannot be summed was written by a resolver that changed its mind
    mid-Run, and folding it into one number would state a total that is not
    one. Not in DESIGN.md §6's list, which does not price calls in the reducer.
    Psych does, so the reducer is where the clash surfaces."""


class CorruptLog(PsychError):
    """A Run's log is in a state the protocol cannot produce.

    Raised by the reducer, never caught by the runtime to work around. The Run
    fails and the error names the reason, the sequence and the Run so the writer
    bug can be found from the log alone.

    Attributes:
        reason: which invariant broke.
        run_id: whose log it is.
        seq: the record the fold was looking at when it noticed. Not necessarily
            the record that is wrong, because the contradiction is between
            records, but it is where to start reading.
        detail: what specifically contradicts what.
    """

    def __init__(
        self,
        reason: CorruptionReason,
        run_id: str,
        seq: int | None,
        detail: str,
    ) -> None:
        self.reason = reason
        self.run_id = run_id
        self.seq = seq
        self.detail = detail
        where = f" at seq {seq}" if seq is not None else ""
        super().__init__(
            f"run {run_id} has a corrupt log{where}: {reason.value}. {detail} "
            "This log could not have been produced by following the protocol, so "
            "it is a bug in a writer. Psych fails the Run rather than repairing "
            "it, because a repair would hide that bug (DESIGN.md §6)."
        )
