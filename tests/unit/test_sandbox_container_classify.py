"""How the container adapter attributes a hard kill to a limit.

Pure logic over ``docker inspect``'s report, so it belongs here rather than
in the functional suite that needs a runtime and an image. It is here at all
because the container tests could not catch it: they skip on any host without
both, and when they finally did run in CI they passed on one attempt and
failed on the next from an identical tree.

The unreliable input is ``OOMKilled``. A cgroup-v2 kill is reported as false
often enough to matter, and everything downstream of that flag has to stay
correct when it lies.
"""

from __future__ import annotations

import pytest

from psych_runtime.sandbox.container import _classify, _ExitInfo
from psych_runtime.sandbox.port import SandboxFailure, SandboxLimit

pytestmark = pytest.mark.unit

_KILLED = 128 + 9


def _classified(
    *, oom_killed: bool, exit_code: int | None, elapsed: float, cpu_seconds: float = 2.0
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    return _classify(
        exit_info=_ExitInfo(exit_code=exit_code, oom_killed=oom_killed),
        done=None,
        protocol_error=None,
        timed_out=False,
        cancelled=False,
        wall_seconds=15.0,
        elapsed=elapsed,
        cpu_seconds=cpu_seconds,
    )


class TestAKillTooEarlyToBeTheCpuLimit:
    """RLIMIT_CPU measures CPU time, so wall clock bounds it from below."""

    def test_a_sigkill_before_the_cpu_budget_is_the_memory_limit(self) -> None:
        _, limit = _classified(oom_killed=False, exit_code=_KILLED, elapsed=0.4)
        assert limit is SandboxLimit.ADDRESS_SPACE_BYTES

    def test_it_does_not_depend_on_the_oom_flag_being_right(self) -> None:
        # The whole point: this is the exact report a cgroup-v2 kill produces
        # when Docker fails to set OOMKilled, and it used to come back as a
        # CPU limit the program never approached.
        failure, limit = _classified(oom_killed=False, exit_code=_KILLED, elapsed=0.4)
        assert limit is SandboxLimit.ADDRESS_SPACE_BYTES
        assert failure is not None
        assert "memory" in failure.message

    def test_a_sigkill_past_the_cpu_budget_is_still_the_cpu_limit(self) -> None:
        # Above the budget both are possible and CPU stays the answer, which
        # is what the hard limit one second behind the soft one produces.
        _, limit = _classified(oom_killed=False, exit_code=_KILLED, elapsed=3.1)
        assert limit is SandboxLimit.CPU_SECONDS

    def test_the_oom_flag_still_wins_when_it_is_set(self) -> None:
        _, limit = _classified(oom_killed=True, exit_code=_KILLED, elapsed=9.0)
        assert limit is SandboxLimit.ADDRESS_SPACE_BYTES


class TestTheOtherSignalsAreUnaffected:
    def test_sigxcpu_is_the_cpu_limit_however_early_it_arrives(self) -> None:
        _, limit = _classified(oom_killed=False, exit_code=128 + 24, elapsed=0.1)
        assert limit is SandboxLimit.CPU_SECONDS

    def test_sigxfsz_is_the_file_size_limit(self) -> None:
        _, limit = _classified(oom_killed=False, exit_code=128 + 25, elapsed=0.1)
        assert limit is SandboxLimit.FILE_SIZE_BYTES

    def test_an_ordinary_nonzero_exit_is_not_a_limit_at_all(self) -> None:
        _, limit = _classified(oom_killed=False, exit_code=1, elapsed=0.1)
        assert limit is None
