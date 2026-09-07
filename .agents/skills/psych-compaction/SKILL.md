---
name: psych-compaction
description: >-
  Keep a long Psych conversation inside the model's context window with
  `psych_runtime.CompactionPolicy`: trigger_tokens measured from real provider usage,
  keep_recent_turns, a cheaper summariser model, extra summary instructions, and
  the CompactionApplied Record that leaves the original log untouched. Use
  whenever a Psych agent runs long enough to hit a context limit, someone asks
  about summarising or truncating history, mentions context window management or
  a provider rejecting an oversized request, or asks whether compaction loses
  the audit trail (it does not). Read before enabling it, because trigger_tokens
  is required with no default and fires one turn late by design, so setting it
  at the real window size means the turn that crosses the line is the one that
  gets rejected.
---

# Compaction

Off by default. `AgentSpec.compaction` is `None`, and a conversation grows until
the provider refuses it.

```python
spec = psych_runtime.AgentSpec(
    name="support",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    compaction=psych_runtime.CompactionPolicy(
        trigger_tokens=96_000,  # required, no default
        keep_recent_turns=3,
        model="gpt-4o-mini",  # None means the agent's own
        max_summary_tokens=2_048,
        summary_instructions=None,
    ),
)
```

It lives on the Spec rather than the Runtime because an agent that summarises
its own history **is a different agent**: what the model is shown on a long Run
is not the same conversation. That difference belongs in the Version hash, not
in a flag a Runtime could change under a published Version.

## Compaction never loses anything

The replaced records **stay in the log**. What compaction produces is a
`CompactionApplied` Record naming the range it replaced and the summary standing
in for it. The report, the trace and the audit trail are untouched.

Compaction changes what the model sees next. It never changes what happened.

## `trigger_tokens` is required, and fires one turn late

There is no default because Psych has no context window to compare against. The
`ModelClient` port speaks to a proxy that can reach models it has never been
told about; `known_models` returns empty for exactly that reason. Any default
would be a guess about somebody else's model. You know which model you pointed
this at and what fraction of its window you want to spend.

It is measured from `Usage`, which the provider reports on every call, not
estimated by a tokenizer. Psych takes no tokenizer dependency: one tokenizer per
provider is a maintenance burden, and an estimate that looks like a measurement
is worse than no number at all.

The number compared is `input + cache_read + cache_write` of the last finished
call, which is the size of the prompt actually sent. Cached input still occupies
the window.

**The consequence: the trigger is read one call late.** A Run compacts on the
turn *after* the one that crossed the line. So set `trigger_tokens` far enough
below the real window that one more turn fits. A real number one turn late beats
an invented one on time, and that trade is deliberate.

## The cut always lands on a turn boundary

A turn is one model call and the tool results it produced, which is what the
log's `turn_started` boundaries mark. `keep_recent_turns` (default 3) stays
verbatim below the summary.

Cutting anywhere else could put an assistant message's tool calls above the line
and their results below it, and a provider rejects a conversation whose tool
calls have no answers. Cutting on a boundary makes that shape unrepresentable
rather than merely unlikely.

## Crash safety

The summary is requested first, the record appended second, and the next model
call third. That ordering makes both crash directions converge:

- **Died after the summary call, before the append.** No record. The next Worker
  recomputes the same cut, finds it still ahead of the boundary, and summarises
  again. One model call is paid for twice; nothing is compacted twice.
- **Died after the append.** The boundary is at the cut. The next Worker
  recomputes the same cut, finds it no longer ahead, and does not summarise.

Appending first and filling the summary in later would leave a boundary with
nothing standing in for the range it replaced, and a replay would send the model
a conversation with a hole in it.

## The summariser is told what it is doing, not who the agent is

The summarisation request carries its own system message rather than the
agent's. A summariser is doing a bounded, mechanical read of a transcript.
Handing it the agent's persona spends tokens on instructions it must not follow
and invites it to answer the conversation instead of summarising it. That is
also why `CompactionPolicy.model` exists: this call can go to a cheaper model.

## `summary_instructions` adds, never replaces

Psych's own instruction always applies: keep what was asked and the constraints
on it, facts established by tool results, decisions and why, what is done and
what is outstanding; preserve identifiers, numbers and quoted text exactly; drop
pleasantries and superseded attempts.

That is the floor and yours cannot lower it, which is the point. You know what
*your* domain cannot afford to lose, and nobody knows in advance what a
summariser will decide was pleasantry. Yours is placed last, where a model
weighs it most heavily. Caps at 4000 characters.

## Reading it back

```python
report = await psych_runtime.report(store, run_id)
for c in report.compactions:
    print(c.seq, c.reason, c.replaced_from_seq, c.replaced_to_seq, c.usage, c.cost)
```

`reason` is `"threshold"`, `"manual"` or `"overflow"`. The summariser's own
tokens and cost are reported, so compaction is not free and does not hide.
`report.totals.latency.compaction_seconds` accounts for its time.

## Gotchas

- **`max_summary_tokens` defaults to 2048 and caps at 32000.** A summary allowed
  to run as long as the conversation it replaces saves nothing.
- **`CompactionPolicy.model` is checked at publish** against the ModelClient's
  known models, the same as the agent's own model.
- **Compaction is not `Limits.max_history_records`.** That bounds how much of a
  multi-Run conversation chain is replayed. This replaces the middle of one
  Run's conversation with a summary.
