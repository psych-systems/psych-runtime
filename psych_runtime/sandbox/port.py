"""The Sandbox port and its result vocabulary.

DESIGN.md §18. A model writes one program; the runtime executes it in
isolation and returns what it printed and what it returned. This module owns
the contract every adapter honours:

- **One program per execution.** Nothing here accepts a second program on the
  same instance; a fresh call is a fresh process (or container, or remote
  session) every time.
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
- **Guarantees are reported, never inferred.** Every result says which
  isolation level the execution actually achieved and how each guarantee was
  enforced (``SandboxGuarantees``), from checks the execution made on itself
  where a check is possible. A backend that could not provide the level it
  was asked for returns a ``failure`` of kind ``isolation_unavailable`` with
  the program's output withheld, never a result produced under weaker terms
  than were requested. ``describe()`` says the same things ahead of time so a
  deployment learns at startup, not on a customer's first request.

## What "the same contract" means across backends

A subprocess on a laptop, a container, a Linux namespace sandbox, a Windows
job object and a remote service the consumer operates all implement this
port, and ``psych_runtime.sandbox.contract.SandboxContractSuite`` proves they agree
on every observable clause: what a value looks like, how a failure is
reported, that stdout and stderr stay separate, that output past the capture
cap is truncated and marked so, that cancellation kills the whole tree, that a
guarantee the backend does not have is reported as such. Replacing one
backend with another changes ``describe()`` and nothing the model or the
report can see.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.core.errors import PsychError

__all__ = [
    "HostBinding",
    "OutputCapture",
    "Sandbox",
    "SandboxArtifact",
    "SandboxDescription",
    "SandboxFailure",
    "SandboxGuarantees",
    "SandboxLimit",
    "SandboxLimits",
    "SandboxResult",
    "SandboxSetupError",
    "achieved_level",
    "describe_sandbox",
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
    later. This model only fixes the shape every adapter shares, so the
    contract suite (``psych_runtime.sandbox.contract``) can drive them with the same
    values and prove they mean the same thing on every backend.

    Attributes:
        cpu_seconds: CPU time actually consumed, not wall-clock time. A
            program blocked waiting on a host binding's reply burns no CPU
            budget while it waits.
        address_space_bytes: the memory the process may use. A process
            backend applies it as the virtual address space (exceeding it
            surfaces to the program as an ordinary ``MemoryError``, because
            CPython's allocator checks malloc's return value rather than
            trusting it); a container applies it as a cgroup memory limit; a
            Windows job object applies it as a per-process commit limit.
        file_size_bytes: the largest file the process may write, counting
            every file it opens, not a total across files.
        process_count: how many processes (Linux counts threads too) the
            execution may hold at once. See ``psych_runtime.sandbox.subprocess`` for
            why that is a per-user limit on a process backend and what that
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


class OutputCapture(BaseModel):
    """How much of a program's output and workspace a backend keeps.

    The bound on *this process's* memory, distinct from the caps in
    ``SandboxLimits`` that bound the child: a program may print a gigabyte and
    the host must not hold it. Every backend reads each stream until this many
    bytes, then discards the rest while still counting it, so
    ``SandboxResult.stdout_size`` reports what the program actually produced
    and ``stdout_truncated`` says the capture stopped short of it.

    Attributes:
        stream_bytes: the most bytes captured per stream (stdout, stderr).
        collect_artifacts: whether regular files written under the workspace
            are read back into ``SandboxResult.artifacts``.
        artifact_count: the most files collected.
        artifact_bytes: the most bytes collected across every file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream_bytes: int = Field(default=1024 * 1024, ge=4_096)
    collect_artifacts: bool = False
    artifact_count: int = Field(default=16, ge=0)
    artifact_bytes: int = Field(default=16 * 1024 * 1024, ge=0)


class SandboxGuarantees(BaseModel):
    """How each guarantee held, on one execution or for one backend.

    One ``Enforcement`` per dimension a consumer might rely on. Every backend
    fills this from what it actually did and, where the child can check
    itself, from what the child reported: the network probe, the filesystem
    canary and the identity check all run *inside* the execution, so
    ``ENFORCED`` there is an observation and not a promise.

    Attributes:
        filesystem: the host's filesystem is hidden from the program. Verified
            by the child failing to read a canary file the host placed
            outside the workspace.
        network: the program has no route out. Verified by a routing probe
            inside the child that never sends a packet.
        process_tree: every process the program starts ends with the
            execution, by the host killing the group, job or container.
        identity: the program runs as an account distinct from the worker's.
            Reported from the child's own effective uid where there is one.
        cpu: CPU time is capped by the kernel (rlimit, cgroup quota or job).
        memory: memory is capped by the kernel.
        file_size: the size of any one file is capped.
        process_count: the number of processes is capped.
        wall_clock: the host ends the execution at the wall-clock cap.
        environment: the child's environment was built from the allowlist
            alone, never from this process's own environment.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filesystem: Enforcement = Enforcement.UNAVAILABLE
    network: Enforcement = Enforcement.UNAVAILABLE
    process_tree: Enforcement = Enforcement.UNAVAILABLE
    identity: Enforcement = Enforcement.UNAVAILABLE
    cpu: Enforcement = Enforcement.UNAVAILABLE
    memory: Enforcement = Enforcement.UNAVAILABLE
    file_size: Enforcement = Enforcement.UNAVAILABLE
    process_count: Enforcement = Enforcement.UNAVAILABLE
    wall_clock: Enforcement = Enforcement.UNAVAILABLE
    environment: Enforcement = Enforcement.UNAVAILABLE


def achieved_level(
    guarantees: SandboxGuarantees, *, network_required: bool = True
) -> IsolationLevel | None:
    """The isolation level a set of guarantees adds up to, or ``None``.

    One definition, used by every backend to fill ``SandboxResult.isolation``
    and ``SandboxDescription.isolation``, so no adapter can grade itself on a
    different curve.

    ``ISOLATED`` needs the host filesystem hidden, the process tree contained,
    memory and process count enforced by the kernel, a wall clock, and network
    denial where denial was requested (``network_required``; an execution that
    was deliberately granted network access is judged on everything else).
    ``PROCESS`` needs the process tree contained, a wall clock, and an
    environment built from nothing. Anything less is not a level this port
    recognises and is returned as ``None``, which every caller treats as
    failing any request.
    """
    enforced = Enforcement.ENFORCED
    isolated = (
        guarantees.filesystem is enforced
        and guarantees.process_tree is enforced
        and guarantees.memory is enforced
        and guarantees.process_count is enforced
        and guarantees.wall_clock is enforced
        and (not network_required or guarantees.network is enforced)
    )
    if isolated:
        return IsolationLevel.ISOLATED
    process = (
        guarantees.process_tree is enforced
        and guarantees.wall_clock is enforced
        and guarantees.environment is not Enforcement.UNAVAILABLE
    )
    if process:
        return IsolationLevel.PROCESS
    return None


class SandboxDescription(BaseModel):
    """What a backend can promise, learned before any program runs.

    ``Sandbox.describe()`` returns one. A deployment reads it at startup to
    refuse an agent whose ``code_execution.isolation`` this backend cannot
    meet, and a console reads it to show a person what "isolated" means on
    this host. Nothing here is a guess: a backend that has not probed a
    mechanism reports it ``UNVERIFIED``, and ``ready=False`` with a reason
    beats a green light that fails on first use.

    Attributes:
        backend: a short stable identifier: ``subprocess``, ``windows-job``,
            ``bubblewrap``, ``container``, ``remote``, or ``custom``.
        platform: where programs run, as ``sys.platform`` spells it, or what
            a remote provider reports.
        isolation: the strongest level this backend can deliver here, from
            ``achieved_level`` over ``guarantees``. ``None`` when it cannot
            reach even ``PROCESS``.
        guarantees: what each dimension is worth on this backend.
        mechanisms: the named controls in use, for a person reading a
            settings screen: ``rlimit``, ``setsid``, ``netns``, ``job_object``,
            ``cgroups``, ``user_namespace``, ``seatbelt``, and so on.
        network_grant_supported: whether ``network=True`` can be honoured.
        artifacts_supported: whether workspace files can be collected.
        languages: what the backend can run. Python only, today.
        limit_ceiling: caps this backend will not exceed whatever it is asked,
            or ``None`` when it has none of its own.
        ready: whether an execution would be attempted right now.
        problems: why not, when ``ready`` is false, in plain words.
        notes: facts a person configuring this backend should know that fit no
            other field: a deprecated interface in use, a shared per-user
            limit, an image that must be pulled first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    isolation: IsolationLevel | None
    guarantees: SandboxGuarantees
    mechanisms: tuple[str, ...] = ()
    network_grant_supported: bool = False
    artifacts_supported: bool = False
    languages: tuple[str, ...] = ("python",)
    limit_ceiling: SandboxLimits | None = None
    ready: bool = True
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


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
    ``psych_runtime.sandbox.protocol``), ``cancelled`` for an execution the caller
    stopped, ``isolation_unavailable`` for a level the backend could not
    provide, ``provider_error`` for a remote service that failed or
    disconnected. ``setup_failed`` never appears here because setup failures
    raise ``SandboxSetupError`` instead."""
    message: str = Field(max_length=8192)
    traceback: str | None = Field(default=None, max_length=65_536)


class SandboxArtifact(BaseModel):
    """One regular file the program wrote to its workspace.

    Attributes:
        path: relative to the workspace, ``/``-separated, never a host path.
        size_bytes: the file's size on disk.
        data: the bytes collected, up to the artifact budget. Shorter than
            ``size_bytes`` when ``truncated``.
        content_type: guessed from the name, ``application/octet-stream``
            when nothing better is known.
        truncated: the artifact budget cut this file short.
    """

    # base64 in JSON: an artifact is arbitrary bytes, and this model crosses
    # the wire whole when a remote sandbox service returns it.
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    path: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    data: bytes
    content_type: str = "application/octet-stream"
    truncated: bool = False


class SandboxResult(BaseModel):
    """What one execution produced, success or failure.

    Attributes:
        stdout: everything the program printed to standard output, decoded
            with replacement for the model's benefit. ``stdout_data`` is the
            same capture as raw bytes.
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
            not establish it). The same fact as ``guarantees.network`` being
            ``ENFORCED``, kept as a boolean for the callers that predate the
            fuller report.
        stdout_truncated: the program printed more than the capture cap.
        stderr_truncated: same, for standard error.
        stdout_data: the captured standard output, raw. Empty when a backend
            predates raw capture; ``stdout`` is then the only copy.
        stderr_data: same, for standard error.
        stdout_size: how many bytes the program actually wrote, which exceeds
            ``len(stdout_data)`` when the capture was truncated.
        stderr_size: same, for standard error.
        isolation: the level this execution achieved, from ``achieved_level``
            over ``guarantees``. ``None`` when the backend could not grade
            itself or predates the report.
        guarantees: how each guarantee held on this execution.
        artifacts: regular files the program wrote, when collection was on.
        artifacts_omitted: how many files were left behind past the count cap.
        cancelled: the caller's cancellation ended this execution.
    """

    # base64 for the raw captures, for the same reason as SandboxArtifact.
    model_config = ConfigDict(
        frozen=True, extra="forbid", ser_json_bytes="base64", val_json_bytes="base64"
    )

    stdout: str = ""
    stderr: str = ""
    value: Any = None
    failure: SandboxFailure | None = None
    duration_seconds: float = Field(ge=0)
    limit_hit: SandboxLimit | None = None
    network_denied: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_data: bytes = b""
    stderr_data: bytes = b""
    stdout_size: int = Field(default=0, ge=0)
    stderr_size: int = Field(default=0, ge=0)
    isolation: IsolationLevel | None = None
    guarantees: SandboxGuarantees = Field(default_factory=SandboxGuarantees)
    artifacts: tuple[SandboxArtifact, ...] = ()
    artifacts_omitted: int = Field(default=0, ge=0)
    cancelled: bool = False

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
    isolation (``psych_runtime.sandbox.subprocess``, ``psych_runtime.sandbox.windows``) is
    the floor; a kernel boundary (``psych_runtime.sandbox.container``,
    ``psych_runtime.sandbox.namespaces``, or a remote service through
    ``psych_runtime.sandbox.remote``) is what ``IsolationLevel.ISOLATED`` means.

    An implementation written against an earlier version of this port, with
    ``run()`` taking only ``bindings``, ``limits`` and ``network`` and no
    ``describe()``, still works: ``describe_sandbox`` reports it as a
    ``custom`` backend at ``PROCESS`` level with every guarantee unverified,
    and the runtime passes it only the arguments it accepts. Such a backend
    can serve an agent that asked for ``PROCESS`` isolation and no other.
    """

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, HostBinding] | None = None,
        limits: SandboxLimits | None = None,
        network: bool = False,
        isolation: IsolationLevel | None = None,
        capture: OutputCapture | None = None,
        cancel: asyncio.Event | None = None,
    ) -> SandboxResult:
        """Execute ``program`` once, in a fresh process, container or session.

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
            isolation: the minimum level the caller accepts. ``None`` means
                the adapter's own default, which is the strongest it has. An
                adapter that cannot reach the requested level returns a
                result whose ``failure.kind`` is ``isolation_unavailable``
                and whose output is withheld; it never runs the program under
                weaker terms than were asked for.
            capture: how much output and workspace to keep. ``None`` uses the
                adapter's defaults and collects no artifacts.
            cancel: set by the caller to end the execution early. The adapter
                kills the whole process tree, returns a result with
                ``cancelled=True`` and ``failure.kind == "cancelled"``, and
                leaves nothing running. Checked before spawn as well as
                during, so a cancellation that arrives first spawns nothing.

        Returns:
            The result, whether the program succeeded or failed. See the
            module and class docstrings for why a failure is a populated
            ``SandboxResult.failure`` rather than a raised exception.

        Raises:
            SandboxSetupError: the sandbox itself could not be prepared.
                Never raised because of anything the model's program did.
        """
        ...

    async def describe(self) -> SandboxDescription:
        """What this backend can promise on this host, right now.

        Cheap enough to call at startup and from a health check; it must not
        run a model's program, though it may spawn its own probe. Never
        raises for an unavailable backend: that is ``ready=False`` with the
        reason in ``problems``.
        """
        ...


async def describe_sandbox(sandbox: object) -> SandboxDescription:
    """``sandbox.describe()``, or an honest stand-in for a backend without one.

    A backend written against the earlier port has no ``describe``. The port's
    own contract still promises it is out of process, so such a backend is
    reported at ``PROCESS`` level, and every guarantee is ``UNVERIFIED``
    because nothing has checked it. That is enough to serve an agent that
    asked for ``PROCESS`` isolation and, correctly, not enough for one that
    asked for ``ISOLATED``.
    """
    describe = getattr(sandbox, "describe", None)
    if callable(describe):
        result = await describe()
        if isinstance(result, SandboxDescription):
            return result
    unverified = Enforcement.UNVERIFIED
    return SandboxDescription(
        backend="custom",
        platform="unknown",
        isolation=IsolationLevel.PROCESS,
        guarantees=SandboxGuarantees(
            process_tree=unverified,
            cpu=unverified,
            memory=unverified,
            file_size=unverified,
            process_count=unverified,
            wall_clock=unverified,
            environment=unverified,
        ),
        mechanisms=(),
        notes=(
            "this backend predates capability reporting: it is out of process by the "
            "port's contract and nothing else about it has been verified",
        ),
    )
