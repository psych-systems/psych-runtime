"""The Worker: claim, lease, supervise, settle.

DESIGN.md §8. Psych owns execution. The consumer runs the process; Psych runs the
loop.

## The supervisor and the attempt are separate

The wake path must not both execute and supervise. Give one loop both jobs and a
single stalled model stream wedges the only code that could have enforced a
timeout on it, so the Run hangs until something outside the process notices.

- A **supervisor pass** is bounded and storage-only. It arms its own successor
  *before* doing any failable work, so a pass that throws does not end
  supervision.
- An **attempt fiber** is detached, does the actual work, and settles its own Run.

## Two independent mechanisms, not one

This is the part where the implementation and DESIGN.md §8.2's wording differ,
deliberately and with the reason recorded.

§8.2 says "renewal is driven by observable progress, not by the process being
alive". A lease and a deadline answer different questions, and conditioning one
on the other breaks both. Renewal gated on progress expires the lease of a Run
doing legitimate slow work: a reasoning model silent for four minutes is making
no observable progress and is entirely healthy.

So this module ships two mechanisms that never consult each other:

1. **A lease.** Answers "does this Worker still claim to own this Run". Renewed
   on a heartbeat while the process holds the attempt, hung or not. Expiry is
   what lets a *different* Worker reclaim after a crash.
2. **A deadline.** Answers "has this Run run too long". An absolute wall-clock
   value stamped once at admission, never re-derived from renewal, enforced by
   the *same process* holding the attempt through an abort signal, a grace
   period, and then force-settlement. It never consults the lease.

Together they cover both failures. The process dies, so renewal stops, the lease
expires and another Worker reclaims. The process lives but the attempt hangs, so
renewal continues and the in-process deadline fires anyway.

The §8.2 goal is met: the expired-lease branch stays reachable, because a hung
attempt is force-settled by its own process rather than being left to be
reclaimed by a branch its own heartbeat had made unreachable.

## The grace clock starts when the abort was first fired

Anchored to the moment the abort signal went out, not to "now" on each supervisor
pass and not to the deadline itself. Otherwise a supervisor pass that runs late
aborts and force-settles in the same breath, giving the attempt no real chance to
unwind, which defeats the whole point of having a grace period.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from psych_runtime.core.corruption import CorruptLog
from psych_runtime.core.errors import LeaseLost, SeqConflict
from psych_runtime.core.ids import AttemptId, RunId, WorkerId, new_attempt_id, new_worker_id
from psych_runtime.core.records import TerminalState, ToolFailure, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.runtime.abort import AbortReason, AbortSignal
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.notify import notify_parent
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.tools.guidance import describe_exception, failure_guidance, format_traceback

__all__ = ["ActiveAttempt", "AttemptRunner", "Worker"]

_LOG: Final = logging.getLogger("psych.runtime.worker")

DEFAULT_LEASE_SECONDS: Final = 30.0
"""How long a lease is honoured. Short enough that a crashed Worker's Run is
picked up quickly, long enough that a heartbeat hiccup does not cause a
handover storm."""

DEFAULT_HEARTBEAT_SECONDS: Final = 10.0
"""A third of the lease. Two renewals may be lost before a live attempt's lease
expires, which is the margin that keeps an ordinary GC pause from causing a
handover."""

DEFAULT_GRACE_SECONDS: Final = 60.0
"""DESIGN.md §8.4's default. How long an attempt has to unwind after its abort
signal fires before the supervisor writes a terminal record over it."""

DEFAULT_SUPERVISOR_INTERVAL: Final = 5.0
DEFAULT_POLL_INTERVAL: Final = 0.5
_MAX_CLAIM_BACKOFF: Final = 30.0
"""How far the claim loop backs off after repeated store failures. Long enough
that a database outage is not hammered, short enough that recovery is prompt."""

DEFAULT_MAX_ATTEMPTS: Final = 10
"""How many times one Run may be claimed before it is failed rather than
reclaimed again. A Run that kills every Worker that touches it must stop, or it
takes the fleet down one process at a time."""


@dataclass
class ActiveAttempt:
    """One Run this process is executing right now.

    Attributes:
        run_id: what is being worked on.
        attempt_id: this pass over it.
        deadline_at: the absolute wall clock this Run must not pass. Stamped at
            admission and never recomputed, so a renewal cannot extend it.
        abort: fires when the deadline passes or an abort is requested.
        task: the detached fiber doing the work.
        aborted_at: monotonic time the abort signal was first fired. The grace
            period is measured from here, not from each supervisor pass, so a
            late pass does not abort and force-settle in the same breath.
    """

    run_id: RunId
    attempt_id: AttemptId
    scope: Scope
    deadline_at: datetime
    abort: AbortSignal = field(default_factory=AbortSignal)
    task: asyncio.Task[None] | None = None
    aborted_at: float | None = None


AttemptRunner = Callable[[Journal, RunHeader, AbortSignal], Awaitable[None]]
"""What actually executes a Run.

Takes the journal, the run header and the abort signal, and is responsible for
appending the terminal record. Injected rather than hardcoded so a Worker can
drive an agent, a workflow, or a consumer's own step machinery without this
module knowing which.
"""


class Worker:
    """Claims runnable Runs and executes them until told to stop."""

    def __init__(
        self,
        store: Store,
        runner: AttemptRunner,
        *,
        worker_id: WorkerId | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        supervisor_interval: float = DEFAULT_SUPERVISOR_INTERVAL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        concurrency: int = 4,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.store = store
        self.worker_id = worker_id if worker_id is not None else new_worker_id()
        self._runner = runner
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._grace_seconds = grace_seconds
        self._supervisor_interval = supervisor_interval
        self._poll_interval = poll_interval
        self._concurrency = concurrency
        self._max_attempts = max_attempts

        self._active: dict[RunId, ActiveAttempt] = {}
        self._stopping = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        """Claim and execute until ``stop()``. Blocks."""
        self._supervisor = asyncio.create_task(self._supervise_forever())
        try:
            backoff = self._poll_interval
            while not self._stopping.is_set():
                if len(self._active) >= self._concurrency:
                    await self._sleep(self._poll_interval)
                    continue
                try:
                    claimed = await self._claim_one()
                except Exception:
                    # A store blink must not end the claim loop. Supervision
                    # already survives its own failures for the same reason
                    # (see _supervise_forever); a Worker that stopped claiming
                    # on one failed query is a fleet that quietly stops working
                    # while every process is still alive.
                    _LOG.exception("claiming failed; retrying in %.1fs", backoff)
                    await self._sleep(backoff)
                    backoff = min(backoff * 2, _MAX_CLAIM_BACKOFF)
                    continue
                backoff = self._poll_interval
                if not claimed:
                    await self._sleep(self._poll_interval)
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Ask the loop to finish. Attempts in flight are allowed to unwind.

        The shutdown signal reaches every active Attempt here rather than in
        ``_shutdown``, which only runs once the claim loop next wakes: a turn
        that finished in that window started another one, and the Run this was
        meant to hand on ran on until it hit some other stop condition instead.
        An Attempt stops at its next turn boundary and writes no terminal
        record, so the next Worker continues it from the log.
        """
        self._stopping.set()
        for attempt in list(self._active.values()):
            attempt.abort.fire(AbortReason.SHUTDOWN)

    async def _shutdown(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
        for attempt in list(self._active.values()):
            # Shutdown, not the deadline: the Attempt unwinds and writes no
            # terminal record, so the lease is released RUNNABLE and the next
            # Worker continues from the log. See psych_runtime.runtime.abort. Fired
            # again here for an attempt claimed between stop() and this pass.
            attempt.abort.fire(AbortReason.SHUTDOWN)
        tasks = [a.task for a in self._active.values() if a.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early when asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # -- claiming -----------------------------------------------------------

    async def _claim_one(self) -> bool:
        """Take the lease on one runnable Run and start a fiber for it."""
        now = datetime.now(UTC)
        run_id = await self.store.claim(self.worker_id, now, self._lease_seconds)
        if run_id is None:
            return False

        header = await self.store.get_run(run_id)
        if header is None:
            # Claimed a Run whose header vanished. Nothing sane to do with it,
            # and holding the lease would keep it out of everyone's way.
            await self.store.release(run_id, self.worker_id, RunState.SETTLED)
            return False

        if header.attempt_count > self._max_attempts:
            await self._fail_exhausted(header)
            return False

        attempt = ActiveAttempt(
            run_id=run_id,
            attempt_id=new_attempt_id(),
            scope=header.scope,
            deadline_at=header.deadline_at,
        )
        self._active[run_id] = attempt
        attempt.task = asyncio.create_task(self._attempt(attempt, header))
        return True

    async def _fail_exhausted(self, header: RunHeader) -> None:
        """Stop reclaiming a Run that has already killed enough Workers.

        Without this a Run that reliably crashes its Worker takes the fleet down
        one process at a time, and every reclaim looks like progress.
        """
        journal = await Journal.open(self.store, header.run_id, header.scope)
        if not journal.state.settled:
            await journal.append(
                type="run_settled",
                state=TerminalState.FAILED,
                failure=ToolFailure(
                    kind="attempts_exhausted",
                    message=failure_guidance(
                        "attempts_exhausted",
                        f"This run was claimed {header.attempt_count} times without "
                        f"completing, which is past the limit of {self._max_attempts}. "
                        "It is being failed rather than reclaimed again, because a run "
                        "that keeps killing its worker will keep killing workers.",
                    ),
                ),
            )
        # A Run failed here may be somebody's background subagent, and a parent
        # suspended waiting on it is only woken by a notification. Sent through
        # the same path a clean ending uses, so the parent hears one story.
        await notify_parent(self.store, header.run_id, journal.state)
        await self.store.release(header.run_id, self.worker_id, RunState.SETTLED)

    # -- the attempt fiber --------------------------------------------------

    async def _attempt(self, attempt: ActiveAttempt, header: RunHeader) -> None:
        """Execute one Run. Detached, so a stall here cannot wedge supervision."""
        heartbeat = asyncio.create_task(self._heartbeat(attempt))
        try:
            journal = await Journal.open(
                self.store, attempt.run_id, attempt.scope, attempt.attempt_id
            )
            if journal.state.settled:
                return

            await journal.append(
                type="attempt_started",
                worker_id=self.worker_id,
                attempt_number=journal.state.attempt_count + 1,
                # > 1, not > 0: the store increments attempt_count as part of
                # claiming, so a first attempt already sees 1 here. Testing > 0
                # marked every Run as recovered from a dead Worker, which makes
                # the field worse than absent: the one Run that really was
                # reclaimed becomes indistinguishable from the ones that were
                # not.
                #
                # And not the attempt count alone, because a second attempt has
                # two unrelated causes: the previous Worker died holding the
                # lease, or the Run suspended and something woke it. Only the
                # first is a reclaim. Without the second half of this condition
                # every resumed Run announced itself as recovered from a crash
                # that never happened, which a parent waiting on subagents
                # (DESIGN.md §17) turns from occasional into constant.
                reclaimed_expired_lease=(
                    header.attempt_count > 1 and not journal.state.resumed_since_attempt
                ),
            )
            await self._runner(journal, header, attempt.abort)

        except LeaseLost:
            # Another Worker reclaimed this Run. Anything written from here would
            # be a second writer on a log that permits one, so stop silently: the
            # reclaiming Worker owns the story now.
            _LOG.info("lease lost on %s, stopping this attempt", attempt.run_id)
        except SeqConflict:
            # Another Attempt wrote into this log while this one held it, which
            # means the lease moved and the renewal has not noticed yet. Same
            # answer as LeaseLost: stop, write nothing.
            _LOG.info("another attempt is writing %s, stopping this one", attempt.run_id)
        except CorruptLog:
            # The log could not have been produced by the protocol, so nothing
            # can be appended to it -- including a settlement saying so. Left
            # unsettled deliberately and released SETTLED so it is not reclaimed
            # forever: a corrupt log is a writer bug to be read by a person, and
            # every reclaim would raise here again on the way to killing this
            # Worker's claim loop.
            _LOG.exception("run %s has a corrupt log; giving up on it", attempt.run_id)
            with contextlib.suppress(Exception):
                await self.store.release(attempt.run_id, self.worker_id, RunState.SETTLED)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOG.exception("attempt on %s failed", attempt.run_id)
            await self._settle_failure(attempt, err)
        finally:
            heartbeat.cancel()
            # Both, and in that order: cancelling raises CancelledError, which
            # is a BaseException rather than an Exception, and a heartbeat that
            # died on its own raises whatever killed it. Letting either escape
            # this `finally` would skip the release below and strand the lease
            # until it expired.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            self._active.pop(attempt.run_id, None)
            await self._release(attempt)

    async def _settle_failure(self, attempt: ActiveAttempt, err: BaseException) -> None:
        """Record a terminal failure for an attempt that raised.

        Best effort: if this fails too, the lease simply expires and another
        Worker reclaims, which is the same path a crash takes.

        The record carries the exception's type, message and traceback. This
        is the last line of defence, reached only when something escaped every
        handler in the loop, and the one thing the person reading the log needs
        from it is what escaped. A settlement that said only "the attempt
        raised" sent them to this process's stdout for the answer, and a
        platform built on Psych has no stdout to send its users to.
        """
        with contextlib.suppress(Exception):
            journal = await Journal.open(
                self.store, attempt.run_id, attempt.scope, attempt.attempt_id
            )
            if not journal.state.settled:
                await journal.append(
                    type="run_settled",
                    state=TerminalState.FAILED,
                    failure=ToolFailure(
                        kind="attempt_failed",
                        message=failure_guidance(
                            "attempt_failed",
                            "The attempt raised before it could settle the run: "
                            f"{describe_exception(err)}",
                        ),
                        traceback=format_traceback(err),
                    ),
                )

    async def _release(self, attempt: ActiveAttempt) -> None:
        if attempt.abort.reason is AbortReason.LEASE_LOST:
            # Another Worker holds this Run. Releasing would clear its lease
            # and hand the Run to a third Worker mid-attempt; the store's own
            # holder check makes this a no-op, and not calling it says why.
            return
        with contextlib.suppress(Exception):
            journal_state = reduce(await self.store.read(attempt.run_id))
            state = (
                RunState.SETTLED
                if journal_state.settled
                else RunState.SUSPENDED
                if journal_state.suspended
                else RunState.RUNNABLE
            )
            await self.store.release(attempt.run_id, self.worker_id, state)

    async def _heartbeat(self, attempt: ActiveAttempt) -> None:
        """Renew the lease while this process holds the attempt.

        Renewal follows process liveness, and that is not an oversight. See the
        module docstring: a lease answers "do I still own this" and a deadline
        answers "has this run too long", and the deadline is what catches a hang.
        Conditioning renewal on progress would falsely expire the lease of a Run
        waiting on a legitimately slow model.
        """
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                held = await self.store.renew(
                    attempt.run_id, self.worker_id, datetime.now(UTC), self._lease_seconds
                )
            except Exception:
                # A store blink is not a lost lease. Log it and try again on the
                # next beat: raising here would leave the attempt in _active with
                # its lease unreleased, which is strictly worse than one missed
                # renewal (two may be missed before a lease expires by design).
                _LOG.warning("lease renewal for %s failed; retrying", attempt.run_id, exc_info=True)
                continue
            if not held:
                # Another Worker reclaimed. Stop the attempt rather than letting
                # it keep writing into a log it no longer owns.
                attempt.abort.fire(AbortReason.LEASE_LOST)
                return

    # -- supervision --------------------------------------------------------

    async def _supervise_forever(self) -> None:
        while not self._stopping.is_set():
            # Arm the next pass before doing anything that can throw. A
            # supervisor pass that raises must not end supervision, and the only
            # way to guarantee that is to schedule the successor first.
            next_pass = asyncio.create_task(self._sleep(self._supervisor_interval))
            try:
                await self._supervisor_pass()
            except Exception:
                _LOG.exception("supervisor pass failed; supervision continues")
            await next_pass

    async def _supervisor_pass(self) -> None:
        """One bounded, storage-only pass.

        Does no work that belongs to an attempt. Its whole job is noticing that
        something needs to stop and making that happen.
        """
        now = datetime.now(UTC)
        monotonic = time.monotonic()

        for attempt in list(self._active.values()):
            if now >= attempt.deadline_at and attempt.aborted_at is None:
                _LOG.warning("run %s passed its deadline; aborting", attempt.run_id)
                attempt.aborted_at = monotonic
                attempt.abort.fire(AbortReason.DEADLINE)
                continue

            if attempt.aborted_at is None:
                continue

            # The grace clock runs from when the abort was first fired. Measuring
            # from now would abort and force-settle in one pass whenever a pass
            # ran late, giving the attempt no chance to unwind.
            if monotonic - attempt.aborted_at >= self._grace_seconds:
                await self._force_settle(attempt)

    async def _force_settle(self, attempt: ActiveAttempt) -> None:
        """Write a terminal record over work that would not unwind.

        DESIGN.md §8.4. The caller's stream receives a real terminal event rather
        than waiting forever on a fiber that is never coming back.

        Orphaned work is safe to abandon because every tool call is recorded
        before execution and its result after, so an orphaned call is visibly
        incomplete. This settles those calls through the same path crash recovery
        uses rather than inventing a "killed" record, so there is one way a
        dangling call gets closed and one place to fix it.
        """
        _LOG.error("force-settling run %s after the grace period", attempt.run_id)

        # Deliberately not wrapped in a finally that drops the attempt. An
        # earlier version popped it from _active whatever happened, so a store
        # blink during force-settlement meant the Run was silently given up on
        # and left for its lease to expire. Leaving it in _active means the next
        # supervisor pass tries again, which is what a supervisor is for.
        journal = await Journal.open(self.store, attempt.run_id, attempt.scope, attempt.attempt_id)
        if not journal.state.settled:
            for call in list(journal.state.open_tool_calls.values()):
                await journal.append(
                    type="tool_call_finished",
                    call_id=call.call_id,
                    outcome=ToolOutcome.UNKNOWN,
                    failure=ToolFailure(
                        kind="orphaned",
                        message=failure_guidance(
                            "orphaned",
                            "The run was force-settled while this call was in flight. "
                            "Whether it took effect is unknown.",
                        ),
                    ),
                )

            await journal.append(
                type="run_settled",
                state=TerminalState.FORCE_SETTLED,
                orphaned_attempt_id=attempt.attempt_id,
                failure=ToolFailure(
                    kind="force_settled",
                    message=failure_guidance(
                        "force_settled",
                        "This run passed its deadline and did not unwind within the "
                        f"{self._grace_seconds:.0f} second grace period, so a terminal "
                        "record was written over it and the work was orphaned.",
                    ),
                ),
            )

        await notify_parent(self.store, attempt.run_id, journal.state)

        # Only now is the work genuinely abandoned.
        self._active.pop(attempt.run_id, None)
        if attempt.task is not None:
            attempt.task.cancel()
        with contextlib.suppress(Exception):
            await self.store.release(attempt.run_id, self.worker_id, RunState.SETTLED)


async def sweep_expired(store: Store, now: datetime | None = None, limit: int = 100) -> int:
    """Report how many Runs have a reclaimable lease.

    Read-only on purpose. Taking the work is still a conditional ``claim``, so
    this is for a consumer's own monitoring rather than part of the claim path.
    """
    stamp = now if now is not None else datetime.now(UTC)
    return len(await store.expired_leases(stamp, limit))


async def fire_overdue_deadlines(
    store: Store, scope_of: Callable[[RunHeader], Scope] | None = None, limit: int = 100
) -> int:
    """Append an abort record to every Run past its deadline that has none.

    For a Run whose Worker died between passing its deadline and being reclaimed:
    the in-process enforcement above cannot fire because there is no process. A
    supervisor in any Worker can still record the abort, and the next Worker to
    claim it settles it on its first stop-condition check.
    """
    now = datetime.now(UTC)
    fired = 0
    for header in await store.overdue_deadlines(now, limit):
        scope = scope_of(header) if scope_of is not None else header.scope
        with contextlib.suppress(Exception):
            journal = await Journal.open(store, header.run_id, scope)
            if journal.state.settled or journal.state.aborted:
                continue
            await journal.append(
                type="abort_requested",
                reason=(
                    f"the run passed its deadline of {header.deadline_at.isoformat()} "
                    "while no worker held it"
                ),
            )
            fired += 1
    return fired


def lease_deadline(now: datetime, lease_seconds: float) -> datetime:
    """When a lease taken at ``now`` stops being honoured."""
    return now + timedelta(seconds=lease_seconds)
