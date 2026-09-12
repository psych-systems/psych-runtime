"""Sandbox profiles: how a deployment answers a Spec's ``code_execution``.

DESIGN.md §4 and §18. A Spec names a *profile*; it never holds the sandbox.
This module is the other half of that sentence: the deployment builds a
``SandboxProfile`` per logical name, each wrapping a ``Sandbox`` and the hard
limits the deployment will not let any agent exceed, and the runtime resolves
a Spec's request against it here.

## The intersection, and who computes it

The terms an execution actually runs under are the intersection of four
things, in this order, and each one can only narrow what the previous left:

1. **What the backend can provide.** ``Sandbox.describe()`` says which
   isolation level it can reach and whether it can grant network access.
2. **What the deployment allows.** ``SandboxProfile.hard_limits`` and
   ``SandboxProfile.allow_network`` are ceilings nothing below can raise.
3. **What the tenant's policy allows.** An optional ``CodeExecutionPolicy``
   narrows the grant per ``Scope``. Its answer is intersected again with what
   it was given, so a policy that tries to widen a grant has no effect.
4. **What the Spec asked for.** ``CodeExecution.limits`` are requests; a
   request above a ceiling is clamped to it, silently, because asking for
   more than the deployment allows is not an error the agent's author can
   fix from inside the Spec. A request for an isolation level or network
   access the profile cannot give *is* refused, because running the program
   under weaker terms than it asked for would be a lie in the report.

``narrow_limits`` is the one function that takes the minimum on every
dimension, and both the profile registry and the runtime call it so the two
cannot disagree (the same rule DESIGN.md §10.5 applies to tool access).

## Why refusals are values here and failures at the call site

``resolve_execution`` returns either an ``ExecutionPlan`` or an
``ExecutionRefusal`` naming the clause that could not be met. The runtime turns
a refusal into a structured ``run_code`` result the model can read, so an agent
whose deployment cannot meet its request learns that once, in data, rather than
the whole Run failing. A profile that does not exist at all is different: that
is a configuration error nothing about the Run can route around, so it raises
``SandboxSetupError`` at the moment the Attempt starts, and publish-time
validation (``ValidationContext.sandbox_profiles``) catches it earlier still.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from psych_runtime.core.code_execution import IsolationLevel, NetworkAccess
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import CodeExecution, CodeExecutionLimits
from psych_runtime.sandbox.port import (
    OutputCapture,
    Sandbox,
    SandboxDescription,
    SandboxLimits,
    SandboxSetupError,
    describe_sandbox,
)

__all__ = [
    "DEFAULT_HARD_LIMITS",
    "DEFAULT_PROFILE",
    "CodeExecutionGrant",
    "CodeExecutionPolicy",
    "ExecutionPlan",
    "ExecutionRefusal",
    "SandboxProfile",
    "SandboxProfiles",
    "intersect_grants",
    "narrow_limits",
    "resolve_execution",
]

DEFAULT_PROFILE: Final = "default"
"""The profile ``Runtime(sandbox=...)`` registers and a ``CodeExecution`` names
when it names nothing else."""

DEFAULT_HARD_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
"""The ceiling a profile applies when the deployment sets none. The same
numbers every local adapter defaults to, with the same reasoning (see
``psych_runtime.sandbox.subprocess``): generous for a short program, short enough
that a runaway one does not hold a worker. A deployment that wants longer
programs raises this on the profile; an agent cannot."""


def narrow_limits(ceiling: SandboxLimits, requested: CodeExecutionLimits | None) -> SandboxLimits:
    """The smaller of ``ceiling`` and ``requested`` on every dimension.

    A request left ``None`` takes the ceiling. A request above the ceiling is
    clamped to it rather than refused: the ceiling is the deployment's to
    raise, and an agent asking for more than it is allowed should get what it
    is allowed, not nothing.
    """
    if requested is None:
        return ceiling

    def pick(cap: float, ask: float | None) -> float:
        return cap if ask is None else min(cap, ask)

    return SandboxLimits(
        cpu_seconds=pick(ceiling.cpu_seconds, requested.cpu_seconds),
        address_space_bytes=int(pick(ceiling.address_space_bytes, requested.memory_bytes)),
        file_size_bytes=int(pick(ceiling.file_size_bytes, requested.file_size_bytes)),
        process_count=int(pick(ceiling.process_count, requested.process_count)),
        wall_seconds=pick(ceiling.wall_seconds, requested.wall_seconds),
    )


def _min_limits(a: SandboxLimits, b: SandboxLimits) -> SandboxLimits:
    return SandboxLimits(
        cpu_seconds=min(a.cpu_seconds, b.cpu_seconds),
        address_space_bytes=min(a.address_space_bytes, b.address_space_bytes),
        file_size_bytes=min(a.file_size_bytes, b.file_size_bytes),
        process_count=min(a.process_count, b.process_count),
        wall_seconds=min(a.wall_seconds, b.wall_seconds),
    )


@dataclass(frozen=True, slots=True)
class CodeExecutionGrant:
    """What one party allows an execution: limits, network, bindings, level.

    The unit ``intersect_grants`` operates on. A ``CodeExecutionPolicy``
    returns one of these for a Scope, and the runtime intersects it with the
    grant it derived from the profile and the Spec, so a policy can only take
    away.

    Attributes:
        limits: resource caps.
        network: whether raw network access may be granted at all.
        bindings: which tool names a program may call. ``None`` means "no
            opinion" (every tool the Spec grants); a frozenset restricts.
        isolation: the minimum level required. Intersecting takes the
            stronger requirement.
        artifacts: whether workspace files may be collected.
    """

    limits: SandboxLimits
    network: bool = False
    bindings: frozenset[str] | None = None
    isolation: IsolationLevel = IsolationLevel.PROCESS
    artifacts: bool = True


def intersect_grants(a: CodeExecutionGrant, b: CodeExecutionGrant) -> CodeExecutionGrant:
    """The grant both ``a`` and ``b`` allow. Never wider than either."""
    if a.bindings is None:
        bindings = b.bindings
    elif b.bindings is None:
        bindings = a.bindings
    else:
        bindings = a.bindings & b.bindings
    isolation = a.isolation if a.isolation.satisfies(b.isolation) else b.isolation
    return CodeExecutionGrant(
        limits=_min_limits(a.limits, b.limits),
        network=a.network and b.network,
        bindings=bindings,
        isolation=isolation,
        artifacts=a.artifacts and b.artifacts,
    )


@runtime_checkable
class CodeExecutionPolicy(Protocol):
    """A tenant-level narrowing of code execution, asked per Scope.

    Optional. A consumer whose tenants get different budgets, or who forbids
    network access for one plan and allows it for another, implements this
    and hands it to ``Runtime``. Whatever it returns is intersected with the
    grant it was given, so it cannot widen anything; a policy that raises is
    treated as refusing the execution, the same rule ``Policy`` follows.
    """

    async def narrow(self, scope: Scope, grant: CodeExecutionGrant) -> CodeExecutionGrant:
        """Return a grant no wider than ``grant`` for this ``scope``."""
        ...


@dataclass(slots=True)
class SandboxProfile:
    """One logical sandbox a deployment offers, by name.

    Attributes:
        name: what a Spec's ``code_execution.profile`` names.
        sandbox: the implementation. Any object satisfying ``Sandbox``,
            including one written against the earlier port.
        hard_limits: the ceiling no agent using this profile can exceed.
        allow_network: whether an agent may be granted raw network access
            through this profile. Off by default; on is a deliberate choice
            because it bypasses the egress seam for that program.
        description: what ``sandbox.describe()`` last said, filled by
            ``SandboxProfiles.verify`` or on first use. ``None`` until then.
    """

    name: str
    sandbox: Sandbox
    hard_limits: SandboxLimits = field(default_factory=lambda: DEFAULT_HARD_LIMITS)
    allow_network: bool = False
    description: SandboxDescription | None = None

    async def describe(self) -> SandboxDescription:
        """The backend's own report, fetched once and cached."""
        if self.description is None:
            self.description = await describe_sandbox(self.sandbox)
        return self.description

    def refresh(self) -> None:
        """Forget the cached description so the next ``describe`` asks again."""
        self.description = None


class SandboxProfiles:
    """The deployment's profile registry: names to implementations."""

    def __init__(self, profiles: Iterable[SandboxProfile] = ()) -> None:
        self._profiles: dict[str, SandboxProfile] = {}
        for profile in profiles:
            self.add(profile)

    def add(self, profile: SandboxProfile) -> None:
        if profile.name in self._profiles:
            raise SandboxSetupError(
                f"a sandbox profile named {profile.name!r} is already registered"
            )
        self._profiles[profile.name] = profile

    @property
    def names(self) -> frozenset[str]:
        """For ``ValidationContext(sandbox_profiles=...)``."""
        return frozenset(self._profiles)

    def get(self, name: str) -> SandboxProfile | None:
        return self._profiles.get(name)

    def require(self, name: str) -> SandboxProfile:
        profile = self._profiles.get(name)
        if profile is None:
            known = ", ".join(sorted(self._profiles)) or "none"
            raise SandboxSetupError(
                f"no sandbox profile named {name!r} is registered on this Runtime "
                f"(known: {known}). Register one, or point the agent's code_execution.profile "
                "at a profile that exists."
            )
        return profile

    def __iter__(self) -> Iterable[SandboxProfile]:
        return iter(list(self._profiles.values()))

    def __len__(self) -> int:
        return len(self._profiles)

    async def verify(self) -> Mapping[str, SandboxDescription]:
        """Ask every backend what it can do, and cache the answers.

        Call at startup. Nothing here runs a model's program. A backend that
        is not ready is reported, not raised: a deployment decides whether a
        profile being down should stop the process (``require_ready``) or
        merely refuse the agents that need it.
        """
        found: dict[str, SandboxDescription] = {}
        for profile in list(self._profiles.values()):
            profile.refresh()
            found[profile.name] = await profile.describe()
        return found

    async def require_ready(self) -> None:
        """``verify``, then raise if any profile cannot run anything.

        Raises:
            SandboxSetupError: naming every profile that is not ready and why.
        """
        descriptions = await self.verify()
        broken = [
            f"{name}: {'; '.join(description.problems) or 'not ready'}"
            for name, description in descriptions.items()
            if not description.ready
        ]
        if broken:
            raise SandboxSetupError("sandbox profile(s) are not ready: " + " | ".join(broken))


@dataclass(frozen=True, slots=True)
class ExecutionRefusal:
    """Why a Spec's request cannot be met by its profile, as data.

    Attributes:
        kind: a stable token for the clause: ``isolation_unavailable``,
            ``network_not_allowed``, ``backend_not_ready``.
        message: what to tell a person, and the model.
    """

    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Everything the ``run_code`` tool needs for one Run, resolved once.

    Attributes:
        profile: where programs run.
        limits: the effective caps, after every narrowing.
        network: whether the program gets raw network access.
        isolation: the minimum level the execution must achieve.
        bindings: the tool names the program may call, sorted.
        capture: how much output and workspace the backend keeps.
        config: the Spec's own request, for output budgets and preservation.
    """

    profile: SandboxProfile
    limits: SandboxLimits
    network: bool
    isolation: IsolationLevel
    bindings: tuple[str, ...]
    capture: OutputCapture
    config: CodeExecution


async def resolve_execution(
    config: CodeExecution,
    granted_tools: Iterable[str],
    profiles: SandboxProfiles,
    *,
    scope: Scope,
    policy: CodeExecutionPolicy | None = None,
) -> ExecutionPlan | ExecutionRefusal:
    """Turn a Spec's request into the terms an execution will run under.

    Args:
        config: the Spec's ``code_execution``.
        granted_tools: the names in the Spec's ``tools``, which bound what a
            program may call.
        profiles: the deployment's registry.
        scope: whose Run this is, for the policy.
        policy: the optional tenant narrowing.

    Returns:
        A plan, or a refusal naming the clause the profile cannot meet.

    Raises:
        SandboxSetupError: ``config.profile`` names no registered profile.
    """
    profile = profiles.require(config.profile)
    description = await profile.describe()

    spec_bindings = (
        frozenset(granted_tools)
        if config.bindings is None
        else frozenset(config.bindings) & frozenset(granted_tools)
    )
    grant = CodeExecutionGrant(
        limits=narrow_limits(profile.hard_limits, config.limits),
        network=profile.allow_network and config.network is NetworkAccess.UNRESTRICTED,
        bindings=spec_bindings,
        isolation=config.isolation,
        artifacts=config.artifacts.collection == "collect",
    )
    if description.limit_ceiling is not None:
        grant = CodeExecutionGrant(
            limits=_min_limits(grant.limits, description.limit_ceiling),
            network=grant.network,
            bindings=grant.bindings,
            isolation=grant.isolation,
            artifacts=grant.artifacts,
        )
    if policy is not None:
        try:
            narrowed = await policy.narrow(scope, grant)
        except Exception as err:  # a policy that raises refuses, never widens
            return ExecutionRefusal(
                kind="policy_refused",
                message=f"the code execution policy refused this Run: {err}",
            )
        grant = intersect_grants(grant, narrowed)

    if not description.ready:
        reasons = "; ".join(description.problems) or "the backend reported it is not ready"
        return ExecutionRefusal(
            kind="backend_not_ready",
            message=f"sandbox profile {profile.name!r} cannot run programs right now: {reasons}",
        )
    if description.isolation is None or not description.isolation.satisfies(grant.isolation):
        offered = description.isolation.value if description.isolation is not None else "none"
        return ExecutionRefusal(
            kind="isolation_unavailable",
            message=(
                f"this agent requires {grant.isolation.value!r} isolation and sandbox profile "
                f"{profile.name!r} ({description.backend} on {description.platform}) can "
                f"provide {offered!r}. The program was not run. Configure a profile that "
                "provides the requested level, or set code_execution.isolation to "
                "'process' only for code you trust."
            ),
        )
    if config.network is NetworkAccess.UNRESTRICTED and not grant.network:
        return ExecutionRefusal(
            kind="network_not_allowed",
            message=(
                f"this agent asks for unrestricted network access and sandbox profile "
                f"{profile.name!r} does not allow it. The program was not run. Route the "
                "fetch through a host binding, or allow network on the profile."
            ),
        )
    if grant.network and not description.network_grant_supported:
        return ExecutionRefusal(
            kind="network_not_allowed",
            message=(
                f"sandbox profile {profile.name!r} cannot grant network access on this "
                "backend. The program was not run."
            ),
        )

    capture = OutputCapture(
        stream_bytes=config.output.max_bytes,
        collect_artifacts=grant.artifacts and description.artifacts_supported,
        artifact_count=config.artifacts.max_count,
        artifact_bytes=config.artifacts.max_total_bytes,
    )
    bindings = tuple(sorted(grant.bindings or ()))
    return ExecutionPlan(
        profile=profile,
        limits=grant.limits,
        network=grant.network,
        isolation=grant.isolation,
        bindings=bindings,
        capture=capture,
        config=config,
    )
