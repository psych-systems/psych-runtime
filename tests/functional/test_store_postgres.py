"""The PostgreSQL Store against the shared contract suite.

DESIGN.md §22: no mocked store. This runs the full contract suite against a
real PostgreSQL, skipping (never faking) when ``PSYCH_TEST_POSTGRES_DSN`` is
unset, per the ``postgres_dsn`` fixture in ``tests/conftest.py``.

The pool and the migration are set up once per module and reused across tests
for speed, but the three tables are truncated before every single test. Most
contract tests mint their own ``run_id`` and would not need this, but
``claim()`` deliberately takes no ``run_id`` (``psych_runtime.store.contract`` module
docstring: it hands back whatever is runnable), so a Run left ``RUNNABLE`` by
one test is a live candidate for the next test's ``claim()`` call unless the
table starts empty each time. A shared, un-truncated table would make the
claim and lease tests interfere with each other depending on run order, which
is exactly what every test must avoid. The fixture builds the
pool itself and hands it to ``PostgresStore`` as ``pool=``, which exercises the
externally managed pool path; the ``dsn=``-owned-pool path is exercised below
by the adapter-specific tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg  # type: ignore[import-untyped]  # no bundled py.typed marker
import pytest
import pytest_asyncio

from psych_runtime.core.ids import new_run_id, new_worker_id
from psych_runtime.store.contract import StoreContractSuite, _header, _record
from psych_runtime.store.port import Store
from psych_runtime.store.postgres import PostgresStore

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pg_pool(postgres_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=postgres_dsn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def pg_store(pg_pool: asyncpg.Pool) -> AsyncIterator[PostgresStore]:
    store = PostgresStore(pool=pg_pool)
    await store.migrate()
    yield store
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE psych_records, psych_runs, psych_versions")


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_tables(pg_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Empty tables before every test.

    ``claim()`` picks among every runnable Run in the table, not just the one
    a given test created, so a Run another test left behind is otherwise a
    silent source of cross-test interference (see the module docstring).
    """
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE psych_records, psych_runs, psych_versions")
    yield


class TestPostgresStore(StoreContractSuite):
    @pytest.fixture
    def store(self, pg_store: PostgresStore) -> Store:
        return pg_store


class TestPostgresAdapterSpecifics:
    """Behaviour this ticket calls out explicitly, beyond the shared contract."""

    async def test_migrate_is_idempotent(self, postgres_dsn: str) -> None:
        """`migrate()` is the explicit, consumer-driven call the port never
        makes on connect; calling it twice against a schema already applied
        must not raise."""
        store = PostgresStore(dsn=postgres_dsn)
        try:
            await store.migrate()
            await store.migrate()
        finally:
            await store.close()

    async def test_duplicate_append_fails_on_the_primary_key(
        self, pg_pool: asyncpg.Pool, pg_store: PostgresStore
    ) -> None:
        """The literal acceptance criterion: a duplicate append fails on the
        ``(run_id, seq)`` primary key constraint itself, not on an
        application-level read-then-write check racing against it."""
        run_id = new_run_id()
        record = _record(run_id, 1)
        await pg_store.append(run_id, 1, record)

        with pytest.raises(asyncpg.UniqueViolationError):
            await pg_pool.execute(
                "INSERT INTO psych_records (run_id, seq, record) VALUES ($1, $2, $3::jsonb)",
                run_id,
                1,
                record.model_dump_json(),
            )

    async def test_ten_concurrent_workers_yield_exactly_one_claim_winner(
        self, pg_store: PostgresStore
    ) -> None:
        """The literal acceptance criterion: lease claim under ten concurrent
        workers yields exactly one winner, against real Postgres rather than
        the in-memory adapter's single ``asyncio.Lock``."""
        run_id = new_run_id()
        await pg_store.create_run(_header(run_id))
        now = datetime.now(UTC)
        workers = [new_worker_id() for _ in range(10)]

        results = await asyncio.gather(*(pg_store.claim(worker, now, 30.0) for worker in workers))
        winners = [result for result in results if result == run_id]
        assert len(winners) == 1
        assert results.count(None) == len(workers) - 1
