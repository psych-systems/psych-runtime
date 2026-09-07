---
name: psych-report
description: >-
  Read what a Psych Run did through the five projections over its log:
  `psych_runtime.report()` (steps, tool calls, model calls, usage, cost, latency,
  subagent tree), `psych_runtime.answer()` (the conclusion plus the work behind it),
  `psych_runtime.status()` (a serialisable RunStatus for a UI), `psych_runtime.state()` (the
  reducer's working object) and `psych_runtime.thread()` (a whole conversation across a
  chain of Runs). Use whenever someone displays a Psych Run in a UI, needs an
  audit trail, asks what an agent did or why, asks which projection to use for a
  chat transcript versus a debug view, or hits CorruptLog. Read before building
  any read path, because `status()` is the one shaped for a screen and `state()`
  is the reducer's internals, and passing `scope=` is what stops a leaked run id
  being a readable conversation.
---

# Reading a Run

Every projection is a **pure fold over the log**. None is a parallel copy, so no
two can disagree, and any of them can be recomputed at any time.

| Call | Returns | Use for |
|---|---|---|
| `psych_runtime.status()` | `RunStatus`, frozen and JSON-serialisable | A UI. Start here. |
| `psych_runtime.answer()` | `AnswerView` | "What did it conclude, and what did it do to get there." |
| `psych_runtime.thread()` | `ThreadView` | A chat transcript across a chain of Runs. |
| `psych_runtime.report()` | `RunReport` | Audit, debugging, billing. |
| `psych_runtime.state()` | `RunStateView` | Extending the runtime. Rarely what you want. |
| `psych_runtime.records()` | `Sequence[Record]` | Building your own projection. |

Every one takes `scope=`. **Pass it whenever you serve end users**: a Run
belonging to another tenant is then refused rather than returned, which is what
keeps one leaked run id from being a readable conversation.

## status(): what to put on a screen

```python
status = await psych_runtime.status(store, run_id, scope=scope)
status.lifecycle  # queued | running | waiting | stopping | done | failed | stopped
status.terminal_state
status.suspend_reason, status.suspend_expires_at
status.pending_approval  # the exact call awaiting a decision
status.pending_question
status.components, status.tasks
status.turn, status.tool_calls, status.model_calls
status.head_seq  # reconnect a stream from here
status.failure_message, status.failure_kind
```

`Lifecycle` is coarser than `TerminalState` and finer than the store's
`RunState`: those answer "how did it end" and "is it claimable", and this
answers "what should the screen say".

`STOPPING` earns its place. An abort is in the log and the terminal record has
not landed. A UI with no word for it shows "running" and offers a stop button
that does nothing, which is how a person concludes stop is broken.

`status()` exists because `state()` is the reducer's own mutable working object,
carrying bookkeeping whose purpose is making the next fold cheap
(`open_tool_calls`, `repeat_counts`, `approval_decisions`) and which is not
JSON. Serving that to a UI made every consumer write their own projection and
decide separately what a status even is.

## answer(): the conclusion and the work

```python
view = await psych_runtime.answer(store, run_id, scope=scope)
view.text  # the answer; "" when there is not one yet
view.finished  # distinguishes "said nothing" from "no answer yet"
view.work  # every turn before the answer, oldest first
view.tool_call_count
view.summary()  # "3 turns, 2 tool calls", or "" when there is no work
```

The split is **derived, not decided**: the answer is the turn the loop itself
finished on, and the work is everything before it. Nothing asks the model to
classify its own question, which is a judgement it is unreliable at and does not
need to make.

`summary()` counts rather than characterises. "Looked up the order" would be a
guess about what the tools did; "2 tool calls over 3 turns" is what happened,
and the reader opening the section is about to see the rest anyway.

## thread(): the whole conversation

A second chat message starts a **new Run**, so one Run's messages are one
exchange rather than the conversation a person sees.

```python
thread = await psych_runtime.thread(store, run_id, scope=scope, limit=None)
thread.run_ids  # oldest first
for m in thread.messages:
    m.role, m.content, m.run_id, m.seq, m.at, m.tool_name, m.is_error
```

It walks `continues_run_id` back to the Run that opened the thread. **The walk
stops rather than crossing a tenant**, and enforcing that at read time as well
as write time is the point: a control enforced only on write is worse than none.

Distinct from what gets replayed into the model, which is bounded by
`Limits.max_history_records` because replaying a long chat is the dominant cost
of a chat agent. A person scrolling back expects their whole conversation, not
the slice the model was shown.

A link naming a Run your retention policy already deleted stops the walk and
renders what survives rather than failing the whole thread.

## report(): everything, typed

```python
report = await psych_runtime.report(store, run_id, child_depth=0, scope=scope)

report.spec_name, report.version_hash, report.system_prompt  # the prompt AS SENT
report.steps  # StepReport, workflow only
report.tool_calls  # ToolCallReport, in start order
report.model_calls  # ModelCallReport, in start order
report.suspensions  # SuspensionReport: reason, who, when, decision
report.compactions
report.failure_streak_trips
report.terminal_state, report.orphaned_attempt_id
report.totals  # this Run: usage, cost, unpriced_model_calls
report.subtree  # plus every descendant
report.totals.latency  # wall_clock / model / tool / compaction / unaccounted
report.continues_run_id
```

`child_depth` is **bounded rather than unlimited** so a deep delegation tree
cannot read the whole database by accident. `0` reports this Run only.

This ships with Psych rather than being left to consumers because it is the most
visible thing you get on day one, and if every consumer writes their own
projection they will each get the token arithmetic wrong in a different way.

## CorruptLog

A log the protocol could not have produced raises a typed `CorruptLog` carrying
a `CorruptionReason`, and the Run fails loudly. **Never repair it.** Silent
repair hides the writer bug that produced it and corrupts everything downstream.

A legally incomplete log, which is the normal shape after a crash, is a
different thing and folds cleanly.

Psych implements fourteen reasons: the eleven the design lists, plus
`non_consecutive_attempt` and `invalid_compaction_reason` for states its own
protocol can reach, and `inconsistent_cost` for a Run priced in two currencies.

## Gotchas

- **`state()` is not `status()`.** Use `status()` unless you are extending the
  runtime.
- **`report.system_prompt` is the prompt as sent,** read from the log, not
  re-rendered. That is what makes it useful when a Run went wrong.
- **An empty `answer.text` needs a reason,** and the reason is on `status()`. A
  reader shown a blank space needs `failed` or `waiting`, not nothing.
- **Reports are recomputable.** If a number looks wrong, the log is the
  authority; re-derive rather than patching a stored total, because there is no
  stored total.
