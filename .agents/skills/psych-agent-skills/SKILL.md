---
name: psych-agent-skills
description: >-
  Give a Psych agent instruction packs it loads on demand: `psych_runtime.Skill` in a
  Spec, the skills index that sits in the system prompt, the `load_skill`
  built-in, and `[[skill:name]]` links validated at publish. Use whenever
  someone wants an agent to follow a long policy, playbook, style guide or
  runbook without paying for it on every turn, asks how to keep an oversized
  system prompt under control, mentions progressive disclosure of instructions,
  or hits a dangling `[[skill:...]]` link at publish. Note this is about Skills
  *inside a Psych Spec*, which are a different thing from the `.agents/skills`
  files an agent reads. Read before stuffing a long policy into `instructions`,
  because that text is billed on every turn of every Run while a skill body is
  billed only when the model asks for it.
---

# Skills in a Spec

Two things share the word "skill" here, and they are unrelated:

- **This page**: `psych_runtime.Skill`, an instruction pack inside an `AgentSpec` that
  the *model being run* loads on demand.
- **`.agents/skills/`**: files a *coding agent* reads to work with Psych. That
  is what you are reading now.

A Spec Skill exists because instruction packs are large and most of them are
irrelevant to any given turn. Descriptions sit in the system prompt always;
bodies load only when the model asks.

```python
spec = psych_runtime.AgentSpec(
    name="support",
    instructions="Help the customer. Follow [[skill:refund-policy]] before refunding.",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    skills=(
        psych_runtime.Skill(
            name="refund-policy",
            description="When a refund is allowed, and the limits on one",
            body="Refunds are allowed within 30 days of delivery...\n\n"
            "For anything over £500 see [[skill:escalation]].",
        ),
        psych_runtime.Skill(
            name="escalation",
            description="Who to escalate to, and what to include",
            body="...",
        ),
    ),
)
```

## What the model sees

The system prompt gets an index of names and descriptions, never bodies:

```
## Skills

Instruction packs you can load when you need them. Call `load_skill` with a
name to read one. Load a skill before doing work it covers rather than guessing
at its content.

- `refund-policy`: When a refund is allowed, and the limits on one
- `escalation`: Who to escalate to, and what to include
```

Putting bodies there would defeat the whole mechanism.

`load_skill` is registered automatically whenever a Spec has skills. You do not
wire it, and you cannot name a tool `load_skill` yourself: it is reserved.

## Write the description for a router

The description is the only thing the model has when deciding whether to load.
Write it as the condition under which the skill is relevant, not as a title.

"Refund rules" tells the model nothing about when to reach for it. "When a
refund is allowed, and the limits on one" does.

## `[[skill:name]]` links

Use them anywhere in `instructions` or in another skill's `body`. The pattern is
`[[skill:name]]` where the name matches `[a-zA-Z_][a-zA-Z0-9_.-]{0,127}`.

Publish-time validation walks the link graph and **rejects a dangling link**. By
the time a Spec reaches the runtime, every link in every body names a skill that
really exists. That is what publish-time validation is for: a customer waiting
on a response is the wrong place to discover a typo.

When `load_skill` returns a body containing links, it mentions what the skill
leads to, so the model can follow the chain.

## Loading the same skill twice

`load_skill` always returns the body, and only adds a note when this is a
repeat. Saying "you already loaded this" without returning the body would be
useless, because the reason it asked again is that the first answer fell out of
its context window.

That "already loaded" bookkeeping lives in process memory for one Attempt and
does not survive a crash, which is fine: the conversation rebuilt from the log
after a reclaim still contains every earlier `load_skill` call and its result,
so a model needing the body again simply calls again.

## When to use one

**Yes**: a refund policy, a brand style guide, an incident runbook, an API
convention document, anything long that applies to some turns and not others.

**No**: something needed on every turn, which belongs in `instructions`; a large
dataset, which belongs behind a tool; anything that changes more often than you
want to republish, since skills are part of the Spec and version with it.

## Gotchas

- **Skills are inside the Version hash.** Editing a body produces a new Version.
  That is correct and it means skill text is reviewed like code.
- **`description` caps at 1024 characters and `body` has no cap.** Keep the
  description short; it is paid for on every turn.
- **Bodies and descriptions are whitespace-normalised** on construction, so two
  Specs differing only in indentation hash the same.
- **A skill is instructions, not tools.** It cannot grant a tool. Grant tools on
  the Spec and let the skill explain when to use them.
