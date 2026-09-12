"""Admission: exactly one Run per idempotency key.

DESIGN.md §20. The consumer owns the clock and the event source entirely: their
CronJob, their Celery beat, their EventBridge, their webhook handler. Psych
provides exactly one thing, which is this.

Psych stores no cron expressions, evaluates no schedules and runs no timers. The
refusal is the feature: a scheduler inside a library is a scheduler the consumer
cannot see, cannot pause during an incident, and cannot reconcile against the one
they already run.

## At-least-once, and saying so

DESIGN.md §8.3: delivery is at-least-once and exactly-once does not exist.
Pretending otherwise produces double refunds. So ``dispatch`` takes an
idempotency key, and creating a Run with a key that already exists returns the
existing Run rather than a second one. The caller can retry the same dispatch as
many times as their queue redelivers it and get one Run.

A caller that omits the key gets a new Run every call, which is correct for a
human pressing send and wrong for anything driven by a queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from psych_runtime.core.errors import (
    AccessDenied,
    RunAlreadySettled,
    RunNotFound,
    RunNotSuspended,
    SuspensionExpired,
    VersionNotFound,
)
from psych_runtime.core.ids import RunId, VersionHash, WorkerId, new_queue_entry_id, new_run_id
from psych_runtime.core.records import RECORD_ADAPTER, QueueKind, Record, TerminalState, ToolFailure
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec
from psych_runtime.core.version import Version
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.tools.guidance import failure_guidance

__all__ = ["Dispatched", "dispatch", "interrupt", "resume", "send"]

_RESUME_ACTOR: Final = WorkerId("psych.resume")
"""``release`` takes the Worker that held the lease, and a resume is not a Worker
operation: the Run was suspended and nobody holds it. The Store port documents
``release`` as tolerant of a caller that does not hold the lease, so this names
the caller honestly rather than passing an empty string or borrowing a real
Worker's id.
"""


# `input` shadows a builtin and keeps that name deliberately: DESIGN.md §20
# specifies the signature as dispatch(version, input, scope, idempotency_key),
# and renaming it here would make the code and the design disagree about the
# public API.


@dataclass(frozen=True, slots=True)
class Dispatched:
    """What a dispatch produced.

    Attributes:
        run_id: the Run, new or existing.
        created: False when an existing Run was returned for a repeated
            idempotency key. A caller that wants to know whether their retry was
            the one that did the work reads this.
    """

    run_id: RunId
    created: bool


async def dispatch(
    store: Store,
    version: Version | VersionHash,
    scope: Scope,
    *,
    input: dict[str, Any] | None = None,  # noqa: A002
    idempotency_key: str | None = None,
    deadline_seconds: float | None = None,
    parent_run_id: RunId | None = None,
    delegation_depth: int = 0,
    continues: RunId | None = None,
    nested: bool = False,
) -> Dispatched:
    """Admit a Run.

    Args:
        store: where the Run lives.
        version: the published Version to run, or its hash. Passing the hash
            requires the Version to already be stored, which is the normal case
            for anything running from a deployed Spec.
        scope: whose Run this is. Stamped on every Record from here on.
        input: the Run's input. A ``message`` key reaches the model as the user's
            first message; anything else is serialised for it.
        idempotency_key: makes admission exactly-once per key. Omit it only when
            a repeat genuinely means "do it again".
        deadline_seconds: overrides the Spec's own deadline. Every Run has one;
            there is no way to ask for none.
        parent_run_id: set when this Run is a nested step or a subagent. Not
            what continues a chat thread -- see ``continues``.
        delegation_depth: how deep this Run sits. Monotone: a caller may deepen
            it but a resumed child must never be counted from zero, or it
            delegates as though it were top-level and the recursion budget is
            defeated (DESIGN.md §17).
        nested: this Run is executed inline by whoever dispatched it, so it
            is admitted ``NESTED`` and no Worker claims it (DESIGN.md §17).
            A subagent is the usual case (``psych_runtime.runtime.execute``);
            a fixture that drives a Runtime itself against a store a Worker
            is also polling is the other, and it needs this for the same
            reason -- two Attempts writing one log is what the Journal
            refuses. A caller who wants a Run a Worker will pick up leaves
            it False.

            Whoever executes a ``NESTED`` Run settles it with
            ``Store.settle_inline`` when it finishes. ``Store.release`` cannot:
            it answers only the Worker holding the lease, and nothing ever
            claimed this Run. A caller that drives a Run and never settles it
            leaves a header that disagrees with its own log for good, so it is
            part of the bargain rather than an optional tidy-up.
        continues: the Run this one continues, for "a second message in the
            same conversation" (DESIGN.md §23.3). ``psych_runtime.report()``
            covers this Run alone, as always; the joined conversation a Worker
            actually sends the model is built by
            ``psych_runtime.runtime.thread.load_thread_history`` walking this field
            back through the store, bounded by the Version's own
            ``Limits.max_history_records``. Unrelated to ``parent_run_id``,
            which is delegation -- see ``RunAdmitted.continues_run_id``'s
            docstring in ``psych_runtime.core.records`` for why the two must not be
            conflated.

    Returns:
        The Run, and whether this call created it.

    Raises:
        VersionNotFound: a hash was passed that the store does not hold.
        RunNotFound: ``continues`` names a Run the store does not hold.
        AccessDenied: ``continues`` names a Run in a different Scope. A
            continuation is a new way to reach another Run's content and gets
            the same scrutiny as the MCP pool key (DESIGN.md §10.4).
    """
    resolved = await _resolve_version(store, version)
    now = datetime.now(UTC)

    if continues is not None:
        predecessor = await store.get_run(continues)
        if predecessor is None:
            raise RunNotFound(continues)
        if predecessor.scope.tenant != scope.tenant:
            raise AccessDenied(
                f"run {continues}",
                f"belongs to tenant {predecessor.scope.tenant!r}, not "
                f"{scope.tenant!r}. A Run may only continue one Run in its own Scope.",
            )

    seconds = deadline_seconds
    if seconds is None:
        spec = resolved.spec
        seconds = spec.limits.deadline_seconds
    deadline_at = now + timedelta(seconds=seconds)

    run_id = new_run_id()
    header = RunHeader(
        run_id=run_id,
        scope=scope,
        version_hash=resolved.hash,
        state=RunState.NESTED if nested else RunState.RUNNABLE,
        created_at=now,
        deadline_at=deadline_at,
        idempotency_key=idempotency_key,
        parent_run_id=parent_run_id,
        delegation_depth=delegation_depth,
        continues_run_id=continues,
    )

    stored = await store.create_run(header)
    if stored.run_id != run_id:
        # The key was already used. Return the Run it made rather than a second
        # one: delivery is at-least-once and this is what makes that survivable.
        if stored.scope.tenant != scope.tenant:
            # ...unless it belongs to someone else. Idempotency keys are chosen
            # by the consumer and collide naturally across tenants ("order-1234",
            # a webhook delivery id), and handing back another tenant's run_id
            # would let this caller stream, report and resume that Run. Every
            # shipped adapter now scopes the key by tenant, so this is the
            # defence for a deployment still on an older schema.
            raise AccessDenied(
                f"idempotency key {idempotency_key!r}",
                f"it already admitted a Run for tenant {stored.scope.tenant!r}, not "
                f"{scope.tenant!r}. Idempotency keys are per tenant.",
            )
        return Dispatched(run_id=stored.run_id, created=False)

    admitted: Record = RECORD_ADAPTER.validate_python(
        {
            "type": "run_admitted",
            "run_id": run_id,
            "seq": 1,
            "at": now,
            "scope": scope,
            "version_hash": resolved.hash,
            "input": input or {},
            "idempotency_key": idempotency_key,
            "deadline_at": deadline_at,
            "parent_run_id": parent_run_id,
            "delegation_depth": delegation_depth,
            "continues_run_id": continues,
        }
    )
    await store.append(run_id, 1, admitted)
    return Dispatched(run_id=run_id, created=True)


async def resume(
    store: Store,
    run_id: RunId,
    *,
    payload: dict[str, Any] | None = None,
    approved: bool | None = None,
    by: str | None = None,
) -> None:
    """Deliver a decision or payload to a suspended Run and make it runnable.

    DESIGN.md §11. Because suspension and resume run through the same lease and
    log machinery as everything else, approvals, clarifying questions and webhook
    waits are one mechanism rather than three.

    Raises:
        RunNotFound: no such Run.
        RunNotSuspended: the Run is not waiting for anything. Resuming a
            running Run would append a record the reducer refuses, and failing
            here names the real problem instead.
        SuspensionExpired: the suspension waited past its expiry. The Run is
            settled ``ABANDONED`` before this raises, so a stale approval never
            executes (DESIGN.md §11).
    """
    records = await store.read(run_id)
    if not records:
        raise RunNotFound(run_id)

    journal = await Journal.open(store, run_id, records[0].scope)
    if not journal.state.suspended:
        raise RunNotSuspended(run_id, "settled" if journal.state.settled else "runnable")
    expires_at = journal.state.suspend_expires_at
    if expires_at is not None and datetime.now(UTC) > expires_at:
        # DESIGN.md §11: suspensions expire. Honouring an approval granted
        # after its expiry executes a call decided against a world that has
        # moved on -- the price, the permission, the catalogue behind that tool
        # name. The Run is settled abandoned instead, which is what the design
        # says happens to a suspension nobody answered in time.
        await journal.append(
            type="run_settled",
            state=TerminalState.ABANDONED,
            failure=ToolFailure(
                kind="suspension_expired",
                message=failure_guidance(
                    "suspension_expired",
                    f"This run waited for {journal.state.suspend_reason} past its expiry "
                    f"of {expires_at.isoformat()} and was abandoned.",
                ),
            ),
        )
        await store.release(run_id, _RESUME_ACTOR, RunState.SETTLED)
        raise SuspensionExpired(run_id, str(journal.state.suspend_reason))

    await journal.append(type="resumed", payload=payload or {}, approved=approved, resumed_by=by)
    await store.set_runnable_at(run_id, None)
    await store.release(run_id, _RESUME_ACTOR, RunState.RUNNABLE)


async def send(
    store: Store,
    run_id: RunId,
    *,
    message: dict[str, Any] | str,
    queue: QueueKind = QueueKind.STEER,
) -> str:
    """Put a message into a Run that is still executing (DESIGN.md §9).

    The three queues named by ``QueueKind`` are three different intentions,
    not three names for the same thing:

    - ``QueueKind.STEER`` (the default): inject into the turn that is running
      right now. Drained at the start of the *next* turn -- there is no
      mid-turn mutation (DESIGN.md §10.2) -- so a steer sent while the model
      is mid-stream reaches it on the turn after this one, not this one.
    - ``QueueKind.FOLLOW_UP``: deliver once the current turn settles with
      nothing left to do, instead of letting the Run complete. What "keep
      going, and also..." means without interrupting anything.
    - ``QueueKind.NEXT_RUN``: hand a message to whatever Run comes next,
      explicitly legal even after this Run has aborted. This is the queue
      DESIGN.md §9 names for "stop mid-response and immediately send another
      request": call ``interrupt()``, then ``send(queue=NEXT_RUN)``, then
      ``dispatch(continues=run_id)`` once this Run settles, and the new Run's
      own admission is what actually carries that message forward as its
      conversation-continuing input -- this function only records that the
      message arrived and when, which is what makes the arrival visible in
      the log even if the continuation is dispatched much later.

    This is a low-level primitive for a Run that is *already open*. The
    common case -- a second chat message after the first Run has already
    settled, with nothing left running to steer -- is not this; it is
    ``dispatch(continues=run_id, input=...)``, which starts a fresh Run
    carrying the earlier conversation forward instead of trying to inject
    into one that is no longer there to receive it.

    Args:
        store: where the Run's log lives.
        run_id: the Run to enqueue into.
        message: the payload. A plain string is wrapped as ``{"message":
            ...}``, matching what ``psych_runtime.core.conversation`` reads back out
            of a queued entry's payload and what ``dispatch``'s own ``input``
            accepts, so a caller does not have to remember two shapes for the
            same kind of text.
        queue: which of the three queues. Defaults to ``STEER``, the one that
            reaches the model soonest.

    Returns:
        The entry id, for a caller that wants to ``psych_runtime.core.records`` back
        later and find exactly this enqueue.

    Raises:
        ValueError: the Run is already settled. There is no turn left to
            steer, no turn left to follow up after, and a next Run this Run
            itself will never dispatch, so nothing here would ever be read;
            ``dispatch(continues=run_id, ...)`` is what a caller wants instead.
        CorruptLog: a ``STEER`` or ``FOLLOW_UP`` entry was sent after this
            Run's abort was recorded. DESIGN.md §9 permits only ``NEXT_RUN``
            in that position -- "stop mid-response and immediately send
            another request" is a ``next_run`` enqueue after an abort, and a
            steer or follow-up there is exactly the race the protocol
            forbids, so the reducer refuses it before it is written rather
            than silently losing the race to the abort.
    """
    records = await store.read(run_id)
    if not records:
        raise RunNotFound(run_id)

    journal = await Journal.open(store, run_id, records[0].scope)
    if journal.state.settled:
        raise RunAlreadySettled(
            run_id,
            "there is no turn left to steer or follow up, and no next Run this call could reach",
        )

    payload = {"message": message} if isinstance(message, str) else dict(message)
    entry_id = new_queue_entry_id()
    await journal.append(type="queue_enqueued", queue=queue, entry_id=entry_id, payload=payload)
    return entry_id


async def interrupt(store: Store, run_id: RunId, reason: str = "", by: str | None = None) -> None:
    """Record an abort.

    An abort is a Record, not a flag (DESIGN.md §9). Because it has a sequence
    number, "what arrived after the stop" becomes a question the log answers
    rather than a race the runtime has to win.

    Safe to call on a Run that is already aborted or already settled: the first
    abort is the one that counts and a second changes nothing.
    """
    records = await store.read(run_id)
    if not records:
        raise RunNotFound(run_id)

    journal = await Journal.open(store, run_id, records[0].scope)
    if journal.state.settled or journal.state.aborted:
        return
    await journal.append(type="abort_requested", reason=reason, requested_by=by)


async def _resolve_version(store: Store, version: Version | VersionHash) -> Version:
    if isinstance(version, Version):
        stored = await store.get_version(version.hash)
        if stored is None:
            # Publishing on first dispatch keeps a consumer from having to
            # remember a separate step, and is a no-op when the hash is present.
            await store.put_version(version)
        return version

    found = await store.get_version(version)
    if found is None:
        raise VersionNotFound(version)
    return found


def is_agent(version: Version) -> bool:
    """Whether this Version runs through the agent loop rather than the workflow
    engine. One engine, two shapes (DESIGN.md §5)."""
    return isinstance(version.spec, AgentSpec)
