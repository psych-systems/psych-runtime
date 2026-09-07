"""DESIGN.md §23 item 8: the same Spec, the same scripted model, run
identically on all four store adapters.

Not four separate contract-suite passes -- ``psych_runtime/store``'s own contract
suite (``tests/functional/test_store_*.py``) already proves each adapter
alone satisfies the ``Store`` port. What this checks is the runtime *through*
each store: one Spec, publish through dispatch through report, driven byte
for byte the same way against ``InMemoryStore``, real PostgreSQL, real MySQL
and real DynamoDB Local, and every one of them must produce the same
terminal state, the same tool calls and the same usage totals. A subtle
difference in how an adapter round-trips a datetime or a Decimal shows up
here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.dsns import dynamodb_endpoint, mysql_dsn, postgres_dsn
from app.scenarios.support import SCOPE_A, drive, lookup_registry, report_dict, support_agent_spec
from psych_runtime.store.dynamodb import DynamoDBStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.mysql import MySQLStore
from psych_runtime.store.port import Store
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.testing.fake_model import FakeModel

INFO = ScenarioInfo(
    id="four-stores",
    title="Four stores, one behaviour",
    proves=(
        "The same Spec, run against the same scripted model, produces the "
        "same terminal state, tool calls and usage totals on InMemoryStore, "
        "real PostgreSQL, real MySQL and real DynamoDB Local."
    ),
    design_ref="§23.8",
    requires=("postgres", "mysql", "dynamodb"),
)

_DYNAMODB_LOCAL_CREDENTIAL = "dummy"
"""DynamoDB Local checks nothing about a credential's identity, only that it
looks like one -- see ``tests/functional/test_store_dynamodb.py``'s own
fixture, which uses the same value rather than relying on whatever the
process environment happens to export (a real AWS profile would otherwise
be picked up by aioboto3's default credential chain and pointed at a
localhost endpoint it was never meant for)."""


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    problems: list[str] = []

    pg_dsn = postgres_dsn()
    try:
        async with PostgresStore(dsn=pg_dsn) as pg:
            await pg.migrate()
    except Exception as err:
        problems.append(f"PostgreSQL at {pg_dsn!r}: {err}")

    my_dsn = mysql_dsn()
    try:
        async with MySQLStore(dsn=my_dsn) as my:
            await my.migrate()
    except Exception as err:
        problems.append(f"MySQL at {my_dsn!r}: {err}")

    endpoint = dynamodb_endpoint()
    try:
        probe_prefix = f"avail-{datetime.now(UTC).timestamp():.6f}".replace(".", "")
        async with DynamoDBStore(
            endpoint_url=endpoint,
            table_prefix=probe_prefix,
            aws_access_key_id=_DYNAMODB_LOCAL_CREDENTIAL,
            aws_secret_access_key=_DYNAMODB_LOCAL_CREDENTIAL,
        ) as ddb:
            await ddb.ensure_tables()
            await ddb.drop_tables()
    except Exception as err:
        problems.append(f"DynamoDB Local at {endpoint!r}: {err}")

    if problems:
        joined = "; ".join(problems)
        return f"not all four stores are reachable -- run scripts/dev-services.sh start: {joined}"
    return None


async def _memory_store() -> Store:
    return InMemoryStore()


async def _postgres_store() -> Store:
    store = PostgresStore(dsn=postgres_dsn())
    await store.migrate()
    pool = await store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE psych_records, psych_versions, psych_runs")
    return store


async def _mysql_store() -> Store:
    store = MySQLStore(dsn=mysql_dsn())
    await store.migrate()
    pool = await store._ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        for table in ("records", "versions", "runs"):
            await cur.execute(f"TRUNCATE TABLE {table}")
        await conn.commit()
    return store


async def _dynamodb_store() -> Store:
    prefix = f"playground-{datetime.now(UTC).timestamp():.6f}".replace(".", "")
    store = DynamoDBStore(
        endpoint_url=dynamodb_endpoint(),
        table_prefix=prefix,
        aws_access_key_id=_DYNAMODB_LOCAL_CREDENTIAL,
        aws_secret_access_key=_DYNAMODB_LOCAL_CREDENTIAL,
    )
    await store.ensure_tables()
    return store


_BUILDERS: tuple[tuple[str, Callable[[], Awaitable[Store]]], ...] = (
    ("memory", _memory_store),
    ("postgres", _postgres_store),
    ("mysql", _mysql_store),
    ("dynamodb", _dynamodb_store),
)


async def _drive_one_store(store: Store, name: str) -> dict[str, Any]:
    """Runs the identical Spec and scripted model against one store and
    returns just what is compared: terminal state, tool calls and usage."""
    model = (
        FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 has shipped.")
    )
    run_id = await drive(store, support_agent_spec(), model, lookup_registry(), scope=SCOPE_A)
    report = await psych_runtime.report(store, run_id)
    outcomes = [(c.tool, c.outcome.value if c.outcome else None) for c in report.tool_calls]
    return {
        "run_id": f"{name}:{run_id}",
        "terminal_state": report.terminal_state.value if report.terminal_state else None,
        "tool_calls": outcomes,
        "usage": report_dict(report)["totals"]["usage"],
    }


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    per_store: dict[str, dict[str, Any]] = {}
    stores_to_close: list[Store] = []

    try:
        for name, builder in _BUILDERS:
            await emit("run", f"driving the same Spec and scripted model against {name}")
            store = await builder()
            stores_to_close.append(store)
            per_store[name] = await _drive_one_store(store, name)
            if isinstance(store, DynamoDBStore):
                await store.drop_tables()
    finally:
        for store in stores_to_close:
            close = getattr(store, "close", None)
            if close is not None:
                await close()

    await emit("compare", "comparing every store's report against the first")
    baseline_name, baseline = next(iter(per_store.items()))
    for name, result in per_store.items():
        checks.require(
            f"{name} reached the same terminal_state as {baseline_name}",
            result["terminal_state"] == baseline["terminal_state"],
            f"{name}={result['terminal_state']!r} vs "
            f"{baseline_name}={baseline['terminal_state']!r}",
        )
        checks.require(
            f"{name} recorded the same tool calls, in the same order, as {baseline_name}",
            result["tool_calls"] == baseline["tool_calls"],
            f"{name}={result['tool_calls']!r} vs {baseline_name}={baseline['tool_calls']!r}",
        )
        checks.require(
            f"{name} reported the same usage totals as {baseline_name}",
            result["usage"] == baseline["usage"],
            f"{name}={result['usage']!r} vs {baseline_name}={baseline['usage']!r}",
        )
    checks.require(
        "all four stores were actually exercised",
        set(per_store) == {"memory", "postgres", "mysql", "dynamodb"},
        f"stores exercised: {sorted(per_store)}",
    )

    return checks.result(
        "one Spec, one scripted model, four store adapters, one behaviour",
        run_ids=[result["run_id"] for result in per_store.values()],
        report={"per_store": per_store},
    )
