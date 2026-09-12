"""The Sandbox port, its local and remote adapters, and the profile registry.

Import the adapters from their own modules: ``psych_runtime.sandbox.subprocess``,
``psych_runtime.sandbox.windows``, ``psych_runtime.sandbox.namespaces``,
``psych_runtime.sandbox.container``, ``psych_runtime.sandbox.remote``. This package
re-exports only what is safe to import on every platform.
"""

from __future__ import annotations

from psych_runtime.sandbox.container import ContainerSandbox, detect_container_runtime
from psych_runtime.sandbox.local import LocalBackend, detect_local_backends, local_sandbox
from psych_runtime.sandbox.port import (
    HostBinding,
    OutputCapture,
    Sandbox,
    SandboxArtifact,
    SandboxDescription,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimit,
    SandboxLimits,
    SandboxResult,
    SandboxSetupError,
    achieved_level,
    describe_sandbox,
)
from psych_runtime.sandbox.profiles import (
    DEFAULT_HARD_LIMITS,
    DEFAULT_PROFILE,
    CodeExecutionGrant,
    CodeExecutionPolicy,
    ExecutionPlan,
    ExecutionRefusal,
    SandboxProfile,
    SandboxProfiles,
    intersect_grants,
    narrow_limits,
    resolve_execution,
)
from psych_runtime.sandbox.remote import RemoteSandbox
from psych_runtime.sandbox.subprocess import SubprocessSandbox

__all__ = [
    "DEFAULT_HARD_LIMITS",
    "DEFAULT_PROFILE",
    "CodeExecutionGrant",
    "CodeExecutionPolicy",
    "ContainerSandbox",
    "ExecutionPlan",
    "ExecutionRefusal",
    "HostBinding",
    "LocalBackend",
    "OutputCapture",
    "RemoteSandbox",
    "Sandbox",
    "SandboxArtifact",
    "SandboxDescription",
    "SandboxFailure",
    "SandboxGuarantees",
    "SandboxLimit",
    "SandboxLimits",
    "SandboxProfile",
    "SandboxProfiles",
    "SandboxResult",
    "SandboxSetupError",
    "SubprocessSandbox",
    "achieved_level",
    "describe_sandbox",
    "detect_container_runtime",
    "detect_local_backends",
    "intersect_grants",
    "local_sandbox",
    "narrow_limits",
    "resolve_execution",
]
