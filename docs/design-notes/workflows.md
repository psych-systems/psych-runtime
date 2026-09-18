# Workflows: the step tree, waiting, retries, replay

Covers DESIGN.md §5 (one engine, two authoring surfaces; step memoisation) as
it applies to a workflow with structure: composite steps, data flow between
steps, steps that wait, retries, and rerunning from a chosen step. Read this
before touching `psych_runtime/runtime/workflow.py`, the step kinds in
`psych_runtime/core/spec.py`, or the step records.

## Everything is a step, and the log is the tree

A workflow used to be a flat list: each step a `step_started`/`step_completed`
pair, each id derived from `(run_id, path)`. That is still exactly the
mechanism. What changed is that a composite step (`parallel`, `branch`,
`foreach`, `loop`, a nested `workflow`) is a step too: it opens its own start
record, runs its children with paths that extend its own, and closes with a
completion carrying the children's combined output. `step_started` now also
carries the path, the parent's step id and the iteration number, so the tree
is reconstructable from the log without the Spec.

The obvious alternative was a single record per composite with the children's
results inside it. That would have made a crash inside a parallel step lose
every finished branch, because memoisation reads completions, and the
branches would have had none of their own. One record per step, at every
level, is what makes a finished branch survive a sibling that did not.

The view in `psych_runtime.core.workflow_view` walks the Spec for the static
shape (so a step not reached yet is `pending` rather than absent) and the log,
by parent id, for the children the Spec cannot count: loop iterations and
foreach items.

## Data flows by reference

A step's inputs are declared as `ValuePath`s (`input.x`, `steps.a.output.y`,
`state.k`, `item`, `index`, `iteration`) and resolved by the engine when the
step starts. The resolved values go on the start record. Two refusals:

- **No template language.** A workflow that needs to compute a value between
  steps uses a `map` step, so the shaping is a step in the log like everything
  else. A templating layer inside step inputs would be a second execution
  model hiding in the first, invisible to the log.
- **No `None` for a missing path.** A path that does not resolve fails the
  step with kind `unresolved_path`, naming what was there. Feeding `None` to
  a tool that would fail later and further from the cause is the failure
  mode this exists to prevent.

Conditions are data (`{path, op, value}` and `all_of`/`any_of`), evaluated by
the engine, so a branch decision can be re-derived from the log by hand.
Cases not chosen are recorded as skipped steps: the log says the branch
considered them.

`state` is the fold of every completed `set_state` step's output, in log
order, over the Spec's `initial_state`. Nothing is stored beside the log; a
reclaiming Worker derives it. It is one flat namespace shared with nested
workflows, deliberately, because the alternative (a state per nesting level
with rules for reading across) was a second scoping system for values that a
nested workflow's `input` mapping and output already move.

## Waiting is suspension, keyed to the step

`sleep`, a retry backoff, `wait`, `human`, a tool step's approval and a
breakpoint all suspend the Run (DESIGN.md §11) with the step named on the
`suspended` record. The reducer files each `resumed` under that step with its
log position (`step_resumes`); the engine, re-entering after a claim, reads
only answers that came after the attempt began. That position rule is what
lets one step wait twice, a breakpoint before it and then its own event,
without the second wait reading the first answer.

A suspended step keeps its open start record. On re-entry the engine asks:
is there an answer for this step after its start? Then the same attempt
continues. Is there not? Then the previous Attempt died mid-step and a new
attempt starts. The log alone distinguishes a wait from a crash.

`TIMER` is a sixth `SuspendReason` rather than a reuse of `EXTERNAL` because
nothing outside Psych answers it. The store parks the Run (`set_runnable_at`)
and the Worker releases it `RUNNABLE`, not `SUSPENDED`, so a claim at the wake
time picks it up and the claiming Attempt writes the `resumed` itself. A
timer parked `SUSPENDED` would wait for a resume nobody sends.

`BREAKPOINT` pauses *before* the step's own start record, so a paused step
reads as "about to run", and the suspension carries the step's name because
there is no other record to read it from.

## Retries are new attempts, and a composite is not retried

A failed attempt whose policy allows another is recorded as
`step_completed(failure, will_retry=True, retry_at)`. That is the one case in
which the reducer accepts a later `step_started` for a completed step; every
other restart of a completed step is `INCONSISTENT_STEP`. Attempt numbers
increment by one and a report shows one row per attempt.

A composite step is never retried as a whole. Its failed child is settled in
the log, so a second attempt of the parent would find the child memoised as
failed and fail again for nothing. Retries belong to the child that failed.
A denial (`kind="denied"`) is never retried either: asking again is not a
retry.

A suspension in one parallel branch pauses the whole Run: siblings in flight
are cancelled and re-run from their own starts on resume, while what they
completed stays completed. The alternative, letting siblings keep appending
after the lease is released, is a writer with no lease.

## Replay copies, and never shares

`psych_runtime.replay(run_id, from_step=...)` admits a new Run whose
admission names the source and the step. Before the engine walks it, every
step the source settled before that step is copied into the new log as an
ordinary start/completion pair marked `replayed_from`. The engine then finds
those steps memoised and begins at the named step.

Copying rather than pointing keeps the rule that a Run's state derives from
its own log alone, and it keeps the provenance visible. The source Run is
untouched: it keeps its log and its ending. Seeding is idempotent, so a
Worker that dies mid-copy leaves a prefix the next Attempt completes.

## Breakpoints are a property of the Run

`breakpoints` and `step_mode` live on `RunAdmitted`, not on the Spec. Where a
person wants to stop and look is a property of this Run, and putting it on the
Spec would change the Version hash of a workflow nobody edited.

## What this note does not change

Memoisation over replay (DESIGN.md §5), the pure reducer, one log per Run,
the Store port's key/range shape, and the rule that a Spec holds data and
never a callable. Every new step kind is data; every condition and mapping is
data; the engine is the only code that runs.
