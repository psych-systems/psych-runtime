"""The MySQL/MariaDB Store adapter.

DESIGN.md §7: conditional writes only, no transactions, no joins, no
``SELECT ... FOR UPDATE``. MySQL has no ``UPDATE ... RETURNING``, which is the
one place this adapter genuinely differs from a Postgres implementation, so the
shape of every conditional operation here is chosen around that gap.

## Why every conditional insert uses ``ON DUPLICATE KEY UPDATE`` rather than
## ``INSERT IGNORE``

Both express "insert unless it's already there", but ``INSERT IGNORE`` turns
the duplicate-key error into a MySQL *warning*, and aiomysql surfaces every
server warning through Python's ``warnings`` module. Under this project's
``filterwarnings = ["error"]`` that warning becomes a real exception on every
conflict the contract suite deliberately provokes (two writers racing for one
``seq``, a retried idempotent ``create_run``). ``INSERT ... ON DUPLICATE KEY
UPDATE <col> = <col>`` reaches the identical state, a row that was already
there stays exactly as it was, without MySQL ever calling it a warning: the
affected-rows count is 0 for the no-op and 1 for a genuine insert, which is
the only thing a conditional write needs to tell the two apart. The
CLIENT_FOUND_ROWS connection flag would make that 0-vs-1 distinction collapse
(a same-value ``UPDATE`` and a no-op ``ON DUPLICATE KEY UPDATE`` both start
reporting "matched" rather than "changed"), so this adapter never sets it and
instead falls back to an explicit existence or ownership check wherever an
``UPDATE``'s affected-rows count alone would be ambiguous (``renew``,
``set_runnable_at``): see the docstrings on those methods.

## Solving ``claim()`` without ``RETURNING``

Postgres can claim a row and read back which one it got in a single
``UPDATE ... RETURNING run_id``. MySQL cannot return columns from an
``UPDATE``, so claiming happens in two steps that stay individually
conditional:

1. Read a short list of candidate ``run_id``s that currently look claimable
   (``SELECT ... WHERE <claimable> ORDER BY created_at LIMIT N``). This read is
   not itself a lock and proves nothing by the time step 2 runs.
2. For each candidate in order, run
   ``UPDATE runs SET state = 'running', ... WHERE run_id = :candidate AND
   <claimable predicate repeated>``. The predicate is re-checked in the
   ``WHERE`` clause of the row-scoped update, not trusted from step 1, so a
   candidate that another Worker claimed in between step 1 and step 2 fails
   this update with affected-rows 0 rather than being claimed twice. 0 means
   "someone else won it"; the loop moves to the next candidate. 1 means this
   call won it, and the same ``run_id`` it just conditionally updated is what
   gets returned, so there is never a moment where a row is claimed without
   this call knowing which one.

No row is ever locked between steps 1 and 2. The race is closed entirely by
step 2's ``WHERE`` clause re-stating the claimability predicate against one
named row, which is what turns "pick a candidate" into a conditional write
rather than a plan two callers could both act on.

## DATETIME(6) and timezones

Every lease and deadline column is ``DATETIME(6)``: plain ``DATETIME``
truncates to whole seconds and the contract suite compares leases at
sub-second resolution. aiomysql returns naive ``datetime`` values for these
columns and binds naive values on write, so every value crossing this
boundary is converted explicitly: ``_to_naive_utc`` before a write,
``_to_aware_utc`` after a read. Skipping either direction means a comparison
against ``datetime.now(UTC)`` eventually raises, or a lease silently drifts
by whatever the local system's UTC offset happens to be.

## Pooling

``MySQLStore`` either wraps a pool the caller already owns (``pool=``,
``close()`` becomes a no-op) or owns one it creates from a DSN (``dsn=``,
``close()`` tears it down). Either way it is usable as an async context
manager.
"""

from __future__ import annotations

import asyncio
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import aiomysql

from psych_runtime.core.errors import RunNotFound, SeqConflict, StoreError
from psych_runtime.core.ids import RunId, VersionHash, WorkerId
from psych_runtime.core.records import RECORD_ADAPTER, Record
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version
from psych_runtime.store.port import RunHeader, RunState

__all__ = ["MySQLStore"]

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations_mysql"
"""Where the forward-only schema files live. Nothing in this module reads
them: applying migrations is the caller's decision, never something a Store
does for itself on connect (psych_runtime.store never provisions the consumer's
database, per this package's README)."""

_CREATE_SCHEMA_MIGRATIONS_TABLE: Final = """
CREATE TABLE IF NOT EXISTS psych_schema_migrations (
    version INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
"""The same ledger ``PostgresStore`` keeps, in MySQL's spelling.

Without it this adapter reapplied every statement of every file on every
call, which is a different thing from what its docstring claimed and forced
each migration to carry its own idempotence. There is no portable way to
write that for a column: ``ADD COLUMN IF NOT EXISTS`` is MariaDB's, MySQL
rejects it outright, and a development script that runs MariaDB while
calling it MySQL hides which one you are actually testing against."""

_DEFAULT_PORT: Final = 3306
_POOL_MAXSIZE: Final = 20
"""Comfortably above the 12-way contention the contract suite races claim()
with, so a test never queues on the pool itself and only on the row it wants."""

_RUN_COLUMNS: Final = (
    "run_id",
    "scope",
    "version_hash",
    "state",
    "created_at",
    "deadline_at",
    "lease_holder",
    "lease_expires_at",
    "idempotency_key",
    "attempt_count",
    "runnable_at",
    "parent_run_id",
    "delegation_depth",
    "continues_run_id",
)
_RUN_COLUMNS_SQL: Final = ", ".join(_RUN_COLUMNS)
_RUN_PLACEHOLDERS_SQL: Final = ", ".join(["%s"] * len(_RUN_COLUMNS))

# Re-checked in claim()'s per-candidate UPDATE, not trusted from the candidate
# read: a state of RUNNABLE is always claimable; RUNNING is claimable only once
# its lease has expired (or was never set, treated as expired rather than
# stranding the row forever); either way runnable_at must have passed.
_CLAIMABLE_PREDICATE: Final = (
    "(state = 'runnable' OR (state = 'running' "
    "AND (lease_expires_at IS NULL OR lease_expires_at <= %s))) "
    "AND (runnable_at IS NULL OR runnable_at <= %s)"
)
_CLAIM_CANDIDATE_LIMIT: Final = 50


def _to_naive_utc(value: datetime) -> datetime:
    """aiomysql binds naive datetimes; drop the tzinfo after converting to UTC
    so what a later read produces compares equal to what was written."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _to_naive_utc_opt(value: datetime | None) -> datetime | None:
    return None if value is None else _to_naive_utc(value)


def _to_aware_utc(value: datetime) -> datetime:
    """aiomysql returns naive datetimes for DATETIME columns; without this a
    comparison against datetime.now(UTC) raises rather than being wrong."""
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def _to_aware_utc_opt(value: datetime | None) -> datetime | None:
    return None if value is None else _to_aware_utc(value)


def _header_params(header: RunHeader) -> tuple[Any, ...]:
    """``header``'s fields in ``_RUN_COLUMNS`` order, ready to bind."""
    return (
        header.run_id,
        header.scope.model_dump_json(),
        header.version_hash,
        header.state.value,
        _to_naive_utc(header.created_at),
        _to_naive_utc(header.deadline_at),
        header.lease_holder,
        _to_naive_utc_opt(header.lease_expires_at),
        header.idempotency_key,
        header.attempt_count,
        _to_naive_utc_opt(header.runnable_at),
        header.parent_run_id,
        header.delegation_depth,
        header.continues_run_id,
    )


def _row_to_header(row: Any) -> RunHeader:
    """Reassemble a ``RunHeader`` from one row selected as ``_RUN_COLUMNS``."""
    (
        run_id,
        scope_json,
        version_hash,
        state,
        created_at,
        deadline_at,
        lease_holder,
        lease_expires_at,
        idempotency_key,
        attempt_count,
        runnable_at,
        parent_run_id,
        delegation_depth,
        continues_run_id,
    ) = row
    return RunHeader(
        run_id=RunId(run_id),
        scope=Scope.model_validate_json(scope_json),
        version_hash=VersionHash(version_hash),
        state=RunState(state),
        created_at=_to_aware_utc(created_at),
        deadline_at=_to_aware_utc(deadline_at),
        lease_holder=WorkerId(lease_holder) if lease_holder is not None else None,
        lease_expires_at=_to_aware_utc_opt(lease_expires_at),
        idempotency_key=idempotency_key,
        attempt_count=attempt_count,
        runnable_at=_to_aware_utc_opt(runnable_at),
        parent_run_id=RunId(parent_run_id) if parent_run_id is not None else None,
        delegation_depth=delegation_depth,
        continues_run_id=RunId(continues_run_id) if continues_run_id is not None else None,
    )


class MySQLStore:
    """A ``Store`` backed by MySQL or MariaDB via ``aiomysql``.

    Implements the ``Store`` protocol structurally; there is no base class to
    inherit because the port is a ``Protocol``.
    """

    def __init__(self, *, pool: aiomysql.Pool | None = None, dsn: str | None = None) -> None:
        if pool is None and dsn is None:
            raise ValueError("MySQLStore needs either an existing pool= or a dsn=")
        self._owns_pool = pool is None
        self._pool = pool
        self._dsn = dsn
        # Guards pool creation only: every Store call after that acquires its
        # own connection from the pool and holds no lock while it runs, so
        # this never serialises the concurrent operations the contract suite
        # deliberately races (claim() under contention, competing appends).
        self._pool_creation_lock = asyncio.Lock()

    async def __aenter__(self) -> MySQLStore:
        await self._ensure_pool()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Tear down the pool this store created. A no-op over a pool the
        caller supplied; that pool is the caller's to close."""
        if not self._owns_pool or self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def _ensure_pool(self) -> aiomysql.Pool:
        pool = self._pool
        if pool is not None:
            return pool
        # Double-checked locking: two concurrent first calls (the contract
        # suite's contention tests call every Store method from the first
        # await, with no earlier call to warm the pool) would otherwise both
        # see no pool, both create one, and whichever assignment loses leaks
        # its pool and its already-opened connection. The second read of
        # self._pool below is a fresh attribute read, not the narrowing of
        # the one above: the await for the lock is exactly where another
        # coroutine could have set it.
        async with self._pool_creation_lock:
            pool = self._pool
            if pool is not None:
                return pool
            if self._dsn is None:
                raise StoreError("MySQLStore has no pool and no dsn to create one from")
            parsed = urlsplit(self._dsn)
            self._pool = await aiomysql.create_pool(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or _DEFAULT_PORT,
                user=parsed.username,
                password=parsed.password or "",
                db=parsed.path.lstrip("/"),
                autocommit=True,
                maxsize=_POOL_MAXSIZE,
                # Both of these are stated rather than inherited from the
                # server, because inheriting them is how a store works in
                # development and corrupts data in production.
                #
                # utf8mb4 is the only MySQL charset that is actually UTF-8. The
                # one called "utf8" is three bytes per character and cannot hold
                # an emoji or several CJK extensions, and a server configured
                # that way does not error: it truncates the record at the first
                # character it cannot encode, or replaces it. A Run whose input
                # contains an emoji is not exotic.
                charset="utf8mb4",
                use_unicode=True,
            )
            # READ COMMITTED rather than MySQL's REPEATABLE READ default. The
            # Store does conditional writes and no multi-statement transactions,
            # so it needs no snapshot, and REPEATABLE READ takes gap locks on
            # range scans, which is what claim() and expired_leases() do. Under
            # contention those gap locks turn independent Workers into a queue.
            async with self._pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            return self._pool

    async def _fetch_run(
        self,
        cur: Any,
        *,
        run_id: RunId | None = None,
        idempotency_key: str | None = None,
        tenant: str | None = None,
    ) -> RunHeader | None:
        if run_id is not None:
            await cur.execute(f"SELECT {_RUN_COLUMNS_SQL} FROM runs WHERE run_id = %s", (run_id,))
        else:
            await cur.execute(
                f"SELECT {_RUN_COLUMNS_SQL} FROM runs WHERE idempotency_key = %s AND tenant = %s",
                (idempotency_key, tenant),
            )
        row = await cur.fetchone()
        return None if row is None else _row_to_header(row)

    # -- the log --------------------------------------------------------

    async def migrate(self) -> None:
        """Apply every migration not yet applied, in order.

        Real parity with ``PostgresStore.migrate`` now, rather than the claim
        of it this docstring used to make: applied versions are recorded in
        ``psych_schema_migrations`` and skipped, so a second call is a no-op
        for anything already there. The adapters implement one Store contract
        and a consumer switching between them should not have to discover that
        one of them sets its schema up a different way.

        What the ledger buys is not tidiness. Reapplying every statement meant
        each migration had to be idempotent on its own, and for adding a column
        the only compact spelling of that -- ``ADD COLUMN IF NOT EXISTS`` -- is
        MariaDB's alone. MySQL 8 rejects it as a syntax error, so this adapter
        did not work against the database it is named for. Version-gating is
        the same mechanism the Postgres adapter already used and needs nothing
        dialect-specific from the files.

        MySQL reports "table already exists" as a warning rather than an error,
        and this project turns warnings into exceptions, so the ledger's own
        ``CREATE TABLE IF NOT EXISTS`` is executed with warnings suppressed.
        Nothing else runs inside that block whose warning could be masked.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await cur.execute(_CREATE_SCHEMA_MIGRATIONS_TABLE)
            await cur.execute("SELECT version FROM psych_schema_migrations")
            applied = {int(row[0]) for row in await cur.fetchall()}
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(migration.name.split("_", 1)[0])
                if version in applied:
                    continue
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    trimmed = statement.strip()
                    if not trimmed:
                        continue
                    # Kept from before the ledger existed. On a fresh database
                    # nothing here warns, because each file now runs exactly
                    # once. It still matters for a database migrated by the
                    # previous version of this method, which has the tables
                    # but no ledger: 0001 is entirely `IF NOT EXISTS` and
                    # replays harmlessly, and MySQL reports "table already
                    # exists" as a warning this project would otherwise raise.
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        await cur.execute(trimmed)
                await cur.execute(
                    "INSERT INTO psych_schema_migrations (version, name) VALUES (%s, %s)",
                    (version, migration.name),
                )
            await conn.commit()

    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        payload = RECORD_ADAPTER.dump_json(record).decode("utf-8")
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO records (run_id, seq, record) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE run_id = run_id",
                (run_id, seq, payload),
            )
            if cur.rowcount == 0:
                raise SeqConflict(run_id, seq)

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            if limit is None:
                await cur.execute(
                    "SELECT record FROM records WHERE run_id = %s AND seq > %s ORDER BY seq ASC",
                    (run_id, after),
                )
            else:
                await cur.execute(
                    "SELECT record FROM records WHERE run_id = %s AND seq > %s "
                    "ORDER BY seq ASC LIMIT %s",
                    (run_id, after, limit),
                )
            rows = await cur.fetchall()
        return [RECORD_ADAPTER.validate_json(row[0]) for row in rows]

    async def head(self, run_id: RunId) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("SELECT MAX(seq) FROM records WHERE run_id = %s", (run_id,))
            row = await cur.fetchone()
        value = row[0] if row is not None else None
        return 0 if value is None else int(value)

    # -- versions -----------------------------------------------------------

    async def put_version(self, version: Version) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            # Idempotent by construction: the hash is the identity, so a
            # second put of the same Version is a no-op, not an overwrite.
            await cur.execute(
                "INSERT INTO versions (version_hash, version, published_at) "
                "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE version_hash = version_hash",
                (version.hash, version.model_dump_json(), _to_naive_utc(version.published_at)),
            )

    async def get_version(self, version_hash: VersionHash) -> Version | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT version FROM versions WHERE version_hash = %s", (version_hash,)
            )
            row = await cur.fetchone()
        return None if row is None else Version.model_validate_json(row[0])

    # -- runs ---------------------------------------------------------------

    async def create_run(self, header: RunHeader) -> RunHeader:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            if header.idempotency_key is not None:
                existing = await self._fetch_run(
                    cur, idempotency_key=header.idempotency_key, tenant=header.scope.tenant
                )
                if existing is not None:
                    return existing

            await cur.execute(
                f"INSERT INTO runs ({_RUN_COLUMNS_SQL}) VALUES ({_RUN_PLACEHOLDERS_SQL}) "
                "ON DUPLICATE KEY UPDATE run_id = run_id",
                _header_params(header),
            )
            if cur.rowcount == 1:
                return header

            # affected-rows 0: the conditional insert found a row already
            # there, on either unique constraint. Re-read to say which.
            if header.idempotency_key is not None:
                existing = await self._fetch_run(
                    cur, idempotency_key=header.idempotency_key, tenant=header.scope.tenant
                )
                if existing is not None:
                    return existing
            existing = await self._fetch_run(cur, run_id=header.run_id)
            if existing is not None:
                return existing
            raise StoreError(
                f"create_run for {header.run_id} reported a conflict but no existing row "
                "was found on re-read; this should be unreachable"
            )

    async def get_run(self, run_id: RunId) -> RunHeader | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            return await self._fetch_run(cur, run_id=run_id)

    async def claim(self, worker_id: WorkerId, now: datetime, lease_seconds: float) -> RunId | None:
        now_naive = _to_naive_utc(now)
        expires_at = _to_naive_utc(now + timedelta(seconds=lease_seconds))
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT run_id FROM runs WHERE {_CLAIMABLE_PREDICATE} "
                "ORDER BY created_at ASC, run_id ASC LIMIT %s",
                (now_naive, now_naive, _CLAIM_CANDIDATE_LIMIT),
            )
            candidates = [row[0] for row in await cur.fetchall()]

            for candidate in candidates:
                # The claimable predicate is repeated here, scoped to this one
                # run_id, rather than trusted from the read above: this is the
                # entire race fix documented at the top of the module.
                await cur.execute(
                    f"UPDATE runs SET state = %s, lease_holder = %s, lease_expires_at = %s, "
                    f"attempt_count = attempt_count + 1 "
                    f"WHERE run_id = %s AND {_CLAIMABLE_PREDICATE}",
                    (
                        RunState.RUNNING.value,
                        worker_id,
                        expires_at,
                        candidate,
                        now_naive,
                        now_naive,
                    ),
                )
                if cur.rowcount == 1:
                    return RunId(candidate)
            return None

    async def renew(
        self, run_id: RunId, worker_id: WorkerId, now: datetime, lease_seconds: float
    ) -> bool:
        expires_at = _to_naive_utc(now + timedelta(seconds=lease_seconds))
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE runs SET lease_expires_at = %s WHERE run_id = %s AND lease_holder = %s",
                (expires_at, run_id, worker_id),
            )
            if cur.rowcount >= 1:
                return True
            # affected-rows 0 is ambiguous on its own: it means either no such
            # holder, or a holder whose new expiry happens to equal the old
            # one. This adapter never sets CLIENT_FOUND_ROWS (it would make
            # the same ambiguity worse for the conditional inserts above), so
            # the two cases are told apart with an explicit ownership check.
            await cur.execute(
                "SELECT 1 FROM runs WHERE run_id = %s AND lease_holder = %s", (run_id, worker_id)
            )
            return await cur.fetchone() is not None

    async def release(self, run_id: RunId, worker_id: WorkerId, state: RunState) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            # No error on affected-rows 0: by the time a slow Attempt unwinds,
            # another Worker may already own the run, and this must not
            # clobber that Worker's state (Store.release's contract).
            await cur.execute(
                "UPDATE runs SET state = %s, lease_holder = NULL, lease_expires_at = NULL "
                "WHERE run_id = %s AND lease_holder = %s",
                (state.value, run_id, worker_id),
            )

    async def set_runnable_at(self, run_id: RunId, runnable_at: datetime | None) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                # Clearing the parking on a suspended Run is a resume, and
                # resume is the only path back to claimable because nobody holds
                # the lease at that point.
                "UPDATE runs "
                "   SET runnable_at = %s, "
                "       state = CASE WHEN %s IS NULL AND state = 'suspended' "
                "                    THEN 'runnable' ELSE state END "
                " WHERE run_id = %s",
                (
                    _to_naive_utc_opt(runnable_at),
                    _to_naive_utc_opt(runnable_at),
                    run_id,
                ),
            )
            if cur.rowcount >= 1:
                return
            # Same ambiguity as renew(): affected-rows 0 could mean the run
            # does not exist, or it exists and runnable_at already held the
            # value being set. Only the first case is RunNotFound.
            await cur.execute("SELECT 1 FROM runs WHERE run_id = %s", (run_id,))
            if await cur.fetchone() is None:
                raise RunNotFound(run_id)

    # -- supervision --------------------------------------------------------

    async def expired_leases(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_RUN_COLUMNS_SQL} FROM runs "
                "WHERE state = %s AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s "
                "ORDER BY lease_expires_at ASC LIMIT %s",
                (RunState.RUNNING.value, _to_naive_utc(now), limit),
            )
            rows = await cur.fetchall()
        return [_row_to_header(row) for row in rows]

    async def overdue_deadlines(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_RUN_COLUMNS_SQL} FROM runs "
                "WHERE state != %s AND deadline_at <= %s ORDER BY deadline_at ASC LIMIT %s",
                (RunState.SETTLED.value, _to_naive_utc(now), limit),
            )
            rows = await cur.fetchall()
        return [_row_to_header(row) for row in rows]
