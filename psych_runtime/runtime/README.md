# psych_runtime.runtime

## Owns
The Worker and its supervisor, the attempt and lease machinery, the agent turn
loop, the workflow engine and step memoisation, suspend and resume, interrupts
and the three steering queues (`dispatch.send`), and conversation continuity
across a chain of Runs (`dispatch.dispatch(continues=...)`,
`thread.load_thread_history`).

Subagents, both shapes (§17). A `SubagentRef` an author wrote is delegated to
inline and blocks its parent (`execute.Runtime._delegate`). One a parent
composes at run time inside its `SpawnEnvelope` is published as a Version, run
in the background as its own claimable Run, and reports back as a Record
(`subagent.py` for the rules, `execute._ComposedChildren` for the wiring,
`notify.py` for how a finished child reaches a parent that may be suspended,
on another machine, or not running at all).

Compacting a conversation (`compaction.py`, `AgentLoop._compact`), which is
the write side of what `psych_runtime.core` can already represent and replay. This
package decides when a conversation is too long, where to cut it -- always on a
turn boundary, so an assistant message and the results answering it never end
up on opposite sides of the line -- and asks a model for the summary; the
`CompactionApplied` Record it appends is what `psych_runtime.core.conversation` then
reads. Opt-in per Spec (`CompactionPolicy`) and off by default.

The workflow engine (`workflow.py`) walks a `WorkflowSpec`'s step tree:
composite steps (parallel, branch, foreach, loop, nested workflow) are steps
in the log with children under them, data moves between steps by reference
(`psych_runtime.core.workflow_values`), and a step that waits -- a sleep, a
retry backoff, an event, a person, a tool step's approval, a breakpoint --
suspends the Run with the step named on the record and continues on the next
claim. `workflow_replay.py` seeds a Run admitted by `replay()` with the
settled prefix of the Run it replays. See `docs/design-notes/workflows.md`.

`abort.py` holds the reason an Attempt was told to stop, because the four
reasons need four different answers: a deadline settles the Run, a graceful
shutdown hands it back RUNNABLE for the next Worker, a lost lease writes
nothing at all (another Worker owns the log), and a user's abort is already a
Record. Collapsing them into one bare event settled healthy Runs as "passed
its deadline" on every redeploy.

## Does not own
Scheduling. Psych evaluates no cron expressions and runs no scheduler
(DESIGN.md §20). The consumer owns the clock and calls `dispatch()`. The one
clock the runtime keeps is a workflow step's sleep or retry backoff, and it
keeps it in the Store: the Run is parked with a wake time and any Worker's
claim wakes it, so no timer lives in a process.

## Ports
Consumes Store, BlobStore, ModelClient, Telemetry, Policy and Sandbox. Defines
none.

## The rules that live here
The supervisor pass and the attempt fiber are separate, and a supervisor pass
arms its successor before doing failable work (§8.1). Lease renewal follows
observable progress, never process liveness, or the expired-lease branch becomes
unreachable (§8.2). An abort is a Record, not a flag (§9).
