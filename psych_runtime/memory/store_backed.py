"""The default MemoryStore adapter.

DESIGN.md §15 calls for "a default adapter over the configured store", and it
is worth being precise about what that can and cannot mean given
``psych_runtime.store.port.Store`` as it actually stands (DESIGN.md §7).

## Why this does not write memory facts through ``Store.append``

``Store`` is deliberately narrow: an append-only log keyed by ``(run_id,
seq)``, a content-addressed Version table, and Run headers for leasing. Every
Record the log accepts is immutable once written (DESIGN.md §6, rule 1:
"Records are never updated or deleted") and belongs to one Run's execution
history by construction (``_RecordBase.run_id``, checked against a pinned
Version at admission).

Memory facts need the opposite of both properties. Forget and erase must
actually delete, so a fact has to stop existing rather than gain a tombstone
record beside it forever, and a fact is keyed by tenant and end user rather
than by any one Run: it outlives the Run that wrote it and is read by Runs
that never wrote it. Modelling a fact as a Record would mean either inventing
a new Record variant solely to be deleted (violating rule 1, and requiring a
change to the closed union in
``psych_runtime.core.records``, which is not this module's to open) or storing it under
a synthetic ``run_id`` and reading it back by scanning the log without ever
folding it through the reducer, quietly relying on ``Store`` internals no
adapter's contract actually promises. Both are worse than the alternative
below, and the second is also indistinguishable from a bug the moment someone
adds a real reducer invariant that assumes every record they find belongs to
a genuine Run.

## What "store-agnostic" means here instead

One implementation, not one per backend. Nothing about tenant isolation or
fact storage depends on which of the four ``Store`` adapters a deployment runs,
so this module holds its own small, mutable, per-process table of facts,
addressed by ``MemoryKey``, the same shape ``psych_runtime.store.memory.InMemoryStore``
already uses for the Store port's own state (a lock-guarded dict), and, by that
port's own docstring, a legitimate and sufficient story for tests and
single-process deployments.

A consumer running Postgres, MySQL or DynamoDB in production and wanting
memories that survive a process restart implements the same four-method
``MemoryStore`` protocol against their own table. The protocol is small enough
that this is a short adapter, not a project: it is the same shape every other
port in Psych takes (``Store``, ``ModelClient``, ``Sandbox``, ``Policy`` are
all consumer-implementable), and ``MemoryStore`` is not special-cased to need
four shipped adapters the way ``Store`` does, because nothing about its
correctness depends on the backend the way lease semantics do.

The constructor still takes a ``Store``, kept as ``self._store`` and not read
by any method below. Not a placeholder: it is what makes this genuinely "the
default adapter over the configured store" for the case that actually is safe
today, no adapter beyond this one is offered, and it is where a future adapter
that legitimately can read the configured backend's own connection would plug
in without changing this class's public shape.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Final

from psych_runtime.core.scope import Scope
from psych_runtime.memory.port import Memory, MemoryKey, memory_key
from psych_runtime.store.port import Store

__all__ = ["StoreBackedMemory"]

_ID_PREFIX: Final = "mem"
_ID_RANDOM_BYTES: Final = 12


def _new_memory_id() -> str:
    return f"{_ID_PREFIX}_{secrets.token_hex(_ID_RANDOM_BYTES)}"


class StoreBackedMemory:
    """The default ``MemoryStore``: process-local, correctly isolated.

    Guarded by one ``asyncio.Lock``, mirroring ``psych_runtime.store.memory.InMemoryStore``:
    every method here does a small, fixed amount of dict bookkeeping with no
    ``await`` in between, so a single lock never becomes a throughput problem
    worth trading isolation correctness for.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._facts: dict[MemoryKey, dict[str, Memory]] = {}
        self._lock = asyncio.Lock()

    async def remember(self, scope: Scope, end_user_id: str, content: str) -> Memory:
        key = memory_key(scope, end_user_id)
        fact = Memory(
            id=_new_memory_id(),
            key=key,
            content=content,
            created_at=datetime.now(UTC),
        )
        async with self._lock:
            self._facts.setdefault(key, {})[fact.id] = fact
        return fact

    async def forget(self, scope: Scope, end_user_id: str, memory_id: str) -> bool:
        key = memory_key(scope, end_user_id)
        async with self._lock:
            bucket = self._facts.get(key)
            if bucket is None or memory_id not in bucket:
                return False
            del bucket[memory_id]
            return True

    async def recall(
        self, scope: Scope, end_user_id: str, *, limit: int | None = None
    ) -> tuple[Memory, ...]:
        key = memory_key(scope, end_user_id)
        async with self._lock:
            facts = sorted(
                self._facts.get(key, {}).values(),
                key=lambda memory: (memory.created_at, memory.id),
            )
        if limit is not None:
            facts = facts[:limit]
        return tuple(facts)

    async def erase(self, scope: Scope, end_user_id: str) -> int:
        key = memory_key(scope, end_user_id)
        async with self._lock:
            bucket = self._facts.pop(key, None)
        return len(bucket) if bucket else 0
