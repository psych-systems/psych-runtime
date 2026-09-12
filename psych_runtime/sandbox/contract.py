"""The Sandbox contract suite, shared by every adapter.

DESIGN.md §22: two ``Sandbox`` implementations with no shared test are two
divergent behaviours discovered in production, the same reason
``psych_runtime.store.contract`` exists for the ``Store`` adapters. This module
is that shared test for every backend: the POSIX subprocess, the Windows job
object, the Linux namespace sandbox, the container, the remote adapter over
the reference service, and any consumer's own implementation. It defines no
``sandbox`` fixture itself and its class name does not start with ``Test``,
so pytest does not collect it directly; an adapter joins the suite by
subclassing ``SandboxContractSuite`` under ``tests/functional/`` and
providing a ``sandbox`` fixture that returns a fresh instance of the adapter
under test.

## What "exit codes" means here

DESIGN.md uses the phrase "exit codes" for what this suite covers, language
carried over from a general command-execution sandbox. Psych's ``Sandbox``
does not expose a raw process exit code in its port at all: it runs one
Python program and reports ``{value, failure}``, per DESIGN.md §18. So "exit
codes" here means what that phrase is standing in for: a program that
completes cleanly is reported as success with its value, and a program that
raises is reported as a populated ``failure`` carrying a traceback, never as
a raised exception out of ``run()`` itself.

## What every backend must agree on, and what it may honestly not provide

Every case below holds on every backend, with one family of exceptions: a
guarantee a backend reports it does not have. The suite reads the backend's
own ``describe()`` and each result's own ``guarantees`` and asks for
*consistency*: a backend that says ``network: enforced`` must actually have
no route, and one that says ``unavailable`` must not claim a denial. A skip
here is therefore never a fudge: it is the backend saying, in data the
runtime also reads, that this dimension is not one it provides on this host.
Whether that is acceptable is the deployment's decision through
``IsolationLevel``, not the suite's.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox.port import (
    OutputCapture,
    Sandbox,
    SandboxDescription,
    SandboxLimit,
    SandboxLimits,
    describe_sandbox,
)

__all__ = ["SandboxContractSuite"]

_TINY_LIMITS: SandboxLimits = SandboxLimits(
    cpu_seconds=2.0,
    address_space_bytes=64 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)
"""Deliberately small, so the resource-limit tests below finish quickly on
every backend without any one needing its own tuned numbers."""


class SandboxContractSuite:
    """Subclass this and provide a ``sandbox`` fixture yielding a ``Sandbox``."""

    @pytest.fixture
    def sandbox(self) -> Sandbox:
        raise NotImplementedError(
            "subclasses of SandboxContractSuite must override the `sandbox` fixture "
            "to return the adapter under test"
        )

    @pytest.fixture
    async def description(self, sandbox: Sandbox) -> SandboxDescription:
        return await describe_sandbox(sandbox)

    # -- success and failure signalling ("exit codes") -----------------------

    async def test_a_program_that_returns_a_value_reports_it_as_success(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("return 6 * 7", limits=_TINY_LIMITS)
        assert result.ok
        assert result.failure is None
        assert result.value == 42

    async def test_a_raising_program_reports_failure_as_data_not_an_exception(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("raise ValueError('bad input')", limits=_TINY_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert "bad input" in result.failure.message
        assert result.failure.traceback is not None
        assert "ValueError" in result.failure.traceback

    async def test_a_syntax_error_is_a_failure_with_a_traceback(self, sandbox: Sandbox) -> None:
        result = await sandbox.run("def broken(:\n    pass\n", limits=_TINY_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "exception"
        assert "SyntaxError" in (result.failure.traceback or result.failure.message)

    async def test_a_value_that_is_not_json_is_a_failure_not_a_crash(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("return {1, 2, 3}", limits=_TINY_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "value_not_serialisable"
        assert result.value is None

    async def test_a_program_that_returns_nothing_has_a_none_value(self, sandbox: Sandbox) -> None:
        result = await sandbox.run("x = 1", limits=_TINY_LIMITS)
        assert result.ok
        assert result.value is None
        assert result.stdout == ""
        assert result.stderr == ""

    # -- streams --------------------------------------------------------------

    async def test_stdout_and_stderr_are_kept_separate(self, sandbox: Sandbox) -> None:
        program = "import sys\nprint('on stdout')\nprint('on stderr', file=sys.stderr)\n"
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok
        assert "on stdout" in result.stdout
        assert "on stderr" not in result.stdout
        assert "on stderr" in result.stderr
        assert "on stdout" not in result.stderr

    async def test_stdout_keeps_its_order(self, sandbox: Sandbox) -> None:
        program = "for i in range(50):\n    print(i)\n"
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok
        assert [int(line) for line in result.stdout.split()] == list(range(50))

    async def test_binary_and_invalid_unicode_output_is_captured_whole(
        self, sandbox: Sandbox
    ) -> None:
        program = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe\\x00ok')\nsys.stdout.flush()\n"
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok
        assert result.stdout_data == b"\xff\xfe\x00ok"
        assert result.stdout_size == 5
        assert "ok" in result.stdout  # decoded with replacement, never raised

    async def test_output_past_the_capture_cap_is_truncated_and_counted(
        self, sandbox: Sandbox
    ) -> None:
        program = "import sys\nsys.stdout.write('x' * 200_000)\nsys.stdout.flush()\nreturn 1\n"
        result = await sandbox.run(
            program, limits=_TINY_LIMITS, capture=OutputCapture(stream_bytes=8_192)
        )
        assert result.ok, result.failure
        assert result.value == 1
        assert result.stdout_truncated
        assert len(result.stdout_data) == 8_192
        assert result.stdout_size == 200_000

    async def test_a_flood_on_both_streams_does_not_deadlock(self, sandbox: Sandbox) -> None:
        """A program writing far more than any pipe buffers, on both streams
        at once, must still finish: the host drains both concurrently."""
        program = (
            "import sys\n"
            "for _ in range(400):\n"
            "    sys.stdout.write('o' * 1024)\n"
            "    sys.stderr.write('e' * 1024)\n"
            "sys.stdout.flush(); sys.stderr.flush()\n"
            "return 'done'\n"
        )
        result = await sandbox.run(
            program, limits=_TINY_LIMITS, capture=OutputCapture(stream_bytes=16_384)
        )
        assert result.ok, result.failure
        assert result.value == "done"
        assert result.stdout_size >= 400 * 1024
        assert result.stderr_size >= 400 * 1024

    # -- working directory and paths ------------------------------------------

    async def test_the_working_directory_is_not_the_host_process_cwd(
        self, sandbox: Sandbox
    ) -> None:
        result = await sandbox.run("import os\nreturn os.getcwd()", limits=_TINY_LIMITS)
        assert result.ok
        assert result.value != str(Path.cwd())

    async def test_a_relative_path_resolves_inside_the_execution_working_directory(
        self, sandbox: Sandbox
    ) -> None:
        program = (
            "with open('scratch.txt', 'w') as f:\n"
            "    f.write('hello')\n"
            "import os\n"
            "return os.path.exists('scratch.txt') and os.path.getsize('scratch.txt')\n"
        )
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok
        assert result.value == 5

    async def test_no_state_carries_between_two_executions(self, sandbox: Sandbox) -> None:
        first = await sandbox.run(
            "with open('marker', 'w') as f:\n    f.write('x')\ncounter = 1\nreturn counter",
            limits=_TINY_LIMITS,
        )
        assert first.value == 1
        second = await sandbox.run(
            "import os\nreturn [os.path.exists('marker'), 'counter' in dir()]",
            limits=_TINY_LIMITS,
        )
        assert second.ok
        assert second.value == [False, False]

    # -- artifacts ---------------------------------------------------------------

    async def test_workspace_files_come_back_as_artifacts_by_relative_path(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if not description.artifacts_supported:
            pytest.skip("this backend reports that it does not collect artifacts")
        program = (
            "import os\n"
            "os.makedirs('out')\n"
            "with open('out/report.csv', 'w') as f:\n"
            "    f.write('a,b\\n1,2\\n')\n"
            "with open('notes.txt', 'w') as f:\n"
            "    f.write('hello')\n"
            "return 'ok'\n"
        )
        result = await sandbox.run(
            program, limits=_TINY_LIMITS, capture=OutputCapture(collect_artifacts=True)
        )
        assert result.ok, result.failure
        by_path = {artifact.path: artifact for artifact in result.artifacts}
        assert set(by_path) == {"out/report.csv", "notes.txt"}
        assert by_path["notes.txt"].data == b"hello"
        assert by_path["out/report.csv"].content_type.startswith("text/csv")
        for artifact in result.artifacts:
            assert not Path(artifact.path).is_absolute()
            assert ".." not in artifact.path.split("/")

    async def test_artifact_caps_are_honoured_and_counted(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if not description.artifacts_supported:
            pytest.skip("this backend reports that it does not collect artifacts")
        program = (
            "for i in range(5):\n"
            "    with open(f'f{i}.txt', 'w') as f:\n"
            "        f.write('x' * 100)\n"
            "return 'ok'\n"
        )
        result = await sandbox.run(
            program,
            limits=_TINY_LIMITS,
            capture=OutputCapture(collect_artifacts=True, artifact_count=2, artifact_bytes=150),
        )
        assert result.ok, result.failure
        assert len(result.artifacts) == 2
        assert result.artifacts_omitted == 3
        assert result.artifacts[0].data == b"x" * 100
        assert result.artifacts[1].truncated
        assert len(result.artifacts[1].data) == 50

    async def test_a_symbolic_link_in_the_workspace_is_never_collected(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if not description.artifacts_supported:
            pytest.skip("this backend reports that it does not collect artifacts")
        program = (
            "import os\n"
            "with open('real.txt', 'w') as f:\n"
            "    f.write('real')\n"
            "try:\n"
            "    os.symlink(os.path.abspath(os.sep), 'escape')\n"
            "    os.symlink('real.txt', 'alias.txt')\n"
            "except OSError:\n"
            "    return 'no-symlinks'\n"
            "return 'linked'\n"
        )
        result = await sandbox.run(
            program, limits=_TINY_LIMITS, capture=OutputCapture(collect_artifacts=True)
        )
        assert result.ok, result.failure
        paths = {artifact.path for artifact in result.artifacts}
        assert "real.txt" in paths
        assert "alias.txt" not in paths
        assert not any(path.startswith("escape") for path in paths)

    # -- resource limits --------------------------------------------------------

    async def test_a_program_that_exceeds_its_cpu_limit_is_stopped(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if description.guarantees.cpu is Enforcement.UNAVAILABLE:
            pytest.skip("this backend reports no CPU time cap")
        limits = _TINY_LIMITS.model_copy(update={"cpu_seconds": 1.0})
        result = await sandbox.run("while True:\n    pass\n", limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.CPU_SECONDS

    async def test_a_program_that_exceeds_its_memory_limit_is_stopped(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if description.guarantees.memory is Enforcement.UNAVAILABLE:
            pytest.skip("this backend reports no memory cap")
        limits = _TINY_LIMITS.model_copy(update={"address_space_bytes": 32 * 1024 * 1024})
        program = "data = bytearray(1024 * 1024 * 1024)\nreturn len(data)\n"
        result = await sandbox.run(program, limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.ADDRESS_SPACE_BYTES

    async def test_a_hung_program_is_killed_at_the_wall_clock(self, sandbox: Sandbox) -> None:
        limits = _TINY_LIMITS.model_copy(update={"cpu_seconds": 30.0, "wall_seconds": 1.0})
        result = await sandbox.run("import time\ntime.sleep(30)\n", limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.WALL_SECONDS
        assert result.duration_seconds < 10.0

    async def test_a_program_that_spawns_too_many_processes_is_stopped(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if description.guarantees.process_count is Enforcement.UNAVAILABLE:
            pytest.skip("this backend reports no process count cap")
        limits = _TINY_LIMITS.model_copy(update={"process_count": 4, "wall_seconds": 12.0})
        program = (
            "import subprocess, sys\n"
            "held = []\n"
            "try:\n"
            "    for _ in range(12):\n"
            "        held.append(subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(20)']))\n"
            "except OSError:\n"
            "    return 'capped'\n"
            "return 'unbounded'\n"
        )
        result = await sandbox.run(program, limits=limits)
        assert result.duration_seconds < 12.0
        if result.ok:
            assert result.value == "capped"
        else:
            assert result.limit_hit is not None or result.failure is not None

    async def test_child_processes_do_not_survive_the_execution(self, sandbox: Sandbox) -> None:
        """A grandchild left sleeping must be gone once run() returns."""
        program = (
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "return p.pid\n"
        )
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok, result.failure
        pid = int(result.value)
        await asyncio.sleep(0.5)
        assert not _pid_alive(pid), f"child {pid} outlived the execution"

    # -- cancellation --------------------------------------------------------------

    async def test_cancellation_before_start_runs_nothing(self, sandbox: Sandbox) -> None:
        cancel = asyncio.Event()
        cancel.set()
        result = await sandbox.run("return 1", limits=_TINY_LIMITS, cancel=cancel)
        assert not result.ok
        assert result.cancelled
        assert result.failure is not None
        assert result.failure.kind == "cancelled"
        assert result.value is None

    async def test_cancellation_during_execution_kills_the_tree(self, sandbox: Sandbox) -> None:
        cancel = asyncio.Event()
        program = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n"
        )

        async def fire() -> None:
            await asyncio.sleep(1.5)
            cancel.set()

        asyncio.get_running_loop().create_task(fire())
        limits = _TINY_LIMITS.model_copy(update={"wall_seconds": 30.0, "cpu_seconds": 30.0})
        result = await sandbox.run(program, limits=limits, cancel=cancel)
        assert result.cancelled
        assert result.failure is not None
        assert result.failure.kind == "cancelled"
        assert result.duration_seconds < 15.0
        printed = result.stdout.strip()
        if printed.isdigit():
            await asyncio.sleep(0.5)
            assert not _pid_alive(int(printed)), "the grandchild outlived the cancellation"

    async def test_cancellation_during_a_binding_ends_the_execution(self, sandbox: Sandbox) -> None:
        cancel = asyncio.Event()
        released = asyncio.Event()

        async def slow(_arguments: Mapping[str, Any]) -> Any:
            cancel.set()
            await released.wait()
            return "late"

        limits = _TINY_LIMITS.model_copy(update={"wall_seconds": 30.0, "cpu_seconds": 30.0})
        try:
            result = await sandbox.run(
                "return await slow()", bindings={"slow": slow}, limits=limits, cancel=cancel
            )
        finally:
            released.set()
        assert result.cancelled
        assert result.failure is not None
        assert result.failure.kind == "cancelled"

    # -- environment -----------------------------------------------------------------

    async def test_the_host_environment_is_not_visible(
        self, sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PSYCH_CONTRACT_SECRET", "do-not-leak-me")
        result = await sandbox.run(
            "import os\nreturn os.environ.get('PSYCH_CONTRACT_SECRET')", limits=_TINY_LIMITS
        )
        assert result.ok, result.failure
        assert result.value is None

    # -- guarantees are consistent with what was observed ---------------------------

    async def test_network_reporting_is_honest(self, sandbox: Sandbox) -> None:
        program = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(2)\n"
            "try:\n"
            "    s.connect(('192.0.2.1', 80))\n"
            "except OSError:\n"
            "    return 'blocked'\n"
            "else:\n"
            "    return 'reached'\n"
        )
        result = await sandbox.run(program, limits=_TINY_LIMITS, network=False)
        assert result.ok, result.failure
        if result.network_denied:
            assert result.guarantees.network is Enforcement.ENFORCED
            assert result.value == "blocked"
        else:
            assert result.guarantees.network is not Enforcement.ENFORCED

    async def test_the_result_grades_itself_and_the_description_agrees(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        result = await sandbox.run("return 1", limits=_TINY_LIMITS)
        assert result.ok
        assert result.isolation is not None, "a backend must grade every execution"
        assert description.isolation is not None
        assert result.isolation.satisfies(IsolationLevel.PROCESS)
        # A backend never delivers more than it promised, and what it promised
        # is what the profile resolution trusts.
        assert description.isolation.satisfies(result.isolation)
        assert result.guarantees.process_tree is Enforcement.ENFORCED
        assert result.guarantees.wall_clock is Enforcement.ENFORCED
        if result.guarantees.filesystem is Enforcement.ENFORCED:
            assert result.isolation is IsolationLevel.ISOLATED or (
                result.guarantees.memory is not Enforcement.ENFORCED
                or result.guarantees.network is not Enforcement.ENFORCED
            )

    async def test_a_request_above_the_backends_level_is_refused_with_output_withheld(
        self, sandbox: Sandbox, description: SandboxDescription
    ) -> None:
        if description.isolation is IsolationLevel.ISOLATED:
            result = await sandbox.run(
                "print('secret')\nreturn 'ran'",
                limits=_TINY_LIMITS,
                isolation=IsolationLevel.ISOLATED,
            )
            assert result.ok, result.failure
            assert result.value == "ran"
            return
        result = await sandbox.run(
            "print('secret')\nreturn 'ran'", limits=_TINY_LIMITS, isolation=IsolationLevel.ISOLATED
        )
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "isolation_unavailable"
        assert result.value is None
        assert "secret" not in result.stdout

    async def test_describe_is_ready_and_names_its_mechanisms(
        self, description: SandboxDescription
    ) -> None:
        assert description.ready, description.problems
        assert description.backend
        assert description.platform
        assert description.mechanisms

    # -- host bindings -------------------------------------------------------

    async def test_a_host_binding_is_called_and_its_result_comes_back(
        self, sandbox: Sandbox
    ) -> None:
        async def lookup(arguments: Mapping[str, Any]) -> dict[str, Any]:
            return {"order_id": arguments["order_id"], "status": "shipped"}

        result = await sandbox.run(
            "x = await lookup(order_id='A1')\nreturn x",
            bindings={"lookup": lookup},
            limits=_TINY_LIMITS,
        )
        assert result.ok
        assert result.value == {"order_id": "A1", "status": "shipped"}

    async def test_a_host_binding_that_raises_surfaces_inside_the_program(
        self, sandbox: Sandbox
    ) -> None:
        async def explode(_arguments: Mapping[str, Any]) -> Any:
            raise RuntimeError("host tool is down")

        result = await sandbox.run(
            "return await explode()",
            bindings={"explode": explode},
            limits=_TINY_LIMITS,
        )
        assert not result.ok
        assert result.failure is not None
        assert "host tool is down" in result.failure.message

    async def test_a_large_binding_result_crosses_the_channel_whole(self, sandbox: Sandbox) -> None:
        async def big(_arguments: Mapping[str, Any]) -> Any:
            return {"rows": [{"i": i, "text": "x" * 100} for i in range(2000)]}

        result = await sandbox.run(
            "r = await big()\nreturn [len(r['rows']), r['rows'][-1]['i']]",
            bindings={"big": big},
            limits=_TINY_LIMITS,
        )
        assert result.ok, result.failure
        assert result.value == [2000, 1999]

    async def test_a_binding_that_was_not_offered_cannot_be_called(self, sandbox: Sandbox) -> None:
        async def lookup(_arguments: Mapping[str, Any]) -> Any:
            return "should not be reachable"

        result = await sandbox.run(
            "return await other()", bindings={"lookup": lookup}, limits=_TINY_LIMITS
        )
        assert not result.ok
        assert result.failure is not None
        assert "other" in result.failure.message

    async def test_concurrent_executions_do_not_share_state(self, sandbox: Sandbox) -> None:
        programs = [
            f"import os\nwith open('n', 'w') as f:\n    f.write('{i}')\n"
            f"import time\ntime.sleep(0.2)\nreturn [{i}, open('n').read(), os.getcwd()]"
            for i in range(4)
        ]
        results = await asyncio.gather(*(sandbox.run(p, limits=_TINY_LIMITS) for p in programs))
        cwds = set()
        for i, result in enumerate(results):
            assert result.ok, result.failure
            assert result.value[0] == i
            assert result.value[1] == str(i)
            cwds.add(result.value[2])
        assert len(cwds) == 4


def _pid_alive(pid: int) -> bool:
    """Whether a process id still names a live process, on any platform."""
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_windows(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return bool(code.value == 259)  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie is not alive for this purpose: it can do nothing further.
    with contextlib.suppress(OSError):
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        if "State:	Z" in status:
            return False
    return True
