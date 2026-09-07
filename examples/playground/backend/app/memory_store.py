"""A durable `MemoryStore`, and the decision about who an end user is here.

DESIGN.md §15 splits three things people call "memory" and Psych owns two of
them. Conversation history within a Run is already the log. Durable facts
across Runs are this: what the agent still knows tomorrow, after the
conversation that taught it has ended.

## Why this file exists rather than using the shipped adapter

`psych_runtime.memory.store_backed.StoreBackedMemory` keeps its facts in a dict on the
instance. Its own docstring is honest about that, and it is the right default
for tests and for a single-process deployment that does not need them to
outlive the process.

It is the wrong default *here*, and for a reason that goes to what memory is:
the whole claim is that a fact survives the conversation that produced it. A
memory store that empties on restart would demonstrate the API and disprove
the feature, which is worse than not wiring it at all. So the console writes
its own file-backed adapter, which is exactly what a consumer does with a port.

Same shape as `app.settings_store`: one JSON file, one lock, an atomic
`<path>.tmp` write and rename, so a crash mid-write never leaves a truncated
file for the next boot to fail on.

## Who is the end user

`Runtime._end_user_id` resolves in a documented order: the Run's input, then
`Runtime.end_user_id`, then `Scope.principal`. In this console `principal` is
already the account id, so wiring a memory store and changing nothing else
would give one bucket of facts per account **by falling through to the third
case**. That is very likely the behaviour somebody wants, and arriving at it
by accident is still wrong: nobody reading the code later would be able to
tell a decision from a default.

So `app.main` passes `end_user_id` in the Run's input explicitly. Three things
follow from that, all of them worth having:

- It is a decision, written where it is made.
- It is **in the log**, on `RunAdmitted.input`, so a trace read months later
  says whose memories that Run was reading rather than leaving it to be
  re-derived from whatever `Scope.principal` happened to be.
- It demonstrates the mechanism a real consumer needs. A company using Psych
  is a *developer* whose agent serves *their* customers, and the customer is
  the end user, not the developer. Passing the id per Run is how they do that,
  and the console now shows the shape of it rather than only the degenerate
  case where the two coincide.

The alternatives were considered and rejected. Per conversation thread is
closer to context than memory and duplicates what the log already holds. A
fake end-user picker in the UI would be demo furniture: this console really is
one person talking to their own agents, and inventing a customer to show off a
capability is the kind of thing this repo refuses elsewhere.

## Isolation is the port's, not this file's

`MemoryKey` requires both a tenant and an end-user id, and `memory_key()` is
the one place it is built. This adapter stores facts under that key and does
no key arithmetic of its own, so there is no second place for a bug that drops
the tenant to live.

## What this deliberately does not do

No embeddings, no ranking, no similarity search. `recall` returns every fact
for one end user, oldest first, in full. DESIGN.md §1 refuses semantic
retrieval and the memory package's README defends that refusal at length: it
looks like a small addition and is a permanent commitment to an embedding
model, a chunking strategy and a reindexing job somebody has to operate.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.scope import Scope
from psych_runtime.memory.port import Memory, MemoryKey, memory_key

__all__ = ["FileMemoryStore"]


class _MemoryFile(BaseModel):
    """Every fact this installation holds, verbatim on disk.

    A flat list rather than a map keyed by `MemoryKey`: a Pydantic model is not
    a JSON object key, and flattening to a string key here would reintroduce
    exactly the ad-hoc key formatting `MemoryKey` exists to prevent. Each fact
    already carries its own key, so the grouping is rebuilt on load.
    """

    model_config = ConfigDict(extra="forbid")

    facts: list[Memory] = Field(default_factory=list)


class FileMemoryStore:
    """A `psych_runtime.memory.port.MemoryStore` that survives a restart.

    Structurally satisfies the port: `remember`, `forget`, `recall`, `erase`.
    Not declared as implementing it, matching how the other adapters in this
    backend are wired -- the Protocol is `runtime_checkable` and the wiring
    site is where the shape is actually checked.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._facts: dict[MemoryKey, dict[str, Memory]] = {}
        self._loaded = False

    def _load_unlocked(self) -> None:
        """Read the file once, on first use. Callers hold the lock.

        Lazily rather than in a constructor because construction happens in
        `lifespan` where an exception takes the whole process down, and a
        corrupt memory file should cost somebody their remembered facts rather
        than their console.
        """
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        parsed = _MemoryFile.model_validate_json(self._path.read_text())
        for fact in parsed.facts:
            self._facts.setdefault(fact.key, {})[fact.id] = fact

    def _flush_unlocked(self) -> None:
        """Write every fact out. Callers hold the lock.

        Written through on every change rather than flushed periodically: the
        moment a person expects a fact to be durable is the moment the agent
        said it remembered.
        """
        if self._path is None:
            return
        state = _MemoryFile(
            facts=[fact for bucket in self._facts.values() for fact in bucket.values()]
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(self._path)

    async def remember(self, scope: Scope, end_user_id: str, content: str) -> Memory:
        """Store one new fact. Always adds; never merges or deduplicates.

        Deduplication would need to decide when two facts are "the same", a
        judgement the port deliberately leaves above itself.
        """
        key = memory_key(scope, end_user_id)
        fact = Memory(
            id=uuid.uuid4().hex[:16],
            key=key,
            content=content,
            created_at=datetime.now(UTC),
        )
        async with self._lock:
            self._load_unlocked()
            self._facts.setdefault(key, {})[fact.id] = fact
            self._flush_unlocked()
        return fact

    async def forget(self, scope: Scope, end_user_id: str, memory_id: str) -> bool:
        """Remove one fact by id, if it belongs to this Scope and end user.

        Looked up under the key rather than by scanning for the id, so a fact
        id guessed or copied from another tenant finds nothing. Returns
        `False` rather than raising: asking to forget something already gone is
        not an error a caller needs to handle.
        """
        key = memory_key(scope, end_user_id)
        async with self._lock:
            self._load_unlocked()
            existed = self._facts.get(key, {}).pop(memory_id, None) is not None
            if existed:
                self._flush_unlocked()
            return existed

    async def recall(
        self, scope: Scope, end_user_id: str, *, limit: int | None = None
    ) -> tuple[Memory, ...]:
        """Every fact for this Scope and end user, oldest first.

        `limit` truncates the oldest-first order and never ranks or selects.
        The full set is the point: this is where DESIGN.md §15 draws the line
        against retrieval.
        """
        key = memory_key(scope, end_user_id)
        async with self._lock:
            self._load_unlocked()
            facts = sorted(self._facts.get(key, {}).values(), key=lambda f: f.created_at)
        return tuple(facts if limit is None else facts[:limit])

    async def erase(self, scope: Scope, end_user_id: str) -> int:
        """Delete every fact for this Scope and end user.

        Complete and immediate, never a tombstone. DESIGN.md §15 exists because
        a consumer's own customers will ask them to delete their data, and a
        fact still readable after erasure was asked for is a bug rather than a
        caching nuance.
        """
        key = memory_key(scope, end_user_id)
        async with self._lock:
            self._load_unlocked()
            bucket = self._facts.pop(key, None)
            if bucket:
                self._flush_unlocked()
            return len(bucket or {})
