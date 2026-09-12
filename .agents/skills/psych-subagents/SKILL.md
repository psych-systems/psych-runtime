---
name: psych-subagents
description: >-
  Delegate work from one Psych agent to another, two ways: a `SubagentRef`
  roster an author writes (blocking, pinned in the parent's Version hash) and a
  `SpawnEnvelope` permitting children the model composes at run time (background,
  published as their own Versions). Covers the delegate / spawn_subagent /
  check_subagent / message_subagent tools, depth and fanout limits, tool
  narrowing down the tree, and SuspendReason.CHILDREN. Use whenever someone
  builds a multi-agent or supervisor pattern on Psych, wants an agent to hand
  work to a specialist, asks about agent fan-out or parallel research, or asks
  why a parent suspends instead of holding its lease. Read before wiring any of
  it, because a subagent description under 20 characters is refused and vague
  descriptions are the most common cause of bad routing.
---

# Subagents

Two shapes, both real, neither replacing the other.

| | `SubagentRef` | `SpawnEnvelope` |
|---|---|---|
| Written by | the author, at publish | the model, at run time |
| Shape | a roster | a permission |
| Execution | inline, blocks the parent | background, its own claimable Run |
| Pinned by | the parent's Version hash | the child's own Run pinning its own hash |
| Think of it as | a pre-composed type | writing the brief fresh |

## A roster the author wrote

The child's whole Spec is embedded in the parent's, so one Version hash pins the
entire tree. That is load-bearing: the runtime reloads the pinned Version at the
top of every Attempt, a reclaiming Worker's included, so a tree that could change
between a crash and a reclaim would resume as a different tree.

```python
spec = psych_runtime.AgentSpec(
    name="support",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    subagents=(
        psych_runtime.SubagentRef(
            name="researcher",
            description="Digs through the knowledge base for policy details and cites them",
            spec=research_spec,
        ),
    ),
)
```

The parent gets a `delegate` tool listing its subagents by description.
Delegation is inline and blocks the parent until the child completes.

**A description under 20 characters is refused.** Bad routing traces back to
vague descriptions more often than to anything else, and this text is exactly
what the parent reads to choose.

## An envelope permitting composition

What the roster cannot cover is a child whose instructions the *model* wrote
during the Run, which by definition cannot be in the parent's Version. The
envelope is the permission that makes that legal, and being on the Spec it joins
the Version hash like everything else. "This agent was allowed to write its own
subagents" is pinned along with the rest of what it is.

```python
spec = psych_runtime.AgentSpec(
    name="lead",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    spawn=psych_runtime.SpawnEnvelope(
        tools=("search_*", "read_doc"),  # ceiling, not a grant
        models=("gpt-4o-mini",),  # empty means the parent's own model only
        max_depth=2,
        max_alive=3,
        may_message=True,
    ),
)
```

The parent gets `spawn_subagent`, `check_subagent` and `message_subagent`
(the last only when `may_message` is true).

The composed child Spec is published as a Version like any other, and the spawn
record holds its hash. So crash recovery is unchanged: the child Run pins a
hash, and replaying it re-reads that Spec rather than re-composing one from a
prompt the model would write differently the second time.

## The model must write a real brief

Three floors, and each catches a different failure:

| Field | Minimum | Why |
|---|---|---|
| purpose | 20 chars | Same reason a `SubagentRef` description is refused when vague. |
| task | 40 chars | The child **does not see the parent's conversation**, so a task assuming context it cannot reach produces a confident answer to the wrong question. |
| deliverable | 15 chars | "A JSON list of URLs" is complete at fifteen. A parent with no statement of what it asked for is what makes a returned blob unusable. |

These are floors low enough that no legitimate brief hits them and high enough
to catch "do the research".

## Access narrows down the tree

`SpawnEnvelope.tools` is a **ceiling, not a grant**. What a child actually gets
is the intersection of what the model asked for, this list, and what the parent
itself holds, computed by the same `narrow` the validator and the per-turn
resolver call. **A parent cannot write itself a child with more access than it
has.**

Empty `tools` means everything the parent holds, matching the empty-means-all
rule everywhere else. Inverting it here alone would make `spawn` silently
useless rather than obviously wrong, and the fix people reach for is `*`.

Empty `models` means the parent's own model and nothing else. A model the author
never mentioned is not a model they priced.

## Depth and how many at once

- `Limits.max_delegation_depth` (default 3) bounds a tree an author wrote and
  can read.
- `SpawnEnvelope.max_depth` (default 2) bounds a tree nobody has read yet.
  Separate numbers because they answer different questions.
- `Limits.max_fanout_per_turn` (default 4) counts delegations started in one
  turn, which is right for a blocking delegation that finishes inside the turn.
- `SpawnEnvelope.max_alive` (default 3) counts background children **still
  running**. A per-turn cap would let a parent hold twenty children open by
  spawning four a turn for five turns. Alive is the number that costs money.

## A parent with nothing left to do

It suspends on `SuspendReason.CHILDREN` rather than holding its lease. A
fan-out of four children holding four Workers doing nothing, while those
children's own children queue behind them, is exactly what suspension exists to
prevent.

A finished child tells its parent with a **Record**, not a callback, so a parent
that is suspended, on another machine, or not running at all still gets the
news. A parent about to suspend reconciles its children first and does not
suspend at all if they have already finished.

The whole tree is reconstructable from the log: a child's spawn, every message
its parent sent it, and its ending are all Records. Nothing about a running tree
lives in a process.

## Reading the tree

```python
report = await psych_runtime.report(store, run_id, child_depth=2)
report.totals  # this Run alone
report.subtree  # this Run plus every descendant
```

`child_depth` is bounded rather than unlimited so a deep tree cannot read the
whole database by accident. Both totals exist because "what did this agent cost"
and "what did this request cost" stop being the same question the moment a tree
exists.

## Gotchas

- **A child does not inherit the parent's conversation.** Everything it needs is
  in the task.
- **`may_message=False` makes spawning fire-and-forget.** Right for a fan-out of
  independent jobs, wrong for anything a person is watching.
- **`tools` and `models` are sorted sets.** Two authors writing the same
  envelope in a different order have written the same agent.
- **Inline children are settled by their parent.** An inline `delegate`
  admits its child `NESTED` so no Worker can claim a Run the parent is already
  executing, and settles it with `Store.settle_inline` when it finishes.
  Anything else that drives a Run it dispatched owes the same call: without it
  the header stays `NESTED` for good and disagrees with its own log.
- **A workflow's `AgentStep` is a third thing.** It runs a nested agent as a
  deterministic step; see `psych-workflows`.
