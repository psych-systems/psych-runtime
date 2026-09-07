"""``ContainerSandbox`` against a real container runtime, when one exists.

The bar for this backend: it passes the same
``SandboxContractSuite`` the subprocess backend does. There is no
``docker``/``podman`` daemon in the environment this was written in, so
every test below is written to run for real and then marked to skip, at
collection time, with an explicit reason, rather than faked to report a
pass it did not earn. In any environment with a reachable runtime
(including CI, if one is configured there), these run unmodified.

``detect_container_runtime`` checks for both a runtime binary on ``PATH``
*and* a reachable daemon: a binary with nothing listening
behind it is exactly as unusable here as no binary at all, and skipping only
on the weaker check would misreport "skip: no runtime" as "skip: daemon
happens to be down right now" or vice versa.

A reachable runtime is not sufficient on its own. ``ContainerSandbox`` never
pulls an image implicitly, by design, so a host with a daemon but without
the image cannot run these either -- and it fails in the least useful way
available: every case starts a container that exits immediately, waits the
adapter's full connect-back timeout, and reports "the container never
connected back", which reads as an adapter bug rather than a missing image.
That is what a CI runner with docker but no pre-pulled image did, thirteen
times, for most of the job's wall clock. So the image is a checked
precondition here too, and the pull belongs to whoever provisions the host
(the gate workflow does it) rather than to a test.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

import pytest

from psych_runtime.sandbox.container import (
    ContainerSandbox,
    _close_listener,
    _listen_for_one_connection,
    _reap,
    detect_container_runtime,
)
from psych_runtime.sandbox.contract import SandboxContractSuite
from psych_runtime.sandbox.port import Sandbox, SandboxLimits

pytestmark = pytest.mark.functional

_FAST_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=128 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=32,
    wall_seconds=45.0,
)
"""Wall clock is generous relative to the subprocess adapter's own tests: a
container's cold start (and, the first time an image is used, a pull) adds
real latency that a local fork/exec never has."""

_IMAGE: Final = "python:3.12-slim"
"""Named here rather than inherited from the adapter's default so that the
presence check below and the container the tests actually run are the same
image by construction."""


def _runtime_or_skip() -> str:
    runtime = asyncio.run(detect_container_runtime())
    if runtime is None:
        pytest.skip(
            "no container runtime is available (checked for `docker` and `podman`, "
            "each with a reachable daemon). ContainerSandbox is fully implemented "
            "and covered by this file; it runs for real wherever a runtime is "
            "present, including CI, if one is configured there."
        )
    return runtime


def _image_or_skip(runtime: str) -> None:
    """Skip unless the image is already local.

    ``docker image inspect`` rather than a pull: provisioning the host is not
    a test's job, and a test that fetched 28MB over the network to satisfy
    itself would be the suite's only outbound call.
    """
    probe = subprocess.run(
        [runtime, "image", "inspect", _IMAGE],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"{runtime} is reachable but the image {_IMAGE!r} is not present locally. "
            f"ContainerSandbox never pulls implicitly, so these would each time out "
            f"waiting for a container that cannot start. Run `{runtime} pull {_IMAGE}` "
            f"to run them for real."
        )


@pytest.fixture(scope="module")
def runtime() -> str:
    found = _runtime_or_skip()
    _image_or_skip(found)
    return found


@pytest.fixture
def sandbox(runtime: str) -> Sandbox:
    return ContainerSandbox(runtime=runtime, image=_IMAGE, default_limits=_FAST_LIMITS)


class TestContainerSandbox(SandboxContractSuite):
    @pytest.fixture
    def sandbox(self, runtime: str) -> Sandbox:
        return ContainerSandbox(runtime=runtime, image=_IMAGE, default_limits=_FAST_LIMITS)


class TestContainerTeardown:
    async def test_the_container_is_removed_after_a_successful_execution(
        self, sandbox: Sandbox, runtime: str
    ) -> None:
        result = await sandbox.run("return 1", limits=_FAST_LIMITS)
        assert result.ok

        listing = subprocess.run(
            [runtime, "ps", "-a", "--filter", "name=psych-sandbox-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert listing.stdout.strip() == ""

    async def test_the_container_is_removed_after_a_failing_execution(
        self, sandbox: Sandbox, runtime: str
    ) -> None:
        result = await sandbox.run("raise ValueError('boom')", limits=_FAST_LIMITS)
        assert not result.ok

        listing = subprocess.run(
            [runtime, "ps", "-a", "--filter", "name=psych-sandbox-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert listing.stdout.strip() == ""

    async def test_the_container_is_removed_after_a_timeout(
        self, sandbox: Sandbox, runtime: str
    ) -> None:
        import subprocess

        limits = _FAST_LIMITS.model_copy(update={"wall_seconds": 2.0, "cpu_seconds": 30.0})
        result = await sandbox.run("import time\ntime.sleep(60)\n", limits=limits)
        assert not result.ok

        listing = subprocess.run(
            [runtime, "ps", "-a", "--filter", "name=psych-sandbox-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert listing.stdout.strip() == ""


class TestRuntimeDetectionLeavesNothingRunning:
    """A probe that times out must be killed, not abandoned.

    ``detect_container_runtime`` used to let its timeout fall through with the
    child still running. ``podman info`` on a host where podman is installed
    but unconfigured never returns, so every probe leaked a process and an
    unclosed transport. On CI that showed up as orphaned podman processes
    outliving the session, a job that spent twenty-six minutes to report it had
    skipped everything, and -- since this project turns warnings into errors --
    tests failing on the ResourceWarning nobody closed.
    """

    async def test_a_probe_that_times_out_is_killed_and_reaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeper = shutil.which("sleep")
        if sleeper is None:
            pytest.skip("no `sleep` on PATH to stand in for a probe that hangs")
        monkeypatch.setattr(shutil, "which", lambda _name: sleeper)

        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def _record(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
            # "30" replaces "info": a probe that will not answer in time.
            proc = await real_exec(str(args[0]), "30", **kwargs)  # type: ignore[arg-type]
            spawned.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _record)

        assert await detect_container_runtime(timeout=0.2) is None
        assert spawned, "the probe never ran, so this proves nothing"
        for proc in spawned:
            assert proc.returncode is not None, (
                "a timed-out probe was left running rather than killed and reaped"
            )


class TestTeardownCannotHangForever:
    """`docker rm -f` runs after every execution and nothing bounded it.

    The connect timeout and the wall clock both belong to the program being
    run, not to cleaning up after it, so a runtime CLI that blocks on removal
    blocks the execution past every guard in the adapter.

    This covers a bound the adapter was missing. It is not what hung the
    container suite in CI, though it was written believing it was; that was
    the listener's cleanup, covered below.
    """

    async def test_a_teardown_helper_that_will_not_finish_is_killed(self) -> None:
        sleeper = shutil.which("sleep")
        if sleeper is None:
            pytest.skip("no `sleep` on PATH to stand in for a runtime that blocks")
        proc = await asyncio.create_subprocess_exec(
            sleeper,
            "30",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _reap(proc, timeout=0.2)
        assert proc.returncode is not None, "a blocked teardown helper was left running"


class TestCleanupCannotOutliveTheExecution:
    """Closing the listener must not wait on the connection it accepted.

    From 3.12 on, ``Server.wait_closed()`` waits for the connections a server
    accepted and not just for its listening socket, so closing the listener
    while still holding the container's connection open never returns. That
    runs after the execution already has its result, so no guard in the
    adapter is left to cut it short: the container exits, the container is
    removed, the daemon stays healthy, and the call still hangs.

    This needs neither a runtime nor an image, which is the point. The
    thirteen tests above skip on any host without both, so the adapter's
    cleanup went unexercised everywhere except CI, which is where the hang
    was found. Under 3.11 this passes either way.
    """

    async def test_a_live_connection_does_not_hold_the_listener_open(self) -> None:
        # mkdtemp rather than tmp_path: a Unix socket path is capped near 108
        # bytes and pytest's per-test directory names spend most of that.
        ipc_dir = Path(tempfile.mkdtemp(prefix="psych-sandbox-ipc-"))
        try:
            socket_path = ipc_dir / "ipc.sock"
            server, connected, accepted = await _listen_for_one_connection(socket_path)
            _, client_writer = await asyncio.open_unix_connection(str(socket_path))
            try:
                await asyncio.wait_for(connected, timeout=5.0)

                await asyncio.wait_for(_close_listener(server, connected, accepted), timeout=10.0)

                assert accepted, "nothing was recorded as accepted, so this proves nothing"
                assert accepted[0].is_closing(), (
                    "the accepted connection was left open for the listener to wait on"
                )
            finally:
                client_writer.close()
                with contextlib.suppress(OSError):
                    await client_writer.wait_closed()
        finally:
            shutil.rmtree(ipc_dir, ignore_errors=True)
