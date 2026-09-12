"""The local backend this host has, through ``local_sandbox()``.

The cross-platform half of the sandbox matrix. ``local_sandbox()`` picks the
strongest backend the host supports (bubblewrap on Linux where installed,
the subprocess backend elsewhere on POSIX, the job object on Windows), and
this file runs the whole ``SandboxContractSuite`` against whatever that is,
so a CI job on each operating system proves the same observable contract
with real child processes rather than skipping. The platform-specific files
beside this one cover what only one backend has to get right.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from psych_runtime.core.code_execution import IsolationLevel
from psych_runtime.sandbox.contract import SandboxContractSuite
from psych_runtime.sandbox.local import detect_local_backends, local_sandbox
from psych_runtime.sandbox.port import Sandbox, SandboxLimits, SandboxSetupError

pytestmark = pytest.mark.functional

_FAST_LIMITS = SandboxLimits(
    cpu_seconds=3.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _python_bin() -> str | None:
    """A system interpreter where the worker is root and drops privileges.

    A root-owned virtualenv is not traversable by ``nobody``; a system
    interpreter is. Everywhere else this process's own interpreter is right.
    """
    if sys.platform != "win32" and os.geteuid() == 0:
        for candidate in (
            f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
            "/usr/bin/python3",
        ):
            if Path(candidate).is_file():
                return candidate
    return None


def _backend() -> Sandbox:
    return local_sandbox(python_bin=_python_bin(), default_limits=_FAST_LIMITS)


class TestLocalSandbox(SandboxContractSuite):
    @pytest.fixture
    def sandbox(self) -> Sandbox:
        return _backend()


class TestDetection:
    def test_every_platform_has_a_process_level_backend(self) -> None:
        backends = detect_local_backends()
        available = [b for b in backends if b.available]
        assert available, backends
        assert any(
            b.isolation is IsolationLevel.PROCESS or b.isolation.satisfies(IsolationLevel.PROCESS)
            for b in available
        )

    def test_the_picked_backend_describes_itself_consistently(self) -> None:
        sandbox = _backend()
        backends = {b.name: b for b in detect_local_backends()}
        expected = next(b for b in backends.values() if b.available)
        assert expected.name in ("bubblewrap", "subprocess", "windows-job")
        assert sandbox is not None

    async def test_describe_matches_what_detection_promised(self) -> None:
        sandbox = _backend()
        description = await sandbox.describe()
        assert description.ready, description.problems
        picked = next(b for b in detect_local_backends() if b.available)
        assert description.isolation is not None
        assert picked.isolation.satisfies(description.isolation)

    def test_asking_for_isolated_where_unavailable_is_refused_not_downgraded(self) -> None:
        picked = next(b for b in detect_local_backends() if b.available)
        if picked.isolation is IsolationLevel.ISOLATED:
            sandbox = local_sandbox(isolation=IsolationLevel.ISOLATED, python_bin=_python_bin())
            assert sandbox is not None
            return
        with pytest.raises(SandboxSetupError, match="isolated"):
            local_sandbox(isolation=IsolationLevel.ISOLATED, python_bin=_python_bin())
