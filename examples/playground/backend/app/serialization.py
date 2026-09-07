"""One timestamp the Run header does not carry.

This module used to hold two more projections: a JSON-safe view of the
reducer's ``RunStateView``, and a chat-message list that recovered each
message's position by re-implementing ``psych_runtime.core.conversation``'s emission
rules beside it and zipping the results positionally -- with an assertion
guarding against the two drifting.

Both are gone, because the library ships them now: ``psych_runtime.status`` returns a
frozen ``RunStatus`` that serialises directly, and ``psych_runtime.core.thread_view
.message_views`` emits each message *at* the record that produced it, so
there is nothing to zip. Writing them here was the honest thing to do while
they did not exist; keeping them once they do would be a second answer to a
question that already has one.

What is left is ``settled_at``, which is not a projection of anything Psych
owns: it is one field ``RunHeader`` does not carry and ``GET /api/runs``
wants without paying for a full ``psych_runtime.report()`` per row.
"""

from __future__ import annotations

from collections.abc import Sequence

from psych_runtime.core.records import (
    Record,
    RunSettled,
)


def settled_at(records: Sequence[Record]) -> str | None:
    """The wall-clock time a Run settled, read off its ``RunSettled`` record.

    Cheaper than ``psych_runtime.report()`` for a caller that only wants this one
    timestamp: no cost computation, no system prompt reconstruction, just a
    scan of records the caller already fetched. Returns ``None`` for a Run
    that has not settled -- there is no ``RunSettled`` record to find.
    """
    for record in records:
        if isinstance(record, RunSettled):
            return record.at.isoformat()
    return None
