"""The Store port.

DESIGN.md §7. Deliberately thin, so the same semantics land on a relational
database and on a key-value store: conditional insert, range read, head
sequence, version put and get, conditional run creation, and lease claim, renew
and release.

**No transactions. No joins. No ``SELECT ... FOR UPDATE``. Conditional writes
only.** That constraint is what makes DynamoDB a first-class target rather than
a compromise, and it is why every operation below can be expressed as a single
conditional statement against one item or one row.

## The queue is the store

There is no separate broker at v1. A runnable Run is one whose lease is unheld
or expired; ``claim()`` finds one and takes the lease with a conditional write.
This works identically on all four adapters and needs no infrastructure.

A ``Queue`` port exists for consumers at scale who want Redis or SQS, but
nothing in Psych requires it and the default path has no broker in it.

## The contract suite is the deliverable

Four implementations with no shared test is four divergent behaviours discovered
in production. Every adapter passes ``psych_runtime.store.contract``, and an adapter is
added by registering a fixture rather than by writing new tests.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.ids import RunId, VersionHash, WorkerId
from psych_runtime.core.records import Record
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version

__all__ = ["RunHeader", "RunState", "Store"]


class RunState(StrEnum):
    """Where a Run is, from the store's point of view.

    Deliberately coarser than the terminal states in ``psych_runtime.core.records``: the
    store needs to know what is claimable, and the log holds the detail.
    """

    RUNNABLE = "runnable"
    """Claimable now. Either never claimed, or its lease expired."""
    RUNNING = "running"
    """A Worker holds a live lease. Still claimable once that lease expires,
    which is the whole crash-recovery path."""
    SUSPENDED = "suspended"
    """Waiting on an approval, a question, an external event or a time. Not
    claimable until resumed or until its suspension expires."""
    NESTED = "nested"
    """A subagent's Run, executed inline by the Attempt that spawned it and
    never claimable on its own (DESIGN.md §17).

    Without this state a child Run was admitted RUNNABLE while its parent was
    already executing it, so the parent's own Worker could claim it a poll
    later and two writers would race one log. The parent settles it with
    ``Store.settle_inline`` when the child finishes -- not ``release``, which
    matches on a lease this Run never had -- and a parent that dies leaves it
    here rather than running twice: the parent's reclaim re-executes the
    delegation and mints a new child, which is the same at-least-once story
    every other step has."""
    SETTLED = "settled"
    """Terminal. Never claimable again."""


class RunHeader(BaseModel):
    """The mutable row beside a Run's immutable log.

    Everything that changes about a Run lives here; everything that happened
    lives in the log. Keeping the two apart is what lets the log stay
    append-only while a lease is renewed every few seconds.

    Attributes:
        run_id: the Run.
        scope: tenancy. Every store query filters by it (DESIGN.md §14).
        version_hash: the Version this Run pinned at admission and reads for its
            whole life.
        state: claimability.
        lease_holder: the Worker holding the lease, if any.
        lease_expires_at: when that lease stops being honoured. A Run whose lease
            is past this is claimable by anyone, and that is the only mechanism
            by which a crashed Worker's Run recovers.
        deadline_at: when the supervisor should fire the abort signal.
        idempotency_key: what makes admission exactly-once per key.
        attempt_count: how many times this Run has been claimed. Bounds crash
            reclaim so a Run that kills every Worker it touches eventually stops.
        runnable_at: not claimable before this. Used by a Run waiting on a clock,
            which suspends as ``EXTERNAL``.
        continues_run_id: the immediate predecessor Run in a conversation
            thread, mirrored from ``RunAdmitted.continues_run_id`` so
            a caller can check a continuation's Scope with one ``get_run``
            rather than reading the whole log. Distinct from
            ``parent_run_id``, which is delegation -- see that field's own
            docstring in ``psych_runtime.core.records``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    scope: Scope
    version_hash: VersionHash
    state: RunState
    created_at: datetime
    deadline_at: datetime
    lease_holder: WorkerId | None = None
    lease_expires_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)
    attempt_count: int = Field(default=0, ge=0)
    runnable_at: datetime | None = None
    parent_run_id: RunId | None = None
    delegation_depth: int = Field(default=0, ge=0)
    continues_run_id: RunId | None = None


@runtime_checkable
class Store(Protocol):
    """Persistence, as narrow as an append-only log can be.

    Every method here is expressible as one conditional write or one bounded
    read. An implementation that needs a transaction to satisfy this contract has
    misread it.
    """

    # -- the log ------------------------------------------------------------

    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        """Insert ``record`` at ``seq``, asserting ``seq`` does not exist.

        Raises:
            SeqConflict: ``seq`` is already present. This means a second writer
                exists, so the caller aborts its Attempt rather than retrying at
                the next sequence, which would interleave two Attempts into one
                log.
        """
        ...

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        """Records with ``seq > after``, ascending, at most ``limit`` of them.

        This is the whole of the streaming implementation (DESIGN.md §12): a
        client subscribes with "everything after N", gets the backlog from here,
        then tails. An adapter that paginates internally must not let a page
        boundary drop or duplicate a record, which is the single most valuable
        thing the contract suite checks.
        """
        ...

    async def head(self, run_id: RunId) -> int:
        """The highest sequence written, or 0 for a Run with no records."""
        ...

    # -- versions -----------------------------------------------------------

    async def put_version(self, version: Version) -> None:
        """Store a Version. Idempotent: the hash is the identity, so writing the
        same Version twice is a no-op rather than a conflict."""
        ...

    async def get_version(self, version_hash: VersionHash) -> Version | None: ...

    # -- runs ---------------------------------------------------------------

    async def create_run(self, header: RunHeader) -> RunHeader:
        """Admit a Run, conditionally on its id.

        Idempotent by ``idempotency_key``: creating a Run with a key that already
        exists returns the existing header rather than a second Run. Delivery is
        at-least-once and exactly-once does not exist; pretending otherwise
        produces double refunds (DESIGN.md §8.3).

        Returns:
            The stored header. The existing one when the key was already used,
            which the caller detects by comparing ``run_id``.
        """
        ...

    async def get_run(self, run_id: RunId) -> RunHeader | None: ...

    async def claim(self, worker_id: WorkerId, now: datetime, lease_seconds: float) -> RunId | None:
        """Take the lease on one runnable Run, conditionally.

        A Run is claimable when it is ``RUNNABLE``, or ``RUNNING`` with a lease
        expired at ``now``, and its ``runnable_at`` has passed. Under contention
        exactly one of N callers wins; the losers get ``None`` or a different Run.

        Returns:
            The claimed Run, or ``None`` when nothing is runnable.
        """
        ...

    async def renew(
        self, run_id: RunId, worker_id: WorkerId, now: datetime, lease_seconds: float
    ) -> bool:
        """Extend a lease this Worker holds.

        Returns:
            ``False`` when this Worker no longer holds the lease, meaning another
            Worker reclaimed it after expiry. The caller stops writing
            immediately: anything it appends from here is a second writer.

        Note:
            Renewal is driven by observable progress rather than by the process
            being alive. The failure mode is worth naming: a Worker that renews
            on a bare heartbeat keeps a hung Attempt's lease alive forever, the
            expired-lease branch becomes unreachable, and the Run never
            recovers. Enforcing that is the caller's job, but it is written here
            because this is the method that gets misused.
        """
        ...

    async def release(self, run_id: RunId, worker_id: WorkerId, state: RunState) -> None:
        """Drop the lease and set the Run's state.

        Idempotent, and tolerant of a Worker that no longer holds the lease: by
        the time a slow Attempt unwinds, another Worker may already own the Run,
        and turning that into an error would only produce noise in a log that
        already recorded the real story.
        """
        ...

    async def settle_inline(self, run_id: RunId) -> bool:
        """Settle a ``NESTED`` Run, which nobody holds a lease on.

        ``release`` cannot do this. It takes the Worker that held the lease and
        matches on it, and a ``NESTED`` Run never had one: it is executed inline
        by the Attempt that dispatched it and no Worker ever claims it. So the
        settle an inline executor issues at the end of a child matches no row,
        the header stays ``NESTED`` for good, and the store disagrees with a log
        that says the Run finished. This is the operation that agrees with it.

        Addressed by Run rather than by lease, which is what makes it usable by
        the one caller that legitimately has no lease. The state is the
        authorisation: only ``NESTED`` is touched, and a ``NESTED`` Run has no
        competing writer by construction, so there is no lease to check.

        Invariants:

        - ``NESTED`` becomes ``SETTLED``. Every other state is left exactly as
          it is, which is what stops this from resurrecting a settled Run or
          taking one a Worker is holding.
        - Idempotent. A second call changes nothing.
        - Never makes a Run claimable. There is no path from here to
          ``RUNNABLE``.
        - Never writes to the log. The log already said the Run finished; this
          brings the header into line with it.

        Returns:
            ``True`` when this call settled the Run, ``False`` when it did not
            -- already settled, never admitted, or in a state this may not
            touch.
        """
        ...

    async def set_runnable_at(self, run_id: RunId, runnable_at: datetime | None) -> None:
        """Park a suspended Run until a time, or wake it on resume.

        Passing a time parks the Run: it is not claimable before then, which is
        how a Run waiting on a clock waits. There is no ``scheduled`` suspend
        reason; such a Run suspends as ``EXTERNAL``, because from the runtime's
        side a clock and a webhook are the same thing.

        Passing ``None`` clears the parking **and moves a SUSPENDED Run back to
        RUNNABLE**. That second half is not decoration. Resume is not a lease
        operation: nobody holds the Run, so ``release`` cannot be the way it
        becomes claimable again, and an adapter that only cleared the timestamp
        would leave a resumed Run suspended forever with no error to show for it.

        A Run in any other state keeps that state. Waking a settled Run would
        resurrect it, and waking a running one would tell a lie about a Run
        another Worker is holding.
        """
        ...

    # -- supervision --------------------------------------------------------

    async def expired_leases(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        """Runs whose lease has expired and which are therefore reclaimable.

        Read-only. The supervisor uses this to see what needs attention; taking
        the work is still a conditional ``claim``.
        """
        ...

    async def overdue_deadlines(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        """Live Runs past their deadline, for the supervisor to abort."""
        ...
