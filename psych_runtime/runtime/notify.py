"""Telling a parent that one of its background children finished.

DESIGN.md §17 gives a subagent its own Run, its own log and its own budget.
Once that child runs in the background rather than inline, the parent needs to
find out how it ended, and this module is the whole of how that happens.

## The notification is a Record, and only a Record

A callback would be the obvious shape and is the wrong one. When a child
finishes, its parent may be suspended, executing on another machine, or not
running anywhere at all, and the only thing those three have in common is the
log. Writing ``subagent_finished`` into the parent's log means the result is
durable before anything is woken, survives the notifier dying immediately
afterwards, and keeps the reducer pure: the tree's state is folded from records
rather than from whatever a process happened to observe.

## Two halves, and why the second one can be lost

Notifying is two operations that cannot be one, because the Store has no
transactions by design (DESIGN.md §7):

1. **Append the record.** This is the half that matters. Appended by the
   Attempt that settled the child, into the parent's log, as one of the
   external record types a parent's own Attempt tolerates arriving underneath
   it (``psych_runtime.runtime.journal.EXTERNAL_RECORD_TYPES``) -- the same mechanism
   an interrupt or a steer already uses.
2. **Wake the parent, if it is asleep.** A parent with nothing else to do
   suspends on ``SuspendReason.CHILDREN`` rather than holding a lease and
   spinning, so somebody has to resume it. This is that somebody.

If the second half is lost -- the notifier's process dies between the two --
the parent waits until its suspension expires and is then settled abandoned,
which is exactly what DESIGN.md §11 says happens to any suspension nobody
answers. It is not silent and it is not indefinite. The first half is already
durable, so nothing about the child's result is lost with it.

## The window this races with, and how both sides close it

A parent suspends by appending a ``suspended`` record and then, a moment later,
releasing its lease. In between, its log says "suspended" and the store still
says "running". A resume delivered in that window would clear the lease of an
Attempt that is still unwinding.

So this waits for the store to agree before resuming, and the parent closes the
same window from its own side: after appending its suspension it folds the log
once more, and if a child finished in the meantime it resumes itself and takes
another turn rather than releasing a lease it no longer needs to release. One of
the two always fires, and neither depends on the other having fired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Final

from psych_runtime.core.errors import RunNotSuspended, SeqConflict, SuspensionExpired
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import Record, SuspendReason
from psych_runtime.core.reducer import RunStateView, reduce
from psych_runtime.runtime.dispatch import resume
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.port import RunState, Store

__all__ = ["notify_parent"]

_LOG: Final = logging.getLogger("psych.runtime.notify")

_APPEND_ATTEMPTS: Final = 8
"""How many times to re-fold and retry the append before giving up.

A conflict here is ordinary rather than exceptional: two children of one parent
finishing at once are two legitimate external writers, and so is the parent's
own Attempt still appending its turn. The Journal already absorbs a conflict
caused only by external records; this covers the case where the parent itself
took the sequence, which is not a bug in anybody and only needs to be tried
again."""

_WAKE_TIMEOUT_SECONDS: Final = 2.0
"""How long to wait for the store to catch up with a parent that has just
written its suspension. Bounded because the alternative -- waiting indefinitely
for a state that may never come -- would keep the child's Worker busy on the
parent's behalf, and the parent's own self-resume already covers the case where
this gives up."""

_WAKE_POLL_SECONDS: Final = 0.05


async def notify_parent(store: Store, run_id: RunId, state: RunStateView) -> None:
    """Tell this Run's parent that it has finished.

    Called by whichever Attempt settled the child, and by the Worker on the two
    paths that settle a Run without one -- force-settlement past the grace period
    and a Run that has exhausted its attempts (DESIGN.md §8.4). All three are
    endings a waiting parent has to hear about, and a notification wired to only
    the happy one would leave a parent waiting out its whole expiry for a child
    that died in a way the runtime already knew about.

    Does nothing for a Run with no parent, for one whose parent knows nothing
    about it (an inline ``delegate``, which returns its result to its caller
    directly), or for a child that has not actually settled.

    Never raises. A notification failing must not turn a child that completed
    into a child that failed: the child's own log already holds its result, and
    a parent that is not woken waits out its suspension instead
    (DESIGN.md §11).
    """
    if not state.settled or state.terminal_state is None:
        return
    header = await store.get_run(run_id)
    parent_run_id = header.parent_run_id if header is not None else None
    if parent_run_id is None:
        return

    try:
        parent = await _append_finished(store, parent_run_id, run_id, state)
    except Exception:
        _LOG.warning(
            "could not tell run %s that its child %s finished",
            parent_run_id,
            run_id,
            exc_info=True,
        )
        return

    if parent is None:
        return
    await _wake(store, parent_run_id)


async def _append_finished(
    store: Store,
    parent_run_id: RunId,
    child_run_id: RunId,
    state: RunStateView,
) -> Journal | None:
    """Write the child's ending into the parent's log, once.

    Returns ``None``, having written nothing, in three cases, and each of them
    is a legitimate state rather than a failure:

    - The parent never recorded this child. An inline ``delegate`` returns its
      result to its caller directly and keeps no child in its log.
    - The notification is already there. A second Attempt of an
      already-settled child would otherwise duplicate it, which the reducer
      refuses as a corrupt log -- correctly, since a Run reaches exactly one
      terminal state.
    - The parent has settled. Its log is closed and nothing may follow the
      terminal record (DESIGN.md §6), so a child that outlives the parent that
      started it simply finishes unheard. Its own log still holds its result.
    """
    for attempt in range(_APPEND_ATTEMPTS):
        journal = await Journal.open(store, parent_run_id, state.scope)
        child = journal.state.children.get(child_run_id)
        if child is None or not child.alive or journal.state.settled:
            return None
        try:
            await journal.append(
                type="subagent_finished",
                child_run_id=child_run_id,
                name=child.name,
                state=state.terminal_state,
                output=state.output,
                failure=state.failure,
                usage=state.usage,
                unreported_usage_calls=state.unreported_usage_calls,
                cost=state.cost,
                unpriced_model_calls=state.unpriced_model_calls,
            )
        except SeqConflict:
            # Somebody else wrote at that sequence: the parent's own Attempt,
            # or a sibling child finishing at the same moment. Re-fold and try
            # again rather than forcing, which would interleave two writers.
            await asyncio.sleep(_WAKE_POLL_SECONDS * attempt)
            continue
        return journal
    _LOG.warning(
        "gave up appending a subagent_finished for child %s to run %s after %d attempts",
        child_run_id,
        parent_run_id,
        _APPEND_ATTEMPTS,
    )
    return None


async def _wake(store: Store, parent_run_id: RunId) -> None:
    """Resume a parent that is suspended waiting on its children.

    Waits for the store to say ``SUSPENDED`` before resuming, rather than
    trusting the log alone: the parent writes its suspension slightly before it
    releases its lease, and a resume delivered inside that gap would clear the
    lease of an Attempt that is still unwinding. See the module docstring for
    why giving up here is safe.
    """
    waited = 0.0
    while waited <= _WAKE_TIMEOUT_SECONDS:
        header = await store.get_run(parent_run_id)
        if header is None or header.state is RunState.SETTLED:
            return
        if header.state is RunState.SUSPENDED:
            with contextlib.suppress(RunNotSuspended, SuspensionExpired):
                # Both are ordinary here rather than exceptional: the parent may
                # have resumed itself in the meantime, or waited past its own
                # expiry and been settled abandoned by the resume itself.
                await resume(store, parent_run_id, payload={"woken_by": "subagent"})
            return
        records = await store.read(parent_run_id)
        if not records:
            return
        if not _waiting_on_children(records):
            # The parent is awake. It folds this record at its next turn
            # boundary like any other external write, so there is nothing to
            # wake and nothing to wait for.
            return
        await asyncio.sleep(_WAKE_POLL_SECONDS)
        waited += _WAKE_POLL_SECONDS


def _waiting_on_children(records: Sequence[Record]) -> bool:
    """Whether the parent's log says it is suspended on its children.

    Folded from the log rather than read off the header, because the log is
    where the parent says so first; the header catches up when its Worker
    releases the lease, which is the gap this whole loop exists to wait out.
    """
    folded = reduce(records)
    return folded.suspended and folded.suspend_reason is SuspendReason.CHILDREN
