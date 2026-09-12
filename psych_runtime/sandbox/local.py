"""Pick the local backend this host supports, and say what it is worth.

DESIGN.md §18. A developer should be able to use ``run_code`` on their own
machine with nothing installed, and an operator should be able to ask a
deployment "what can you actually guarantee here?" and get a straight
answer. Both are this module.

``local_sandbox()`` returns the strongest backend the host has, or refuses
loudly when asked for a level it cannot reach:

- **Linux** with a usable bubblewrap: ``NamespaceSandbox``,
  ``IsolationLevel.ISOLATED``. Usable, not merely installed: a host that
  ships ``bwrap`` and forbids unprivileged user namespaces is checked by
  running it, because a backend that cannot spawn is worse than one that
  says it is only process-level.
- **Linux** without it, and **macOS**: ``SubprocessSandbox``,
  ``IsolationLevel.PROCESS``.
- **Windows**: ``WindowsJobSandbox``, ``IsolationLevel.PROCESS``.

Nothing here downloads, installs or pulls anything. A host that cannot
reach the requested level gets a ``SandboxSetupError`` that names what to
install, and ``detect_local_backends()`` reports the same facts as data for
a settings screen or ``psych doctor``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from psych_runtime.core.code_execution import IsolationLevel
from psych_runtime.sandbox.port import Sandbox, SandboxLimits, SandboxSetupError

__all__ = ["LocalBackend", "detect_local_backends", "local_sandbox"]


@dataclass(frozen=True, slots=True)
class LocalBackend:
    """One backend this host could run, and why or why not.

    Attributes:
        name: ``bubblewrap``, ``subprocess``, ``windows-job`` or ``container``.
        isolation: the level it reaches when available.
        available: whether it can be constructed here right now.
        reason: what is missing when it cannot, in plain words.
    """

    name: str
    isolation: IsolationLevel
    available: bool
    reason: str = ""


def _platform() -> str:
    """The platform name, read through a call so every branch below stays
    type-checked on every platform rather than being pruned as dead code."""
    return sys.platform


def detect_local_backends() -> tuple[LocalBackend, ...]:
    """Every local backend, in preference order, with its availability.

    Cheap: looks for binaries and reads the platform; spawns nothing.
    ``Sandbox.describe()`` on a constructed backend is the deeper check.
    """
    found: list[LocalBackend] = []
    if _platform() == "linux":
        from psych_runtime.sandbox.namespaces import (  # noqa: PLC0415
            bubblewrap_usable,
            detect_bubblewrap,
        )

        installed = detect_bubblewrap()
        usable = bubblewrap_usable(installed) if installed else None
        if installed is None:
            why = "install the `bubblewrap` package (provides `bwrap`)"
        elif usable is None:
            why = (
                "`bwrap` is installed but cannot unshare here: this kernel or its "
                "security policy forbids unprivileged user namespaces"
            )
        else:
            why = ""
        found.append(LocalBackend("bubblewrap", IsolationLevel.ISOLATED, usable is not None, why))
    if _platform() in ("linux", "darwin"):
        found.append(LocalBackend("subprocess", IsolationLevel.PROCESS, True))
    if _platform() == "win32":
        found.append(LocalBackend("windows-job", IsolationLevel.PROCESS, True))
    runtime = shutil.which("docker") or shutil.which("podman")
    found.append(
        LocalBackend(
            "container",
            IsolationLevel.ISOLATED,
            runtime is not None and _platform() != "win32",
            (
                "a POSIX worker with docker or podman and a pulled image"
                if runtime is None or _platform() == "win32"
                else "checked at describe(): the daemon must answer and the image be present"
            ),
        )
    )
    return tuple(found)


def local_sandbox(
    *,
    isolation: IsolationLevel = IsolationLevel.PROCESS,
    python_bin: str | None = None,
    default_limits: SandboxLimits | None = None,
    env_allowlist: Mapping[str, str] | None = None,
    allow_same_uid: bool = False,
) -> Sandbox:
    """The strongest local backend this host has, at least ``isolation``.

    Args:
        isolation: the minimum level the returned backend must reach.
            ``PROCESS`` by default, because that is what every platform
            can do with nothing installed; a caller who needs ``ISOLATED``
            says so and gets a refusal with install guidance where the host
            cannot provide it, never a weaker backend.
        python_bin: the interpreter the child runs. Defaults to this one.
        default_limits: the backend's defaults when a call passes none.
        env_allowlist: extra environment variables the child receives.
        allow_same_uid: POSIX only; see ``SubprocessSandbox``. The Windows
            backend always runs as the worker's account and says so.

    Raises:
        SandboxSetupError: nothing on this host reaches ``isolation``.
    """
    if _platform() == "linux":
        from psych_runtime.sandbox.namespaces import (  # noqa: PLC0415
            NamespaceSandbox,
            bubblewrap_usable,
        )

        # Usable, not installed. A host that ships `bwrap` and forbids
        # unprivileged user namespaces would otherwise get an isolated
        # backend whose every execution fails at spawn, which is a worse
        # answer than an honest process-level one.
        if bubblewrap_usable() is not None:
            return NamespaceSandbox(
                python_bin=python_bin, default_limits=default_limits, env_allowlist=env_allowlist
            )
        if isolation is IsolationLevel.ISOLATED:
            raise SandboxSetupError(
                "no local backend on this Linux host reaches 'isolated': install the "
                "`bubblewrap` package (provides `bwrap`) on a kernel that permits "
                "unprivileged user namespaces, or use ContainerSandbox with a pulled "
                "image, or a RemoteSandbox. Nothing is installed implicitly."
            )
        from psych_runtime.sandbox.subprocess import SubprocessSandbox  # noqa: PLC0415

        return SubprocessSandbox(
            python_bin=python_bin,
            default_limits=default_limits,
            env_allowlist=env_allowlist,
            allow_same_uid=allow_same_uid or os.geteuid() != 0,
        )
    if _platform() == "darwin":
        if isolation is IsolationLevel.ISOLATED:
            raise SandboxSetupError(
                "no local backend on macOS reaches 'isolated': the subprocess backend can "
                "deny network and confine the filesystem through seatbelt but cannot cap "
                "memory. Use ContainerSandbox with a pulled image, or a RemoteSandbox."
            )
        from psych_runtime.sandbox.subprocess import SubprocessSandbox  # noqa: PLC0415

        return SubprocessSandbox(
            python_bin=python_bin,
            default_limits=default_limits,
            env_allowlist=env_allowlist,
            allow_same_uid=allow_same_uid or os.geteuid() != 0,
        )
    if _platform() == "win32":
        if isolation is IsolationLevel.ISOLATED:
            raise SandboxSetupError(
                "no local backend on Windows reaches 'isolated': the job-object backend "
                "contains the process tree and caps its resources but cannot hide the "
                "filesystem or deny the network. Use a RemoteSandbox, or run the worker "
                "on Linux with ContainerSandbox or bubblewrap."
            )
        from psych_runtime.sandbox.windows import WindowsJobSandbox  # noqa: PLC0415

        return WindowsJobSandbox(
            python_bin=python_bin, default_limits=default_limits, env_allowlist=env_allowlist
        )
    raise SandboxSetupError(f"no local sandbox backend supports platform {_platform()!r}")
