---
name: psych-interrupts
description: >-
  Stop or steer a running Psych agent: `psych_runtime.interrupt()`, the three
  `QueueKind` queues on `psych_runtime.send()` (STEER, FOLLOW_UP, NEXT_RUN), what
  happens to an in-flight tool call, and how "stop and immediately send another
  message" is a modelled transition rather than a race. Use whenever someone
  wires a stop button, wants to inject a message into a running agent, asks why
  a steer did not reach the current turn, asks what happens to a half-finished
  tool call on abort, or is building multi-turn chat on Psych and unsure whether
  to use `send()` or `dispatch(continues=...)`. Read before implementing any of
  it, because a second chat message after a Run settles is `dispatch(continues=)`
  and not `send()`, and `interruptible=False` is what stops a refund being
  half-issued.
---

# Interrupts and steering

**An abort is a Record, not a flag.** It has a sequence number, so "what arrived
after the stop" is a question the log answers rather than a race the runtime has
to win. A client streaming with `after=N` sees the abort and everything after it
in order.

```python
await psych_runtime.interrupt(store, run_id, reason="the user pressed stop", by="user-42")
```

## Three queues, because there are three intentions

Collapsing them into one flag is what produces the race that breaks streaming
after an interrupt.

| Queue | Means | After an abort |
|---|---|---|
| `QueueKind.STEER` (default) | Inject into the turn running right now. | Refused |
| `QueueKind.FOLLOW_UP` | Deliver once the current turn settles, instead of letting the Run complete. "Keep going, and also..." | Refused |
| `QueueKind.NEXT_RUN` | Hand a message to whatever Run comes next. | **Legal**, and this is the point |

```python
await psych_runtime.send(store, run_id, message="actually, check the Berlin warehouse")
await psych_runtime.send(store, run_id, message="...", queue=psych_runtime.QueueKind.FOLLOW_UP)
```

A plain string is wrapped as `{"message": ...}`, matching what `dispatch`'s own
`input` accepts, so there is one shape to remember rather than two.

## A steer reaches the turn after this one

Steers are drained at the start of the **next** turn. There is no mid-turn
mutation: tool resolution happens at a turn boundary and stays fixed for the
turn. So a steer sent while the model is mid-stream lands on the following turn,
not the one you were watching. That is not latency to tune away; it is what
keeps a turn's tool set stable while it executes.

## Stop and immediately send another

This is the sequence, and every part of it is in the log in order:

```python
await psych_runtime.interrupt(store, run_id, reason="user pressed stop")
await psych_runtime.send(
    store, run_id, message="do this instead", queue=psych_runtime.QueueKind.NEXT_RUN
)
# once the aborted Run settles:
new_run = await psych_runtime.dispatch(
    store, version, scope, input={"message": "do this instead"}, continues=run_id
)
```

`send(queue=NEXT_RUN)` records that the message arrived and when, which is what
makes the arrival visible even if the continuation is dispatched much later. The
new Run's own admission is what carries the message forward.

## `send()` or `dispatch(continues=...)`

`send()` is a low-level primitive for a Run **already open**. The common case,
a second chat message after the first Run settled with nothing left to steer, is
not this. Use `dispatch(continues=run_id, input=...)`, which starts a fresh Run
carrying the earlier conversation forward instead of injecting into one that is
no longer there to receive it.

`continues` is refused with `AccessDenied` when that Run belongs to a different
Scope. A continuation is another way to reach another Run's content and gets the
same scrutiny as the MCP pool key.

## In-flight tool calls

A tool registered `interruptible=True` (the default) may be cancelled mid-call.
One registered `interruptible=False` is not, and the abort waits for it. That is
the difference between a stopped agent and a half-issued refund.

A call recorded as started whose Worker then died is a **dangling** call. The
next Worker replays the log and settles it rather than guessing. Whether it may
be re-executed is `safe_to_retry`, which defaults to `False` because assuming a
side effect is repeatable is how double refunds happen.

## Terminal states

Every Run reaches exactly one:

| State | Means |
|---|---|
| `COMPLETED` | The loop finished on a turn with no tool calls. |
| `FAILED` | An unrecoverable error. |
| `ABORTED` | Interrupted, or past its deadline. |
| `ABANDONED` | A suspension waited past its expiry. |
| `FORCE_SETTLED` | The supervisor wrote a terminal record over work that would not unwind inside the grace period, and orphaned it. |

`report.orphaned_attempt_id` is set only for `FORCE_SETTLED`.

## Gotchas

- **`interrupt()` on a settled Run raises `RunAlreadySettled`.** Check
  `psych_runtime.status()` first if that is a normal path for you.
- **A steer on a Run whose Worker already stopped goes nowhere useful.** The
  message is recorded; nothing drains it. Dispatch a continuation instead.
- **Aborting does not roll anything back.** Psych has no compensation model. A
  side effect already committed stays committed, which is why
  `interruptible=False` exists.
