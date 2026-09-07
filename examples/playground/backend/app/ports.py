"""The ports this deployment implements for Psych, per account.

Each class here answers one question the library refuses to answer itself
(DESIGN.md §1, §14): which hosts a tenant may reach, which tools a tenant may
call, and whether model-written code may run at all. Every one reads the
asking account's own ``RuntimeSettings`` fresh on each call rather than
holding a snapshot, so a setting changed in the console applies to the next
call with nothing to invalidate -- the same rule ``app.secrets`` follows and
for the same reason.

None of them is a security boundary between accounts. Isolation comes from
``Scope``, which every call carries; these decide what one account has chosen
for itself.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

from app.settings_store import RuntimeSettings, SettingsStore
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version
from psych_runtime.sandbox.port import SandboxLimits, SandboxSetupError
from psych_runtime.sandbox.subprocess import SubprocessSandbox
from psych_runtime.tools.policy import Decision

_LOG = logging.getLogger("psych.playground.ports")


class AccountEgressPolicy:
    """``EgressPolicy`` over each account's own allow-list.

    Consulted by the one ``HttpTransport`` every outbound call goes through,
    which is the property DESIGN.md §14 asks for: a control that covered the
    model but not MCP, or MCP but not an HTTP tool, would be worse than none.
    An empty list allows everything, which is what a fresh account wants.
    """

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def allow(self, scope: Scope, url: str) -> bool:
        patterns = (await self._settings.load(scope.tenant)).runtime.egress_allow
        if not patterns:
            return True
        host = httpx.URL(url).host.lower()
        allowed = any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in patterns)
        if not allowed:
            _LOG.info(
                "egress refused for tenant %s: %s is not on the allow-list", scope.tenant, host
            )
        return allowed


class AccountToolPolicy:
    """``Policy`` over each account's denied-tool list.

    ``allow_run`` says yes to everything: which agents an account may run is
    already decided by ownership in ``app.main``, and answering it twice would
    invite someone to rely on the weaker check. ``allow_tool`` is the part a
    consumer actually writes: a name on the list is refused with a reason the
    model can read and act on, whatever the Spec granted.
    """

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
        _ = args
        denied = (await self._settings.load(scope.tenant)).runtime.denied_tools
        if tool in denied:
            return Decision.deny(
                f"the tool {tool!r} is switched off for this workspace in Settings; "
                "do something else or tell the user it is unavailable"
            )
        return Decision.allow()

    async def allow_run(self, scope: Scope, version: Version) -> Decision:
        _ = scope, version
        return Decision.allow()


def sandbox_python_bin() -> str | None:
    """A system interpreter the sandboxed child can execute.

    The adapter drops the child to an unprivileged account when it can, and a
    virtualenv under a root-owned home is not traversable from there; a system
    interpreter is. The same search ``app.scenarios.sandboxed_code`` makes.
    """
    for candidate in (
        f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
        shutil.which("python3"),
        sys.executable,
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class SandboxProvision:
    """Whether this process can offer ``run_code`` at all, decided once at boot.

    ``SubprocessSandbox`` refuses to run the model's program as the worker's
    own uid unless told that is acceptable, because a same-uid child can read
    the worker's environment. Root drops to ``nobody`` and is fine; an
    unprivileged worker needs ``PSYCH_PLAYGROUND_SANDBOX_SAME_UID=1`` to say
    the trade is understood, which the playground's own image sets because a
    demo whose secrets live in a file the same user can read anyway loses
    nothing by it. Elsewhere the reason is reported to the console rather than
    swallowed, so "no run_code" reads as a decision and not a bug.
    """

    def __init__(self, *, allow_same_uid: bool) -> None:
        self.reason: str | None = None
        self._python = sandbox_python_bin()
        self._allow_same_uid = allow_same_uid
        if self._python is None:
            self.reason = "no Python interpreter was found for the sandboxed child process"
            return
        try:
            SubprocessSandbox(python_bin=self._python, allow_same_uid=allow_same_uid)
        except SandboxSetupError as err:
            self.reason = str(err)

    @property
    def available(self) -> bool:
        return self.reason is None

    def build(self, runtime: RuntimeSettings) -> SubprocessSandbox | None:
        """A sandbox carrying this account's limits, or ``None`` when the
        account switched it off or this host cannot offer one."""
        if not self.available or not runtime.sandbox_enabled or self._python is None:
            return None
        limits = runtime.sandbox_limits
        return SubprocessSandbox(
            python_bin=self._python,
            allow_same_uid=self._allow_same_uid,
            default_limits=SandboxLimits(
                cpu_seconds=limits.cpu_seconds,
                address_space_bytes=limits.address_space_bytes,
                file_size_bytes=limits.file_size_bytes,
                process_count=limits.process_count,
                wall_seconds=limits.wall_seconds,
            ),
        )


def same_uid_opt_in() -> bool:
    return os.environ.get("PSYCH_PLAYGROUND_SANDBOX_SAME_UID", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
