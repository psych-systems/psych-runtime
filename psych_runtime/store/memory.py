"""The in-memory Store adapter.

DESIGN.md §7 and §22: this exists to test other components fast. It is never
the thing that proves the Store contract, ``psych_runtime.store.contract``, does that,
against every adapter including this one.

## Why a single lock rather than per-run locking

Every method below does a fixed, small amount of dict bookkeeping between
acquiring and releasing the lock, with no ``await`` in between, so contention
on one lock never becomes a throughput problem worth trading correctness for.
A per-run lock table would need its own locking to create entries safely,
which is the kind of complexity this adapter exists to avoid. Real concurrency
guarantees (conditional writes surviving genuine contention, not just the
absence of an ``await`` point) are what the contract suite in
``psych_runtime.store.contract`` exercises against this adapter under
``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from psych_runtime.core.errors import RunNotFound, SeqConflict
from psych_runtime.core.ids import RunId, VersionHash, WorkerId
from psych_runtime.core.records import Record
from psych_runtime.core.version import Version
from psych_runtime.store.port import RunHeader, RunState

__all__ = ["InMemoryStore"]

_RECLAIMABLE_STATES = frozenset({RunState.RUNNABLE, RunState.RUNNING})


class InMemoryStore:
    """A ``Store`` backed by plain dicts, guarded by one ``asyncio.Lock``.

    Implements the ``Store`` protocol structurally; there is no base class to
    inherit because the port is a ``Protocol``.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._logs: dict[RunId, dict[int, Record]] = {}
        self._versions: dict[VersionHash, Version] = {}
        self._runs: dict[RunId, RunHeader] = {}
        self._idempotency_keys: dict[tuple[str, str], RunId] = {}
        """Keyed by ``(tenant, key)``. A key is the consumer's own string and
        collides across tenants by nature, so keying by it alone handed one
        tenant another tenant's run_id back from ``create_run``."""

    # -- the log --------------------------------------------------------

    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        async with self._lock:
            log = self._logs.setdefault(run_id, {})
            if seq in log:
                raise SeqConflict(run_id, seq)
            log[seq] = record

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        async with self._lock:
            log = self._logs.get(run_id, {})
            seqs = sorted(seq for seq in log if seq > after)
            if limit is not None:
                seqs = seqs[:limit]
            return [log[seq] for seq in seqs]

    async def head(self, run_id: RunId) -> int:
        async with self._lock:
            log = self._logs.get(run_id)
            if not log:
                return 0
            return max(log)

    # -- versions ---------------------------------------------------------

    async def put_version(self, version: Version) -> None:
        async with self._lock:
            # The hash is the identity (psych_runtime.core.version), so a second put of
            # the same Version is a no-op rather than an overwrite. Nothing else
            # ever needs to change under an existing hash.
            self._versions.setdefault(version.hash, version)

    async def get_version(self, version_hash: VersionHash) -> Version | None:
        async with self._lock:
            return self._versions.get(version_hash)

    # -- runs ---------------------------------------------------------------

    async def create_run(self, header: RunHeader) -> RunHeader:
        async with self._lock:
            if header.idempotency_key is not None:
                existing_id = self._idempotency_keys.get(
                    (header.scope.tenant, header.idempotency_key)
                )
                if existing_id is not None:
                    return self._runs[existing_id]
            existing = self._runs.get(header.run_id)
            if existing is not None:
                # A duplicate run_id never clobbers the first admission: at-least-once
                # delivery means a second create_run for a run already admitted is a
                # retry of the same message, not a request to overwrite it.
                return existing
            self._runs[header.run_id] = header
            self._logs.setdefault(header.run_id, {})
            if header.idempotency_key is not None:
                key = (header.scope.tenant, header.idempotency_key)
                self._idempotency_keys[key] = header.run_id
            return header

    async def get_run(self, run_id: RunId) -> RunHeader | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def claim(self, worker_id: WorkerId, now: datetime, lease_seconds: float) -> RunId | None:
        async with self._lock:
            for run_id, header in self._runs.items():
                if not self._is_claimable(header, now):
                    continue
                self._runs[run_id] = header.model_copy(
                    update={
                        "state": RunState.RUNNING,
                        "lease_holder": worker_id,
                        "lease_expires_at": now + timedelta(seconds=lease_seconds),
                        "attempt_count": header.attempt_count + 1,
                    }
                )
                return run_id
            return None

    @staticmethod
    def _is_claimable(header: RunHeader, now: datetime) -> bool:
        if header.state not in _RECLAIMABLE_STATES:
            return False
        if header.runnable_at is not None and header.runnable_at > now:
            return False
        if header.state == RunState.RUNNABLE:
            return True
        # RUNNING: claimable only once its lease has expired. A header with no
        # lease_expires_at should not occur (claim() always sets one on the
        # transition into RUNNING), but treat it as expired rather than raise,
        # since guessing "not claimable" here would strand the Run forever.
        return header.lease_expires_at is None or header.lease_expires_at <= now

    async def renew(
        self, run_id: RunId, worker_id: WorkerId, now: datetime, lease_seconds: float
    ) -> bool:
        async with self._lock:
            header = self._runs.get(run_id)
            if header is None or header.lease_holder != worker_id:
                # Not held by this Worker: either never claimed, released, or
                # reclaimed by someone else after the lease expired. All three
                # collapse to the same answer per the port's contract.
                return False
            self._runs[run_id] = header.model_copy(
                update={"lease_expires_at": now + timedelta(seconds=lease_seconds)}
            )
            return True

    async def release(self, run_id: RunId, worker_id: WorkerId, state: RunState) -> None:
        async with self._lock:
            header = self._runs.get(run_id)
            if header is None or header.lease_holder != worker_id:
                # Tolerant by design (DESIGN.md §7 docstring on Store.release): a
                # slow Attempt unwinding after its lease was reclaimed must not
                # turn into an error, and must not clobber the new holder's state.
                return
            self._runs[run_id] = header.model_copy(
                update={"state": state, "lease_holder": None, "lease_expires_at": None}
            )

    async def set_runnable_at(self, run_id: RunId, runnable_at: datetime | None) -> None:
        async with self._lock:
            header = self._runs.get(run_id)
            if header is None:
                raise RunNotFound(run_id)
            update: dict[str, object] = {"runnable_at": runnable_at}
            if runnable_at is None and header.state is RunState.SUSPENDED:
                # Resume. Nobody holds the lease, so this is the only path back
                # to claimable, and leaving it suspended would strand the Run.
                update["state"] = RunState.RUNNABLE
            self._runs[run_id] = header.model_copy(update=update)

    # -- supervision --------------------------------------------------------

    async def expired_leases(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        async with self._lock:
            expired: list[tuple[datetime, RunHeader]] = []
            for header in self._runs.values():
                expires_at = header.lease_expires_at
                if (
                    header.state == RunState.RUNNING
                    and expires_at is not None
                    and expires_at <= now
                ):
                    expired.append((expires_at, header))
            expired.sort(key=lambda pair: pair[0])
            return [header for _, header in expired[:limit]]

    async def overdue_deadlines(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        async with self._lock:
            overdue = [
                header
                for header in self._runs.values()
                if header.state != RunState.SETTLED and header.deadline_at <= now
            ]
            overdue.sort(key=lambda header: header.deadline_at)
            return overdue[:limit]
