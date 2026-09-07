"""The four numbers a platform team asks for before choosing to build instead.

Each is compared against a committed baseline and fails past its tolerance. See
`benchmarks/README.md` for what each measures and why the numbers belong to the
machine that produced them.

Tolerances are wide on purpose. A shared runner is noisy, and a benchmark that
fails on a busy neighbour teaches people to rerun the job until it passes, which
is worse than having no benchmark at all. Tighten them once the variance on your
own runner is known.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest

import psych_runtime
from psych_runtime.core.ids import WorkerId, new_worker_id
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel

pytestmark = pytest.mark.benchmark

SCOPE = psych_runtime.Scope(tenant="acme", principal="user-1")


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    """The store under measurement.

    In-memory by default, which measures the runtime rather than a database.
    `PSYCH_BENCH_POSTGRES_DSN` swaps in real PostgreSQL, which is the only
    configuration in which the scaling ceiling means anything: an in-memory
    store has no lock contention, no round trip and no `claim()` to bottleneck.
    """
    dsn = os.getenv("PSYCH_BENCH_POSTGRES_DSN")
    if not dsn:
        yield InMemoryStore()
        return

    from psych_runtime.store.postgres import PostgresStore

    postgres = PostgresStore(dsn=dsn)
    await postgres.migrate()
    yield postgres
    await postgres.close()


def spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="bench",
        instructions="Answer briefly.",
        model=psych_runtime.ModelRef(model="fake-standard"),
    )


async def published(store: Store) -> psych_runtime.Version:
    return await psych_runtime.publish(store, spec())


class TestAdmitting:
    async def test_dispatch_throughput(self, store: Store, benchmark: Callable[..., None]) -> None:
        """Runs admitted per second. This is what a request handler pays.

        Admission is the only Psych call on a hot HTTP path, so its cost bounds
        how many conversations a front end can start per second regardless of
        how many Workers are behind it.
        """
        version = await published(store)
        count = 200

        started = time.perf_counter()
        for index in range(count):
            await psych_runtime.dispatch(
                store, version, SCOPE, input={"message": "hi"}, idempotency_key=f"bench-{index}"
            )
        elapsed = time.perf_counter() - started

        benchmark(
            "dispatch_throughput",
            count / elapsed,
            unit="runs/sec",
            higher_is_better=True,
            tolerance=0.40,
        )


class TestClaiming:
    async def test_claim_latency_under_contention(
        self, store: Store, benchmark: Callable[..., None]
    ) -> None:
        """How long one claim takes while others compete for the same Runs.

        `claim()` is the queue, so this is the latency between work existing and
        a Worker holding it. Measured with more claimants than Runs, which is
        the shape that produces contention rather than the one that hides it.
        """
        version = await published(store)
        runs = 100
        for index in range(runs):
            await psych_runtime.dispatch(store, version, SCOPE, idempotency_key=f"claim-{index}")

        workers = [new_worker_id() for _ in range(8)]

        async def claim_all(worker: WorkerId) -> int:
            taken = 0
            while await store.claim(worker, datetime.now(UTC), 30.0) is not None:
                taken += 1
            return taken

        started = time.perf_counter()
        claimed = await asyncio.gather(*(claim_all(worker) for worker in workers))
        elapsed = time.perf_counter() - started

        assert sum(claimed) == runs, "every Run must be claimed exactly once"

        benchmark(
            "claim_latency_ms",
            (elapsed / runs) * 1000,
            unit="ms/claim",
            higher_is_better=False,
            tolerance=0.60,
        )


class TestStorage:
    async def test_log_bytes_per_turn(self, store: Store, benchmark: Callable[..., None]) -> None:
        """What one turn costs to keep. Multiply by volume and retention.

        The log is the only copy of what happened, so this is the storage bill
        for auditability. Measured as serialised record bytes rather than as
        rows, because a row count says nothing about a database's disk.
        """
        model = (
            FakeModel()
            .turn(text="Looking.", tool_calls=[("noop", {"value": "x"})])
            .turn(text="Done.")
        )

        async def noop(value: str) -> dict[str, str]:
            """Do nothing, cheaply."""
            return {"value": value}

        async with psych_runtime.session(model, store=store, tools=[noop], tenant="acme") as s:
            agent = psych_runtime.AgentSpec(
                name="bench",
                model=psych_runtime.ModelRef(model="fake-standard"),
                tools=(psych_runtime.CodeTool(name="noop"),),
            )
            view = await s.ask(agent, "go", scope=SCOPE)

        records = await psych_runtime.records(store, view.run_id, scope=SCOPE)
        total = sum(len(record.model_dump_json().encode()) for record in records)
        turns = sum(1 for record in records if record.type == "turn_started")

        benchmark(
            "log_bytes_per_turn",
            total / max(turns, 1),
            unit="bytes/turn",
            higher_is_better=False,
            tolerance=0.30,
        )


class TestScaling:
    async def test_worker_scaling_ceiling(
        self, store: Store, benchmark: Callable[..., None]
    ) -> None:
        """Claims per second across many Workers: where `claim()` becomes the wall.

        The build-versus-buy number. It is the difference between "add another
        Worker" and "shard the store", and it is the first thing a platform team
        asks because nobody publishes it.

        Against the in-memory store this measures the runtime's own floor: one
        lock, no network. Against PostgreSQL it measures what it says.
        """
        version = await published(store)
        runs = 300
        for index in range(runs):
            await psych_runtime.dispatch(store, version, SCOPE, idempotency_key=f"scale-{index}")

        workers = [new_worker_id() for _ in range(32)]

        async def drain(worker: WorkerId) -> int:
            taken = 0
            while await store.claim(worker, datetime.now(UTC), 30.0) is not None:
                taken += 1
            return taken

        started = time.perf_counter()
        results = await asyncio.gather(*(drain(worker) for worker in workers))
        elapsed = time.perf_counter() - started

        assert sum(results) == runs

        benchmark(
            "claims_per_second_32_workers",
            runs / elapsed,
            unit="claims/sec",
            higher_is_better=True,
            tolerance=0.40,
        )
