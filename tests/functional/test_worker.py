"""The Worker: leases, deadlines, force-settlement and supervision.

DESIGN.md §8. These run against a real store rather than a stub, because the
whole point of a lease is that it is a conditional write two processes race for,
and a stub that hands out leases politely proves nothing.

The two mechanisms tested apart, because they are independent by design:

- The **lease** answers "does this Worker still claim to own this Run". Its
  expiry is what lets a different Worker reclaim after a crash.
- The **deadline** answers "has this Run run too long". It is enforced in the
  process holding the attempt and never consults the lease.

Conflate the two and the heartbeat renews a hung attempt's lease forever, the
expired-lease branch becomes unreachable, and the Run never recovers. Testing
them apart is what keeps that from being reintroduced quietly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from psych_runtime.core.ids import RunId, WorkerId, new_run_id
from psych_runtime.core.records import RECORD_ADAPTER, Record, TerminalState, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef
from psych_runtime.core.version import publish
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme")


async def admit(
    store: InMemoryStore, *, deadline_seconds: float = 900.0
) -> tuple[RunId, RunHeader]:
    spec = AgentSpec(name="a", model=ModelRef(model="fake-standard"))
    version = publish(spec)
    await store.put_version(version)
    run_id = new_run_id()
    now = datetime.now(UTC)
    header = RunHeader(
        run_id=run_id,
        scope=SCOPE,
        version_hash=version.hash,
        state=RunState.RUNNABLE,
        created_at=now,
        deadline_at=now + timedelta(seconds=deadline_seconds),
    )
    await store.create_run(header)
    await store.append(
        run_id,
        1,
        _admitted(run_id, version.hash, header.deadline_at),
    )
    stored = await store.get_run(run_id)
    assert stored is not None
    return run_id, stored


def _admitted(run_id: RunId, version_hash: str, deadline_at: datetime) -> Record:
    return RECORD_ADAPTER.validate_python(
        {
            "type": "run_admitted",
            "run_id": run_id,
            "seq": 1,
            "at": datetime.now(UTC),
            "scope": SCOPE,
            "version_hash": version_hash,
            "deadline_at": deadline_at,
        }
    )


class TestLease:
    async def test_exactly_one_of_many_workers_claims_a_run(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store)

        now = datetime.now(UTC)
        results = await asyncio.gather(
            *(store.claim(WorkerId(f"w{i}"), now, 30.0) for i in range(12))
        )
        winners = [r for r in results if r == run_id]
        assert len(winners) == 1

    async def test_a_held_lease_blocks_another_claim(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store)
        now = datetime.now(UTC)

        assert await store.claim(WorkerId("first"), now, 30.0) == run_id
        assert await store.claim(WorkerId("second"), now, 30.0) is None

    async def test_an_expired_lease_is_reclaimable_by_anyone(self) -> None:
        """The crash-recovery path, and the only one there is. A Worker that dies
        stops renewing and this is what picks its Run up."""
        store = InMemoryStore()
        run_id, _ = await admit(store)
        now = datetime.now(UTC)

        assert await store.claim(WorkerId("dead"), now, lease_seconds=1.0) == run_id
        later = now + timedelta(seconds=2)
        assert await store.claim(WorkerId("alive"), later, 30.0) == run_id

    async def test_renewal_succeeds_for_the_holder_and_fails_for_anyone_else(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store)
        now = datetime.now(UTC)
        await store.claim(WorkerId("holder"), now, 30.0)

        assert await store.renew(run_id, WorkerId("holder"), now, 30.0)
        assert not await store.renew(run_id, WorkerId("impostor"), now, 30.0)

    async def test_renewal_fails_after_another_worker_reclaimed(self) -> None:
        """This is how a hung attempt learns it no longer owns its Run: not by
        being told, but by its next renewal being refused."""
        store = InMemoryStore()
        run_id, _ = await admit(store)
        now = datetime.now(UTC)
        await store.claim(WorkerId("first"), now, lease_seconds=1.0)

        later = now + timedelta(seconds=2)
        await store.claim(WorkerId("second"), later, 30.0)

        assert not await store.renew(run_id, WorkerId("first"), later, 30.0)

    async def test_expired_leases_are_visible_to_a_supervisor(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store)
        now = datetime.now(UTC)
        await store.claim(WorkerId("dead"), now, lease_seconds=1.0)

        expired = await store.expired_leases(now + timedelta(seconds=5))
        assert [h.run_id for h in expired] == [run_id]

    async def test_a_live_lease_is_not_reported_as_expired(self) -> None:
        store = InMemoryStore()
        await admit(store)
        now = datetime.now(UTC)
        await store.claim(WorkerId("alive"), now, lease_seconds=300.0)
        assert await store.expired_leases(now + timedelta(seconds=5)) == []


class TestDeadlineAndForceSettlement:
    """DESIGN.md §8.4, and the independence of the two mechanisms."""

    async def test_a_run_that_will_not_unwind_is_force_settled(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store, deadline_seconds=0.05)

        never_returns = asyncio.Event()

        async def hangs(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = header
            await journal.append(type="turn_started", turn=1)
            await journal.append(
                type="tool_call_started",
                call_id="call-1",
                tool="slow",
                arguments={},
                turn=1,
            )
            # Deliberately ignores the abort signal. This is the hung attempt the
            # grace period exists for.
            _ = abort
            await never_returns.wait()

        worker = Worker(
            store,
            hangs,
            poll_interval=0.01,
            supervisor_interval=0.02,
            grace_seconds=0.1,
            lease_seconds=30.0,
        )
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        never_returns.set()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert state.terminal_state is TerminalState.FORCE_SETTLED
        assert state.failure is not None
        assert "grace period" in state.failure.message

    async def test_force_settlement_closes_the_dangling_call_honestly(self) -> None:
        """Orphaned work is safe to abandon because the call was recorded before
        it ran. The result says unknown, because unknown is the truth."""
        store = InMemoryStore()
        run_id, _ = await admit(store, deadline_seconds=0.05)
        never_returns = asyncio.Event()

        async def hangs(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = header, abort
            await journal.append(type="turn_started", turn=1)
            await journal.append(
                type="tool_call_started",
                call_id="call-1",
                tool="charge_card",
                arguments={"cents": 4000},
                turn=1,
            )
            await never_returns.wait()

        worker = Worker(
            store, hangs, poll_interval=0.01, supervisor_interval=0.02, grace_seconds=0.1
        )
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        never_returns.set()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert not state.has_dangling_tool_calls
        assert state.tool_results[0].outcome is ToolOutcome.UNKNOWN

    async def test_an_attempt_that_unwinds_in_time_settles_cleanly(self) -> None:
        """The grace period is a real chance, not a formality. An attempt that
        honours its abort signal gets a clean terminal record."""
        store = InMemoryStore()
        run_id, _ = await admit(store, deadline_seconds=0.05)

        async def cooperative(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = header
            await abort.wait()
            await journal.append(type="run_settled", state=TerminalState.ABORTED)

        worker = Worker(
            store,
            cooperative,
            poll_interval=0.01,
            supervisor_interval=0.02,
            grace_seconds=5.0,
        )
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert state.terminal_state is TerminalState.ABORTED

    async def test_a_run_inside_its_deadline_is_left_alone(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store, deadline_seconds=600)

        async def quick(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = header, abort
            await journal.append(type="run_settled", state=TerminalState.COMPLETED)

        worker = Worker(store, quick, poll_interval=0.01, supervisor_interval=0.02)
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert state.terminal_state is TerminalState.COMPLETED


class BlinkingStore(InMemoryStore):
    """A store whose reads fail the first few times, to break a supervisor pass.

    Subclassed rather than monkeypatched so the failure is visible in the test
    rather than hidden in a fixture, and so it fails on a real code path.
    """

    def __init__(self, fail_reads: int) -> None:
        super().__init__()
        self.remaining_failures = fail_reads
        self.read_attempts = 0
        self.failures_raised = 0

    async def raw_read(self, run_id: RunId) -> list[Record]:
        """A read that never blinks, so the test can observe the outcome without
        being subject to the failure it injected."""
        return await InMemoryStore.read(self, run_id)

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        self.read_attempts += 1
        # After the claiming Attempt's own first read. Everything past that
        # belongs to supervision or to the release path, which is where this
        # test wants the failure to land.
        if self.remaining_failures > 0 and self.read_attempts > 1:
            self.remaining_failures -= 1
            self.failures_raised += 1
            raise RuntimeError("the store blinked")
        return await super().read(run_id, after=after, limit=limit)


class TestSupervision:
    async def test_a_supervisor_pass_that_throws_does_not_end_supervision(self) -> None:
        """The reason a pass arms its successor before doing failable work.

        A supervisor that dies on its first exception stops enforcing deadlines
        the moment anything unusual happens, which is exactly when it is needed.
        Here the store fails several reads from inside the force-settle path; the
        Run must still end up force-settled once the store recovers.
        """
        store = BlinkingStore(fail_reads=3)
        run_id, _ = await admit(store, deadline_seconds=0.05)
        never_returns = asyncio.Event()

        async def hangs(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = journal, header, abort
            await never_returns.wait()

        worker = Worker(
            store, hangs, poll_interval=0.01, supervisor_interval=0.02, grace_seconds=0.05
        )

        async def settled() -> bool:
            return reduce(await store.raw_read(run_id)).settled

        task = asyncio.create_task(worker.run())
        await _wait_until(settled, timeout=8)
        worker.stop()
        never_returns.set()
        await asyncio.wait_for(task, timeout=8)

        # At least one supervisor pass really did throw. It does not matter how
        # many: the property is that supervision survived one, not that it
        # survived a specific number.
        assert store.failures_raised >= 1, "the store never actually failed"
        state = reduce(await store.raw_read(run_id))
        assert state.terminal_state is TerminalState.FORCE_SETTLED

    async def test_a_run_reclaimed_too_many_times_is_failed_rather_than_retried(
        self,
    ) -> None:
        """A Run that keeps killing its Worker will keep killing Workers. Without
        this it takes a fleet down one process at a time."""
        store = InMemoryStore()
        run_id, _ = await admit(store)

        # Simulate eleven previous Workers that each claimed it and died.
        for index in range(11):
            moment = datetime.now(UTC) + timedelta(seconds=index * 2)
            claimed = await store.claim(WorkerId(f"w{index}"), moment, lease_seconds=1.0)
            assert claimed == run_id
        # Leave it claimable right now rather than at a simulated future time.
        await store.release(run_id, WorkerId("w10"), RunState.RUNNABLE)

        header = await store.get_run(run_id)
        assert header is not None
        assert header.attempt_count > 10

        async def never_called(
            journal: Journal, header_: RunHeader, abort: AbortSignal
        ) -> None:  # pragma: no cover - the point is that it is not called
            _ = journal, header_, abort
            raise AssertionError("a Run past its attempt limit must not be executed")

        worker = Worker(
            store, never_called, poll_interval=0.01, supervisor_interval=0.05, max_attempts=10
        )
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "attempts_exhausted"


async def _settled(store: InMemoryStore, run_id: RunId) -> bool:
    records = await store.read(run_id)
    return bool(records) and reduce(records).settled


async def _wait_until(condition: object, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        if await condition():  # type: ignore[operator]
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition was not met within {timeout}s")


class TestReclaimedExpiredLeaseIsHonest:
    """``AttemptStarted.reclaimed_expired_lease`` is how an operator learns a
    Run was recovered from a Worker that died holding its lease.

    That makes a false positive worse than a missing field. A flag set on every
    Run says every Run crashed and recovered, so the one that actually did is
    indistinguishable from the ones that did not, and the field stops being
    worth reading at all.

    The trap is an off-by-one that is invisible from inside ``Worker``: the
    store increments ``attempt_count`` as part of claiming, so by the time the
    attempt runs, a *first* attempt already sees ``header.attempt_count == 1``.
    Testing ``> 0`` therefore reports a reclaim on every Run.

    ``psych_runtime.runtime.execute`` sets the matching telemetry attribute from
    ``> 1``, and ``tests/functional/test_telemetry.py`` asserts it is ``False``
    for an ordinary Run. So the record and the span disagreed about the same
    attempt, and only the span was checked.
    """

    async def test_a_first_attempt_did_not_reclaim_anything(self) -> None:
        store = InMemoryStore()
        run_id, _header = await admit(store)

        seen: list[bool] = []

        async def runner(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            log = await store.read(run_id)
            seen.extend(
                bool(getattr(record, "reclaimed_expired_lease", False))
                for record in log
                if record.type == "attempt_started"
            )
            await journal.append(type="run_settled", state=TerminalState.COMPLETED)

        worker = Worker(store, runner, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            deadline = datetime.now(UTC) + timedelta(seconds=5)
            while not seen and datetime.now(UTC) < deadline:
                await asyncio.sleep(0.01)
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)

        assert seen == [False], (
            "a first attempt reclaimed nothing, because there was no earlier attempt "
            f"whose lease could expire; got reclaimed_expired_lease={seen}"
        )


class TestAnAttemptThatRaisesIsSettledWithItsCause:
    """The last line of defence names what escaped.

    A runner that raises past every handler used to settle the Run with the
    fixed text "The attempt raised before it could settle the run" and nothing
    else. The traceback went to the Worker's own log, which a platform built on
    Psych cannot show its users. The settlement carries the exception now.
    """

    async def test_the_settlement_carries_the_exception_and_its_traceback(self) -> None:
        store = InMemoryStore()
        run_id, _ = await admit(store)

        def inner() -> None:
            raise KeyError("inner detail")

        async def explode(journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
            _ = journal, header, abort
            try:
                inner()
            except KeyError as err:
                raise RuntimeError("the runner blew up") from err

        worker = Worker(store, explode, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _wait_until(lambda: _settled(store, run_id), timeout=5)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = reduce(await store.read(run_id))
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "attempt_failed"
        assert "RuntimeError: the runner blew up" in state.failure.message
        assert "KeyError: 'inner detail'" in state.failure.message
        assert state.failure.traceback is not None
        assert "explode" in state.failure.traceback
        assert "inner detail" in state.failure.traceback
