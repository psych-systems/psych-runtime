"""Identifier types.

These are ``NewType`` aliases over ``str`` rather than classes. They cost nothing
at runtime, mypy refuses to swap one for another, and they serialise as plain
strings so a Record stays readable in a database row.

Run and step identifiers are generated here; everything else arrives from the
consumer or is derived from content.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Final, NewType

__all__ = [
    "AttemptId",
    "RunId",
    "StepId",
    "ToolCallId",
    "VersionHash",
    "WorkerId",
    "derive_step_id",
    "new_attempt_id",
    "new_queue_entry_id",
    "new_run_id",
    "new_tool_call_id",
    "new_worker_id",
]

RunId = NewType("RunId", str)
"""Identifies one durable execution of a Version."""

StepId = NewType("StepId", str)
"""Identifies one checkpointed unit of work inside a Run.

Derived, never random: a Step's id must be the same on a replay as it was on the
original attempt, or memoisation cannot find the earlier result and the Step
re-executes after a crash.
"""

AttemptId = NewType("AttemptId", str)
"""Identifies one execution pass over a Run by one Worker holding a lease."""

WorkerId = NewType("WorkerId", str)
"""Identifies a Worker process. Leases are held by a Worker, not by an Attempt."""

ToolCallId = NewType("ToolCallId", str)
"""Identifies one tool invocation. Assigned by the model, or by us when it is not."""

VersionHash = NewType("VersionHash", str)
"""The content hash of a published Version. See psych_runtime.core.version."""


_RUN_PREFIX: Final = "run"
_ATTEMPT_PREFIX: Final = "att"
_WORKER_PREFIX: Final = "wrk"
_CALL_PREFIX: Final = "call"
_QUEUE_ENTRY_PREFIX: Final = "qe"

_RANDOM_BYTES: Final = 12
"""96 bits of randomness per id. Enough that a collision is not a thing that
happens, short enough that a log line stays readable."""


def _token(prefix: str) -> str:
    """A sortable, prefixed, url-safe id.

    The millisecond timestamp leads so that ids sort roughly by creation time,
    which makes a raw table scan during an incident far easier to read. It is a
    convenience and nothing depends on it for correctness.
    """
    millis = int(time.time() * 1000)
    return f"{prefix}_{millis:012x}{secrets.token_hex(_RANDOM_BYTES)}"


def new_run_id() -> RunId:
    return RunId(_token(_RUN_PREFIX))


def new_attempt_id() -> AttemptId:
    return AttemptId(_token(_ATTEMPT_PREFIX))


def new_worker_id() -> WorkerId:
    return WorkerId(_token(_WORKER_PREFIX))


def new_tool_call_id() -> ToolCallId:
    return ToolCallId(_token(_CALL_PREFIX))


def new_queue_entry_id() -> str:
    """A fresh id for one ``QueueEnqueued`` entry.

    Plain ``str``, not a ``NewType``: ``QueueEnqueued.entry_id`` is declared as
    ``str`` in ``psych_runtime.core.records`` because a consumer may supply their own
    (idempotency-keying a steer the way ``dispatch`` idempotency-keys a Run),
    so there is no distinct identity type to wrap here the way there is for a
    Run or a tool call.
    """
    return _token(_QUEUE_ENTRY_PREFIX)


_STEP_ID_LENGTH: Final = 32
"""Half a SHA-256, hex encoded. 128 bits against accidental collision between
two different step paths in one Run, which is far more than enough."""


def derive_step_id(run_id: RunId, path: tuple[str | int, ...]) -> StepId:
    """Derive a Step's id from its position in the Version plus the Run id.

    DESIGN.md §5: workflows use step memoisation rather than deterministic
    replay, and memoisation needs an id that is stable across attempts. That
    rules out anything random and anything clock-derived.

    ``path`` is the Step's position: the chain of node names and loop indices
    from the Version root down to this Step. Two Steps in one Run collide only
    if they genuinely occupy the same position, in which case they are the same
    Step and sharing a memoised result is correct.

    The Run id is mixed in so that the same Version running twice produces
    different Step ids, which keeps two Runs from reading each other's results
    out of a shared store.

    Args:
        run_id: the Run this Step belongs to.
        path: the Step's position, outermost element first. Integers are loop
            indices; strings are node names.

    Returns:
        A stable, opaque Step id.
    """
    digest = hashlib.sha256()
    digest.update(run_id.encode("utf-8"))
    for element in path:
        # The separator and the type tag together stop ("a", "b") from hashing
        # the same as ("ab",) or as (1, "b") when an index stringifies.
        digest.update(b"\x1f")
        digest.update(b"i" if isinstance(element, int) else b"s")
        digest.update(str(element).encode("utf-8"))
    return StepId(f"stp_{digest.hexdigest()[:_STEP_ID_LENGTH]}")
