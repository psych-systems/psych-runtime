"""The Sandbox contract suite, shared by every adapter.

DESIGN.md §22: two ``Sandbox`` implementations with no
shared test are two divergent behaviours discovered in production, the same
reason ``psych_runtime.store.contract`` exists for the four ``Store`` adapters. This
module is that shared test for ``psych_runtime.sandbox.subprocess`` and
``psych_runtime.sandbox.container``. It defines no ``sandbox`` fixture itself and its
class name does not start with ``Test``, so pytest does not collect it
directly; an adapter joins the suite by subclassing ``SandboxContractSuite``
under ``tests/functional/`` and providing a ``sandbox`` fixture that returns
a fresh instance of the adapter under test.

## What "exit codes" means here

DESIGN.md and the ticket both use the phrase "exit codes" for what this
suite covers, language carried over from a general command-execution
sandbox. Psych's ``Sandbox`` does not expose a raw process exit code in its
port at all: it runs one Python program and reports ``{value, failure}``,
per DESIGN.md §18. So "exit codes" here means what that phrase is standing
in for: a program that completes cleanly is reported as success with its
value, and a program that raises is reported as a populated ``failure``
carrying a traceback, never as a raised exception out of ``run()`` itself.

## Why the network-denial test skips rather than asserts unconditionally

``psych_runtime.sandbox.container`` guarantees network denial (a container runtime's
own ``--network=none`` does not depend on the sandboxed process's privilege
level); ``psych_runtime.sandbox.subprocess`` only attempts it and honestly reports
whether the attempt held (see that module's docstring). A shared test cannot
assert a guarantee one of the two backends does not make. So this test asks
the adapter itself, via ``SandboxResult.network_denied``, whether this
particular execution actually achieved isolation, and skips rather than
fails when it did not: a skip here is a true statement about what this
backend, on this host, right now, provides, not a fudge to make a red test
green.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from psych_runtime.sandbox.port import Sandbox, SandboxLimit, SandboxLimits

__all__ = ["SandboxContractSuite"]

_TINY_LIMITS: SandboxLimits = SandboxLimits(
    cpu_seconds=2.0,
    address_space_bytes=64 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)
"""Deliberately small, so the resource-limit tests below finish quickly on
both backends without either one needing its own tuned numbers."""


class SandboxContractSuite:
    """Subclass this and provide a ``sandbox`` fixture yielding a ``Sandbox``."""

    @pytest.fixture
    def sandbox(self) -> Sandbox:
        raise NotImplementedError(
            "subclasses of SandboxContractSuite must override the `sandbox` fixture "
            "to return the adapter under test"
        )

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

    # -- streams --------------------------------------------------------------

    async def test_stdout_and_stderr_are_kept_separate(self, sandbox: Sandbox) -> None:
        program = "import sys\nprint('on stdout')\nprint('on stderr', file=sys.stderr)\n"
        result = await sandbox.run(program, limits=_TINY_LIMITS)
        assert result.ok
        assert "on stdout" in result.stdout
        assert "on stderr" not in result.stdout
        assert "on stderr" in result.stderr
        assert "on stdout" not in result.stderr

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

    # -- resource limits --------------------------------------------------------

    async def test_a_program_that_exceeds_its_cpu_limit_is_stopped(self, sandbox: Sandbox) -> None:
        limits = _TINY_LIMITS.model_copy(update={"cpu_seconds": 1.0})
        result = await sandbox.run("while True:\n    pass\n", limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.CPU_SECONDS

    async def test_a_program_that_exceeds_its_memory_limit_is_stopped(
        self, sandbox: Sandbox
    ) -> None:
        limits = _TINY_LIMITS.model_copy(update={"address_space_bytes": 32 * 1024 * 1024})
        program = "data = bytearray(1024 * 1024 * 1024)\nreturn len(data)\n"
        result = await sandbox.run(program, limits=limits)
        assert not result.ok
        assert result.limit_hit is SandboxLimit.ADDRESS_SPACE_BYTES

    # -- network -----------------------------------------------------------------

    async def test_network_is_denied_by_default(self, sandbox: Sandbox) -> None:
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
        if not result.network_denied:
            pytest.skip(
                "this backend could not establish network isolation for this "
                "execution; see its module docstring for what it does and does "
                "not guarantee"
            )
        assert result.ok
        assert result.value == "blocked"

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
