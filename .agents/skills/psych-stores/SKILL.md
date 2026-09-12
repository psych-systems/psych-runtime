---
name: psych-stores
description: >-
  Choose, wire and migrate a Psych `Store`: the in-memory, PostgreSQL, MySQL and
  DynamoDB adapters, the append-only log with conditional writes, why the queue
  IS the store (no broker), running migrations, and adding your own adapter
  against the shared contract suite. Use whenever someone picks a database for
  Psych, asks whether Runs survive a restart, wires PostgresStore / MySQLStore /
  DynamoDBStore, asks about migrations, sees a SeqConflict, wonders why there are
  no transactions or joins, or asks how Runs get queued to Workers. Read before
  choosing a store, because MemoryStore is a real Store that keeps nothing across
  a process, and because the contract suite (not the adapter) is what makes a new
  backend correct.
---

# Stores

One port over an **append-only log with conditional writes**. Every adapter
needs the same three primitives: append at an expected sequence, claim under a
condition, and read a range. That is what lets one persistence model serve
Postgres, MySQL and DynamoDB with no second code path.

## Four adapters, all shipped

```python
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.store.mysql import MySQLStore
from psych_runtime.store.dynamodb import DynamoDBStore

store = MemoryStore()  # tests, scripts, single process

store = PostgresStore(dsn="postgresql://localhost/psych")  # or pool=<asyncpg.Pool>
await store.migrate()

store = MySQLStore(dsn="mysql://...")  # or pool=<aiomysql.Pool>
await store.migrate()

store = DynamoDBStore(
    endpoint_url=None,  # set for DynamoDB Local
    region_name="us-east-1",
    table_prefix="psych",
)
```

`PostgresStore` takes **exactly one** of `dsn` or `pool`. Passing your own pool
is the right move in an application that already has one.

Install the extra you need: `psych-runtime[postgres]`,
`psych-runtime[mysql]`, `psych-runtime[dynamodb]`, or `psych-runtime[all]`.

## MemoryStore is real, and keeps nothing

It is a genuine `Store` implementation, not a stub, guarded by one lock. It is
correct and it is entirely in-process: nothing survives the process. Right for a
first run, a test and a script. Wrong for anything whose Runs must outlive it.

Every adapter implements the port **structurally**. There is no base class to
inherit, because the port is a `Protocol`.

## The queue is the store

A runnable Run is one whose lease is unheld or expired. `claim()` is the queue.
There is no broker, no separate queue table to keep in step, and nothing to
reconcile when the two disagree, because there is no two.

```python
worker = psych_runtime.Worker(store, runtime, concurrency=8, lease_seconds=30.0)
await worker.run()
```

Scale by running more Workers, in more processes, on more machines. They all
claim from the same store.

## Conditional writes only

No transactions, no joins, no `SELECT ... FOR UPDATE`. That constraint is what
makes DynamoDB a first-class target rather than a compromise, and it is why a
`SeqConflict` is a normal outcome rather than an error: two writers raced for
one sequence number and one lost. The loser re-reads and retries.

## Migrations

Sequential and forward-only, under `psych_runtime/store/migrations/` (Postgres) and
`psych_runtime/store/migrations_mysql/`. `await store.migrate()` applies them. DynamoDB
creates its tables instead.

Adding one: check the highest existing number first, and never edit a migration
that has shipped.

## Retention is yours

Psych does not expire, garbage-collect or size-cap what accumulates. That
lifecycle policy is yours, the same as the database itself. Psych uses your
database through this port and never provisions or migrates it beyond its own
tables.

## Adding an adapter

The contract suite is the deliverable as much as the adapter. Divergent
implementations with no shared test are divergent behaviours waiting to be
discovered in production.

```python
# tests/functional/test_store_mystore.py
from psych_runtime.store.contract import StoreContractSuite


class TestMyStore(StoreContractSuite):
    @pytest.fixture
    async def store(self):
        yield MyStore(...)
```

Nothing else in the suite changes. That is what keeps every implementation
honest against one definition of correct rather than an independent reading of
the design per adapter.

**No mocked store.** Store tests run against real Postgres, real MySQL and real
DynamoDB Local. The in-memory adapter tests other components and never proves
the contract.

## The port

`append(run_id, seq, record)`, `read(run_id, after, limit)`, `head(run_id)`,
`put_version`, `get_version`, `create_run`, `get_run`, `claim`, `renew`,
`release`, `settle_inline`, `set_runnable_at`, `expired_leases`,
`overdue_deadlines`. Plus an optional `Queue` and the separate `BlobStore` port
(see `psych-blobs`).

`settle_inline(run_id)` is the one settle that is addressed by Run rather than
by lease, and the only one. A `NESTED` Run — a subagent executed inline, or
anything else driven by whoever dispatched it — never has a lease, so
`release` matches nothing and would leave a header saying `nested` beside a log
saying the Run finished. Its state is its authorisation: only `NESTED` is
touched, because only a `NESTED` Run has no competing writer. It returns
whether it changed anything, is idempotent, never makes a Run claimable, and
never writes to the log.

## The same Spec on all four

One of Psych's ten definition-of-done items: the same Spec runs identically
against all four adapters. If yours behaves differently, the contract suite is
where to prove it.

## Gotchas

- **`await store.migrate()` before the first Run,** not lazily on first use.
- **DynamoDB caps one item at 400KB.** An oversized tool result needs a
  `BlobStore`; see `psych-blobs`.
- **Lease length is a Worker setting, not a store one.** Too short and a slow
  model call loses its lease; too long and a dead Worker's Runs sit idle.
- **In development this repository ships `scripts/dev-services.sh`,** which runs
  the three databases as ordinary processes where there is no container runtime.
