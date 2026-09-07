"""The PostgreSQL ``Store`` adapter.

DESIGN.md §7: ``asyncpg``, records keyed by ``(run_id, seq)`` as the primary
key so a duplicate append fails on the constraint rather than on a
read-then-write race, and lease claim as a single conditional ``UPDATE``.

## Why this file never opens a transaction

The port's whole design is that every operation is expressible as one
conditional write. This adapter honours that literally: no ``BEGIN``, no
``SELECT ... FOR UPDATE``, no advisory lock, anywhere below. ``claim()`` looks
like it needs one, because "pick one runnable Run and take its lease" sounds
like a read followed by a write. It is not: the candidate is chosen by a CTE,
but the ``UPDATE`` re-states the exact same claimability predicate against the
target row. PostgreSQL's MVCC guarantees that when two claims race for the
same row, the loser's ``UPDATE`` re-evaluates its ``WHERE`` clause against the
row PostgreSQL made it wait for, and by the time it gets to run, the winner has
already flipped ``state`` to ``running`` with a live lease, so the loser's
predicate no longer matches and it updates zero rows. No lock statement is
needed for that guarantee; it is what an ``UPDATE ... WHERE`` already does.
Getting this right without ``SELECT ... FOR UPDATE SKIP LOCKED`` (which
PostgreSQL has but DynamoDB has no equivalent for) is exactly what keeps the
lease model identical across all four adapters (DESIGN.md §7).

## Why headers are columns, not one JSONB blob

``claim()``, ``expired_leases()`` and ``overdue_deadlines()`` all filter and
order on ``state``, ``lease_expires_at``, ``runnable_at`` and ``deadline_at``.
Indexing a JSONB path is possible but is worse to read, worse to reason about
under `EXPLAIN`, and buys nothing here since every one of those fields is
scalar. ``psych_runs`` mirrors ``RunHeader`` field for field instead; only
``scope`` stays JSONB, since it is itself a nested model with no query of its
own against it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Final, Self, cast

import asyncpg  # type: ignore[import-untyped]  # no bundled py.typed marker; see mypy override below

from psych_runtime.core.errors import RunNotFound, SeqConflict
from psych_runtime.core.ids import RunId, VersionHash, WorkerId
from psych_runtime.core.records import RECORD_ADAPTER, Record
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version
from psych_runtime.store.port import RunHeader, RunState

__all__ = ["PostgresStore"]

_MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"

_RUN_COLUMNS: Final = (
    "run_id, scope, version_hash, state, created_at, deadline_at, lease_holder, "
    "lease_expires_at, idempotency_key, attempt_count, runnable_at, parent_run_id, "
    "delegation_depth, continues_run_id"
)

_CREATE_SCHEMA_MIGRATIONS_TABLE: Final = """
CREATE TABLE IF NOT EXISTS psych_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _row_to_header(row: asyncpg.Record) -> RunHeader:
    """Rebuild a ``RunHeader`` from a ``psych_runs`` row.

    Column order follows ``_RUN_COLUMNS`` so every ``SELECT`` in this module
    lists columns explicitly rather than using ``SELECT *``, and a row from any
    of them decodes the same way.
    """
    return RunHeader(
        run_id=RunId(row["run_id"]),
        scope=Scope.model_validate(json.loads(row["scope"])),
        version_hash=VersionHash(row["version_hash"]),
        state=RunState(row["state"]),
        created_at=row["created_at"],
        deadline_at=row["deadline_at"],
        lease_holder=WorkerId(row["lease_holder"]) if row["lease_holder"] is not None else None,
        lease_expires_at=row["lease_expires_at"],
        idempotency_key=row["idempotency_key"],
        attempt_count=row["attempt_count"],
        runnable_at=row["runnable_at"],
        parent_run_id=RunId(row["parent_run_id"]) if row["parent_run_id"] is not None else None,
        delegation_depth=row["delegation_depth"],
        continues_run_id=(
            RunId(row["continues_run_id"]) if row["continues_run_id"] is not None else None
        ),
    )


class PostgresStore:
    """A ``Store`` backed by PostgreSQL via ``asyncpg``.

    Implements the ``Store`` protocol structurally; there is no base class to
    inherit because the port is a ``Protocol``.

    Accepts either a DSN, in which case this instance opens and owns a
    connection pool, or an externally managed ``asyncpg.Pool`` that the caller
    keeps ownership of. Only the owned case closes the pool on ``close()``, so
    handing in a shared pool never gets it pulled out from under other users of
    it.
    """

    def __init__(self, *, dsn: str | None = None, pool: asyncpg.Pool | None = None) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("PostgresStore takes exactly one of `dsn` or `pool`")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = pool
        self._owns_pool = pool is None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            # dsn is not None here: __init__ requires exactly one of the two.
            self._pool = await asyncpg.create_pool(dsn=self._dsn)
        return self._pool

    async def close(self) -> None:
        """Close the pool this instance owns. A no-op for a pool handed in."""
        if self._owns_pool and self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self) -> Self:
        await self._get_pool()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # -- migrations -----------------------------------------------------------

    async def migrate(self) -> None:
        """Apply every migration in ``psych_runtime/store/migrations`` not yet applied.

        Sequential and forward-only: files are named ``NNNN_description.sql``
        and applied in numeric order, each recorded in
        ``psych_schema_migrations`` so a second call is a no-op for anything
        already there. Never called implicitly: the consumer decides when
        their database's schema changes, and Psych does not run migrations on
        connect (DESIGN.md §22, this ticket's acceptance criteria).
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_CREATE_SCHEMA_MIGRATIONS_TABLE)
            applied = {
                row["version"]
                for row in await conn.fetch("SELECT version FROM psych_schema_migrations")
            }
            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                # A DDL script with no query arguments runs over asyncpg's simple
                # query protocol, which is what lets one `execute()` carry the
                # several `;`-separated statements a migration file holds.
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO psych_schema_migrations (version, name) VALUES ($1, $2)",
                    version,
                    path.name,
                )

    # -- the log ----------------------------------------------------------------

    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        pool = await self._get_pool()
        payload = record.model_dump_json()
        status = await pool.execute(
            "INSERT INTO psych_records (run_id, seq, record) VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (run_id, seq) DO NOTHING",
            run_id,
            seq,
            payload,
        )
        # asyncpg's execute() returns a command tag like "INSERT 0 1"; the
        # trailing count is 0 when ON CONFLICT DO NOTHING suppressed the row,
        # which is how a conditional insert's outcome is read back without a
        # RETURNING clause and a second round trip.
        if _affected_rows(status) == 0:
            raise SeqConflict(run_id, seq)

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        pool = await self._get_pool()
        if limit is None:
            rows = await pool.fetch(
                "SELECT record FROM psych_records WHERE run_id = $1 AND seq > $2 ORDER BY seq",
                run_id,
                after,
            )
        else:
            rows = await pool.fetch(
                "SELECT record FROM psych_records WHERE run_id = $1 AND seq > $2 "
                "ORDER BY seq LIMIT $3",
                run_id,
                after,
                limit,
            )
        return [RECORD_ADAPTER.validate_json(row["record"]) for row in rows]

    async def head(self, run_id: RunId) -> int:
        pool = await self._get_pool()
        value = await pool.fetchval(
            "SELECT COALESCE(MAX(seq), 0) FROM psych_records WHERE run_id = $1", run_id
        )
        return cast(int, value)

    # -- versions ---------------------------------------------------------------

    async def put_version(self, version: Version) -> None:
        pool = await self._get_pool()
        await pool.execute(
            "INSERT INTO psych_versions (hash, version) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (hash) DO NOTHING",
            version.hash,
            version.model_dump_json(),
        )

    async def get_version(self, version_hash: VersionHash) -> Version | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "SELECT version FROM psych_versions WHERE hash = $1", version_hash
        )
        if row is None:
            return None
        return Version.model_validate_json(row["version"])

    # -- runs ---------------------------------------------------------------

    async def create_run(self, header: RunHeader) -> RunHeader:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO psych_runs ({_RUN_COLUMNS}) VALUES "
                "($1, $2::jsonb, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
                "ON CONFLICT DO NOTHING "
                f"RETURNING {_RUN_COLUMNS}",
                header.run_id,
                header.scope.model_dump_json(),
                header.version_hash,
                header.state.value,
                header.created_at,
                header.deadline_at,
                header.lease_holder,
                header.lease_expires_at,
                header.idempotency_key,
                header.attempt_count,
                header.runnable_at,
                header.parent_run_id,
                header.delegation_depth,
                header.continues_run_id,
            )
            if row is not None:
                return _row_to_header(row)

            # Lost the race to insert, on either the run_id primary key or the
            # partial unique index on idempotency_key. Whichever it was, the row
            # that beat us is the one the port says to return: the existing
            # header rather than a second Run created under an idempotency key
            # already used, and never clobbering a Run already admitted under
            # this run_id (DESIGN.md §8.3, the port's docstring on create_run).
            if header.idempotency_key is not None:
                existing = await conn.fetchrow(
                    f"SELECT {_RUN_COLUMNS} FROM psych_runs "
                    "WHERE idempotency_key = $1 AND scope->>'tenant' = $2",
                    header.idempotency_key,
                    header.scope.tenant,
                )
                if existing is not None:
                    return _row_to_header(existing)
            existing = await conn.fetchrow(
                f"SELECT {_RUN_COLUMNS} FROM psych_runs WHERE run_id = $1", header.run_id
            )
            if existing is None:
                # The conflicting row vanished between the failed insert and this
                # read, which nothing in the port's contract permits (rows are
                # never deleted). Surfacing this as RunNotFound would misname a
                # bug in the caller's concurrency as an absence.
                raise RuntimeError(
                    f"create_run for {header.run_id} conflicted but no existing row was found"
                )
            return _row_to_header(existing)

    async def get_run(self, run_id: RunId) -> RunHeader | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            f"SELECT {_RUN_COLUMNS} FROM psych_runs WHERE run_id = $1", run_id
        )
        return _row_to_header(row) if row is not None else None

    async def claim(self, worker_id: WorkerId, now: datetime, lease_seconds: float) -> RunId | None:
        pool = await self._get_pool()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        # `candidate` and the UPDATE's own WHERE clause both state the
        # claimability predicate. That duplication is what makes this safe
        # under contention without SELECT ... FOR UPDATE: PostgreSQL re-checks
        # the WHERE clause against the current row before an UPDATE actually
        # applies to it, so a second claimant whose UPDATE had to wait behind
        # the first sees the row post-claim and its own predicate fails.
        row = await pool.fetchrow(
            """
            WITH candidate AS (
                SELECT run_id
                FROM psych_runs
                WHERE state IN ('runnable', 'running')
                  AND (runnable_at IS NULL OR runnable_at <= $3)
                  AND (state = 'runnable' OR lease_expires_at <= $3)
                ORDER BY created_at
                LIMIT 1
            )
            UPDATE psych_runs AS r
            SET state = 'running',
                lease_holder = $1,
                lease_expires_at = $2,
                attempt_count = r.attempt_count + 1
            FROM candidate c
            WHERE r.run_id = c.run_id
              AND r.state IN ('runnable', 'running')
              AND (r.runnable_at IS NULL OR r.runnable_at <= $3)
              AND (r.state = 'runnable' OR r.lease_expires_at <= $3)
            RETURNING r.run_id
            """,
            worker_id,
            lease_expires_at,
            now,
        )
        return RunId(row["run_id"]) if row is not None else None

    async def renew(
        self, run_id: RunId, worker_id: WorkerId, now: datetime, lease_seconds: float
    ) -> bool:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            "UPDATE psych_runs SET lease_expires_at = $3 "
            "WHERE run_id = $1 AND lease_holder = $2 "
            "RETURNING run_id",
            run_id,
            worker_id,
            now + timedelta(seconds=lease_seconds),
        )
        return row is not None

    async def release(self, run_id: RunId, worker_id: WorkerId, state: RunState) -> None:
        pool = await self._get_pool()
        # Tolerant of a non-holder by construction: the WHERE clause matches
        # zero rows rather than raising when another Worker already reclaimed
        # this Run's lease (DESIGN.md §7, the port's docstring on release).
        await pool.execute(
            "UPDATE psych_runs SET state = $3, lease_holder = NULL, lease_expires_at = NULL "
            "WHERE run_id = $1 AND lease_holder = $2",
            run_id,
            worker_id,
            state.value,
        )

    async def set_runnable_at(self, run_id: RunId, runnable_at: datetime | None) -> None:
        pool = await self._get_pool()
        row = await pool.fetchrow(
            # Clearing the parking on a suspended Run is a resume, and resume is
            # the only path back to claimable because nobody holds the lease.
            """
            UPDATE psych_runs
               SET runnable_at = $2,
                   state = CASE
                             WHEN $2::timestamptz IS NULL AND state = 'suspended'
                               THEN 'runnable'
                             ELSE state
                           END
             WHERE run_id = $1
             RETURNING run_id
            """,
            run_id,
            runnable_at,
        )
        if row is None:
            raise RunNotFound(run_id)

    # -- supervision --------------------------------------------------------

    async def expired_leases(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"SELECT {_RUN_COLUMNS} FROM psych_runs "
            "WHERE state = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= $1 "
            "ORDER BY lease_expires_at LIMIT $2",
            now,
            limit,
        )
        return [_row_to_header(row) for row in rows]

    async def overdue_deadlines(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        pool = await self._get_pool()
        rows = await pool.fetch(
            f"SELECT {_RUN_COLUMNS} FROM psych_runs "
            "WHERE state != 'settled' AND deadline_at <= $1 "
            "ORDER BY deadline_at LIMIT $2",
            now,
            limit,
        )
        return [_row_to_header(row) for row in rows]


def _affected_rows(command_tag: str) -> int:
    """The row count asyncpg's ``execute()`` reports in a command tag.

    Tags look like ``INSERT 0 1`` or ``UPDATE 3``; the count is always the
    last whitespace-separated field.
    """
    return int(command_tag.rsplit(" ", 1)[-1])
