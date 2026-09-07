"""``SubprocessSandbox`` against a real child process.

DESIGN.md §22 names this explicitly: the code-execution sandbox is tested
against a real subprocess, not a mock of one. Everything here spawns an
actual CPython child and inspects what really happened to it.

This module doubles as the home of the shared ``SandboxContractSuite``
(``psych_runtime.sandbox.contract``) for this backend, plus everything specific to
process isolation that a container backend has no equivalent of: environment
scrubbing, rlimit enforcement, the wall-clock kill, and no state carrying
between two separate executions.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from psych_runtime.sandbox.contract import SandboxContractSuite
from psych_runtime.sandbox.port import Sandbox, SandboxLimit, SandboxLimits, SandboxSetupError
from psych_runtime.sandbox.subprocess import SubprocessSandbox

pytestmark = pytest.mark.functional

_FAST_LIMITS = SandboxLimits(
    cpu_seconds=3.0,
    address_space_bytes=128 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _candidate_interpreters() -> list[str]:
    """Interpreters worth probing, this process's own first.

    ``sys.executable`` usually works and is tried first; the rest are common
    system locations, since a system interpreter (unlike a user-owned
    virtualenv) is the one most likely to still be reachable after dropping
    to an unprivileged account. Order is a preference, not a guarantee: every
    candidate is actually probed below rather than trusted on sight.
    """
    names = (f"python{sys.version_info.major}.{sys.version_info.minor}", "python3")
    found = [sys.executable] if sys.executable else []
    for directory in ("/usr/bin", "/usr/local/bin", "/bin"):
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                found.append(str(candidate))
    for name in names:
        which = shutil.which(name)
        if which:
            found.append(which)
    # A distinct name from the Path loop above: reusing `candidate` for a str
    # here made mypy see one variable with two types, which it is right about.
    seen: set[str] = set()
    ordered: list[str] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


@functools.lru_cache(maxsize=1)
def _test_python_bin() -> str:
    """A Python interpreter this process, and an unprivileged account this
    process might drop children to, can both actually execute.

    Not a path heuristic on its own: each candidate is *probed* by actually
    spawning it as the account ``SubprocessSandbox`` would use, because this
    suite may itself run as root (a development container commonly does),
    and a root-owned virtualenv interpreter is frequently unreachable once a
    child drops to an unprivileged uid. See ``SubprocessSandbox``'s own
    docstring, "Dropping privileges", for why that is a real deployment
    concern and not just a quirk of this one environment.
    """
    probe_uid = 65534 if os.geteuid() == 0 else None
    for candidate in _candidate_interpreters():
        if probe_uid is None:
            return candidate
        try:
            subprocess.run(
                [candidate, "-c", "pass"],
                preexec_fn=lambda: os.setuid(probe_uid),
                check=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            continue
        return candidate
    pytest.skip("no Python interpreter reachable by an unprivileged account was found")


@pytest.fixture
def python_bin() -> str:
    return _test_python_bin()


# Both fixtures below opt in to a same-uid child. The adapter refuses one by
# default because a same-uid child can read the worker's environment through
# /proc/<ppid>/environ, and that refusal is itself covered by
# TestSameUidIsRefused. It is passed unconditionally rather than behind an
# os.geteuid() check because it only ever takes effect when there is no
# privilege drop to make: run as root, run_as defaults to "nobody", the child
# is a different uid, and this flag is never consulted. It matters on an
# unprivileged CI runner, where there is no account to drop to and the suite
# would otherwise fail at construction rather than test the adapter. This host
# is the case the flag documents: a worker holding nothing worth reading.


@pytest.fixture
def sandbox(python_bin: str) -> Sandbox:
    return SubprocessSandbox(
        python_bin=python_bin, default_limits=_FAST_LIMITS, allow_same_uid=True
    )


class TestSubprocessSandbox(SandboxContractSuite):
    @pytest.fixture
    def sandbox(self, python_bin: str) -> Sandbox:
        return SubprocessSandbox(
            python_bin=python_bin, default_limits=_FAST_LIMITS, allow_same_uid=True
        )


class TestNoStateBetweenExecutions:
    async def test_a_variable_set_in_one_execution_is_gone_in_the_next(
        self, sandbox: Sandbox
    ) -> None:
        first = await sandbox.run("counter = 1\nreturn counter", limits=_FAST_LIMITS)
        assert first.value == 1

        second = await sandbox.run("return counter", limits=_FAST_LIMITS)
        assert not second.ok
        assert second.failure is not None
        assert "counter" in second.failure.message

    async def test_two_executions_are_two_different_working_directories(
        self, sandbox: Sandbox
    ) -> None:
        first = await sandbox.run(
            "with open('marker.txt', 'w') as f:\n    f.write('x')\nimport os\nreturn os.getcwd()",
            limits=_FAST_LIMITS,
        )
        second = await sandbox.run(
            "import os\nreturn os.path.exists('marker.txt')", limits=_FAST_LIMITS
        )
        assert first.ok
        assert second.ok
        assert second.value is False


class TestEnvironmentScrubbing:
    async def test_a_secret_in_the_parents_environment_is_not_visible_to_the_child(
        self, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PSYCH_TEST_SECRET", "do-not-leak-me")
        result = await sandbox.run(
            "import os\nreturn os.environ.get('PSYCH_TEST_SECRET')", limits=_FAST_LIMITS
        )
        assert result.ok
        assert result.value is None

    async def test_only_the_explicit_allowlist_reaches_the_child(self, python_bin: str) -> None:
        sandbox_with_extra = SubprocessSandbox(
            python_bin=python_bin,
            default_limits=_FAST_LIMITS,
            env_allowlist={"PSYCH_TEST_ALLOWED": "visible"},
            allow_same_uid=True,
        )
        result = await sandbox_with_extra.run(
            "import os\nreturn os.environ.get('PSYCH_TEST_ALLOWED')", limits=_FAST_LIMITS
        )
        assert result.ok
        assert result.value == "visible"


class TestCwdIsolation:
    async def test_the_execution_cwd_is_not_the_repository_working_directory(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("import os\nreturn os.getcwd()", limits=_FAST_LIMITS)
        assert result.ok
        assert result.value != str(Path.cwd())
        assert not str(result.value).startswith(str(Path.cwd()))

    async def test_the_working_directory_does_not_survive_the_execution(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("import os\nreturn os.getcwd()", limits=_FAST_LIMITS)
        assert result.ok
        assert not Path(str(result.value)).exists()


class TestFileSizeLimit:
    async def test_a_program_that_writes_past_the_file_size_limit_is_stopped(
        self, sandbox: Sandbox
    ) -> None:
        limits = _FAST_LIMITS.model_copy(update={"file_size_bytes": 4096})
        program = (
            "with open('big.bin', 'wb') as f:\n"
            "    f.write(b'x' * (1024 * 1024))\n"
            "return 'never gets here'\n"
        )
        result = await sandbox.run(program, limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.FILE_SIZE_BYTES


class TestProcessCountLimit:
    async def test_a_fork_bomb_is_contained_quickly_and_does_not_hang(
        self, sandbox: Sandbox
    ) -> None:
        limits = _FAST_LIMITS.model_copy(update={"process_count": 12, "wall_seconds": 10.0})
        program = (
            "import os\n"
            "spawned = 0\n"
            "try:\n"
            "    while True:\n"
            "        os.fork()\n"
            "        spawned += 1\n"
            "except OSError:\n"
            "    pass\n"
            "return spawned\n"
        )
        result = await sandbox.run(program, limits=limits)
        # The bomb is contained (this must finish well inside the wall clock,
        # not by exhausting it) and does not crash this test process either.
        assert result.duration_seconds < 5.0
        if result.ok:
            assert isinstance(result.value, int)
        else:
            assert result.limit_hit is not None or result.failure is not None


class TestWallClockTimeout:
    async def test_a_hung_program_is_actually_killed_not_just_stopped_reading(
        self, sandbox: Sandbox
    ) -> None:
        limits = _FAST_LIMITS.model_copy(update={"cpu_seconds": 30.0, "wall_seconds": 1.0})
        # Sleeping burns no CPU time, so only the wall clock can end this.
        result = await sandbox.run("import time\ntime.sleep(30)\n", limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.WALL_SECONDS
        assert result.duration_seconds < 5.0


class TestSetupFailures:
    async def test_an_unreachable_interpreter_raises_setup_error_not_a_program_failure(
        self,
    ) -> None:
        with pytest.raises(SandboxSetupError):
            SubprocessSandbox(python_bin="/no/such/interpreter/exists")


class TestSameUidIsRefused:
    """The scrubbed environment is not a boundary when the child runs as the
    worker's own uid: ``/proc/<ppid>/environ`` is readable by a same-uid
    process, so every credential the worker holds is one ``open()`` away."""

    def test_running_as_the_workers_own_uid_is_refused_by_default(self, python_bin: str) -> None:
        with pytest.raises(SandboxSetupError, match="same uid"):
            SubprocessSandbox(python_bin=python_bin, run_as=(os.geteuid(), os.getegid()))

    def test_it_can_be_allowed_explicitly(self, python_bin: str) -> None:
        """A host where the worker holds nothing worth reading -- this test
        suite, for one -- can opt in, and has to say so."""
        sandbox = SubprocessSandbox(
            python_bin=python_bin,
            run_as=(os.geteuid(), os.getegid()),
            allow_same_uid=True,
        )
        assert sandbox is not None
