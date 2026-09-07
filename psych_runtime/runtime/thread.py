"""Conversation continuity across a chain of Runs.

## The two shapes, and why this picks a chain of Runs

Continuing a conversation could mean one long-lived Run that suspends waiting
for the next message, or a chain of separate Runs each carrying forward what
the last one saw. DESIGN.md §11 gives a suspended Run exactly four reasons:
``approval``, ``question``, ``external``, ``children`` -- none of them "the
user might type something later". Stretching one of those to cover an
idle chat window conflates "the agent is waiting on a specific answer to a
specific question it asked" with "nobody has said anything in a while", and
those expire differently: a suspension times out and the Run is abandoned
(§11), which is the wrong ending for a chat thread that is merely quiet. A
single Run also means one lease, one deadline and one attempt-count budget
for the whole thread's lifetime, and DESIGN.md §8.4 sizes a deadline for one
unit of work, not for "however long this conversation goes on".

DESIGN.md §23.3 says it directly: *"a message sent immediately after starts a
new Run carrying it"*. A chain of Runs is what that sentence describes, and it
is what the rest of the runtime already assumes -- ``max_steps``, ``max_turns``
and the deadline are all sized per Run, a report is built over one Run's log,
and the store's claim/lease machinery is built around Runs that end. The gap
left open was never the shape, which DESIGN.md already settled. It was that
nothing carried the *history* forward once a new Run started. That is what
this module does.

## Continuity is derived, never a side table

A continuing Run's own log carries exactly one new fact:
``RunAdmitted.continues_run_id``, the immediate predecessor. Everything else
-- how far back the chain goes, what was actually said -- is derived by
walking that field through the store at read time, the same way every other
projection in ``psych_runtime.core`` derives its answer from records rather than from
something written to remember it. A consumer never has to keep its own
"which Runs make up this thread" table, and ``psych_runtime.report()`` and a rebuilt
conversation read the same field, so the two cannot disagree (DESIGN.md §6).

## The bound, and why it always keeps at least the immediate parent

Replaying a long chat history into every turn is the dominant cost of a chat
agent, so how far back the walk goes is bounded by
``Limits.max_history_records`` -- a Spec field, so the bound is versioned
along with everything else a Run pins at admission (DESIGN.md §4).

The walk below always includes the immediate predecessor Run's full records,
even when that predecessor alone exceeds the budget. A thread's very first
continuation has to carry *something* forward, and a bound that could erase
all context on the one message it exists to enable would defeat the feature
before it does anything. Every Run further back is included whole or not at
all, never truncated mid-Run: splitting a Run's own records would separate a
tool call from its result and corrupt
``psych_runtime.core.conversation.build_conversation``'s pairing, which assumes it is
handed one Run's complete log.

## Tenancy does not widen

A continuation is a new way to reach another Run's content, and DESIGN.md
§10.4 treats exactly that shape -- a pool key or, here, a chain link, that can
silently cross a tenant boundary -- as the mistake that ends the project. Every
ancestor visited must share the continuing Run's own tenant, checked here on
every walk rather than trusted from whatever ``psych_runtime.dispatch(continues=...)``
already checked at creation: a chain link enforced once at write time and
never again at read time is exactly the kind of control the project warns is
worse than none, because someone will believe it covers a case it does not.
"""

from __future__ import annotations

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId
from psych_runtime.core.messages import Message
from psych_runtime.core.records import Record
from psych_runtime.core.scope import Scope
from psych_runtime.store.port import Store

__all__ = ["load_thread_history"]


async def load_thread_history(
    store: Store,
    scope: Scope,
    continues_run_id: RunId | None,
    *,
    max_history_records: int,
) -> list[Message]:
    """The conversation of every ancestor Run in this thread, oldest first.

    Each ancestor Run's records are projected through
    ``build_conversation`` independently and the resulting messages
    concatenated, rather than concatenating raw records and projecting once:
    ``seq`` is only unique *within* one Run's log, so folding several Runs'
    records through one pass would collide on it and corrupt
    ``build_conversation``'s own compaction-boundary arithmetic, which compares
    ``seq`` values to decide what a compaction record replaced. Projecting
    per-Run sidesteps that entirely, and each ancestor's own compaction (if it
    had one) is still respected on its own terms.

    Args:
        store: where the ancestor Runs' logs and headers live.
        scope: the continuing Run's own Scope. Every ancestor visited must
            share its tenant or this raises -- see the module docstring.
        continues_run_id: the immediate predecessor, or ``None`` for a
            thread's first Run, which has no history to load.
        max_history_records: the budget, from ``Limits.max_history_records``.
            ``0`` disables history entirely, including the immediate parent.

    Returns:
        Messages in thread order: the oldest ancestor's conversation first,
        the immediate predecessor's last. The caller prepends this to the
        current Run's own ``build_conversation`` output.

    Raises:
        AccessDenied: an ancestor Run's Scope has a different tenant than
            ``scope``.
    """
    if continues_run_id is None or max_history_records <= 0:
        return []

    chain: list[list[Record]] = []
    budget = max_history_records
    current: RunId | None = continues_run_id
    seen: set[RunId] = set()

    while current is not None and current not in seen:
        seen.add(current)
        header = await store.get_run(current)
        if header is None:
            # An ancestor id that names no Run. Nothing sane to derive from a
            # broken chain link; stop here rather than raising, so a thread
            # whose oldest history has been purged by the consumer's own
            # retention policy still continues with whatever is left.
            break
        if header.scope.tenant != scope.tenant:
            raise AccessDenied(
                f"run {current}",
                f"belongs to tenant {header.scope.tenant!r}, not {scope.tenant!r}. "
                "A Run may only continue one Run in its own Scope.",
            )

        records = await store.read(current)
        chain.append(records)
        budget -= len(records)
        current = header.continues_run_id

        if budget <= 0:
            # The immediate parent (the first iteration) is always kept even
            # if it alone blew the budget; anything further back stops here.
            break

    chain.reverse()
    messages: list[Message] = []
    for records in chain:
        messages.extend(build_conversation(records))
    return messages
