---
name: psych-agents
description: >-
  Build an `AgentSpec` for the Psych runtime: instructions, model and fallbacks,
  every field on `Limits` and `SuspensionPolicy`, stop conditions, and the flags
  that turn on tasks, components and answer style. Use whenever someone is
  writing, editing or reviewing a Psych agent definition, asks what an agent can
  be configured with, wants to change turn or step budgets, hits
  `SpecValidationError`, asks why a Run stopped early or looped, or asks why two
  Specs produce different Version hashes. Read before writing any `AgentSpec`,
  because the defaults (32 turns, 48 steps, 900s deadline) are load-bearing, and
  because a Spec that holds a callable rather than a tool name is the mistake
  that breaks serialisation and gets a change rejected.
---

# AgentSpec

An agent is data. `AgentSpec` is a frozen Pydantic model with no callables in
it, which is what lets it be hashed into a `Version`, stored, and replayed after
a crash.

```python
spec = psych_runtime.AgentSpec(
    name="support",
    description="Answers order questions and issues refunds.",
    instructions="Help the customer with their order. Be brief.",
    model=psych_runtime.ModelRef(model="gpt-4o", temperature=0.2, fallbacks=("gpt-4o-mini",)),
    tools=(
        psych_runtime.CodeTool(name="lookup_order"),
        psych_runtime.CodeTool(name="issue_refund", interruptible=False),
    ),
    limits=psych_runtime.Limits(max_turns=12, deadline_seconds=300),
)
```

## Fields

| Field | Default | What it is |
|---|---|---|
| `name` | required | Identifier. Lowercase, digits, `-` and `_`. |
| `description` | `""` | Shown to a caller choosing between agents, including a parent's delegation tool. |
| `instructions` | `""` | The system prompt body. May reference a skill with `[[skill:name]]`. |
| `model` | required | A `ModelRef`. No default: there is no model Psych could pick that would not surprise someone. |
| `tools` | `()` | `CodeTool` and `HttpTool` grants, by name. |
| `mcp_servers` | `()` | See `psych-mcp`. |
| `a2a_peers` | `()` | See `psych-a2a`. |
| `skills` | `()` | Instruction packs loaded on demand. See `psych-agent-skills`. |
| `subagents` | `()` | Nested agents to delegate to. See `psych-subagents`. |
| `spawn` | `None` | A `SpawnEnvelope` permitting children composed at run time. |
| `compaction` | `None` | Off by default. See `psych-compaction`. |
| `code_execution` | `None` | Off by default. Lets the agent write and run a program. See `psych-sandbox`. |
| `limits` | `Limits()` | Below. |
| `suspension` | `SuspensionPolicy()` | Below. |
| `tasks_enabled` | `False` | Gives the model `update_tasks` to keep a visible plan. |
| `components_enabled` | `False` | Gives it `show_component` to answer with a card, chart or timeline instead of a sentence. |
| `answer_style` | `None` | `"concise"` trims the closing turn. |

Every one of these is inside the Version hash, including the flags. An agent
that keeps a plan is a different agent from one that does not, so it is a
different Version.

## Code execution

`code_execution=None`, the default, means the agent is never shown `run_code`,
whatever sandbox the deployment wired. Present, it is a request expressed in
serialisable names — never a backend, a client or a credential:

```python
psych_runtime.CodeExecution(
    enabled=True,
    profile="default",  # a name the Runtime maps
    isolation=psych_runtime.IsolationLevel.ISOLATED,  # the minimum acceptable
    network=psych_runtime.NetworkAccess.DENIED,
    limits=psych_runtime.CodeExecutionLimits(wall_seconds=20),
    bindings=("lookup_order",),  # None means every granted tool
    output=psych_runtime.OutputPolicy(preview_bytes=4_000),
    artifacts=psych_runtime.ArtifactPolicy(),
)
```

Everything here narrows and never widens: the deployment's profile caps the
limits, the tenant's Policy caps that, and the Spec asks for something no wider
still. `bindings` must name tools the Spec already grants, and publishing fails
if it does not. `profile` must be a profile the Runtime knows, and publishing
fails if it is not. The whole object joins the Version hash. See
`psych-sandbox`.

## ModelRef

```python
psych_runtime.ModelRef(
    model="gpt-4o",
    temperature=0.2,  # 0..2
    top_p=None,  # >0..1
    max_output_tokens=None,
    reasoning_effort=None,  # "low" | "medium" | "high"
    fallbacks=("gpt-4o-mini",),
)
```

`fallbacks` is failover for a provider outage, tried in order on a transient
failure. It is not routing on cost or capability. Psych ships no model router
and will not grow one.

## Limits, and what each one prevents

```python
psych_runtime.Limits(
    max_steps=48,  # workflow steps across the Run
    max_turns=32,  # model calls in an agent loop
    max_tool_calls_per_turn=16,
    deadline_seconds=900.0,  # wall clock, enforced in the process holding the Attempt
    transient_retry_budget=8,  # retries across the whole Run, not per call
    max_delegation_depth=3,
    max_fanout_per_turn=4,  # concurrent delegations in one turn
    failure_streak_threshold=3,  # consecutive failures of ONE tool before the model is told to stop
    failure_streak_hard_stop=6,  # and before the Run is stopped for it
    repeat_call_threshold=3,  # identical call with identical arguments
    repeat_call_hard_stop=6,
    stream_idle_seconds=300.0,  # no token for this long means the stream is dead
    large_result_bytes=32_768,  # elide a tool result larger than this for the model
    max_history_records=300,  # how much of a conversation chain is replayed into the model
)
```

Three of these are worth understanding rather than tuning blindly:

- **`failure_streak_*` counts per tool per Run, not per turn.** A per-turn reset
  would let a model launder a broken tool by taking a turn off. The count
  survives suspend and resume, and does not count a call the user cancelled or
  one a policy refused before execution.
- **`deadline_seconds` and the lease answer different questions.** The lease
  follows process liveness and lets another Worker take over. The deadline is
  enforced in the process holding the Attempt and catches the hang the lease
  cannot see.
- **`max_history_records` bounds replay, not the person's transcript.**
  `psych_runtime.thread()` still shows the whole conversation; this is only what gets
  sent to the model, because replaying a long chat is the dominant cost of a
  chat agent.

## SuspensionPolicy

```python
psych_runtime.SuspensionPolicy(
    approval_expires_seconds=86_400.0,
    question_expires_seconds=86_400.0,
    external_expires_seconds=604_800.0,
    children_expires_seconds=3_600.0,
    may_ask_questions=False,  # gives the model `ask_question`
)
```

A decision arriving after expiry never executes: the Run is settled `ABANDONED`
first and `psych_runtime.resume()` raises `SuspensionExpired`.

## Stop conditions

An agent loop ends when the model takes a turn with no tool calls, or when it
hits `max_turns`, the deadline, an interrupt, the failure-streak hard stop, or
the repeat-call hard stop. `psych_runtime.answer()` calls the finishing turn the answer
and everything before it the work.

## Publishing

```python
version = await psych_runtime.publish(
    store, spec, context=psych_runtime.ValidationContext(registered_tools=registry.names)
)
```

Validation runs here and never at run time. Without the `context`, you get
structural checks only, which is right for an offline validation pass and wrong
for a real deployment: a Spec naming an unregistered tool or a dangling
`[[skill:name]]` link will publish and then fail mid-conversation.

Publishing an identical Spec returns the existing Version rather than creating a
duplicate, so redeploying on every boot is free.

## Two Specs, one hash

The same agent built in Python and built from a dict must produce the same
Version hash. Canonicalisation sorts keys, formats floats one way, materialises
defaults before hashing, excludes server-assigned fields, and decides array
order per field. Tuples of tools, servers, peers, skills and subagents are
sorted sets, so declaration order does not change the hash.

If two Specs you expect to match hash differently, compare them field by field
after construction rather than as you wrote them; a default you set explicitly
and one left alone produce the same hash, but a `""` and a `None` may not.

## Gotchas

- **A tool name that collides with a Psych built-in is refused.** Reserved:
  `load_skill`, `read_tool_output`, `remember`, `forget`, `ask_question`,
  `update_tasks`, `show_component`, `run_code`, plus the delegation and
  discovery tools. The model would see two tools with one name and could address
  neither.
- **A `SubagentRef` description must be at least 20 characters.** Bad routing
  traces back to vague descriptions more often than to anything else.
- **Never put a function in a Spec.** Register it and put the name in.
