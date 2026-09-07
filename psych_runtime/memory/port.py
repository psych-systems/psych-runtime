"""The MemoryStore port: durable facts across Runs.

DESIGN.md §15. Three things share the word "memory" and Psych owns two:
conversation history within a Run (already the log, not this module) and
durable facts across Runs (this module). Semantic retrieval is explicitly
refused; see the package README for why.

## The key is a type, not a string built at the call site

Isolation is the property this port exists to guarantee: memories written for
one tenant's end user must be unreachable from any other tenant, and from any
other end user in the *same* tenant. A string key built ad hoc at each call
site (``f"{tenant}:{end_user_id}"``) makes that guarantee depend on every
caller getting the format right forever; one call site using ``"-"`` instead of
``":"`` silently fragments a user's memories into two keys with no error
anywhere. ``MemoryKey`` makes the two fields required, typed and validated in
one place, so a bug that omits one cannot compile, let alone run.

Principal is deliberately not part of the key. DESIGN.md §15 and the ticket
that shaped this module both key memory by "Scope plus an end-user id", and an
end user is who the facts are about, not which internal principal happened to
be acting when a fact was written. Two different principals in one tenant
serving the same end user should see the same memories; two different end
users should never see each other's, regardless of which principal is acting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.scope import Scope

__all__ = ["Memory", "MemoryKey", "MemoryStore", "memory_key"]


class MemoryKey(BaseModel):
    """The isolation boundary for durable memory.

    Frozen and hashable so it can be a dict key in an adapter's own storage,
    which is exactly how ``psych_runtime.memory.store_backed`` uses it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str = Field(min_length=1, max_length=256)
    end_user_id: str = Field(min_length=1, max_length=256)


def memory_key(scope: Scope, end_user_id: str) -> MemoryKey:
    """Build the isolation key. The one place this construction happens.

    Every port method and every tool wrapper calls this rather than
    constructing a ``MemoryKey`` by hand, so there is exactly one place a bug
    that drops the tenant or the end-user id could live, and it is covered by
    ``MemoryKey``'s own field validation.
    """
    return MemoryKey(tenant=scope.tenant, end_user_id=end_user_id)


class Memory(BaseModel):
    """One durable fact, remembered for one end user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    key: MemoryKey
    content: str = Field(min_length=1, max_length=4096)
    created_at: datetime


@runtime_checkable
class MemoryStore(Protocol):
    """Durable facts across Runs, isolated by tenant and end user.

    Not conversation history (that is the log) and not semantic retrieval (that
    is explicitly refused; DESIGN.md §15 and this package's README). A
    ``MemoryStore`` is a flat, small collection of facts per end user, recalled
    in full every time: there is no ranking, no similarity search and no
    partial retrieval, because those are exactly the features that would make
    this module the RAG framework Psych refuses to own.
    """

    async def remember(self, scope: Scope, end_user_id: str, content: str) -> Memory:
        """Store one new fact. Always adds; never merges or deduplicates.

        Deduplication would need to decide when two facts are "the same", which
        is a judgement call Psych is not in a position to make on the
        consumer's behalf. A consumer wanting that supplies it above this port.
        """
        ...

    async def forget(self, scope: Scope, end_user_id: str, memory_id: str) -> bool:
        """Remove one fact by id.

        Returns:
            Whether a fact with that id existed for this Scope and end user.
            ``False`` rather than an exception: asking to forget something
            already gone, or something that never belonged to this end user,
            is not an error a caller needs to handle specially.
        """
        ...

    async def recall(
        self, scope: Scope, end_user_id: str, *, limit: int | None = None
    ) -> tuple[Memory, ...]:
        """Every fact for this Scope and end user, oldest first.

        The full set, not a search result: this is what DESIGN.md §15 draws the
        line at. ``limit`` caps how many are returned when a consumer wants to
        bound prompt size; it never ranks or selects, it only truncates the
        oldest-first order.
        """
        ...

    async def erase(self, scope: Scope, end_user_id: str) -> int:
        """Delete every fact for this Scope and end user. Erasure, not forget.

        DESIGN.md §15: a consumer will be asked by their own
        customers to delete an end user's data, and this is that operation.
        Complete and immediate, not a soft delete: a fact left readable after
        erasure was asked for is a bug, not a caching nuance.

        Returns:
            How many facts were deleted.
        """
        ...
