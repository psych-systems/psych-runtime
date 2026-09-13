"""The Linux namespace ``Sandbox`` adapter: a private root without a daemon.

DESIGN.md §18. For a Linux host with no container runtime that still needs
``IsolationLevel.ISOLATED``. It runs each program under bubblewrap
(``bwrap``), a small setuid-free helper that builds a private mount
namespace from a handful of read-only binds, so the program sees the
interpreter and the system libraries and nothing else: no home directories,
no worker files, no credentials on disk. bubblewrap is packaged by every
mainstream distribution (``bubblewrap`` on Debian, Ubuntu, Fedora, Alpine)
and this adapter never installs it; ``describe()`` says when it is missing
and what to install.

## What each layer of containment actually is, and who backs it

- **Filesystem.** ``--unshare-user`` plus a mount namespace built from
  ``--ro-bind`` of the system directories the interpreter needs, ``--proc``,
  ``--dev``, a ``--tmpfs`` at ``/tmp``, the workspace bound read-write at
  ``/workspace`` and the channel directory at ``/run/psych-sandbox``. Every
  path on the host outside those binds does not exist inside. Verified per
  execution by the canary: a host file outside the binds that the child
  cannot open.
- **Network.** ``--unshare-net``: a network namespace with loopback only.
  Verified by the child's own routing probe. Granting ``network=True``
  leaves the namespace shared instead.
- **Process tree.** ``--unshare-pid`` and ``--die-with-parent``: the program
  is PID 2 in its own namespace, nothing it starts can outlive bubblewrap,
  and bubblewrap dies with this process. The adapter still signals the
  process group at teardown, so a clean finish and a kill look the same.
- **CPU, memory, file size, process count.** The same rlimits the
  subprocess adapter applies, set in ``preexec_fn`` on bubblewrap itself and
  inherited across its exec: ``RLIMIT_CPU``, ``RLIMIT_AS``, ``RLIMIT_FSIZE``,
  ``RLIMIT_NPROC``. Kernel-enforced, unaffected by the namespaces.
- **Identity.** ``--uid 65534 --gid 65534`` inside the user namespace. The
  kernel still charges everything to the worker's real uid, so
  ``identity`` is reported ``unavailable`` rather than pretending the
  mapped number is a different account: the point of the user namespace
  here is the mount namespace it makes possible, not a privilege drop.
- **Capabilities.** None. A user namespace starts with a full set that maps
  to nothing outside it, and ``--cap-drop ALL`` removes even that.
- **Environment.** ``--clearenv`` and then only what this adapter sets.
- **Wall clock.** Enforced by this adapter killing the process group.

## What it needs from the host

Unprivileged user namespaces. Most distributions allow them; some restrict
them (a ``kernel.apparmor_restrict_unprivileged_userns`` policy, or
``kernel.unprivileged_userns_clone=0``) and ship an AppArmor profile for
``bwrap`` that re-allows exactly this use. ``describe()`` runs a probe under
the full argument set and reports what happened, so a host that cannot do
this is found at startup rather than on a customer's first program.

The interpreter must be visible inside the private root. ``/usr``, ``/lib``,
``/lib64``, ``/bin`` and ``/sbin`` are bound read-only; an interpreter
elsewhere (a virtualenv under a home directory, a ``uv`` managed Python) has
its ``sys.prefix`` and ``sys.base_prefix`` directories bound read-only as
well, resolved once from the interpreter itself at construction.

## Wire protocol

The same bind-mounted Unix socket ``psych_runtime.sandbox.container`` uses,
for the same reason: nothing here is forked from this process in a way that
carries a descriptor across the mount namespace cleanly, and a socket path
bound into the private root works with the network namespace unshared.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

if sys.platform != "win32":
    import resource

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox._bootstrap import BOOTSTRAP_SOURCE, CANARY_ENV
from psych_runtime.sandbox._local import (
    DEFAULT_CAPTURE,
    Canary,
    Conversation,
    admission,
    bounded,
    classify_done,
    collect_artifacts,
    converse,
    read_capped,
    remove_tree,
    withhold_if_weaker,
)
from psych_runtime.sandbox.port import (
    HostBinding,
    OutputCapture,
    SandboxDescription,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimit,
    SandboxLimits,
    SandboxResult,
    SandboxSetupError,
    achieved_level,
)
from psych_runtime.sandbox.protocol import DoneFrame, ReadyFrame, SandboxProtocolError

__all__ = ["NamespaceSandbox", "detect_bubblewrap"]

_DEFAULT_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
_GRACE_SECONDS: Final = 3.0
_CONNECT_TIMEOUT_SECONDS: Final = 20.0
_IPC_DIR: Final = "/run/psych-sandbox"
_WORKDIR: Final = "/workspace"
_SYSTEM_BINDS: Final = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/alternatives")
_ETC_FILES: Final = (
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
)
_PROBE_LIMITS: Final = SandboxLimits(
    cpu_seconds=2.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=10.0,
)


def detect_bubblewrap() -> str | None:
    """The ``bwrap`` binary on ``PATH``, or ``None``."""
    return shutil.which("bwrap")


def bubblewrap_usable(bwrap_bin: str | None = None, *, timeout: float = 5.0) -> str | None:
    """The ``bwrap`` that can actually unshare here, or ``None``.

    The binary being installed is not the same as the kernel letting an
    unprivileged process use it. Several distributions ship ``bwrap`` and
    then restrict unprivileged user namespaces (an AppArmor profile, a
    sysctl, a hardened kernel), where every execution would fail at spawn
    with a permission error. This runs the cheapest real thing -- unshare a
    user and a network namespace and exit -- so selection can fall back to a
    process-level backend rather than handing back an isolated one that
    never works.
    """
    found = bwrap_bin or detect_bubblewrap()
    if found is None:
        return None
    probe = [found, "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "/bin/true"]
    try:
        completed = subprocess.run(probe, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return found if completed.returncode == 0 else None


class NamespaceSandbox:
    """``Sandbox`` over bubblewrap on Linux. See the module docstring."""

    def __init__(
        self,
        *,
        python_bin: str | None = None,
        bwrap_bin: str | None = None,
        default_limits: SandboxLimits | None = None,
        env_allowlist: Mapping[str, str] | None = None,
        extra_ro_binds: tuple[str, ...] = (),
    ) -> None:
        """Build an adapter. No process is spawned until ``run()`` is called.

        Args:
            python_bin: the interpreter to run inside the private root.
                Defaults to ``sys.executable``. Its prefix directories are
                bound read-only so it works from a virtualenv.
            bwrap_bin: the bubblewrap binary. Defaults to ``bwrap`` on
                ``PATH``.
            default_limits: used when ``run()`` is not given ``limits``.
            env_allowlist: extra environment variables the child receives.
            extra_ro_binds: more host directories to bind read-only, for an
                interpreter whose libraries live somewhere unusual.

        Raises:
            SandboxSetupError: not Linux, no ``bwrap``, or no interpreter.
        """
        if sys.platform != "linux":
            raise SandboxSetupError(
                "NamespaceSandbox requires Linux: it builds user, mount, pid and network "
                "namespaces with bubblewrap. Use psych_runtime.sandbox.local.local_sandbox() to "
                "pick the backend this host supports."
            )
        found = bwrap_bin or detect_bubblewrap()
        if found is None:
            raise SandboxSetupError(
                "bubblewrap (bwrap) is not on PATH. Install the `bubblewrap` package for this "
                "distribution, or use the container backend."
            )
        self._bwrap = found
        resolved = python_bin or sys.executable
        if not Path(resolved).is_file():
            raise SandboxSetupError(f"python_bin {resolved!r} does not exist")
        self._python_bin = str(Path(resolved).resolve())
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._env_allowlist = dict(env_allowlist or {})
        self._binds = _interpreter_binds(self._python_bin) + tuple(extra_ro_binds)

    async def describe(self) -> SandboxDescription:
        problems: list[str] = []
        try:
            probe = await self.run("return 0", limits=_PROBE_LIMITS)
        except SandboxSetupError as err:
            probe = None
            problems.append(str(err))
        if probe is not None and probe.failure is not None:
            problems.append(f"the probe program failed: {probe.failure.message}")
        guarantees = probe.guarantees if probe is not None else _grade(ReadyFrame(False), False)
        return SandboxDescription(
            backend="bubblewrap",
            platform="linux",
            isolation=achieved_level(guarantees, network_required=True),
            guarantees=guarantees,
            mechanisms=(
                "user_namespace",
                "mount_namespace",
                "pid_namespace",
                "netns",
                "rlimit",
                "cap_drop_all",
                "die_with_parent",
                "scrubbed_env",
            ),
            network_grant_supported=True,
            artifacts_supported=True,
            ready=not problems,
            problems=tuple(problems),
            notes=(
                "requires unprivileged user namespaces; a host that restricts them "
                "fails the probe and reports it here",
            ),
        )

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, HostBinding] | None = None,
        limits: SandboxLimits | None = None,
        network: bool = False,
        isolation: IsolationLevel | None = None,
        capture: OutputCapture | None = None,
        cancel: asyncio.Event | None = None,
    ) -> SandboxResult:
        bound: dict[str, HostBinding] = dict(bindings or {})
        active_limits = limits or self._default_limits
        active_capture = capture or DEFAULT_CAPTURE
        started = time.monotonic()
        if cancel is not None and cancel.is_set():
            return SandboxResult(
                duration_seconds=time.monotonic() - started,
                failure=SandboxFailure(
                    kind="cancelled",
                    message="the execution was cancelled before the program started",
                ),
                cancelled=True,
            )

        ipc_dir = Path(tempfile.mkdtemp(prefix="psych-sandbox-ipc-"))
        workdir = Path(tempfile.mkdtemp(prefix="psych-sandbox-"))
        canary = Canary()
        socket_path = ipc_dir / "ipc.sock"
        try:
            server, connected, accepted = await _listen(socket_path)
            try:
                argv = self._argv(ipc_dir=ipc_dir, workdir=workdir, network=network, canary=canary)
                env = self._build_env(canary)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        preexec_fn=_make_preexec(active_limits),
                        start_new_session=True,
                    )
                except OSError as err:
                    raise SandboxSetupError(f"could not start bubblewrap: {err}") from err
                result = await _drive(
                    proc,
                    connected,
                    program,
                    bound,
                    limits=active_limits,
                    capture=active_capture,
                    started=started,
                    cancel=cancel,
                    workdir=workdir,
                    network=network,
                    isolation=isolation,
                )
            finally:
                await _close_listener(server, connected, accepted)
        finally:
            canary.close()
            remove_tree(ipc_dir)
            remove_tree(workdir)
        return withhold_if_weaker(result, isolation, backend="bubblewrap")

    def _argv(
        self,
        *,
        ipc_dir: Path,
        workdir: Path,
        network: bool,
        canary: Canary,
    ) -> list[str]:
        argv = [
            self._bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup-try",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--hostname",
            "sandbox",
        ]
        if not network:
            argv.append("--unshare-net")
        for path in _SYSTEM_BINDS:
            argv += ["--ro-bind-try", path, path]
        for path in _ETC_FILES:
            argv += ["--ro-bind-try", path, path]
        for path in self._binds:
            argv += ["--ro-bind-try", path, path]
        argv += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(workdir),
            _WORKDIR,
            "--bind",
            str(ipc_dir),
            _IPC_DIR,
            "--chdir",
            _WORKDIR,
            "--clearenv",
            "--setenv",
            "HOME",
            _WORKDIR,
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
        ]
        for key, value in self._env_allowlist.items():
            argv += ["--setenv", key, value]
        # The real canary, at its real host path. A made-up path proves
        # nothing: it would be unreadable inside a working private root and
        # unreadable on a host whose root leaked in by accident, so the two
        # cases it exists to tell apart would look identical. This file does
        # exist, is readable on the host, and is never bound here -- so
        # reading it means the private root is not private.
        argv += ["--setenv", CANARY_ENV, str(canary.path)]
        argv += [
            "--",
            self._python_bin,
            "-I",
            "-B",
            "-u",
            "-c",
            BOOTSTRAP_SOURCE,
            f"unix:{_IPC_DIR}/ipc.sock",
        ]
        return argv

    def _build_env(self, canary: Canary) -> dict[str, str]:
        # bubblewrap's own environment, not the child's: it clears the
        # environment and the child gets only the --setenv list, which is
        # where the canary path is passed.
        _ = canary
        return {"PATH": "/usr/bin:/bin"}


def _interpreter_binds(python_bin: str) -> tuple[str, ...]:
    """Directories the interpreter needs beyond the system binds."""
    try:
        out = subprocess.run(
            [
                python_bin,
                "-I",
                "-c",
                "import sys, json; "
                "print(json.dumps([sys.prefix, sys.base_prefix, sys.exec_prefix]))",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        prefixes = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        prefixes = []
    binds: list[str] = [str(Path(python_bin).resolve().parent)]
    for prefix in prefixes:
        resolved = str(Path(prefix).resolve())
        if not any(resolved.startswith(root + "/") or resolved == root for root in _SYSTEM_BINDS):
            binds.append(resolved)
    return tuple(dict.fromkeys(binds))


def _make_preexec(limits: SandboxLimits) -> Callable[[], None]:
    def _preexec() -> None:
        _clamp(resource.RLIMIT_CORE, 0, 0)
        cpu = max(1, int(limits.cpu_seconds))
        _clamp(resource.RLIMIT_CPU, cpu, cpu + 1)
        _clamp(resource.RLIMIT_AS, limits.address_space_bytes, limits.address_space_bytes)
        _clamp(resource.RLIMIT_FSIZE, limits.file_size_bytes, limits.file_size_bytes)
        _clamp(resource.RLIMIT_NPROC, limits.process_count, limits.process_count)
        os.umask(0o077)

    return _preexec


def _clamp(which: int, soft: int, hard: int) -> None:
    current_soft, current_hard = resource.getrlimit(which)
    new_hard = _min_inf(hard, current_hard)
    new_soft = _min_inf(_min_inf(soft, current_soft), new_hard)
    resource.setrlimit(which, (new_soft, new_hard))


def _min_inf(a: int, b: int) -> int:
    if a == resource.RLIM_INFINITY:
        return b
    if b == resource.RLIM_INFINITY:
        return a
    return min(a, b)


async def _listen(
    socket_path: Path,
) -> tuple[
    asyncio.AbstractServer,
    asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    list[asyncio.StreamWriter],
]:
    loop = asyncio.get_running_loop()
    connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )
    accepted: list[asyncio.StreamWriter] = []

    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        if not connected.done():
            connected.set_result((reader, writer))

    server = await asyncio.start_unix_server(_on_client, path=str(socket_path))
    return server, connected, accepted


async def _close_listener(
    server: asyncio.AbstractServer,
    connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    accepted: list[asyncio.StreamWriter],
) -> None:
    server.close()
    if not connected.done():
        connected.cancel()
    for writer in accepted:
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await asyncio.wait_for(writer.wait_closed(), timeout=_GRACE_SECONDS)
    with contextlib.suppress(OSError):
        await asyncio.wait_for(server.wait_closed(), timeout=_GRACE_SECONDS)


async def _drive(
    proc: asyncio.subprocess.Process,
    connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    program: str,
    bindings: Mapping[str, HostBinding],
    *,
    limits: SandboxLimits,
    capture: OutputCapture,
    started: float,
    cancel: asyncio.Event | None,
    workdir: Path,
    network: bool,
    isolation: IsolationLevel | None,
) -> SandboxResult:
    stdout_task = asyncio.ensure_future(read_capped(proc.stdout, capture.stream_bytes))
    stderr_task = asyncio.ensure_future(read_capped(proc.stderr, capture.stream_bytes))
    setup_error: str | None = None
    talk = Conversation()
    try:
        try:
            reader, writer = await asyncio.wait_for(connected, timeout=_CONNECT_TIMEOUT_SECONDS)
        except TimeoutError:
            setup_error = (
                f"bubblewrap's child never connected back within {_CONNECT_TIMEOUT_SECONDS}s. "
                "The host may restrict unprivileged user namespaces, or the interpreter's "
                "directories may not be visible inside the private root; run `bwrap` by hand "
                "with the same arguments to see its error."
            )
        else:
            talk = await converse(
                reader,
                writer,
                program,
                bindings,
                wall_seconds=limits.wall_seconds,
                cancel=cancel,
                admit=admission(
                    lambda ready: _grade(ready, network),
                    isolation,
                    network_required=not network,
                    backend="bubblewrap",
                ),
            )
    finally:
        await _terminate_process_group(proc)

    stdout = await bounded(stdout_task)
    stderr = await bounded(stderr_task)
    returncode = await proc.wait()
    if setup_error is not None:
        detail = stderr.data.decode("utf-8", errors="replace").strip()
        raise SandboxSetupError(setup_error + (f" bwrap said: {detail}" if detail else ""))

    failure, limit_hit = _classify(
        returncode=returncode, talk=talk, wall_seconds=limits.wall_seconds
    )
    value = talk.done.value if (talk.done is not None and failure is None) else None
    artifacts, omitted = collect_artifacts(workdir, capture) if failure is None else ((), 0)
    observed = talk.ready if talk.ready is not None else ReadyFrame(network_denied=False)
    guarantees = _grade(observed, network)
    return SandboxResult(
        stdout=stdout.data.decode("utf-8", errors="replace"),
        stderr=stderr.data.decode("utf-8", errors="replace"),
        value=value,
        failure=failure,
        duration_seconds=time.monotonic() - started,
        limit_hit=limit_hit,
        network_denied=observed.network_denied and not network,
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        stdout_data=stdout.data,
        stderr_data=stderr.data,
        stdout_size=stdout.observed,
        stderr_size=stderr.observed,
        isolation=achieved_level(guarantees, network_required=not network),
        guarantees=guarantees,
        artifacts=artifacts,
        artifacts_omitted=omitted,
        cancelled=talk.cancelled,
    )


def _grade(ready: ReadyFrame, network_granted: bool) -> SandboxGuarantees:
    enforced, unavailable, unverified = (
        Enforcement.ENFORCED,
        Enforcement.UNAVAILABLE,
        Enforcement.UNVERIFIED,
    )
    network = unavailable
    if not network_granted and ready.network_denied:
        network = enforced
    if ready.canary_readable is None:
        filesystem = unverified
    else:
        filesystem = unavailable if ready.canary_readable else enforced
    return SandboxGuarantees(
        filesystem=filesystem,
        network=network,
        process_tree=enforced,
        identity=unavailable,
        cpu=enforced,
        memory=enforced,
        file_size=enforced,
        process_count=enforced,
        wall_clock=enforced,
        environment=enforced,
    )


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        await proc.wait()


def _classify(
    *, returncode: int | None, talk: Conversation, wall_seconds: float
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    if talk.cancelled:
        return (
            SandboxFailure(
                kind="cancelled",
                message="the execution was cancelled and its process tree was ended",
            ),
            None,
        )
    if talk.timed_out:
        return (
            SandboxFailure(
                kind="timeout",
                message=f"execution exceeded its {wall_seconds}s wall-clock limit",
            ),
            SandboxLimit.WALL_SECONDS,
        )
    if talk.done is not None:
        return classify_done(talk.done)
    if returncode is not None and returncode < 0:
        return _classify_signal(-returncode)
    if talk.protocol_error is not None:
        return SandboxFailure(kind="protocol_violation", message=str(talk.protocol_error)), None
    return (
        SandboxFailure(
            kind="protocol_violation",
            message="the sandboxed process ended without completing the protocol",
        ),
        None,
    )


def _classify_signal(received: int) -> tuple[SandboxFailure, SandboxLimit | None]:
    if received == signal.SIGXCPU:
        return (
            SandboxFailure(
                kind="resource_limit", message="the program exceeded its CPU time limit"
            ),
            SandboxLimit.CPU_SECONDS,
        )
    if received == signal.SIGXFSZ:
        return (
            SandboxFailure(
                kind="resource_limit", message="the program exceeded its file size limit"
            ),
            SandboxLimit.FILE_SIZE_BYTES,
        )
    if received == signal.SIGKILL:
        return (
            SandboxFailure(
                kind="resource_limit",
                message="the program was killed, most likely by the CPU time hard limit",
            ),
            SandboxLimit.CPU_SECONDS,
        )
    return (
        SandboxFailure(
            kind="terminated", message=f"the process was terminated by signal {received}"
        ),
        None,
    )


DoneFrameType = DoneFrame
SandboxProtocolErrorType = SandboxProtocolError
