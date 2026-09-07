"""The Store contract suite.

DESIGN.md §7 and §22: four Store implementations with no shared test is four
divergent behaviours discovered in production. This module is that shared
test. It is not itself collected by pytest (it defines no ``store`` fixture
and its class name does not start with ``Test``); an adapter is added to the
suite by subclassing ``StoreContractSuite`` in a file under
``tests/functional/`` and registering a ``store`` fixture that returns a
fresh, empty instance of the adapter under test. Nothing else changes.

## What "fresh" means across adapters

Every test here creates its own ``Run`` ids, ``Version`` hashes and
idempotency keys via the helpers in this module, so tests do not depend on
the store being empty at the start of each test. This matters for adapters
whose ``store`` fixture points at a real, possibly shared database rather than
a process that is thrown away afterward: the in-memory adapter gets a new
instance per test, but a Postgres or DynamoDB fixture may reasonably scope a
connection at module or session level and rely on unique ids for isolation
instead of a wipe between tests.

## Why this covers `claim()` by racing a whole Run rather than one call

``Store.claim`` takes no ``run_id``; it hands back whatever is runnable. The
port docstring, DESIGN.md §7, and the ticket that created this suite all frame
contention as "N workers race for a Run", so the contention tests create
exactly the Runs a scenario needs and let ``claim()`` pick among them, then
assert on which Run ids came back rather than pinning the picking order.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from psych_runtime.core.errors import RunNotFound, SeqConflict
from psych_runtime.core.ids import RunId, VersionHash, new_run_id, new_worker_id
from psych_runtime.core.records import AbortRequested, Record
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef
from psych_runtime.core.version import Version, publish
from psych_runtime.store.port import RunHeader, RunState, Store

__all__ = ["StoreContractSuite"]

_DEFAULT_LEASE_SECONDS: Final = 30.0
_DEFAULT_DEADLINE_SECONDS: Final = 3_600.0


def _scope(tenant: str = "acme") -> Scope:
    return Scope(tenant=tenant)


def _record(
    run_id: RunId, seq: int, *, scope: Scope | None = None, at: datetime | None = None
) -> Record:
    """The cheapest legal Record: only the fields every Record carries.

    The contract suite is testing the Store, not the reducer, so which Record
    subtype fills a slot never matters. ``AbortRequested`` has no fields beyond
    the base's.
    """
    return AbortRequested(
        run_id=run_id,
        seq=seq,
        at=at if at is not None else datetime.now(UTC),
        scope=scope if scope is not None else _scope(),
    )


def _header(
    run_id: RunId,
    *,
    scope: Scope | None = None,
    state: RunState = RunState.RUNNABLE,
    idempotency_key: str | None = None,
    runnable_at: datetime | None = None,
    deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    created_at: datetime | None = None,
) -> RunHeader:
    created = created_at if created_at is not None else datetime.now(UTC)
    return RunHeader(
        run_id=run_id,
        scope=scope if scope is not None else _scope(),
        version_hash=VersionHash("sha256:" + "0" * 64),
        state=state,
        created_at=created,
        deadline_at=created + timedelta(seconds=deadline_seconds),
        idempotency_key=idempotency_key,
        runnable_at=runnable_at,
    )


def _version(name: str = "contract-suite-agent") -> Version:
    spec = AgentSpec(name=name, model=ModelRef(model="test-model"))
    return publish(spec)


class StoreContractSuite:
    """Subclass this and provide a ``store`` fixture yielding an empty ``Store``.

    Every test method is a coroutine; ``pytest-asyncio`` is configured with
    ``asyncio_mode = "auto"`` at the project level, so subclasses need no
    additional marker.
    """

    @pytest.fixture
    def store(self) -> Store:
        raise NotImplementedError(
            "subclasses of StoreContractSuite must override the `store` fixture "
            "to return the adapter under test"
        )

    # -- the log: append, read, head ----------------------------------------

    async def test_conditional_append_conflict_under_concurrency(self, store: Store) -> None:
        """Two writers racing for the same seq: exactly one wins, one SeqConflict."""
        run_id = new_run_id()
        results = await asyncio.gather(
            store.append(run_id, 1, _record(run_id, 1)),
            store.append(run_id, 1, _record(run_id, 1)),
            return_exceptions=True,
        )
        successes = [result for result in results if result is None]
        conflicts = [result for result in results if isinstance(result, SeqConflict)]
        assert len(successes) == 1
        assert len(conflicts) == 1

    async def test_conditional_append_conflict_names_run_and_seq(self, store: Store) -> None:
        run_id = new_run_id()
        await store.append(run_id, 1, _record(run_id, 1))
        with pytest.raises(SeqConflict) as excinfo:
            await store.append(run_id, 1, _record(run_id, 1))
        assert excinfo.value.run_id == run_id
        assert excinfo.value.seq == 1

    async def test_gapless_sequencing_across_many_appends(self, store: Store) -> None:
        run_id = new_run_id()
        for seq in range(1, 51):
            await store.append(run_id, seq, _record(run_id, seq))
        records = await store.read(run_id)
        assert [record.seq for record in records] == list(range(1, 51))

    async def test_head_returns_zero_for_unknown_run(self, store: Store) -> None:
        assert await store.head(new_run_id()) == 0

    async def test_head_returns_the_true_max(self, store: Store) -> None:
        run_id = new_run_id()
        for seq in range(1, 11):
            await store.append(run_id, seq, _record(run_id, seq))
        assert await store.head(run_id) == 10

    async def test_range_read_crosses_pagination_boundary(self, store: Store) -> None:
        """250 records, well past a typical 100-item internal page.

        An adapter that fetches internally in pages of 100 must reassemble
        three pages into one gapless, non-duplicated result here. The
        in-memory adapter does not paginate at all, so this is a no-op for it
        and a real exercise for postgres, mysql and dynamodb.
        """
        run_id = new_run_id()
        total = 250
        for seq in range(1, total + 1):
            await store.append(run_id, seq, _record(run_id, seq))

        records = await store.read(run_id)
        assert [record.seq for record in records] == list(range(1, total + 1))

        # A bounded window that itself straddles two internal pages.
        windowed = await store.read(run_id, after=90, limit=120)
        assert [record.seq for record in windowed] == list(range(91, 211))

    async def test_read_after_boundaries(self, store: Store) -> None:
        run_id = new_run_id()
        for seq in range(1, 21):
            await store.append(run_id, seq, _record(run_id, seq))
        head = await store.head(run_id)

        assert [r.seq for r in await store.read(run_id, after=0)] == list(range(1, 21))
        assert [r.seq for r in await store.read(run_id, after=10)] == list(range(11, 21))
        assert [r.seq for r in await store.read(run_id, after=head)] == []
        assert [r.seq for r in await store.read(run_id, after=head + 100)] == []

    async def test_read_respects_limit(self, store: Store) -> None:
        run_id = new_run_id()
        for seq in range(1, 21):
            await store.append(run_id, seq, _record(run_id, seq))
        page = await store.read(run_id, after=0, limit=5)
        assert [r.seq for r in page] == [1, 2, 3, 4, 5]

    # -- versions -------------------------------------------------------------

    async def test_version_put_then_get_round_trips(self, store: Store) -> None:
        version = _version()
        await store.put_version(version)
        fetched = await store.get_version(version.hash)
        assert fetched is not None
        assert fetched.hash == version.hash
        assert fetched.spec == version.spec

    async def test_put_version_twice_is_a_no_op(self, store: Store) -> None:
        version = _version()
        await store.put_version(version)
        await store.put_version(version)  # must not raise
        fetched = await store.get_version(version.hash)
        assert fetched is not None
        assert fetched.hash == version.hash

    async def test_get_unknown_version_returns_none(self, store: Store) -> None:
        assert await store.get_version(VersionHash("sha256:" + "f" * 64)) is None

    # -- run creation -----------------------------------------------------------

    async def test_idempotent_run_creation_returns_existing_header(self, store: Store) -> None:
        key = f"idem-{new_run_id()}"
        first_id = new_run_id()
        second_id = new_run_id()

        created = await store.create_run(_header(first_id, idempotency_key=key))
        retried = await store.create_run(_header(second_id, idempotency_key=key))

        assert created.run_id == first_id
        assert retried.run_id == first_id  # the existing header, not a new Run
        assert await store.get_run(second_id) is None

    async def test_an_idempotency_key_is_scoped_to_its_tenant(self, store: Store) -> None:
        """The same key, chosen independently by two tenants, admits two Runs.

        Keys are consumer-chosen ("order-1234", a webhook delivery id) and
        collide across tenants by nature. Keyed globally, the second tenant's
        dispatch returned the first tenant's ``run_id``, and every read entry
        point takes a bare ``run_id`` -- so a guessed key was a cross-tenant
        read of a whole conversation.
        """
        key = "order-1234"
        acme = await store.create_run(
            _header(RunId("run_acme"), idempotency_key=key, scope=_scope("acme"))
        )
        globex = await store.create_run(
            _header(RunId("run_globex"), idempotency_key=key, scope=_scope("globex"))
        )
        assert acme.run_id == RunId("run_acme")
        assert globex.run_id == RunId("run_globex")
        assert globex.scope.tenant == "globex"

        # Within one tenant the key still deduplicates, which is the whole
        # point of having it.
        again = await store.create_run(
            _header(RunId("run_acme_2"), idempotency_key=key, scope=_scope("acme"))
        )
        assert again.run_id == RunId("run_acme")

    async def test_create_run_with_duplicate_run_id_does_not_clobber(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id, state=RunState.RUNNABLE))
        # A second admission under the same run_id, differing in state. If this
        # clobbered the first, a Run already claimed could be reset to runnable
        # underneath its holder.
        await store.create_run(_header(run_id, state=RunState.SETTLED))

        fetched = await store.get_run(run_id)
        assert fetched is not None
        assert fetched.state == RunState.RUNNABLE

    async def test_get_run_returns_none_for_unknown_run(self, store: Store) -> None:
        assert await store.get_run(new_run_id()) is None

    # -- scope isolation ----------------------------------------------------

    async def test_scope_isolation_across_runs(self, store: Store) -> None:
        """Reading one Run's log or header never surfaces another Run's scope,
        and each faithfully carries the scope it was written with (DESIGN.md
        §14). ``claim()`` itself takes no scope, by design, so isolation is
        checked on what it is possible to check: the headers and the log.
        """
        run_a, run_b = new_run_id(), new_run_id()
        scope_a, scope_b = _scope("tenant-a"), _scope("tenant-b")

        await store.create_run(_header(run_a, scope=scope_a))
        await store.create_run(_header(run_b, scope=scope_b))
        await store.append(run_a, 1, _record(run_a, 1, scope=scope_a))
        await store.append(run_b, 1, _record(run_b, 1, scope=scope_b))

        records_a = await store.read(run_a)
        records_b = await store.read(run_b)
        assert {record.run_id for record in records_a} == {run_a}
        assert all(record.scope.tenant == "tenant-a" for record in records_a)
        assert {record.run_id for record in records_b} == {run_b}
        assert all(record.scope.tenant == "tenant-b" for record in records_b)

        header_a = await store.get_run(run_a)
        header_b = await store.get_run(run_b)
        assert header_a is not None
        assert header_b is not None
        assert header_a.scope.tenant == "tenant-a"
        assert header_b.scope.tenant == "tenant-b"

    # -- leases: claim, renew, release ---------------------------------------

    async def test_claim_under_contention_exactly_one_winner(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        workers = [new_worker_id() for _ in range(12)]

        results = await asyncio.gather(
            *(store.claim(worker, now, _DEFAULT_LEASE_SECONDS) for worker in workers)
        )
        winners = [result for result in results if result == run_id]
        assert len(winners) == 1
        assert results.count(None) == len(workers) - 1

    async def test_claim_returns_none_when_nothing_runnable(self, store: Store) -> None:
        assert await store.claim(new_worker_id(), datetime.now(UTC), _DEFAULT_LEASE_SECONDS) is None

    async def test_claim_skips_a_run_with_a_live_lease(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        first = await store.claim(new_worker_id(), now, _DEFAULT_LEASE_SECONDS)
        assert first == run_id

        second = await store.claim(
            new_worker_id(), now + timedelta(seconds=1), _DEFAULT_LEASE_SECONDS
        )
        assert second is None

    async def test_lease_expiry_then_reclaim_by_a_different_worker(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        holder = new_worker_id()
        assert await store.claim(holder, now, 1.0) == run_id

        past_expiry = now + timedelta(seconds=5)
        reclaimer = new_worker_id()
        reclaimed = await store.claim(reclaimer, past_expiry, _DEFAULT_LEASE_SECONDS)
        assert reclaimed == run_id

        header = await store.get_run(run_id)
        assert header is not None
        assert header.lease_holder == reclaimer

    async def test_reclaim_increments_attempt_count(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        await store.claim(new_worker_id(), now, 1.0)
        header_after_first = await store.get_run(run_id)
        assert header_after_first is not None
        assert header_after_first.attempt_count == 1

        await store.claim(new_worker_id(), now + timedelta(seconds=5), _DEFAULT_LEASE_SECONDS)
        header_after_reclaim = await store.get_run(run_id)
        assert header_after_reclaim is not None
        assert header_after_reclaim.attempt_count == 2

    async def test_renew_true_for_the_holder(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        holder = new_worker_id()
        await store.claim(holder, now, _DEFAULT_LEASE_SECONDS)

        assert await store.renew(run_id, holder, now + timedelta(seconds=5), _DEFAULT_LEASE_SECONDS)

    async def test_renew_false_for_a_non_holder(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        holder = new_worker_id()
        await store.claim(holder, now, _DEFAULT_LEASE_SECONDS)

        stranger = new_worker_id()
        renewed = await store.renew(
            run_id, stranger, now + timedelta(seconds=5), _DEFAULT_LEASE_SECONDS
        )
        assert renewed is False

    async def test_renew_false_for_a_run_that_was_never_claimed(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        renewed = await store.renew(
            run_id, new_worker_id(), datetime.now(UTC), _DEFAULT_LEASE_SECONDS
        )
        assert renewed is False

    async def test_renew_false_after_another_worker_reclaimed_the_expired_lease(
        self, store: Store
    ) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        original_holder = new_worker_id()
        await store.claim(original_holder, now, 1.0)

        past_expiry = now + timedelta(seconds=5)
        reclaimer = new_worker_id()
        assert await store.claim(reclaimer, past_expiry, _DEFAULT_LEASE_SECONDS) == run_id

        # The original holder, unaware it was reclaimed, tries to renew.
        renewed = await store.renew(
            run_id, original_holder, past_expiry + timedelta(seconds=1), _DEFAULT_LEASE_SECONDS
        )
        assert renewed is False

    async def test_release_drops_the_lease_and_sets_the_state(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        holder = new_worker_id()
        await store.claim(holder, now, _DEFAULT_LEASE_SECONDS)

        await store.release(run_id, holder, RunState.SETTLED)

        header = await store.get_run(run_id)
        assert header is not None
        assert header.state == RunState.SETTLED
        assert header.lease_holder is None
        assert header.lease_expires_at is None

    async def test_release_is_tolerant_of_a_non_holder(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        holder = new_worker_id()
        await store.claim(holder, now, _DEFAULT_LEASE_SECONDS)

        # Must not raise, and must not disturb the real holder's lease: by the
        # time a slow Attempt unwinds, another Worker may already own the Run.
        await store.release(run_id, new_worker_id(), RunState.SETTLED)

        header = await store.get_run(run_id)
        assert header is not None
        assert header.state == RunState.RUNNING
        assert header.lease_holder == holder

    # -- parking a Run until a time -------------------------------------------------

    async def test_set_runnable_at_parks_a_run_until_the_time_passes(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        wake_at = now + timedelta(hours=1)

        await store.set_runnable_at(run_id, wake_at)
        assert await store.claim(new_worker_id(), now, _DEFAULT_LEASE_SECONDS) is None

        after_wake = wake_at + timedelta(seconds=1)
        claimed = await store.claim(new_worker_id(), after_wake, _DEFAULT_LEASE_SECONDS)
        assert claimed == run_id

    async def test_set_runnable_at_none_clears_the_parking(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        now = datetime.now(UTC)
        await store.set_runnable_at(run_id, now + timedelta(hours=1))
        await store.set_runnable_at(run_id, None)

        claimed = await store.claim(new_worker_id(), now, _DEFAULT_LEASE_SECONDS)
        assert claimed == run_id

    async def test_set_runnable_at_on_unknown_run_raises(self, store: Store) -> None:
        with pytest.raises(RunNotFound):
            await store.set_runnable_at(new_run_id(), datetime.now(UTC))

    # -- supervision: expired_leases, overdue_deadlines ----------------------

    async def test_expired_leases_lists_exactly_the_runs_past_their_lease(
        self, store: Store
    ) -> None:
        now = datetime.now(UTC)
        expiring = {new_run_id() for _ in range(3)}
        for run_id in expiring:
            await store.create_run(_header(run_id))
        never_claimed = new_run_id()
        await store.create_run(_header(never_claimed))

        claimed_ids: set[RunId] = set()
        for _ in expiring:
            claimed = await store.claim(new_worker_id(), now, 1.0)
            assert claimed is not None
            claimed_ids.add(claimed)
        assert claimed_ids == expiring

        later = now + timedelta(seconds=5)
        expired = await store.expired_leases(later, limit=100)
        assert {header.run_id for header in expired} == expiring
        assert never_claimed not in {header.run_id for header in expired}

    async def test_expired_leases_respects_limit(self, store: Store) -> None:
        now = datetime.now(UTC)
        run_ids = [new_run_id() for _ in range(5)]
        for run_id in run_ids:
            await store.create_run(_header(run_id))
            claimed = await store.claim(new_worker_id(), now, 1.0)
            assert claimed is not None

        later = now + timedelta(seconds=5)
        limited = await store.expired_leases(later, limit=2)
        assert len(limited) == 2

    async def test_overdue_deadlines_lists_exactly_the_runs_past_their_deadline(
        self, store: Store
    ) -> None:
        now = datetime.now(UTC)
        overdue_id = new_run_id()
        healthy_id = new_run_id()
        await store.create_run(_header(overdue_id, created_at=now, deadline_seconds=1.0))
        await store.create_run(_header(healthy_id, created_at=now, deadline_seconds=3_600.0))

        checked_at = now + timedelta(seconds=5)
        overdue = await store.overdue_deadlines(checked_at, limit=100)
        overdue_ids = {header.run_id for header in overdue}
        assert overdue_id in overdue_ids
        assert healthy_id not in overdue_ids

    async def test_overdue_deadlines_excludes_settled_runs(self, store: Store) -> None:
        now = datetime.now(UTC)
        run_id = new_run_id()
        await store.create_run(_header(run_id, created_at=now, deadline_seconds=1.0))
        holder = new_worker_id()
        await store.claim(holder, now, _DEFAULT_LEASE_SECONDS)
        await store.release(run_id, holder, RunState.SETTLED)

        checked_at = now + timedelta(seconds=5)
        overdue = await store.overdue_deadlines(checked_at, limit=100)
        assert run_id not in {header.run_id for header in overdue}

    async def test_overdue_deadlines_respects_limit(self, store: Store) -> None:
        now = datetime.now(UTC)
        for _ in range(5):
            run_id = new_run_id()
            await store.create_run(_header(run_id, created_at=now, deadline_seconds=1.0))

        limited = await store.overdue_deadlines(now + timedelta(seconds=5), limit=2)
        assert len(limited) == 2

    async def test_clearing_the_parking_wakes_a_suspended_run(self, store: Store) -> None:
        """Resume is not a lease operation.

        Nobody holds a suspended Run, so ``release`` cannot be what makes it
        claimable again: an adapter that ignores a non-holder is right to, and a
        resume routed through it would strand the Run suspended forever with no
        error to show for it. Clearing the parking is the path back, and every
        adapter has to agree about that or resume works on one store and silently
        hangs on another.
        """
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        holder = new_worker_id()
        assert await store.claim(holder, datetime.now(UTC), _DEFAULT_LEASE_SECONDS) == run_id
        await store.release(run_id, holder, RunState.SUSPENDED)

        suspended = await store.get_run(run_id)
        assert suspended is not None
        assert suspended.state == RunState.SUSPENDED
        assert await store.claim(new_worker_id(), datetime.now(UTC), _DEFAULT_LEASE_SECONDS) is None

        await store.set_runnable_at(run_id, None)

        woken = await store.get_run(run_id)
        assert woken is not None
        assert woken.state == RunState.RUNNABLE
        assert woken.runnable_at is None
        claimed = await store.claim(new_worker_id(), datetime.now(UTC), _DEFAULT_LEASE_SECONDS)
        assert claimed == run_id

    async def test_parking_a_suspended_run_leaves_it_suspended(self, store: Store) -> None:
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        holder = new_worker_id()
        assert await store.claim(holder, datetime.now(UTC), _DEFAULT_LEASE_SECONDS) == run_id
        await store.release(run_id, holder, RunState.SUSPENDED)
        await store.set_runnable_at(run_id, datetime.now(UTC) + timedelta(seconds=60))

        parked = await store.get_run(run_id)
        assert parked is not None
        assert parked.state == RunState.SUSPENDED
        assert await store.claim(new_worker_id(), datetime.now(UTC), _DEFAULT_LEASE_SECONDS) is None

    async def test_waking_does_not_resurrect_a_settled_run(self, store: Store) -> None:
        """Waking a settled Run would bring a finished Run back to life, and
        waking a running one would lie about a Run another Worker holds."""
        run_id = new_run_id()
        await store.create_run(_header(run_id))
        holder = new_worker_id()
        assert await store.claim(holder, datetime.now(UTC), _DEFAULT_LEASE_SECONDS) == run_id
        await store.release(run_id, holder, RunState.SETTLED)

        await store.set_runnable_at(run_id, None)

        still_settled = await store.get_run(run_id)
        assert still_settled is not None
        assert still_settled.state == RunState.SETTLED
