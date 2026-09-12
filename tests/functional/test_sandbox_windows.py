"""``WindowsJobSandbox`` against a real child process inside a real job object.

DESIGN.md §22: the sandbox is tested against a real child, never a mock of
one. Everything here spawns an actual CPython child on Windows, contains it
in a job object, and inspects what really happened to it. The shared
``SandboxContractSuite`` proves this backend agrees with every other one;
the cases below are what a job object specifically has to get right.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox.contract import SandboxContractSuite
from psych_runtime.sandbox.port import OutputCapture, Sandbox, SandboxLimit, SandboxLimits

pytestmark = [
    pytest.mark.functional,
    pytest.mark.skipif(sys.platform != "win32", reason="the job-object backend is Windows-only"),
]

_FAST_LIMITS = SandboxLimits(
    cpu_seconds=3.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _backend() -> Sandbox:
    from psych_runtime.sandbox.windows import WindowsJobSandbox

    return WindowsJobSandbox(default_limits=_FAST_LIMITS)


@pytest.fixture
def sandbox() -> Sandbox:
    return _backend()


class TestWindowsJobSandbox(SandboxContractSuite):
    @pytest.fixture
    def sandbox(self) -> Sandbox:
        return _backend()


class TestHonestGuarantees:
    async def test_it_is_process_level_and_says_so(self, sandbox: Sandbox) -> None:
        result = await sandbox.run("return 1", limits=_FAST_LIMITS)
        assert result.isolation is IsolationLevel.PROCESS
        assert result.guarantees.filesystem is Enforcement.UNAVAILABLE
        assert result.guarantees.network is Enforcement.UNAVAILABLE
        assert result.guarantees.identity is Enforcement.UNAVAILABLE
        assert result.guarantees.file_size is Enforcement.UNAVAILABLE
        assert result.guarantees.process_tree is Enforcement.ENFORCED
        assert result.guarantees.memory is Enforcement.ENFORCED
        assert result.guarantees.process_count is Enforcement.ENFORCED

    async def test_the_canary_confirms_the_filesystem_is_shared(self, sandbox: Sandbox) -> None:
        """The child could read a file outside its workspace, and the result
        says so rather than claiming a boundary it does not have."""
        result = await sandbox.run("return 1", limits=_FAST_LIMITS)
        assert result.guarantees.filesystem is Enforcement.UNAVAILABLE

    async def test_describe_reports_process_level(self, sandbox: Sandbox) -> None:
        description = await sandbox.describe()
        assert description.ready
        assert description.backend == "windows-job"
        assert description.isolation is IsolationLevel.PROCESS
        assert "job_object" in description.mechanisms


class TestEnvironment:
    async def test_only_what_cpython_needs_plus_the_allowlist_reaches_the_child(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from psych_runtime.sandbox.windows import WindowsJobSandbox

        monkeypatch.setenv("PSYCH_WIN_SECRET", "leak")
        sandbox = WindowsJobSandbox(
            default_limits=_FAST_LIMITS, env_allowlist={"PSYCH_WIN_ALLOWED": "yes"}
        )
        result = await sandbox.run(
            "import os\nreturn [os.environ.get('PSYCH_WIN_SECRET'), "
            "os.environ.get('PSYCH_WIN_ALLOWED'), sorted(os.environ)]",
            limits=_FAST_LIMITS,
        )
        assert result.ok, result.failure
        secret, allowed, names = result.value
        assert secret is None
        assert allowed == "yes"
        assert "SYSTEMROOT" in names
        assert "PSYCH_WIN_SECRET" not in names


class TestWorkspace:
    async def test_the_workspace_is_removed_and_a_junction_in_it_is_not_followed(
        self, sandbox: Sandbox, tmp_path: Path
    ) -> None:
        """A program that plants a junction to a host directory inside its
        workspace must not have that directory emptied by cleanup."""
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("keep", encoding="utf-8")
        program = (
            "import subprocess, os\n"
            f"subprocess.run(['cmd', '/c', 'mklink', '/J', 'link', {str(victim)!r}], "
            "capture_output=True)\n"
            "return [os.path.isdir('link'), os.getcwd()]\n"
        )
        result = await sandbox.run(
            program, limits=_FAST_LIMITS, capture=OutputCapture(collect_artifacts=True)
        )
        assert result.ok, result.failure
        linked, workdir = result.value
        assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert not any(a.path.startswith("link") for a in result.artifacts)
        if linked:
            assert not Path(workdir).exists()

    async def test_a_read_only_file_left_behind_does_not_block_cleanup(
        self, sandbox: Sandbox
    ) -> None:
        program = (
            "import os, stat\n"
            "with open('locked.txt', 'w') as f:\n"
            "    f.write('x')\n"
            "os.chmod('locked.txt', stat.S_IREAD)\n"
            "return os.getcwd()\n"
        )
        result = await sandbox.run(program, limits=_FAST_LIMITS)
        assert result.ok, result.failure
        assert not Path(result.value).exists()


class TestJobLimits:
    async def test_the_cpu_limit_is_the_jobs_own_accounting(self, sandbox: Sandbox) -> None:
        limits = _FAST_LIMITS.model_copy(update={"cpu_seconds": 1.0})
        result = await sandbox.run("while True:\n    pass\n", limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.CPU_SECONDS
        assert result.failure is not None
        assert result.failure.kind == "resource_limit"

    async def test_a_detached_grandchild_is_in_the_job_too(self, sandbox: Sandbox) -> None:
        """``CREATE_NEW_PROCESS_GROUP`` and ``DETACHED_PROCESS`` do not leave the
        job: a program cannot detach its way out."""
        program = (
            "import subprocess, sys\n"
            "flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "creationflags=flags)\n"
            "return p.pid\n"
        )
        result = await sandbox.run(program, limits=_FAST_LIMITS)
        assert result.ok, result.failure
        pid = int(result.value)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            assert code.value != 259, "the detached grandchild outlived the job"

    async def test_a_process_tree_bomb_is_bounded_by_the_active_process_limit(
        self, sandbox: Sandbox
    ) -> None:
        limits = _FAST_LIMITS.model_copy(update={"process_count": 6, "wall_seconds": 12.0})
        program = (
            "import subprocess, sys\n"
            "spawned = 0\n"
            "try:\n"
            "    while True:\n"
            "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "        spawned += 1\n"
            "except OSError:\n"
            "    pass\n"
            "return spawned\n"
        )
        result = await sandbox.run(program, limits=limits)
        assert result.duration_seconds < 12.0
        if result.ok:
            assert int(result.value) < 6


class TestSetup:
    def test_a_missing_interpreter_is_a_setup_error(self) -> None:
        from psych_runtime.sandbox.port import SandboxSetupError
        from psych_runtime.sandbox.windows import WindowsJobSandbox

        with pytest.raises(SandboxSetupError):
            WindowsJobSandbox(python_bin=r"C:\no\such\python.exe")

    def test_the_posix_backend_refuses_to_construct_here(self) -> None:
        from psych_runtime.sandbox.port import SandboxSetupError
        from psych_runtime.sandbox.subprocess import SubprocessSandbox

        with pytest.raises(SandboxSetupError, match="POSIX"):
            SubprocessSandbox(python_bin=subprocess.list2cmdline([sys.executable]))
