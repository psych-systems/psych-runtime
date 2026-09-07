"""The MySQL Store against the shared contract suite.

DESIGN.md §22: store tests run against real MySQL/MariaDB, never a mock. This
module applies ``psych_runtime/store/migrations_mysql/`` once per session through a
throwaway event loop of its own (never one pytest-asyncio hands out, so it can
never end up sharing a loop with a test's aiomysql pool), then hands each test
a ``MySQLStore`` that owns its own pool for that test's function-scoped loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import aiomysql
import pytest

from psych_runtime.store.contract import StoreContractSuite
from psych_runtime.store.mysql import MySQLStore
from psych_runtime.store.port import Store

pytestmark = pytest.mark.mysql


async def _apply_migrations(dsn: str) -> None:
    """Set the schema up through the adapter's own ``migrate()``.

    This used to re-implement it: glob the same directory, split each file on
    the statement terminator, execute the parts. Two copies of one procedure
    drift, and this pair did -- the copy here suppressed warnings so a rerun
    was survivable, which made the fixture look idempotent while the real
    ``migrate()`` had no version ledger at all. Calling the shipped path means
    the schema these tests run against is the schema a consumer gets.
    """
    store = MySQLStore(dsn=dsn)
    try:
        await store.migrate()
    finally:
        await store.close()


async def _reset_tables(dsn: str) -> None:
    """Empty every table this adapter owns.

    ``claim()``, ``expired_leases()`` and ``overdue_deadlines()`` scan the
    whole ``runs`` table rather than one run_id, so unlike the read/head/get
    methods they cannot lean on unique ids for isolation: a run another test
    left behind claimable is a candidate for this test's claim() too. Rather
    than assume anything about what earlier runs of this suite left in a
    database that is never wiped automatically, every test starts from an
    empty table (DESIGN.md §22, "every test cleans up after itself").
    """
    parsed = urlsplit(dsn)
    conn = await aiomysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
        db=parsed.path.lstrip("/"),
        autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            for table in ("runs", "records", "versions"):
                await cur.execute(f"DELETE FROM {table}")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def _mysql_schema(mysql_dsn: str) -> str:
    """Apply migrations once per session and hand back the DSN.

    A plain (non-async) fixture on purpose: ``asyncio.run`` opens and fully
    closes its own event loop before returning, so this never registers with
    pytest-asyncio's loop management and never risks sharing a loop with a
    test's own pool.
    """
    asyncio.run(_apply_migrations(mysql_dsn))
    return mysql_dsn


class TestMySQLMigrations:
    async def test_migrate_is_idempotent(self, mysql_dsn: str) -> None:
        """The Postgres adapter has had this test since it was written and
        this one did not, which is the whole reason a MariaDB-only
        ``ADD COLUMN IF NOT EXISTS`` shipped in a file called MySQL: nothing
        ever ran ``MySQLStore.migrate()`` twice, and the fixture that stood in
        for it suppressed the error a rerun would have raised."""
        store = MySQLStore(dsn=mysql_dsn)
        try:
            await store.migrate()
            await store.migrate()
        finally:
            await store.close()


class TestMySQLStore(StoreContractSuite):
    @pytest.fixture
    async def store(self, _mysql_schema: str) -> AsyncIterator[Store]:
        await _reset_tables(_mysql_schema)
        mysql_store = MySQLStore(dsn=_mysql_schema)
        try:
            yield mysql_store
        finally:
            await mysql_store.close()
