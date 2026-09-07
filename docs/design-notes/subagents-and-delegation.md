# Subagents: depth, addressing and resume

Covers DESIGN.md §17. Read it before touching `psych_runtime/runtime/subagent.py` or
`psych_runtime/core/spec.py`'s spawn permission.

A subagent is a nested Run with its own log, its own budget and a narrowed tool
set. Everything below is about keeping that nesting bounded and keeping a resumed
child from forgetting what it is.

## Depth is monotone, and that is the whole bug

There are two sources of truth for a Run's delegation depth and only one of them is
durable.

**The Run header** carries the depth, written once at creation. Authoritative and
immutable for the life of that Run.

**Runtime options** carry a depth too, supplied fresh every time the Run is
materialised in a process, including on every resume.

Resolve the effective depth as `max(header, runtime)`, never as "prefer the runtime
option, fall back to the header".

The bug the `max` prevents: a resumed child is reconstructed by code that has no
reason to know it is depth 3. Its runtime options say nothing, which reads as zero,
and the child is now free to delegate as though it were top-level. The recursion
budget is defeated silently, by a resume that looked entirely ordinary.

`max` makes the persisted value a floor. Runtime options can only push depth up,
which is what a parent computing `child_depth = parent_depth + 1` needs to do before
the child's own header is written. Nothing at read time can ever make an effective
depth lower than what was persisted, by construction rather than by convention.

A resolved depth is computed once, at child creation, and checked against the cap
right there, raising a typed error naming both the attempted depth and the maximum.

## Both caps ship with defaults

Depth is capped and fan-out per turn is capped, both configurable and **both with
defaults**. One turn spawning a tree is a cost incident, and a cap that is optional
and unset is not a cap.

This is stricter than the minimum that works. Treating depth as an opt-in capability
with no default, and having no fan-out concept at all, is a defensible choice for a
library whose caller is always another engineer. It is the wrong choice for a runtime
whose caller is a model.

## Composed children need a permission, not a roster

§17 describes a roster: a parent's Spec embeds its subagents, so one Version hash
pins the whole tree. That stays exactly as written and it is load-bearing for crash
recovery, because a resumed parent finds its children's definitions inside the Version
it already pinned.

What a roster cannot express is a child whose instructions the *model* wrote during
the Run. Those cannot be in the parent's Version, because they did not exist when it
was published.

`SpawnEnvelope` on `AgentSpec` covers that case as a permission rather than a roster:
may this agent compose children at all, out of which of its own tools, on which
models, how deep, and how many alive at once. The envelope joins the Version hash like
every other permission, so the *authority* is still pinned even though the child is
not.

The composed child's Spec is then published as a Version like any other, and the
child's own Run pins that hash. Nothing about resuming after a crash changes: the child
is recovered from a published Version exactly as a rostered one is.

A parent with background children and no work of its own suspends on
`SuspendReason.CHILDREN` rather than holding its lease. A finished child tells its
parent so by appending a Record, not by calling back into a process that may no longer
exist.

## Addressing and authorisation

An address names both the parent and the child, plus whether the child is one-shot or
continuable. Every control operation carries the address, and authorisation checks that
the claimed parent really is the child's durable parent before anything else happens.

Three checks are worth naming because each closes a real hole:

- **A stale caller is rejected before the target is even looked at.** A parent object
  that used to be live and has since been replaced must not act on a child.
- **Self-targeting is rejected outright.**
- **An ancestor further up the tree may act on a descendant, but only if it is genuinely
  in the live ancestry chain**, not merely because it knows the child's id.

Failure codes are a fixed typed vocabulary declared in one place: parent unavailable, not
resumable, unauthorised, attachment invalid, delivery unavailable, projections
unavailable. Internal error text never reaches the caller.

## Listing children without resuming them

Enumeration is durable-first and live-preferred: merge the in-process registry with
durable persistence, and where both have a row for one id, the live record wins wholly
rather than field by field.

Each row resolves through a three-rung ladder, cheapest first: a live identity snapshot
for a resident child, then a durable projection cache as an accelerator, then a full fold
over the child's log when neither is available. No child is ever loaded or resumed merely
to be listed.

A row is either a resolved child, carrying its mode, activity and whether it has children
of its own, or a diagnostic row naming why it could not be resolved: corrupt, unsupported
or unavailable. A child whose fold throws is contained as one diagnostic row rather than
failing the whole listing. Descendant listing is the same machinery walked recursively in
stable pre-order.

## Projections and forks

A parent reads a child's state through small pure folds over the child's own log: how long
it has been running, and its identity.

The subtlety is forks. A Run seeded from an ancestor's log prefix replays that ancestor's
own descriptor and completed turns before its own descriptor is appended. So:

- A timing fold **resets to zero every time it crosses a descriptor record**, which
  guarantees the last descriptor, the child's own, is the timing origin. Without the
  reset, a fork inherits its parent's accumulated duration.
- An identity fold takes the **last** descriptor wins, for the same reason.
- A fold must never throw. Damage folds to a value, not to an exception. Use an explicit
  null sentinel rather than an absent field, so the value survives a JSON round trip; an
  absent field silently drops and lets a stale identity survive on the client.

Any cached, incrementally folded summary carries a state version, bumped whenever the
stored fold shape changes incompatibly. A checkpoint that no longer matches is refolded
from scratch rather than deserialised into a shape it does not fit. Never migrate
serialised fold state in place.

## Cold resume

When a parent sends to a child that is not live in this process, the resume path is:

1. Read the child's durable state, failing as not-resumable if it is unavailable.
2. **Authorise against the persisted parent link first**, before folding anything. An
   unauthorised caller must not be able to trigger even a fold on a child it does not own.
3. **Fold only the child's own log suffix**, excluding any inherited prefix. Folding the
   whole log risks resuming with an ancestor's descriptor, which is the fork hazard above
   showing up at the worst possible moment.
4. If the folded descriptor is missing or says one-shot, fail as not-resumable. A one-shot
   child cannot be resumed by construction.
5. Materialise from **the child's own durably recorded configuration**: its model, its
   reasoning settings, its tool narrowing. Never from whatever the resuming caller happened
   to pass.
6. Submit the pending message, rolling the whole materialisation back on any failure, so a
   failed resume leaves no half-registered child.

A delivery racing an in-flight teardown waits for the teardown and retries the whole loop,
which falls through to cold resume once the old residency is gone. There is no special
"resume during teardown" branch; it is the same path a beat later.

The two ideas to carry: **resume reconstructs configuration from the child's own record,
never from caller-supplied options**, and **folding a child's state is scoped to that
child's own log suffix**.

## Routing descriptions

A subagent carries a routing descriptor: its name, what it is for, and the tools it holds.
The purpose text has a minimum length, and a description too short to distinguish one child
from another is rejected at publish.

This exists because delegation quality is bounded by routing quality. A parent choosing
between children whose descriptions are "helper" and "assistant" is guessing, and a guess
costs a whole child Run. Refusing the Spec is cheaper than debugging the routing later.
