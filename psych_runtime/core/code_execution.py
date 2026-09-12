"""The closed vocabularies code execution is configured and reported in.

DESIGN.md §18. These enums are the words a Spec uses to *request* how a
model-written program should run and the words a ``Sandbox`` uses to *report*
what an execution actually got. They live in ``psych_runtime.core`` because both
sides need them and ``psych_runtime.core`` imports nothing from the other
subpackages (DESIGN.md §21): the Spec models in ``psych_runtime.core.spec`` name an
isolation level, and ``psych_runtime.sandbox.port`` reports one back, and neither
may import the other for it.

## Two isolation levels, and why there is no third

``IsolationLevel.ISOLATED`` is the level a Spec gets when it says nothing. It
means a kernel-enforced boundary: the program sees a private filesystem and
not the host's, it has no network route unless one was granted deliberately,
every process it starts ends when the execution ends, and its CPU, memory and
process budgets are enforced by the kernel rather than by anything the
program could talk its way past. A container, a Linux namespace sandbox and a
remote provider that promises the same are all ways to get it.

``IsolationLevel.PROCESS`` is for **trusted code only**. It is a fresh
operating-system process with resource limits, a scrubbed environment, a
temporary working directory and whole-tree cleanup, and it hides nothing on
the host filesystem from the program. It exists so a developer can use
``run_code`` on a laptop with nothing installed, and so an operator running
their own vetted programs has a cheap backend. It is never selected
automatically: a Spec that wants it says so, and a backend that can only
provide it refuses an execution that asked for ``ISOLATED`` rather than
quietly running at the weaker level.

There is deliberately no ``"none"`` level. In-process execution is rejected
outright (DESIGN.md §18) and the ``Sandbox`` port has no way to express it.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ArtifactCollection",
    "Enforcement",
    "IsolationLevel",
    "NetworkAccess",
    "OutputPreservation",
    "WorkspacePolicy",
]


class IsolationLevel(StrEnum):
    """How strongly a program is separated from the host it runs on.

    Ordered: ``ISOLATED`` is stronger than ``PROCESS``. ``satisfies`` is the
    one comparison the runtime makes, so the ordering lives here rather than
    being re-derived at each call site.
    """

    ISOLATED = "isolated"
    """A kernel-enforced boundary: private filesystem, network denied unless
    granted, whole process tree contained, kernel-enforced resource limits."""

    PROCESS = "process"
    """A fresh process with resource limits, a scrubbed environment and a
    temporary working directory, sharing the host's filesystem view. For
    trusted code only, and never selected on a Spec's behalf."""

    def satisfies(self, requested: IsolationLevel) -> bool:
        """Whether an execution at this level meets a request for ``requested``."""
        return _RANK[self] >= _RANK[requested]


_RANK: dict[IsolationLevel, int] = {IsolationLevel.PROCESS: 1, IsolationLevel.ISOLATED: 2}


class Enforcement(StrEnum):
    """How much one guarantee is worth, on one execution or one backend.

    A single word per guarantee rather than a boolean, because "we tried"
    and "we know it held" are different facts and a consumer relying on one
    of them needs to know which they have.
    """

    ENFORCED = "enforced"
    """The mechanism was applied and, where a check is possible, verified
    from inside the execution."""

    UNVERIFIED = "unverified"
    """The mechanism was applied, or is promised by the backend, and this
    execution has no way to confirm it from the inside."""

    UNAVAILABLE = "unavailable"
    """Not provided. Either the platform cannot, or it was deliberately
    switched off (network access that was granted, for example)."""


class NetworkAccess(StrEnum):
    """Whether the program may open its own sockets."""

    DENIED = "denied"
    """The default. No route out; anything the program needs fetched goes
    through a host binding that itself uses the egress seam (DESIGN.md §14)."""

    UNRESTRICTED = "unrestricted"
    """Raw sockets allowed. This bypasses the egress seam for that program, so
    a deployment profile must permit it explicitly before a Spec can have it."""


class WorkspacePolicy(StrEnum):
    """What happens to the working directory between executions."""

    EPHEMERAL = "ephemeral"
    """A fresh, empty directory per execution, removed afterwards. The only
    policy today; named so a persistent one can be added as a versioned
    change rather than a silent one."""


class OutputPreservation(StrEnum):
    """What must happen to output too large for the model to see whole."""

    WHEN_AVAILABLE = "when_available"
    """Keep the full bytes in the Runtime's ``BlobStore`` when one is wired,
    otherwise keep only the preview and say so."""

    REQUIRED = "required"
    """The full bytes must be kept. Without a ``BlobStore`` the call fails
    explicitly rather than quietly losing output."""

    NEVER = "never"
    """Keep only the preview, even when a ``BlobStore`` is available."""


class ArtifactCollection(StrEnum):
    """Whether files the program writes to its workspace are collected."""

    COLLECT = "collect"
    """Regular files under the workspace are returned as artifacts, within
    the count and size caps, and never by their host path."""

    IGNORE = "ignore"
    """Files the program writes are discarded with the workspace."""
