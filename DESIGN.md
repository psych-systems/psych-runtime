# Psych design document

**Status:** settled design, ready to build against.
**Audience:** engineers and coding agents implementing Psych.
**Language:** Python 3.12+.
**Licence:** Apache-2.0.

This document is the complete specification of what Psych is, how it is built, and
how it is used. It contains no task list, and that is deliberate. This document
is the reference work items point back at, not a plan that goes stale beside
them.

---

## 1. What Psych is

Psych is an **embeddable agent runtime**. It is a library a company imports to build
their own agentic platform. It ships everything a platform needs except the platform.

The one-line framing: *everything a platform needs except the platform.*

A consumer of Psych is a company with an existing backend, an existing database, an
existing identity system and an existing frontend. They want their users to create
agents and workflows, run them, and see what happened. They do not want to write an
agent loop, a durable execution engine, a token accounting system or an MCP client
pool. Psych is those things, exposed as functions they call from their own code.

### What Psych is not

Psych does not own, and must never grow:

- an HTTP server, a route table, or any transport
- authentication, identity, users, sessions-as-login
- organizations, teams, roles or permissions
- a database (it uses one through a port; it does not own one)
- a scheduler daemon, timer or cron evaluator
- a UI, or any rendering
- prompt management or a prompt library
- an evaluation framework
- a vector store, embeddings, chunking or retrieval
- budget enforcement (metering yes, budgets no)
- a model router (explicitly out of scope for v1)

Each of these is refused because the consumer already has one, or because owning it
would make Psych a platform rather than a library. A pull request that adds one is
rejected on principle, not on quality.

### Naming

Psych is a **library**, not a framework. A framework inverts control; Psych does not.
The consumer calls Psych; Psych calls back only through ports the consumer supplied.
Do not describe it as a framework in documentation or in code comments.

---

## 2. The choices everything else rests on

The sections that follow specify Psych in full. This one names the handful of
decisions the rest of the document depends on, so a reader meets them once,
together, before meeting them again in detail. Each is stated with the failure it
prevents, because that is what makes it hard to argue with later.

**The log is the only truth.** A Run's state is a pure fold over an append-only,
gapless Record log (§6). A Worker that dies mid-tool-call is recovered by replaying
that log, not by reconciling a snapshot against reality. The fold is pure so the
state a restart computes is provably the state the dead process had.

**A contradictory log fails loudly and is never repaired** (§6). A log the protocol
could not have produced is a writer bug, and silently patching it at read time hides
the bug while corrupting everything downstream of it. A legally incomplete log, the
normal shape after a crash, is a different thing and folds cleanly.

**One store port over an append-only log with conditional writes** (§7). Every
adapter needs the same three primitives: append at an expected sequence, claim under
a condition, and read a range. That is what lets one persistence model serve
Postgres, MySQL and DynamoDB without a second code path, and it is what makes the
queue be the store rather than a second system to keep in step.

**Supervision and execution are separate loops** (§8.1). A loop that both executes
Attempts and enforces their deadlines cannot enforce a deadline against itself: one
hung await blocks the only code that could have noticed. The supervisor pass is
bounded, storage-only, and arms its successor before doing anything that can fail.

**A lease and a deadline answer different questions** (§8.2, §8.4). A lease asks
whether some process still claims to own a Run and is renewed on process liveness. A
deadline asks whether the Run has run too long and is enforced in the process holding
it. Psych ships both, independently, because deriving either from the other breaks
it.

**Access narrows and never widens** (§10.5). What the server offers contains what
the tenant permits, contains what the Spec grants, contains what is callable now. One
function computes that intersection and both the validator and the runtime call it,
so the two cannot drift.

**Scope threads through every call** (§14). Multi-tenant correctness without Psych
owning a permission model, orgs, roles or users. The one rule that makes it real:
MCP clients pool by `(scope, server, credential)` and never by URL. That is one line,
and getting it wrong sends one tenant's token on another tenant's call.

**Usage is recorded per model call, split by cache state, and an unknown price is
`None`** (§13.1, §13.2). A single input-token field makes correct cost impossible,
and a silent zero makes metering look right and be wrong.

**Validation runs at publish, never at run** (§4). A Spec is data, it hashes to a
Version, and everything checkable is checked before that Version exists. A customer
waiting on a response is the wrong place to discover a typo.
---

## 3. Vocabulary

These words have exact meanings. Use them in code, in documentation and in commit
messages. Do not introduce synonyms.

**Spec.** A serialisable description of an agent or a workflow. The only artifact
the runtime executes. Data, never code.

**Version.** An immutable, content-hashed publication of a Spec. Runs pin a Version.

**Run.** One durable execution of a Version. Has an id, a Scope, a record log, and a
terminal state.

**Record.** One immutable, sequenced entry in a Run's log. Everything that happens is
a Record.

**Step.** A checkpointed unit of work inside a Run with typed input and output: a
model call, a tool call, or a nested Run. Steps are memoised by a deterministic id.

**Turn.** One model call plus the tool calls it produced. Agents loop over Turns.

**Agent.** A Step that loops Turns until a stop condition.

**Workflow.** A Step that sequences other Steps deterministically.

**Tool.** A named, typed action. Three kinds: code, http, mcp.

**Skill.** An instruction pack loaded on demand into the model's context.

**Subagent.** A nested Run with a narrowed tool set and a delegation depth.

**Scope.** The tenancy and identity context threaded through every call and stamped
on every Record. Opaque to Psych beyond its identity fields.

**Port.** An interface Psych defines and the consumer (or a shipped adapter)
implements. Store, Queue, ModelClient, MemoryStore, Sandbox, Telemetry, Policy,
SecretResolver.

**Attempt.** One execution pass over a Run by one Worker, holding a lease.

**Lease.** A time-bounded claim on a Run held by one Worker, renewed while working.

---

## 4. The spec is the only executable artifact

A Spec is produced by authoring. Authoring has four forms and Psych privileges none:

1. **Chat.** The consumer's users build an agent by talking to one; the conversation
   emits a Spec.
2. **Python builder.** A developer composes a Spec with a typed builder API.
3. **File.** YAML or JSON in the consumer's repository, loaded and validated.
4. **Import.** A Spec produced by another system, or exported from Psych and
   re-imported.

All four converge on the same Pydantic model, the same validator, and the same
publication path. There is exactly one runtime input.

```
  chat ─┐
builder ─┼─→  Spec  ──validate──→  Version (content-hashed, immutable)
  file ─┤                                │
import ─┘                                ▼
                            Run pins a Version for its lifetime
```

### The invariant that must never break

**A Spec references tools by name. It never contains a callable.**

A Python builder may accept a function and register it as a side effect, but what
lands in the Spec is the registered name. The moment a Spec holds a live object it
stops being serialisable, hashable and storable, and the runtime forks into two
execution models. Every reviewer checks this.

### Publication and pinning

Publishing computes a canonical serialisation, hashes it, and stores it as a Version.
Republishing an identical Spec returns the existing Version rather than creating a
duplicate. A Run records its Version hash at admission and reads only that Version
for its entire life, so editing an agent never mutates a Run in flight, and a trace
read six months later says exactly what the agent was.

### Validation happens at publish, not at run

A Spec referencing an unregistered tool, an unreachable MCP connection, a missing
skill or a malformed schema is refused at publish time with a readable error naming
what is missing. A customer waiting on a response is not the right place to discover
a typo.

---

## 5. One engine, two authoring surfaces

Agents and workflows are not two systems. They are two shapes over one execution
engine.

- A **Step** is the unit: typed input, typed output, checkpointed result.
- An **Agent** is a Step that loops: call the model, run the tools it asked for, feed
  results back, repeat until a stop condition.
- A **Workflow** is a Step that sequences Steps deterministically.
- A Workflow step may be an Agent. An Agent's tool may be a Workflow. A Step may be a
  nested Run.

Recursion falls out. There is one durability implementation, one record format, one
metering path, one trace, one resume path.

### Stop conditions for an Agent

An Agent turn loop stops on any of: the model returns no tool calls and finishes; the
step budget is reached; a tool suspends the Run; the abort record is present; the
failure-streak guard trips (§10.5); the deadline fires (§8.4).

### Determinism for a Workflow

Workflows use **step memoisation**, not deterministic replay. Each Step has a
deterministic id derived from its position in the Version plus the Run id. Its result
is written to the log. On resume, a Step whose result is already in the log returns
from the log and is not re-executed.

This is chosen over Temporal-style replay deliberately: replay imposes determinism
rules on surrounding code that consumers will violate, and memoisation gives the
property that actually matters, which is that a crashed Run continues where it
stopped.

---

## 6. The record log

This is the spine of the system. Everything else is built on it.

A Run's state is not stored. It is **derived**. The Run owns an append-only log of
Records, and a pure reducer folds the log into current state.

### Rules

1. **Append-only.** Records are never updated or deleted.
2. **Sequenced.** Every Record carries `(run_id, seq)` where `seq` is a gapless
   integer starting at 1.
3. **Single writer.** Exactly one Attempt holds the lease for a Run and may append.
4. **Conditional write.** Appending Record `n` asserts that `n` does not yet exist.
   A conflict means another writer exists, and the losing Attempt aborts immediately.
5. **The reducer is pure.** Log in, state out, no IO, no clock, no randomness.
6. **Contradiction is refused, never repaired.** A log that could not have been
   produced by the protocol raises a typed corruption error and the Run is failed
   loudly. Silent repair hides the bug that produced it.

### Corruption reasons

These are states the protocol cannot produce. Each is a distinct typed error:

`multiple_open_operations`, `unknown_operation`, `record_after_finish`,
`non_consecutive_seq`, `queue_after_abort`, `invalid_queue_cancellation`,
`inconsistent_step`, `tool_call_mismatch`, `duplicate_tool_invocation`,
`provisioned_entry_mismatch`, `invalid_deferred_handle`.

### What the log gives you for free

Because the log holds everything, four features are projections rather than
subsystems: the run report (§13), the resumable stream (§12), step memoisation (§5),
and the whole trace. Do not build parallel storage for any of them.

---

## 7. Persistence

### The Store port

The Store is deliberately thin. It supports exactly what an append-only log needs, so
it can be implemented on a relational database and on a key-value store with identical
semantics.

```python
class Store(Protocol):
    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        """Conditional insert. Raises SeqConflict if seq exists."""

    async def read(
        self, run_id: RunId, after: int = 0, limit: int | None = None
    ) -> list[Record]: ...
    async def head(self, run_id: RunId) -> int:
        """Highest seq, or 0."""

    async def put_version(self, version: Version) -> None: ...
    async def get_version(self, hash: str) -> Version | None: ...
    async def create_run(self, run: RunHeader) -> None:
        """Conditional insert on run_id. Idempotent by idempotency_key."""

    async def claim(self, worker_id: str, now: float, lease_ms: int) -> RunId | None:
        """Conditional lease acquisition over runnable runs."""

    async def renew(self, run_id: RunId, worker_id: str, now: float, lease_ms: int) -> bool: ...
    async def release(self, run_id: RunId, worker_id: str, state: RunState) -> None: ...
```

No transactions. No joins. No `SELECT ... FOR UPDATE`. Conditional writes only.
This is what makes DynamoDB a first-class target rather than a compromise.

### Adapters, all four shipped at v1

- **in-memory.** For unit tests. Fast, no cleanup, no isolation concerns.
- **postgres.** `asyncpg`. Conditional insert via primary key on `(run_id, seq)`.
- **mysql.** `aiomysql`. Same, with MySQL's own upsert semantics.
- **dynamodb.** `aioboto3`. Partition key `run_id`, sort key `seq`,
  `ConditionExpression="attribute_not_exists(seq)"`. Leases via conditional update on
  the run header item.

Every adapter passes one **store contract suite**. The suite is the deliverable as
much as the adapters: four implementations with no shared test is four divergent
behaviours. It must cover conditional append conflict, gapless sequencing, range
reads across pagination boundaries, lease acquisition under contention, lease
expiry and renewal, and idempotent run creation.

### The queue is the store

There is no separate queue at v1. A runnable Run is one whose lease is unheld or
expired. `claim()` finds one and takes the lease with a conditional write. This works
identically on all four adapters and needs no broker.

A `Queue` port exists for consumers at scale who want Redis or SQS, but nothing in
Psych requires it, and the default path requires no infrastructure beyond the store.

---

## 8. Execution: worker, attempt, lease, deadline

Psych owns execution. The consumer runs the process; Psych runs the loop.

```python
worker = psych_runtime.Worker(store=store, registry=registry, model=model)
await worker.run()  # blocks, claims runs, executes them
```

### 8.1 Supervisor and attempt are separate

The wake path must not both execute and supervise, or one stalled model stream
wedges the only loop that could enforce a timeout.

- **Supervisor pass**: bounded, storage-only, no failable work before it has armed its
  own successor. It scans for expired leases, fires deadlines, and force-settles.
- **Attempt fiber**: detached, does the actual work, settles its own Run.

A supervisor pass that throws must not end supervision. Arm the next pass first, then
do the work.

### 8.2 Leases

An Attempt claims a Run with a lease and expiry, renews on a heartbeat while working,
and releases on settle. An expired lease is reclaimable by any Worker. The reclaiming
Worker replays the log through the reducer and continues from the last checkpoint.

Critical bug to avoid: a Worker must not renew the lease of an Attempt that is
itself hung, or the expired-lease branch becomes unreachable and the Run never
recovers. A lease and a deadline answer different questions, so Psych ships both
rather than deriving one from the other: the lease follows process liveness, and an
independent wall-clock deadline catches the hang the lease cannot see.

### 8.3 Idempotent admission

`dispatch()` takes an idempotency key. Creating a Run with a key that already exists
returns the existing Run rather than a second one. Delivery is at-least-once;
exactly-once does not exist, and pretending otherwise produces double refunds.

### 8.4 Deadlines and force-settlement

Every Run has a deadline. At the deadline the supervisor fires the Attempt's abort
signal. If the Attempt has not unwound within a grace period (default 60 seconds), the
supervisor **force-settles** the Run: it writes a terminal Record over the hung work
and orphans it. The caller's stream receives a real terminal event.

Orphaned work must be safe to abandon. Every tool call is recorded before execution
and its result recorded after, so an orphaned call is visibly incomplete rather than
ambiguous.

### 8.5 Stream idle timeout

A provider that returns 200 and then stops sending chunks must not hold a turn open.
The model stream read carries an idle deadline: a chunk gap past the cap fails the
read as a **retryable** error, which the transient classifier retries.

Three details that are easy to get wrong and are all required:

- The timer runs **only while a source read is outstanding**, so consumer
  backpressure never trips it.
- The default is generous (5 minutes) because reasoning models are legitimately
  silent for long periods.
- It is configurable per provider, and `0` disables it.

### 8.6 Retry and transient classification

A classifier decides whether a failure is worth retrying: 5xx, 429, 408, connection
errors and idle-timeout are transient; 400, 401, 403 and schema errors are not. Retries
are bounded by a per-Run transient budget, not per-call, so a Run cannot retry
forever by spreading failures across steps.

---

## 9. Interrupts, steering and resume

This is the area where the consumer's current platform is most visibly broken, and it
is not three bugs. It is one missing abstraction.

**An abort is a Record, not a flag.**

### The model

- `abort_requested` is appended to the log with a sequence number.
- Anything enqueued *after* that sequence is not a race to handle. It is the
  `queue_after_abort` corruption, and the reducer refuses the log.
- Input arriving while a Run is executing goes into explicit queues, each of which is
  a Record: `pending_steer` (inject into the current turn), `pending_follow_up`
  (deliver after the current turn settles), `pending_next_run` (deliver to the next
  Run).

"Stop mid-response and immediately send another request" is then a modelled state
transition rather than an accident. The new message lands in `pending_next_run`; the
abort settles the current Run; the next Run starts with the queued message.

### In-flight tool calls on abort

A tool declares `interruptible: bool`, defaulting to `True`.

- `interruptible=True`: the tool's cancellation is requested, the call is recorded as
  aborted, the Run settles.
- `interruptible=False`: the tool is allowed to finish and its result is recorded;
  everything after it is stopped.

A refund tool sets `interruptible=False`. This is the difference between a stopped
agent and a half-issued refund.

### Dangling tool calls

If a Run is reclaimed after a crash with tool calls recorded as started but not
finished, the reducer surfaces them and the loop **settles them explicitly** before
continuing, either by executing them (when idempotent and recorded as safe to retry)
or by recording an aborted result. A model must never receive an assistant message
with tool calls whose results are missing; providers reject it and the conversation is
unrecoverable.

### Streaming after an interrupt

Because the stream is a projection of the log (§12), an interrupt is just more
Records. A client reconnecting after an interrupt asks for everything after its last
sequence and receives the abort and the terminal event in order. Streaming cannot
"break after an interrupt", because there is no separate stream state to break.

---

## 10. Tools

### 10.1 Three kinds, one interface

- **Code tools.** Python functions registered at boot by the consumer. Schema derived
  from type hints via Pydantic. Sync functions are accepted and run in a thread pool.
- **HTTP tools.** Data: a URL, a method, a JSON schema, a credential reference.
  Creatable at runtime by end users.
- **MCP tools.** Data: a server URL, transport, credential reference, allowlist.
  Creatable at runtime.

### 10.2 Resolution happens per turn, never at boot

A `ToolResolver` runs at the start of **every turn**, takes the Run's Scope and pinned
Version, and returns a concrete tool set. Nothing about the tool set is fixed at
process start.

The freshness contract is **fresh at every turn boundary**. Connect an MCP server and
the next turn has its tools, with no restart. Mid-turn mutation is not offered: it
rewrites the provider's cached prompt prefix and contradicts what the model was told
it could do at the start of the turn.

### 10.3 MCP catalog cache

Each MCP connection has a cached tool catalogue with an etag and a TTL, refreshed:

- synchronously on connect, so a newly connected server is usable on the next turn;
- on a background sweep at TTL;
- on demand;
- on the server's `notifications/tools/list_changed` where supported.

### 10.4 Tenant isolation, the rule that matters most

**Never pool MCP clients by URL alone.** Pool by `(scope, server, credential)`.
Pooling by URL will eventually use tenant A's OAuth token for tenant B's call. This
is the bug that ends the project, and it is a one-line mistake.

Credentials resolve through the `SecretResolver` port keyed by Scope. Psych never
stores a raw credential.

### 10.5 Access narrowing

Access narrows monotonically and never widens:

```
what the server offers  ⊇  what the tenant permits  ⊇  what the Spec grants  ⊇  what is callable now
```

A Spec granting a tool the tenant no longer permits simply does not get it. There is
exactly one function computing this intersection, called by both the validator and the
runtime, so the two cannot drift.

### 10.6 Failure-streak guard

A counter tracks consecutive failures of the same tool within a Run, surviving suspend and resume. On reaching the threshold (default 3)
the loop stops handing the model the same tool and injects a message stating what
failed and why, forcing it to change approach. On a higher threshold the turn fails
rather than burning the step budget on a loop.

### 10.7 Unreachable connections

An MCP server unreachable at turn start **fails the Run** by default. A connection may
be marked `optional=True`, in which case its tools are omitted and the model is told
they are unavailable. Silent tool disappearance produces an agent that confidently
tells a customer it cannot issue refunds today.

### 10.8 Large tool results

A result over a threshold is written to the log in full and **elided in the model
context**, replaced by a handle plus a preview. A built-in `read_tool_output` tool
reads the stored result with offset and grep. The log always holds the whole thing;
only the model's view is trimmed.

### 10.9 Approvals

A tool may require approval. The Run suspends (§11) with the pending call recorded,
and resumes on decision. Approval requirements may be set per tool and by selector
over MCP annotations (`read-only`, `write`, `destructive`), with an unannotated tool
treated as `write`.

---

## 11. Suspend and resume

Suspension is first class, not an add-on. A Step returns a suspension with a reason
and a payload schema; the Run persists, releases its lease, and waits.

Reasons: `approval`, `question` (the agent asked the user something), `external`
(waiting on a webhook or callback), `scheduled` (waiting until a time).

```python
await psych_runtime.resume(run_id, payload={...})
```

Resume appends a Record, makes the Run runnable, and the next Worker to claim it
replays the log and continues. Because suspension and resume run through the same
lease and log machinery as everything else, approvals, clarifying questions and
webhook waits are one mechanism rather than three.

Suspensions expire. A Run suspended past its expiry is settled as abandoned rather
than waiting forever on a user who left.

---

## 12. Streaming

The worker executing a Run is a different process from the consumer's HTTP server. The
stream must survive that, and survive reconnects.

**The log is the stream.** Every Record has a sequence number and is persisted before
it is delivered. A client subscribes with "everything after N", receives the backlog
from the store, then tails.

```python
async for record in psych_runtime.stream(run_id, after=47):
    ...
```

An optional pub/sub port (Redis, Postgres `LISTEN/NOTIFY`) reduces latency but is
never required for correctness. Build and test the polling path first; add pub/sub as
an accelerator.

---

## 13. Metering, cost and the run report

### 13.1 Usage accounting

Token counters are recorded per model call, never aggregated at write time. The shape
is not negotiable, because a single `input_tokens` field makes correct cost
impossible:

```python
class Usage(BaseModel):
    input: int  # uncached input
    output: int
    cache_read: int  # read from cache, billed at the read rate
    cache_write: int  # written to cache, billed at the write rate
    cache_write_1h: int = 0  # subset of cache_write at 1-hour retention
    reasoning: int = 0
```

### 13.2 Pricing

A pricing table maps a model id to four per-million rates: input, output, cache read,
cache write. Cost is computed per model call from the recorded usage and the rate at
the time of the call, and the computed cost is written into the Record so it never
changes retroactively when the table is updated.

Price resolution is a **port**. The shipped table is a convenience with a known
staleness problem; a consumer reconciling against their real provider bill supplies
their own.

A model with no known rate records `cost=None`, never `0`. A silent zero makes the
metering look correct and be wrong.

### 13.3 Latency

Every Record carries timings. Per model call: queue wait, time to first token, total
stream duration. Per tool call: duration. Per step and per Run: wall-clock and the sum
of its parts, so the gap between them is visible and attributable.

### 13.4 The report

```python
report = await psych_runtime.report(run_id)
```

Returns a typed object, not a dict, containing: the Spec version and its hash, the
resolved system prompt as sent, every step in order with inputs and outputs, every
tool call with arguments and results, every model call with model id, usage split by
cache state, computed cost and latency breakdown, every suspension and resume, the
terminal state, and totals for tokens, cost and duration.

This ships as part of Psych rather than being left to consumers. It is the most
visible thing a consumer gets on day one, and if every consumer writes their own
projection they will each get the token arithmetic wrong in a different way.

### 13.5 Telemetry

OpenTelemetry with `gen_ai.*` semantic conventions, behind a `Telemetry` port with a
no-op default. Span names and attributes are **declared in a schema** and checked by
conformance tests, so spans cannot drift from their contract as the code changes.

---

## 14. Scope: multi-tenancy without a permission model

Every Psych entry point takes a `Scope`. Every Record is stamped with it. Every store
query is filtered by it.

```python
class Scope(BaseModel):
    tenant: str
    principal: str | None = None
    labels: dict[str, str] = {}
```

Psych does not interpret Scope beyond identity and isolation. It does not know what a
tenant is, whether a principal may act, or how they authenticated.

Authorization is a **port**:

```python
class Policy(Protocol):
    async def allow_tool(self, scope: Scope, tool: str, args: dict) -> Decision: ...
    async def allow_run(self, scope: Scope, version: Version) -> Decision: ...
```

The consumer implements it against their own identity system. This is how Psych gets
tenant-correct data and per-tenant metering without owning orgs, teams or roles.

### Egress

All outbound HTTP from tools, MCP clients and the model client goes through **one
seam**, so a consumer-supplied egress policy covers every path, including paths added
later. A control that covers three of four routes is worse than none, because it will
be believed.

---

## 15. Memory

Three distinct things share the word. Psych owns two of them.

1. **Conversation history within a Run.** Owned by Psych. It is the log.
2. **Durable facts across Runs.** Owned by Psych, through a `MemoryStore` port with a
   default adapter over the configured store. Keyed by Scope plus an end-user id, so
   one tenant's user data cannot reach another's. Exposed to the model as `remember`
   and `forget` tools, and injected into the prompt.
3. **Semantic retrieval.** **Not** owned by Psych. The consumer supplies a vector
   store and a retrieval tool. Psych provides no embeddings, chunking, indexing or
   reindexing.

The third refusal is deliberate and should be defended: built-in RAG is the most
requested and most regretted framework feature, and owning it means owning embedding
model drift and reindexing forever.

---

## 16. Skills

A Skill is an instruction pack: a name, a one-line description, and a Markdown body.
Descriptions of all available skills sit in the system prompt; bodies load only when
the model calls `load_skill`.

Skills may reference each other with `[[skill:name]]` links, forming a graph validated
at publish time. A dangling link fails publication.

Skills are part of the Spec, so they version and pin with it.

---

## 17. Subagents

A subagent is a nested Run with its own log, its own budget and a narrowed tool set.

### Delegation depth

Depth is persisted in the Run header and is **monotone**:
runtime options may deepen it but never lower it. A resumed child arriving with fresh
options must not be counted from zero, or it delegates as though it were top-level and
the recursion budget is defeated.

Depth is capped. Fan-out per turn is capped. Both are configurable and both have
defaults, because one turn spawning a tree is a cost incident.

### Tool narrowing

A subagent's tools are the intersection of what it asks for and what its parent holds,
computed by the same narrowing function as §10.5. A subagent can never reach a tool
its parent was not granted.

### Routing

The parent's delegation tool describes each available subagent by name, purpose and
the tools it holds. Vague descriptions are the root cause of bad routing, so the
validator rejects a subagent whose description is missing or under 20 characters.
The delegation call and its result are Records like any other, so a bad routing
decision is visible in the report.

---

## 18. Code execution

A model writes one program; the runtime executes it in isolation and returns what it
printed and what it returned. The contract:

- **One program per run.** No state carries between executions.
- **Host bindings.** The program calls host-provided functions as ordinary Python
  calls; those calls route back through the normal tool path, with the same Policy,
  egress and recording. There is no privileged back door for code.
- **Failures are data.** A traceback is returned as part of the result so the model can
  read it and fix the program, not raised as an exception that kills the turn.

### Backends

A `Sandbox` port with two adapters at v1:

- **subprocess.** A fresh CPython child process with `rlimit` caps on CPU, address
  space, file size and process count, a scrubbed environment, a temporary working
  directory, and no network unless explicitly granted. Communication over a dedicated
  file descriptor, following dsh's protocol shape.
- **container.** One container per execution for consumers who need real isolation.

**In-process "virtual" sandboxing is explicitly rejected.** `RestrictedPython`, `exec`
with a trimmed `__builtins__`, and AST filtering are all escapable, and shipping one
would be advertising a boundary that does not exist. Process isolation is the minimum.

Network access from a sandbox is off by default and, when granted, passes through the
same egress seam as everything else.

---

## 19. The model layer

### Wire protocol, not an SDK

Psych speaks the **OpenAI-compatible wire protocol** through a `ModelClient` port.
The endpoint is supplied by the consumer and may be a provider or a compatible gateway.

The port is rich enough for a native adapter, because the OpenAI-compatible shape
loses prompt-cache control and reasoning blocks, and prompt caching is the largest
cost lever available. Native provider adapters may be added without changing the port.

### Prompt cache discipline

Prompt construction is **cache-deliberate**: the ordering of system prompt, skills
index, memories and tool definitions is fixed so that a mid-conversation change
invalidates the shortest possible prefix. Any change to prompt assembly order is a
performance change and is reviewed as one.

Because the tool set is pinned per turn (§10.2), it cannot churn mid-turn and
invalidate the prefix.

### No router

Automatic model routing is **out of scope for v1** and no partial implementation
should be added. A Spec names its model. A fallback list is permitted for provider
outages; that is failover, not routing.

---

## 20. Triggers

The consumer owns the clock and the event source entirely: their Kubernetes CronJob,
Celery beat, EventBridge, Kafka consumer or webhook handler.

Psych provides exactly one thing: `dispatch(version, input, scope, idempotency_key)`,
which admits a Run exactly once per key. Psych stores no cron expressions, evaluates
no schedules and runs no timers.

---

## 21. Package layout

```
psych/
  core/          Spec models, Version, Record types, reducer, corruption errors
  runtime/       Worker, supervisor, attempt, lease, agent loop, workflow engine,
                 suspend/resume, interrupts, steering queues
  tools/         registry, resolver, code/http/mcp executors, narrowing,
                 failure-streak guard, large-result elision
  model/         ModelClient port, OpenAI-compatible adapter, usage, pricing,
                 prompt assembly, transient classifier, fake model
  store/         Store port + memory, postgres, mysql, dynamodb adapters,
                 contract suite
  sandbox/       Sandbox port + subprocess and container adapters
  memory/        MemoryStore port and default adapter
  telemetry/     Telemetry port, OTel adapter, span schema, conformance tests
  report/        report projection over the log
  builder/       typed Python builder producing Specs
  testing/       fake model, fixtures, helpers exported for consumers
```

`psych_runtime.core` imports nothing from the other packages. Dependencies point inward.

---

## 22. Engineering rules

These are enforced in review and in CI. They are not suggestions.

### Correctness

- **No mocks for the store.** Store tests run against real Postgres, real MySQL and
  real DynamoDB Local in containers. The in-memory adapter is for testing *other*
  components, never for testing the store contract.
- **No automated test may make a real network call.** Every external call goes through
  an injectable seam: `ModelClient`, `fetch_impl`, `Sandbox`. A test that needs a model
  uses the scriptable fake.
- **The fake model must be able to script multi-step tool-calling runs**, including
  malformed tool calls, streams that stall, and streams that abort mid-token. A weak
  fake produces a weak test suite.
- **Type hints everywhere.** `mypy --strict` passes. No `Any` as an escape, no
  `# type: ignore` without a comment naming the reason.
- **Pydantic models for every boundary.** Records, Specs, tool arguments and results
  are validated models, not dicts.
- **When re-raising, chain the cause** (`raise X from err`). A lost original traceback
  costs hours.
- **One canonical owner per type.** No duplicate definitions, no forwarding shims.

### Testing gates

Three layers, all required, all in CI, all blocking:

1. **Unit.** Pure logic: the reducer, narrowing, pricing arithmetic, the classifier,
   step id derivation, prompt assembly. Fast, no IO.
2. **Functional.** A component against real adapters: the store contract suite
   against all four adapters, the agent loop against the fake model, sandbox execution
   against a real subprocess, MCP against a local stub server.
3. **End to end.** A real Run from dispatch to report: agent with tools, workflow with
   nested agent, suspend and resume, interrupt mid-tool, crash and lease reclaim,
   subagent delegation, code execution, all asserted through `psych_runtime.report()`.

The e2e suite is the regression gate. Every feature ships with an e2e case that fails
if the feature breaks. A green unit suite with a broken e2e case is a broken build.

**Coverage is not the metric; the gate is.** A feature without an e2e case asserting
its behaviour is not done, regardless of line coverage.

### Nothing mocked, nothing demoed

No placeholder implementations, no `NotImplementedError` on a shipped path, no
"TODO: wire this up", no demo-only code paths, no sample data standing in for real
behaviour. If a component cannot be built completely, it is not started. A ticket is
done when its acceptance criteria pass against real adapters, not when the shape
exists.

### Process

- Every change runs the full gate before commit: `ruff`, `mypy --strict`, `pytest`
  (all three layers). All green, no exceptions.
- One verified increment per commit, with a message explaining why, not what.
- Migrations are sequential and forward-only; check the highest existing number before
  adding one.
- A change that makes code unused deletes that code in the same change.
- Comments explain intent, trade-offs and constraints. They never restate the code.
- Public API is `0.x` until the design has survived a second consumer. Breaking changes
  are expected and documented in a changelog written at commit time, not reconstructed
  later.

### Documentation

Every package carries a `README.md` stating what it owns, what it does not, and which
port it implements or defines. A package whose README does not match its code is a
bug.

---

## 23. Definition of done for the system

Psych v1 is done when all of the following are true, verified by the e2e suite:

- An agent Spec built in Python, and the same Spec built from a dict, produce the same
  Version hash and run identically.
- A Run survives its Worker being killed mid-tool-call: a second Worker reclaims the
  expired lease, replays the log, settles the dangling call, and completes.
- An interrupt during a tool call stops the Run, and a message sent immediately after
  starts a new Run carrying that message, with both visible in the log in order.
- A client reconnecting mid-Run with `after=N` receives every subsequent record and
  misses none, including across an interrupt.
- A workflow with a nested agent resumes from its last completed step after a crash and
  does not re-execute completed steps.
- An MCP server connected during a Run is usable on the next turn without a restart,
  and its tools are invisible to a different tenant's Run.
- `psych_runtime.report()` returns correct token totals split by cache state, correct cost, and
  a latency breakdown that accounts for the Run's wall-clock time.
- The same Spec runs identically against all four store adapters.
- A model program executes in the subprocess sandbox, calls a host tool, and its
  traceback on failure reaches the model as data.
- Three consecutive failures of one tool stop the model repeating it.
- No test in the suite makes a network call.
