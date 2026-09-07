# psych_runtime.memory

## Owns

Durable facts across Runs (DESIGN.md §15). Not conversation history. That is
already the log, owned by `psych_runtime.core`. This package owns exactly the second of
the three things people call "memory":

- The `MemoryStore` port (`port.py`): `remember`, `forget`, `recall`, `erase`.
- `MemoryKey` (`port.py`): the isolation boundary, tenant plus end-user id, as
  its own validated type rather than a string built at each call site. Both
  fields are required, so a call site that forgets one fails to construct a
  key at all instead of silently reading or writing under a narrower key than
  intended.
- `StoreBackedMemory` (`store_backed.py`): the default adapter.
- The `remember`/`forget` tool wiring lives in `psych_runtime.tools.builtins`, and
  `load_skill` lives in `psych_runtime.tools.skills`, not here, because those are
  tool-shaped things the agent loop's registry and resolver need, and this
  package should not depend on `psych_runtime.tools` (see "Ports" below).

Memories participate in erasure: `MemoryStore.erase(scope, end_user_id)`
deletes everything for one end user, immediately and completely, because a
consumer's own customers will ask them for exactly that and a soft delete or a
tombstone would be lying about it.

## Why the default adapter does not write through `Store`

It would be neater if `StoreBackedMemory` read and wrote through
`psych_runtime.store.port.Store`, and that was the first design tried. It does not
work, for a reason worth stating rather than working around: `Store`'s log is
append-only and its Records are immutable by design (DESIGN.md §6, rule 1).
Memory facts need `forget` and `erase` to actually delete data, which
Store's contract has no operation for anywhere, on any of its four adapters.
Modelling a fact as a Record would mean either widening the closed `Record`
union for a type that violates rule 1 the moment it is deleted, or writing to
the log under a synthetic run id and reading it back by scanning past the
reducer, relying on adapter internals no `Store` contract promises.

So `StoreBackedMemory` keeps its own small, mutable, per-process table of
facts, keyed by `MemoryKey`, guarded by a lock, the same shape
`psych_runtime.store.memory.InMemoryStore` already uses for the `Store` port's own state,
and, by that adapter's own docstring, a legitimate and sufficient story for
tests and single-process deployments. A consumer who needs memories to survive
a process restart on Postgres, MySQL or DynamoDB implements the same four-method
protocol against their own table; it is a short adapter, not a project, because
nothing about memory's correctness depends on the backend the way lease
semantics do. This is what "one adapter, not four" buys: `MemoryStore` is not
special-cased to need per-backend implementations the way `Store` is.

## Does not own

Semantic retrieval. No embeddings, no chunking, no indexing, no reindexing, and
no hook shaped like a place one would later plug those in. `recall()` returns
every fact for an end user, oldest first, full stop. There is no ranking,
no similarity search, no partial result. The consumer supplies a vector store
and a retrieval tool of their own if they want one (DESIGN.md §15).

This refusal is deliberate and it is the one most worth re-litigating on a bad
day, so it is worth defending plainly: built-in RAG is the most requested and
the most regretted feature a framework can ship. It looks like a small
addition, an embedding call and an index and a similarity search. It is
actually a permanent commitment to an embedding model that will be deprecated, a chunking
strategy that will be wrong for someone's documents, and a reindexing job that
someone has to operate forever. None of that is a runtime concern; all of it is
a product concern with its own release cadence, its own cost model and its own
failure modes, and none of it fits the "everything a platform needs except the
platform" promise this project makes. A consumer who needs retrieval already
has, or will build, an opinion about which vector store and which embedding
model; Psych does not need one, and every line of retrieval code here is a line
that makes migrating off that opinion harder for them.

## Ports

Defines `MemoryStore`.
