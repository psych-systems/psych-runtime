"""Streaming: the log is the stream.

DESIGN.md §12. The Worker executing a Run is a different process from the
consumer's HTTP server. The stream must survive that, and survive reconnects.

Every Record has a sequence number and is persisted before it is delivered. A
client subscribes with "everything after N", receives the backlog from the store,
then tails. There is no separate stream state, which is why an interrupt cannot
break streaming: an abort is just more Records, and a client reconnecting after
one asks for everything after its last sequence and receives the abort and the
terminal event in order.

## Polling first, pub/sub as an accelerator

DESIGN.md §12 says to build and test the polling path first and add pub/sub as an
accelerator that is never required for correctness. That is what this is. An
optional notifier shortens the wait between polls; removing it changes latency
and nothing else, and the tests do not use one.

## Why the gap check matters here

A tailing reader that silently skipped a sequence would hand a consumer a
conversation missing its middle and look fine doing it. Reads go through the
store's range read, which the contract suite pins against exactly this, and the
reader asserts contiguity itself rather than trusting it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final, Protocol, runtime_checkable

from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import Record, RunSettled
from psych_runtime.store.port import Store

__all__ = ["Notifier", "stream", "stream_until_settled"]

DEFAULT_POLL_SECONDS: Final = 0.25
"""How long to wait before asking the store again when nothing new arrived.

Quarter of a second is short enough to feel live in a chat UI and long enough
that a thousand idle subscribers are not a load problem. A consumer who wants
lower latency supplies a Notifier rather than lowering this.
"""

_PAGE: Final = 200


@runtime_checkable
class Notifier(Protocol):
    """An optional accelerator: Redis, Postgres LISTEN/NOTIFY, anything.

    Never required for correctness. A notifier that drops every message produces
    a stream that is exactly as complete and slightly slower, because the poll
    still runs.
    """

    async def wait(self, run_id: RunId, timeout: float) -> None:
        """Return when something may have been appended, or when ``timeout``
        elapses. Returning early for no reason is allowed and costs one wasted
        read."""
        ...


async def stream(
    store: Store,
    run_id: RunId,
    *,
    after: int = 0,
    notifier: Notifier | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    stop: Callable[[], bool] | None = None,
) -> AsyncIterator[Record]:
    """Yield every Record after ``after``, then tail until the Run settles.

    Args:
        store: where the log lives.
        run_id: the Run to follow.
        after: the last sequence the client already has. 0 for the whole log.
            A client reconnecting passes the highest sequence it saw, and gets
            every later record and misses none, including across an interrupt.
        notifier: an optional accelerator. Correctness does not depend on it.
        poll_seconds: how long to wait between reads when nothing arrived.
        stop: called between polls; return True to end the stream early. For a
            consumer whose HTTP client disconnected.

    Yields:
        Records in sequence order, contiguously.

    Raises:
        CorruptLog: the store returned a range with a gap in it. Never repaired:
            handing a consumer a conversation missing its middle while looking
            fine is exactly the failure the log design exists to prevent.
    """
    cursor = after

    while True:
        batch = await store.read(run_id, after=cursor, limit=_PAGE)

        for record in batch:
            if record.seq != cursor + 1:
                raise CorruptLog(
                    CorruptionReason.NON_CONSECUTIVE_SEQ,
                    run_id,
                    record.seq,
                    f"the stream asked for records after {cursor} and the next one is "
                    f"{record.seq}. A tailing reader that skipped this would hand the "
                    "consumer a conversation missing its middle.",
                )
            cursor = record.seq
            yield record

            if isinstance(record, RunSettled):
                return

        if stop is not None and stop():
            return

        if len(batch) == _PAGE:
            # A full page probably means more is waiting. Read again rather than
            # sleeping, or catching up on a long backlog takes one poll per page.
            continue

        if notifier is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(notifier.wait(run_id, poll_seconds), poll_seconds * 2)
        else:
            await asyncio.sleep(poll_seconds)


async def stream_until_settled(
    store: Store,
    run_id: RunId,
    *,
    after: int = 0,
    timeout: float | None = None,
    notifier: Notifier | None = None,
) -> list[Record]:
    """Collect every Record from ``after`` until the Run settles.

    A convenience for a caller that wants the whole thing rather than to iterate,
    and for tests. Applies a timeout because a Run that never settles would
    otherwise hang the caller forever; the Run's own deadline is the real bound,
    and this is the caller's.
    """
    collected: list[Record] = []

    async def drain() -> None:
        async for record in stream(store, run_id, after=after, notifier=notifier):
            collected.append(record)

    if timeout is None:
        await drain()
    else:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(drain(), timeout)
    return collected


def subscribe(
    store: Store, run_id: RunId, on_record: Callable[[Record], Awaitable[None]]
) -> asyncio.Task[None]:
    """Run a callback for every Record until the Run settles.

    For a consumer bridging to a websocket or a server-sent-events endpoint,
    where a task is more convenient than an iterator.
    """

    async def pump() -> None:
        async for record in stream(store, run_id):
            await on_record(record)

    return asyncio.create_task(pump())
