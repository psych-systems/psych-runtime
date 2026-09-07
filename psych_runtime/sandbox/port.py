"""The Sandbox port and its result vocabulary.

DESIGN.md §18. A model writes one program; the runtime executes it in
isolation and returns what it printed and what it returned. This module owns
the contract every adapter honours:

- **One program per execution.** Nothing here accepts a second program on the
  same instance; a fresh call is a fresh process (or container) every time.
- **Host bindings are ordinary calls.** The program the model wrote calls a
  host-provided name as if it were a normal Python function. What that name
  does once the call reaches the host is entirely the caller's business: a
  binding closes over whatever Policy check, egress seam and Record it needs
  before this port ever sees it. The Sandbox port itself knows nothing about
  Policy, Scope or the tool registry, on purpose, because ``psych_runtime.sandbox``
  sits beside ``psych_runtime.tools`` in the layering (DESIGN.md §21) and importing
  either would invert the dependency import-linter enforces. A caller in
  ``psych_runtime.runtime`` or ``psych_runtime.tools`` is expected to supply bindings that are
  themselves closures over the normal tool-call path.
- **Failures are data.** ``run()`` never raises for a failure in the model's
  own program, however that program failed: a raised exception, a syntax
  error, a resource limit, a timeout. Every one of those comes back as a
  populated ``SandboxResult.failure`` with a traceback the model can read.
  ``run()`` raises only for something that is not the model's mistake to fix:
  the sandbox itself could not be constructed (no interpreter at the
  configured path, a temporary directory could not be created, a resource
  limit could not be applied even after clamping to what the host allows).
  That is ``SandboxSetupError``, and it is deliberately not a
  ``SandboxResult``: handing an environment failure to the model as "your
  program failed" would send it hunting for a bug in code that was never the
  problem.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.errors import PsychError

__all__ = [
    "HostBinding",
    "Sandbox",
    "SandboxFailure",
    "SandboxLimit",
    "SandboxLimits",
    "SandboxResult",
    "SandboxSetupError",
]


HostBinding = Callable[[Mapping[str, Any]], Awaitable[Any]]
"""One host-callable name, as the sandbox sees it.

Takes the arguments the model's program passed as a mapping (already parsed
out of the framed call, never a raw string) and returns a JSON-round-trippable
value, or raises. A raised exception is relayed to the child as a call failure
so it surfaces inside the model's program exactly where it called the
binding, the same as a real Python function raising would. What the callable
does with Policy, egress or recording before it gets there is the caller's
business, not the sandbox's; see the module docstring.
"""


class SandboxLimit(StrEnum):
    """Which resource cap ended an execution, when one did.

    A single field naming the failing dimension rather than five booleans:
    exactly one limit ends a given execution (the process dies, or the
    program raises, on the first cap it crosses), so a scalar is the honest
    shape and a set would imply a possibility that cannot happen.
    """

    CPU_SECONDS = "cpu_seconds"
    ADDRESS_SPACE_BYTES = "address_space_bytes"
    FILE_SIZE_BYTES = "file_size_bytes"
    PROCESS_COUNT = "process_count"
    WALL_SECONDS = "wall_seconds"


class SandboxLimits(BaseModel):
    """Resource caps for one execution.

    Defaults and rationale live on the adapters that apply them
    (``psych_runtime.sandbox.subprocess``, ``psych_runtime.sandbox.container``), because a
    number with no story behind it is a number nobody can safely change
    later. This model only fixes the shape both adapters share, so the
    contract suite (``psych_runtime.sandbox.contract``) can drive both with the same
    values and prove they mean the same thing on both backends.

    Attributes:
        cpu_seconds: CPU time actually consumed, not wall-clock time. A
            program blocked waiting on a host binding's reply burns no CPU
            budget while it waits.
        address_space_bytes: the virtual address space the process may map.
            Exceeding it surfaces to the program as an ordinary
            ``MemoryError``, not a killed process, because CPython's
            allocator checks malloc's return value rather than trusting it.
        file_size_bytes: the largest file the process may write, counting
            every file it opens, not a total across files.
        process_count: how many processes (Linux counts threads too) the
            executing user may hold at once, across everything else running
            as that user. See ``psych_runtime.sandbox.subprocess`` for why this is a
            per-user limit rather than a per-execution one and what that
            implies for concurrent runs.
        wall_seconds: real time from spawn to teardown. This is the only cap
            here that is not a resource limit on the process; it is enforced
            by the host killing the process (or container) directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: float = Field(gt=0)
    address_space_bytes: int = Field(gt=0)
    file_size_bytes: int = Field(gt=0)
    process_count: int = Field(gt=0)
    wall_seconds: float = Field(gt=0)


class SandboxFailure(BaseModel):
    """A sandboxed program's failure, as data the model can read.

    Deliberately shaped like ``psych_runtime.core.records.ToolFailure`` (``kind``,
    ``message``, ``traceback``) so a caller can build one from the other with
    a field-for-field copy. It is not the *same* type: ``psych_runtime.sandbox`` does
    not import ``psych_runtime.core.records`` for it, because a port should not
    depend on the record shape of whatever happens to consume it today, and
    tying the two together would make an unrelated change to the Record
    schema a breaking change here too.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, max_length=128)
    """A short, stable classifier, not prose. ``exception`` for an uncaught
    error the program's own code raised, ``timeout`` for the wall-clock cap,
    ``resource_limit`` for a cap in ``SandboxLimit``, ``protocol_violation``
    for a child that broke the framed wire protocol (see
    ``psych_runtime.sandbox.protocol``), ``setup_failed`` never appears here because
    setup failures raise ``SandboxSetupError`` instead."""
    message: str = Field(max_length=8192)
    traceback: str | None = Field(default=None, max_length=65_536)


class SandboxResult(BaseModel):
    """What one execution produced, success or failure.

    Attributes:
        stdout: everything the program printed to standard output.
        stderr: everything it printed to standard error.
        value: what the program's top-level code evaluated to, JSON-shaped.
            ``None`` both when the program returned nothing and when it
            genuinely returned ``None``; the two are not distinguished
            because JSON does not distinguish them either.
        failure: set when the program did not complete cleanly. See
            ``SandboxFailure``.
        duration_seconds: wall-clock time from spawn to teardown.
        limit_hit: which cap in ``SandboxLimits`` ended the execution, if
            one did. ``None`` on a clean completion or on a plain program
            error that no limit caused.
        network_denied: ``True`` only when this execution actively verified
            that it has no route to the network, ``False`` when it did not
            (network was granted, or denial was requested but the host could
            not establish it). See the adapter docstrings for exactly what
            "verified" means on that backend and what it does not guarantee;
            this field reports the adapter's own honest self-check, never an
            assumption.
        stdout_truncated: the program printed more than the adapter buffers.
        stderr_truncated: same, for standard error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stdout: str = ""
    stderr: str = ""
    value: Any = None
    failure: SandboxFailure | None = None
    duration_seconds: float = Field(ge=0)
    limit_hit: SandboxLimit | None = None
    network_denied: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.failure is None


class SandboxSetupError(PsychError):
    """The sandbox could not be prepared. Not the model program's fault.

    Raised by ``run()`` itself rather than returned as a ``SandboxResult``:
    an interpreter missing at the configured path, a temporary directory
    that could not be created, or a resource limit that is still unsettable
    after being clamped to what the host permits, are all environment
    problems no amount of the model rewriting its program can fix. Handing
    one of these to the model as a program failure would send it looking for
    a bug that is not there.
    """


@runtime_checkable
class Sandbox(Protocol):
    """Runs one model-written program in isolation and reports the result.

    DESIGN.md §18 rejects in-process execution outright: no implementation
    of this port may run the program in the calling process. Process
    isolation (``psych_runtime.sandbox.subprocess``) is the floor; a container
    (``psych_runtime.sandbox.container``) is available for consumers who need more.
    """

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, HostBinding] | None = None,
        limits: SandboxLimits | None = None,
        network: bool = False,
    ) -> SandboxResult:
        """Execute ``program`` once, in a fresh process or container.

        Args:
            program: Python source. Its top-level statements run inside an
                implicit async function, so a bare top-level ``await`` and a
                bare top-level ``return`` are both legal, the same as the
                body of an ``async def``. What the program evaluates to (an
                explicit ``return`` in it, or ``None`` if it falls off the
                end) becomes ``SandboxResult.value``.
            bindings: names the program may call as ordinary functions. A
                name not in this mapping is simply not defined in the
                program's namespace; calling it is a plain ``NameError`` in
                the program, not a special sandbox error.
            limits: resource caps. ``None`` uses the adapter's own defaults;
                see the adapter for what those are and why.
            network: whether this execution may reach the network at all.
                Off by default. Granting it does not hand the program a
                bound HTTP client; it only stops the adapter from actively
                denying the process a route out. Anything the program is
                meant to fetch should come through a binding that itself
                goes through the egress seam (DESIGN.md §14), not through
                network access granted to raw sockets inside the sandbox.

        Returns:
            The result, whether the program succeeded or failed. See the
            module and class docstrings for why a failure is a populated
            ``SandboxResult.failure`` rather than a raised exception.

        Raises:
            SandboxSetupError: the sandbox itself could not be prepared.
                Never raised because of anything the model's program did.
        """
        ...
