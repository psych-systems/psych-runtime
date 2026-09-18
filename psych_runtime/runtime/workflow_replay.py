"""Seeding a replayed workflow Run from the Run it replays.

``psych_runtime.replay()`` admits a new Run that names an earlier one and a
top-level step to start at. Before the engine walks the new Run, this copies
every step the source Run settled *before* that step into the new log, as an
ordinary ``step_started``/``step_completed`` pair marked ``replayed_from``.
The engine then finds those steps memoised and begins executing at the named
step, exactly as it would after a crash. Nothing else about replay exists:
no special engine mode, no shared state between the two Runs.

## Why copy records rather than point at the source

A step id is derived from its Run id and its path, so the new Run's ids
differ from the source's by construction, and a pointer from one log into
another would be a second place state comes from. Copying keeps the rule that
a Run's state is derived from its own log alone, and it makes the provenance
visible: each copied record says which Run it came from.

## Which steps are copied

Every settled step whose top-level position is before the named step's. A
step in flight when the source Run stopped is not copied, because it never
finished; a step at or after the named position is not copied, because that
is the part being replayed. Composite steps are copied with their children,
parents first, since a parent's start precedes its children's in the log.

Seeding is idempotent: a Worker that dies mid-seeding leaves a prefix of the
copies in the log, and the next Attempt copies only what is missing.
"""

from __future__ import annotations

from psych_runtime.core.errors import AccessDenied, RunNotFound
from psych_runtime.core.ids import StepId, derive_step_id
from psych_runtime.core.reducer import StepRecord, reduce
from psych_runtime.core.spec import WorkflowSpec
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.port import Store

__all__ = ["seed_replay"]

_SHORTEST_PATH = 3
"""``(root, index, name)``: no step sits higher than that."""


async def seed_replay(store: Store, journal: Journal, spec: WorkflowSpec) -> int:
    """Copy the source Run's completed prefix into ``journal``.

    Returns how many steps were copied on this call; zero when the seeding
    was already complete.

    Raises:
        RunNotFound: the source Run is gone.
        AccessDenied: the source Run belongs to another tenant.
    """
    state = journal.state
    source_id = state.replays_run_id
    from_step = state.replay_from_step
    if source_id is None or from_step is None:
        return 0

    source_records = await store.read(source_id)
    if not source_records:
        raise RunNotFound(source_id)
    source = reduce(source_records, run_id=source_id)
    if source.scope.tenant != journal.scope.tenant:
        raise AccessDenied(
            f"run {source_id}",
            f"belongs to tenant {source.scope.tenant!r}, not {journal.scope.tenant!r}. A Run "
            "may only replay a Run in its own Scope.",
        )

    boundary = next(
        (index for index, step in enumerate(spec.steps) if step.name == from_step), None
    )
    if boundary is None:
        return 0

    copied = 0
    for record in sorted(source.steps.values(), key=lambda step: step.started_seq):
        if not _before(record, spec.name, boundary) or not record.settled:
            continue
        step_id = derive_step_id(journal.run_id, record.path)
        if step_id in journal.state.steps:
            continue
        parent_id: StepId | None = None
        if record.parent_step_id is not None:
            parent = source.steps.get(record.parent_step_id)
            if parent is not None:
                parent_id = derive_step_id(journal.run_id, parent.path)
        await journal.append(
            type="step_started",
            step_id=step_id,
            name=record.name,
            kind=record.kind,
            attempt_number=1,
            input=record.input,
            path=record.path,
            parent_step_id=parent_id,
            iteration=record.iteration,
            replayed_from=source_id,
        )
        await journal.append(
            type="step_completed",
            step_id=step_id,
            output=record.output,
            failure=record.failure,
            child_run_id=record.child_run_id,
            skipped=record.skipped,
        )
        copied += 1
    return copied


def _before(record: StepRecord, root: str, boundary: int) -> bool:
    """Whether ``record`` sits under a top-level step positioned before ``boundary``."""
    path = record.path
    if len(path) < _SHORTEST_PATH or path[0] != root:
        return False
    position = path[1]
    return isinstance(position, int) and position < boundary
