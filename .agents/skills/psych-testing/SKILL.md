---
name: psych-testing
description: >-
  Test agents built on Psych without a network: the scriptable `FakeModel`
  (multi-step tool calling, malformed calls, stalled streams, mid-token aborts,
  transient and permanent provider failures), `LogBuilder` for building record
  logs directly, `McpStubServer` over a real loopback socket, and the Store and
  BlobStore contract suites. Use whenever someone writes tests for Psych agents,
  asks how to test an agent loop deterministically, needs to simulate a provider
  failure or a crashed Worker, hits `FakeModelScriptExhausted`, or asks whether a
  store can be mocked (no). Read before writing any Psych test, because no test
  may make a real network call and a weak fake produces a weak suite that passes
  while the real provider breaks.
---

# Testing agents built on Psych

Two rules that shape everything here:

- **No test makes a real network call.** Every external call goes through an
  injectable seam: `ModelClient`, `fetch_impl`, `Sandbox`.
- **No mocked store.** Store tests run against real databases. The in-memory
  adapter tests other components and never proves the store contract.

## FakeModel

```python
from psych_runtime.testing.fake_model import FakeModel

model = (
    FakeModel()
    .turn(text="Checking.", tool_calls=[("lookup_order", {"order_id": "A1"})])
    .turn(text="A1 has shipped.")
)
```

`.turn()` takes `text`, `tool_calls`, `reasoning`, `usage`, `cost`,
`finish_reason` and `chunk_delay_seconds`. `text` and `reasoning` accept a
sequence to script it arriving in chunks.

Failure modes, because the ones you never test are the ones that page you:

```python
FakeModel().stalls(seconds=None)  # a stream that goes quiet
FakeModel().aborts_mid_token(text="partial")  # dies halfway through
FakeModel().raises_transient(status_code=503)  # retryable
FakeModel().raises_permanent(status_code=400)  # not retryable
FakeModel().turn(tool_calls=[("no_such_tool", {})])  # unknown tool name
```

Script malformed tool calls with a `ToolCallScript` carrying raw wire arguments,
which is how you test what happens when a provider sends unparseable JSON.

**A weak fake produces a weak suite,** so the fake's own coverage is part of the
gate in this repository.

## Assert on what the model was sent

```python
model.requests  # every ModelRequest, in order
model.last_request  # the most recent

assert "lookup_order" in {t.name for t in model.last_request.tools}
```

That is how you check prompt assembly and that the tool set is genuinely
recomputed each turn rather than pinned at boot.

## Running past the script raises

`FakeModelScriptExhausted` rather than repeating the last turn. Repeating would
let a model stuck in a tool-calling loop look like it eventually produced a
clean final answer. The error names how many turns were scripted and which call
went past them, so it explains itself without a debugger.

## A whole run in a test

```python
import psych_runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel


async def test_it_looks_the_order_up() -> None:
    model = (
        FakeModel().turn(tool_calls=[("lookup_order", {"order_id": "A1"})]).turn(text="Shipped.")
    )

    async with psych_runtime.session(model, tools=[lookup_order]) as s:
        view = await s.ask(spec, "where is order A1?")

        assert view.text == "Shipped."
        report = await psych_runtime.report(s.store, view.run_id)
        assert [c.tool for c in report.tool_calls] == ["lookup_order"]
```

Assert **through the log**, with `psych_runtime.report()`, `psych_runtime.answer()` or
`psych_runtime.records()`. A feature that works only in memory is a feature that breaks
the moment a Worker dies.

## LogBuilder: a log without running anything

For testing a projection, a reducer change or a UI against a log shape that is
hard to produce for real.

```python
from psych_runtime.testing.logs import LogBuilder

log = (
    LogBuilder(run_id, scope)
    .admitted()
    .attempt()
    .turn()
    .model_started()
    .model_finished(text="", tool_calls=[...])
    .tool_started("c1", tool="issue_refund")
    .suspended(reason=SuspendReason.APPROVAL)
    .resumed(approved=True)
    .tool_finished("c1")
    .settled()
    .build()
)
```

It has a method per record type, including `abort`, `enqueued`, `cancelled`,
`consumed`, `compacted`, `spawned`, `messaged_child`, `child_finished` and
`attempt(reclaimed=True)`. Building a contradictory log on purpose is how you
test that `CorruptLog` fires.

## McpStubServer

A real MCP server over a real loopback socket, not a mock.

```python
from psych_runtime.testing.mcp_stub import McpStubServer, make_server, wire_tool
```

It records what it was asked on `.requests` and `.calls`, and can be told to
stall, break a stream mid-response, demand OAuth, or return each documented
error code. `make_server` and `wire_tool` assemble a Spec against one.

It lives in the shipped package rather than the test suite because it has two
consumers and only one is a test: the playground drives it for the scenarios a
person clicks through. Nothing in it imports pytest, so depending on it costs an
application nothing.

## Contract suites

```python
from psych_runtime.store.contract import StoreContractSuite
from psych_runtime.store.blob_contract import BlobStoreContractSuite
from psych_runtime.sandbox.contract import SandboxContractSuite  # both adapters agree
```

Subclass and override the one fixture. The suite is the deliverable as much as
the adapter: divergent implementations with no shared test are divergent
behaviours waiting to be found in production.

## Three layers

| Layer | Covers |
|---|---|
| **unit** | Pure logic, no IO. The reducer, narrowing, pricing arithmetic, the transient classifier, step id derivation, prompt assembly. |
| **functional** | A component against a real adapter. The store suites, the agent loop against `FakeModel`, the sandbox against a real subprocess, MCP against the stub. |
| **e2e** | A whole Run from dispatch to report, asserted through `psych_runtime.report()`. |

The e2e layer is the regression gate. Every feature ships with a case that fails
when the feature breaks. **A green unit suite over a broken e2e case is a broken
build.** Coverage is not the metric: a feature with no e2e case asserting its
behaviour is not done, whatever the line coverage says.

## Simulating a crash

Kill the Worker mid-tool-call and start another against the same store. The
second reclaims the expired lease, replays the log, settles the dangling call
and completes. This works because nothing about a running Run lives in a
process, and it is one of the ten things the design says must be true.

## Gotchas

- **`MemoryStore` is fine for testing components, not for proving the store
  contract.** Those are different jobs.
- **Set a short `lease_seconds` on the Worker in a crash test,** or the test
  waits out the real one.
- **`filterwarnings = ["error"]`** in this repository, so a warning fails a test.
- **`asyncio_mode = "auto"`**, so async tests need no decorator.
