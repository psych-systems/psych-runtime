---
name: psych
description: >-
  Entry point and router for Psych, the embeddable Python agent runtime
  (`import psych_runtime`) that gives you an agent loop, durable execution, an
  append-only record log, tool resolution, MCP with tenant isolation, token
  accounting and a run report. Read this FIRST whenever a task involves Psych at
  all: building or publishing an agent or workflow Spec, dispatching a Run,
  registering tools, wiring MCP or A2A, approvals, memory, subagents,
  compaction, the sandbox, stores, streaming, cost or the report. It carries the
  mental model, the vocabulary, the invariants that get a change rejected, and
  an index pointing at the right `psych-*` sub-skill. Use it even when the user
  never says "Psych" but the repository imports it, and use it before writing
  any code that touches the library, because Psych's rules (a Spec holds names
  and never callables, MCP pools by scope and not by URL, an unknown price is
  `None` and never `0`) are not guessable from the API shape.
---

# Psych

Psych is a **library**, not a framework. A framework inverts control; Psych does
not. Consumer code calls Psych, and Psych calls back only through ports the
consumer supplied. Never call it a framework in code, comments or docs.

It is what a company imports to build their own agentic platform: everything a
platform needs except the platform.

## The 60-second mental model

An agent is **data**, not code. You write an `AgentSpec` (a Pydantic model),
publish it to get a content-hashed `Version`, and dispatch a `Run` against that
Version. A `Worker` in your process claims the Run and executes it. Everything
that happens is appended to a per-Run log of `Record`s. Every question about a
Run ("is it done", "what did it cost", "what did it say") is a pure fold over
that log.

```
Spec  --publish-->  Version  --dispatch-->  Run  --Worker--> Records
                                                                |
                          report() / answer() / status() / thread() / stream()
```

Five properties follow from that, and they explain most of the API:

- **The log is the only truth.** A Worker killed mid-tool-call is recovered by
  replaying its log, not by reconciling a snapshot. Nothing about a running Run
  lives only in a process.
- **A contradictory log fails loudly.** It raises a typed `CorruptLog` and is
  never repaired, because silent repair hides the writer bug that caused it.
- **Validation happens at publish, never at run.** A customer waiting on a
  response is the wrong place to discover a typo.
- **The queue is the store.** A runnable Run is one whose lease is unheld or
  expired. There is no broker.
- **Psych owns no clock.** You call `dispatch()` from your own scheduler,
  webhook handler or queue consumer.

## The command line

Developer tooling that ships with the package. It scaffolds and reports, and
**never executes a Run** -- `psych new` writes a `main.py` the developer owns
and runs themselves, because a command that could start a Worker would make
Psych something a consumer operates rather than imports (DESIGN.md §1). Never
add `psych run`, `psych serve` or `psych worker`.

```sh
psych new demo && cd demo && python main.py   # runs with no API key
psych new demo --template tour                # approvals, skills, memory, workflows
psych new api --template fastapi              # routes plus a separate Worker process
psych skills install                          # copy these skills into a project
psych doctor                                  # what is installed and configured
```

## The smallest thing that works

Enough to write correct Psych code without reading anything else. `session()`
assembles a store, a registry, a `Runtime` and a running `Worker`, and stops
them again on the way out.

```python
import asyncio

import psych_runtime
from psych_runtime.testing.fake_model import FakeModel


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""  # the docstring IS the tool description
    return {"order_id": order_id, "status": "shipped"}


spec = psych_runtime.AgentSpec(
    name="support",
    instructions="Help the customer with their order.",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    tools=(psych_runtime.CodeTool(name="lookup_order"),),  # a NAME, never the function
    limits=psych_runtime.Limits(max_turns=12, deadline_seconds=300),
)


async def main() -> None:
    model = FakeModel().turn(text="A1 has shipped.")  # or OpenAICompatibleClient
    async with psych_runtime.session(model, tools=[lookup_order]) as s:
        view = await s.ask(spec, "where is order A1?")
        print(view.text)

        report = await psych_runtime.report(s.store, view.run_id)
        print(report.terminal_state, report.totals.usage, report.totals.cost)


asyncio.run(main())
```

Four things that go wrong here and nowhere else:

- **Nothing runs.** No `Worker` is claiming. `session()` starts one; the long
  form needs `asyncio.create_task(worker.run())`.
- **`ToolSchemaError` at registration.** The function has no type hints (the
  schema comes from them) or no docstring (the description comes from it).
- **`SpecValidationError` at publish.** The Spec names a tool nobody registered.
  Pass `context=psych_runtime.ValidationContext(registered_tools=registry.names)` so
  this fails at publish rather than mid-conversation; `session.publish()` does.
- **`finished=False` with empty text.** The Run failed or is still going, which
  is reported rather than raised because a failed Run is fully recorded.
  `psych_runtime.status()` says why.

## Vocabulary, used exactly

Do not invent synonyms for these.

| Word | Means |
|---|---|
| **Spec** | The agent or workflow as data. `AgentSpec` or `WorkflowSpec`. |
| **Version** | An immutable, content-hashed publication of a Spec. |
| **Run** | One execution of a Version, with its own log. |
| **Record** | One sequenced entry in a Run's log. Not a log line. |
| **Attempt** | One execution pass by one Worker holding a lease. |
| **Lease** | A Worker's time-bounded claim on a Run. |
| **Step** | A workflow step, memoised so a resume does not redo it. |
| **Turn** | One model call plus the tool calls it asked for. |
| **Scope** | Tenancy and identity, threaded through every call. |
| **Port** | An interface the consumer implements: Store, ModelClient, Policy, Sandbox, Telemetry, MemoryStore, SecretResolver, BlobStore. |

## The invariants. Break one and the change is wrong

These are cheap to get wrong and expensive to find later.

1. **A Spec references tools by name and never holds a callable.** A builder may
   take a function and register it, but what lands in the Spec is the registered
   name. A Spec holding a live object stops being serialisable and the runtime
   forks into two execution models.
2. **Never pool MCP clients by URL. Pool by `(scope, server, credential)`.**
   Pooling by URL will eventually send tenant A's OAuth token on tenant B's
   call. This is one line and it ends the project.
3. **The reducer is pure.** No IO, no clock, no randomness. Same log in, same
   state out.
4. **Access narrows and never widens.** What the server offers contains what the
   tenant permits, contains what the Spec grants, contains what is callable now.
   One function computes that intersection; the validator and the runtime both
   call it so they cannot drift.
5. **A model with no known price records `cost=None`, never `0`.** A silent zero
   makes metering look correct and be wrong.
6. **In-process sandboxing is rejected.** `RestrictedPython`, `exec` with
   trimmed builtins and AST filtering are all escapable. Process isolation is
   the floor.
7. **Every outbound HTTP call goes through one egress seam,** the model client
   and MCP included. A control covering three of four routes is worse than none,
   because someone will believe it.

## What Psych refuses to own

No HTTP server, no auth, no orgs or roles, no database of its own, no scheduler,
no UI, no prompt library, no eval framework, no vector store or RAG, no budget
enforcement, no model router.

Each refusal is because the consumer already has one, or because owning it turns
Psych into a platform. If a task seems to need one, the answer is to write it in
the consumer's code, not in Psych. Adding one to the library gets rejected on
principle rather than on quality.

## The public API is the `psych_runtime` module and nothing else

`import psych_runtime` and reach for `psych_runtime.<name>`. Anything reached through a submodule
path (`psych_runtime.runtime.agent`, `psych_runtime.tools.resolver`) is internal and moves
without ceremony while the package is `0.x`. The two submodule imports that are
routine and expected are the store adapter you chose
(`from psych_runtime.store.postgres import PostgresStore`) and the test helpers
(`from psych_runtime.testing.fake_model import FakeModel`).

| Call | Does |
|---|---|
| `psych_runtime.session(model, tools=[...])` | Assembles store, registry, Runtime and a running Worker. The short way in. |
| `psych_runtime.publish(store, spec, context=...)` | Validate, hash, store. Idempotent. |
| `psych_runtime.dispatch(store, version, scope, ...)` | Admit a Run, exactly once per idempotency key. |
| `psych_runtime.stream(store, run_id, after=N)` | Every Record after N, then tail until settled. |
| `psych_runtime.stream_text(store, run_id)` | Just the assistant's words. Raises on an abort or a failure rather than ending quietly. |
| `psych_runtime.status(store, run_id)` | What it is doing now, shaped for a screen. |
| `psych_runtime.answer(store, run_id)` | What it concluded, plus the work behind it. |
| `psych_runtime.thread(store, run_id)` | The whole conversation across a chain of Runs. |
| `psych_runtime.report(store, run_id)` | Steps, calls, usage, cost, latency. |
| `psych_runtime.resume(store, run_id, approved=True)` | Deliver a decision to a suspended Run. |
| `psych_runtime.send(store, run_id, message=...)` | Steer a Run that is still executing. |
| `psych_runtime.interrupt(store, run_id)` | Stop a Run. An abort is a Record, not a flag. |
| `psych_runtime.state` / `psych_runtime.records` | The reducer's state, and the raw log. |
| `psych_runtime.agent(name)` / `psych_runtime.workflow(name)` | The fluent builders, which register a tool and grant its name in one call. |

Every read also takes `scope=`. Pass it whenever you serve end users: without
it, one leaked run id is a readable conversation.

## Which sub-skill to read next

Read the one that matches the task. Do not read all of them.

**Starting out**
- `psych-quickstart`, install, first working Run, `psych_runtime.session()`.
- `psych-agents`, `AgentSpec`: instructions, model, limits, stop conditions.
- `psych-workflows`, `WorkflowSpec`: deterministic steps, memoisation.
- `psych-builder`, the fluent Python builder.

**Giving an agent things to do**
- `psych-code-tools`, registering Python functions as tools.
- `psych-http-tools`, calling an HTTP API without writing a function.
- `psych-mcp`, MCP servers, narrowing, pooling, deferred catalogues.
- `psych-mcp-oauth`, OAuth 2.1 against a protected MCP server.
- `psych-a2a`, other agents, in both directions.
- `psych-sandbox`, the model writes a program, a subprocess or container runs it.
- `psych-agent-skills`, Skills in a Spec: instructions loaded on demand.
- `psych-memory`, durable facts across Runs.
- `psych-subagents`, delegation, and children composed at run time.

**Controlling a Run**
- `psych-approvals`, annotations, selectors, and resuming a decision.
- `psych-interrupts`, stopping, steering, and the three queues.
- `psych-suspend-resume`, questions, webhook waits, expiry.
- `psych-compaction`, a conversation too long for the window.

**Reading a Run**
- `psych-streaming`, stream, reconnect with `after=N`, miss nothing.
- `psych-report`, report, answer, status, state, thread.
- `psych-pricing`, usage split by cache state, cost, unknown prices.
- `psych-telemetry`, the Telemetry port and the OTel adapter.

**Operating it**
- `psych-stores`, the four Store adapters and the contract suite.
- `psych-blobs`, where an oversized tool result's bytes live.
- `psych-multitenancy`, Scope, Policy, egress, tenant isolation.
- `psych-testing`, FakeModel, LogBuilder, the MCP stub, contract suites.

## Reading these skills from another repository

If you are working in a project that *uses* Psych rather than in Psych itself,
these files may not be on disk. Fetch the one you need:

```
https://raw.githubusercontent.com/psych-systems/psych-runtime/main/.agents/skills/<skill-name>/SKILL.md
```

To make them permanent for this project, copy the whole directory into it once:

```sh
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/psych-systems/psych-runtime /tmp/psych-skills
git -C /tmp/psych-skills sparse-checkout set .agents/skills
mkdir -p .agents/skills && cp -r /tmp/psych-skills/.agents/skills/psych* .agents/skills/
```

They are plain Markdown with YAML frontmatter, so any agent that reads a skills
directory picks them up, and any agent that does not can still be pointed at a
file directly.
