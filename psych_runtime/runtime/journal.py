"""The Journal: the one place an Attempt writes.

DESIGN.md §6 rules 2, 3 and 4. Every append goes through here so that sequence
allocation, the single-writer rule and the conditional write have exactly one
implementation instead of one per call site.

## Why it holds the state

The Journal folds each record into a ``RunStateView`` as it appends, using the
reducer's ``prior=`` continuation. Re-folding the whole log on every append would
be quadratic in the length of a Run, and a long Run is exactly where that hurts.

The property this rests on, pinned by a test in ``test_reducer.py``: continuing a
fold from prior state gives byte-identical state to folding the whole log. So the
in-memory state a Worker carries and the state a fresh Worker derives on reclaim
are the same state, and a crash is invisible.

## Why a failed append is fatal, and the one case it is not

An append that hits ``SeqConflict`` means someone else wrote at this sequence.
The Journal never retries at the next sequence blindly: if the other writer is
another Attempt, doing so would interleave two Attempts' records into one log,
which the reducer would then correctly refuse as corrupt, having been produced
by a bug here rather than by the writer that actually raced.

There are exactly two other writers the protocol permits while an Attempt holds
the lease: the consumer, appending the handful of records DESIGN.md §9 lets
arrive from outside (``abort_requested``, the three queue records, and
``resumed``), and a background subagent's own Attempt, appending the one record
that says it finished (``subagent_finished``, DESIGN.md §17). Neither is a second
Attempt; both are input, and they are the mechanism by which "the user pressed
stop" and "your subagent is done" reach a Run mid-tool-call. So on a
conflict the Journal reads what landed after its head, and if every record is
one of those external kinds it folds them and appends once more. Anything else
propagates, and the Attempt stops.

Without this, ``psych_runtime.interrupt()`` against a Run a Worker was executing did
not abort the Run: the Worker's next append raised, escaped every handler, and
the Run settled ``FAILED`` with "the attempt raised" rather than ``ABORTED``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from psych_runtime.core.errors import SeqConflict
from psych_runtime.core.ids import AttemptId, RunId
from psych_runtime.core.records import (
    RECORD_ADAPTER,
    AbortRequested,
    QueueCancelled,
    QueueEnqueued,
    Record,
    Resumed,
    SubagentFinished,
)
from psych_runtime.core.reducer import RunStateView, reduce
from psych_runtime.core.scope import Scope
from psych_runtime.store.port import Store

__all__ = ["EXTERNAL_RECORD_TYPES", "Journal"]

EXTERNAL_RECORD_TYPES: Final = (
    AbortRequested,
    QueueEnqueued,
    QueueCancelled,
    Resumed,
    SubagentFinished,
)
"""The records a consumer may append while an Attempt holds the lease.

Everything else is written by the Attempt itself, so a foreign record of any
other type at the Journal's head means a second Attempt is writing and this
one must stop.

``SubagentFinished`` is the one member not written by a consumer. It is written
by whichever Attempt settled a background child (``psych_runtime.runtime.notify``), and
it belongs here for exactly the same reason an interrupt does: it is input
arriving from outside this Attempt, at a moment this Attempt does not choose,
and a parent that treated it as a second writer would stop mid-turn every time
one of its own children finished.
"""


class Journal:
    """Append-side ownership of one Run's log, for one Attempt."""

    def __init__(
        self,
        store: Store,
        run_id: RunId,
        scope: Scope,
        state: RunStateView,
        attempt_id: AttemptId | None = None,
    ) -> None:
        self._store = store
        self.run_id = run_id
        self.scope = scope
        self._state = state
        self.attempt_id = attempt_id

    @classmethod
    async def open(
        cls,
        store: Store,
        run_id: RunId,
        scope: Scope,
        attempt_id: AttemptId | None = None,
    ) -> Journal:
        """Read a Run's log and fold it, ready to append.

        This is what a Worker does on claim, including on a reclaim after a
        crash. The fold is where a corrupt log is discovered, which is the right
        moment: before this Attempt writes anything on top of it.
        """
        records = await store.read(run_id)
        state = reduce(records, run_id=run_id)
        return cls(store, run_id, scope, state, attempt_id)

    @property
    def state(self) -> RunStateView:
        """The derived state, current as of the last append."""
        return self._state

    @property
    def head(self) -> int:
        return self._state.head_seq

    async def append(self, **fields: Any) -> Record:
        """Write the next record.

        Takes the record's own fields; the Journal supplies ``run_id``, ``seq``,
        ``at``, ``scope`` and ``attempt_id``. A caller cannot get the sequence
        wrong because a caller never sets it.

        Raises:
            SeqConflict: another Attempt holds this Run. Propagated, never
                retried at the next sequence. A conflict caused only by the
                consumer's own external records (an interrupt, a steer, a
                resume) is folded and the append made once more instead; see
                the module docstring.
            CorruptLog: this record would make the log impossible. Raised before
                the write, so a bug here cannot persist a contradiction.
        """
        try:
            return await self._append_once(fields)
        except SeqConflict:
            if not await self._absorb_external_records():
                raise
            return await self._append_once(fields)

    async def _append_once(self, fields: dict[str, Any]) -> Record:
        record: Record = RECORD_ADAPTER.validate_python(
            {
                "run_id": self.run_id,
                "seq": self._state.head_seq + 1,
                "at": datetime.now(UTC),
                "scope": self.scope,
                "attempt_id": self.attempt_id,
                **fields,
            }
        )

        # Fold first. A record that would corrupt the log raises here, before it
        # is written, so the log never contains the contradiction that a later
        # reader would have to refuse.
        next_state = reduce([record], prior=self._state)

        await self._store.append(self.run_id, record.seq, record)

        self._state = next_state
        return record

    async def refresh(self) -> bool:
        """Fold whatever a consumer appended since this Journal last looked.

        Cheap: one range read after the head, empty almost every time. The
        loop calls it around every tool call so an interrupt lands while the
        call is still in flight rather than at the next append. Returns
        whether anything new was folded.

        Raises:
            SeqConflict: a record of a kind only an Attempt writes has
                appeared. Another Worker owns this log now, and this one must
                stop; the caller treats it exactly as a lost lease.
        """
        return await self._absorb_external_records()

    async def _absorb_external_records(self) -> bool:
        foreign = await self._store.read(self.run_id, after=self._state.head_seq)
        if not foreign:
            return False
        for record in foreign:
            if not isinstance(record, EXTERNAL_RECORD_TYPES):
                # A second Attempt is writing. Refusing here is what keeps two
                # Attempts' records from being interleaved into one log.
                raise SeqConflict(self.run_id, record.seq)
        self._state = reduce(foreign, prior=self._state)
        return True

    async def read_all(self) -> list[Record]:
        """Every record in this Run's log, for a projection that needs them all.

        The conversation projection walks the whole log rather than the derived
        state, because a message list is an ordered thing and the state is a
        summary. Exposed here so callers do not reach past the Journal to the
        store and lose the single-writer discipline this class exists for.
        """
        return await self._store.read(self.run_id)

    async def append_all(self, records: Sequence[dict[str, Any]]) -> list[Record]:
        """Append several records in order, stopping at the first failure.

        Not a transaction, because the Store has none by design. A partial write
        leaves a valid prefix, which the reducer accepts and a reclaiming Worker
        continues from. That is the whole reason the log is designed to make
        every prefix meaningful.
        """
        written: list[Record] = []
        for fields in records:
            written.append(await self.append(**fields))
        return written
