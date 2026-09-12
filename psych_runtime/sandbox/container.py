"""The container ``Sandbox`` adapter: one container per execution.

DESIGN.md §18. For a consumer who needs isolation stronger
than a process boundary: every guarantee this adapter makes is backed by the
container runtime's own kernel-level controls (namespaces, cgroups), not by
anything this process does to itself, which is the whole reason it exists
alongside ``psych_runtime.sandbox.subprocess``.

**There is no container runtime in this development environment**, so this
module cannot be exercised here. It is implemented fully and correctly
against the documented behaviour of ``docker``/``podman run``, and its tests
(``tests/functional/test_sandbox_container.py``) are written to run for
real, not faked; they skip with an explicit reason when
``detect_container_runtime()`` finds no usable runtime, and run for real in
any environment that has one, including CI.

## What each layer of containment actually is, and who backs it

Unlike the subprocess adapter, where every guarantee ultimately rests on
this process's own privilege and the ``resource`` module, every guarantee
here is backed by the container runtime, which is a real, independent
security boundary this process does not have to trust its own correctness
for:

- **Network.** ``--network=none`` unless ``network=True``. A container
  network namespace with no configured network is a kernel-level guarantee
  that does not depend on anything this process got right, unlike the
  subprocess adapter's best-effort ``unshare`` attempt.
- **Memory (``address_space_bytes``).** ``--memory`` and ``--memory-swap``
  set to the same value (never letting swap absorb an overrun defeats the
  cap). Enforced by the kernel's cgroup memory controller: a container that
  crosses this is OOM-killed by the kernel, not by anything in this
  process's control. ``SandboxResult.limit_hit`` is set to
  ``ADDRESS_SPACE_BYTES`` by checking ``docker inspect``'s own
  ``State.OOMKilled`` field after the container exits, which is the
  runtime's own authoritative record of why it died, not a guess from an
  exit code.
- **Process count (``process_count``).** ``--pids-limit``, the cgroup pids
  controller. A fork bomb runs into this ceiling regardless of what uid the
  container runs as, unlike the subprocess adapter where this is decorative
  under uid 0.
- **CPU time (``cpu_seconds``) and file size (``file_size_bytes``).** Docker
  has no cgroup-backed equivalent of either: ``--cpus`` is a *rate* cap
  (fixed to ``1`` here, so one execution cannot claim more than one core),
  not a *total consumed* budget, and there is no `--max-file-size` flag at
  all. So these two are enforced the same way the subprocess adapter enforces
  them, just one layer further in: the container's entry command is wrapped
  in a small POSIX ``sh -c`` script (``_ULIMIT_WRAPPER`` below) that calls
  ``ulimit -t``/``ulimit -f`` before ``exec``-ing the interpreter, setting
  ``RLIMIT_CPU``/``RLIMIT_FSIZE`` on the process inside the container via the
  same mechanism the subprocess adapter uses directly through the ``resource``
  module. **This means these two specific limits depend on the image
  having a POSIX shell at ``/bin/sh`` that understands ``ulimit -t`` and
  ``ulimit -f``.** Every mainstream Debian/Ubuntu/Alpine-based image
  (including the default ``python:3.12-slim``) does; a distroless or
  scratch-based image with no shell at all would silently not get these two
  limits from this adapter as built. This is a real, named limitation, not
  an oversight: say so plainly rather than imply a guarantee that is not
  there.

## Communication: a bind-mounted Unix socket, not an inherited fd

The subprocess adapter passes the protocol channel to its child as an
inherited file descriptor, which only works because that child is a direct
fork of this process. A container is a separate, isolated filesystem and
process tree reached through the runtime's own API/CLI, not forked from
here, so there is no file descriptor to inherit. Instead: a temporary host
directory is bind-mounted into the container (this is filesystem access, not
network access, so it works unchanged under ``--network=none``); this
process listens on a Unix domain socket at a path inside that directory
before the container starts; the container's bootstrap script (the same
``psych_runtime.sandbox._bootstrap.BOOTSTRAP_SOURCE`` the subprocess adapter uses,
unmodified) connects to that path as a client. Everything past that
handshake, the framing, the call/reply/done vocabulary, the hostile-input
rebuilding, is identical between the two adapters (``psych_runtime.sandbox.protocol``).

The permissions on those two nodes are load-bearing and easy to get wrong.
The container runs as a uid that is deliberately not the worker's, and
``connect(2)`` on a Unix socket requires write permission on it, so the
socket is ``0666`` -- it is reached by an account this process does not
control. What keeps that from being an opening for any local user is the
directory holding it: ``0711`` and randomly named, so the path can be
traversed by someone who already knows it and enumerated by nobody, and the
server accepts one connection and stops.

## What this backend reports about itself

``IsolationLevel.ISOLATED``, and every result grades it from inside: the
child's own ``ready`` frame says whether it could reach the network and
whether it could read a canary file the host placed outside the mounted
directory, so ``filesystem`` and ``network`` are observations. ``describe()``
checks for a usable runtime *and* for the image being present locally (this
adapter never pulls), and is ``ready=False`` naming which is missing.
Artifacts are collected only when a caller asks (``OutputCapture``), by
bind-mounting a host directory at the container's working directory in
place of the tmpfs; the tmpfs stays the default because it carries a size
cap a bind mount does not. On a Windows host this backend is not ready:
the channel is a bind-mounted Unix socket, which Docker Desktop cannot
mount from a Windows path. Run the worker on Linux or macOS, or reach a
container through ``psych_runtime.sandbox.remote``.

## Teardown

Every container is created with an explicit, generated name and torn down
by this adapter issuing its own ``docker stop`` (on a timeout) followed
unconditionally by ``docker rm -f`` in a ``finally`` block, whether the
execution finished cleanly, failed, or was never reachable at all. This does
not rely on ``docker run --rm``, deliberately: ``--rm`` would remove the
container the instant it exits, which would race this adapter's own
post-exit ``docker inspect`` call for ``OOMKilled`` and the exit code,
sometimes losing the very information a memory-limit failure needs to be
reported accurately.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox._bootstrap import BOOTSTRAP_SOURCE, CANARY_ENV
from psych_runtime.sandbox._local import (
    DEFAULT_CAPTURE,
    Canary,
    Conversation,
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

__all__ = ["ContainerSandbox", "detect_container_runtime"]

_DEFAULT_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
"""Identical to the subprocess adapter's own defaults; see that module for
the reasoning. Nothing about running in a container changes what a
reasonable default execution budget is."""

_STOP_GRACE_SECONDS: Final = 3.0
_CONNECT_TIMEOUT_SECONDS: Final = 20.0
"""How long this adapter waits for the container's bootstrap to connect
before giving up and treating the container as unreachable. Generous: it has
to cover a cold image pull's worth of container-start latency on a slow
host, not just process spawn."""

_CONTAINER_IPC_DIR: Final = "/run/psych-sandbox"
_CONTAINER_WORKDIR: Final = "/workspace"
_NOBODY: Final = (65534, 65534)

_ULIMIT_WRAPPER: Final = (
    "python_bin=$1; bootstrap=$2; connspec=$3; shift 3; "
    'ulimit -S -t "$PSYCH_CPU_SECONDS" 2>/dev/null; '
    'ulimit -H -t "$PSYCH_CPU_HARD_SECONDS" 2>/dev/null; '
    'ulimit -f "$PSYCH_FSIZE_BLOCKS" 2>/dev/null; '
    'exec "$python_bin" -I -B -u -c "$bootstrap" "$connspec" "$@"'
)
"""Sets RLIMIT_CPU and RLIMIT_FSIZE on the container's own process before
exec'ing the interpreter, the same POSIX mechanism the subprocess adapter
uses directly via ``resource.setrlimit``. Errors from an unsupported
``ulimit`` flag are silenced (``2>/dev/null``) rather than aborting the
script: a shell that cannot set one of these should still run the program
under whatever containment the cgroup-backed flags (memory, pids, network)
still provide, not fail the execution outright over a missing shell
feature. See the module docstring for exactly which limits this affects and
what depends on the image having a capable shell at all.

The soft and hard CPU limits are set separately, one second apart, rather
than as one ``ulimit -t N`` (which sets both to the same value): verified
directly against dash that setting them equal makes the kernel deliver
``SIGKILL`` as the first and only observed signal, never ``SIGXCPU``, which
would make a CPU-limit kill indistinguishable from every other reason a
container's process might receive ``SIGKILL``. With a one-second gap,
exactly as ``psych_runtime.sandbox.subprocess`` uses, the soft limit's ``SIGXCPU``
arrives first and is what ``_classify`` below actually observes.

Deliberately not ``"${@:3}"``: that offset form of ``$@`` slicing is a
bash extension, not POSIX, and Debian/Ubuntu's ``/bin/sh`` is dash, which
rejects it outright with "Bad substitution" before this script runs at
all (verified directly against dash, not assumed). Named variables plus a

Deliberately not ``"${@:3}"``: that offset form of ``$@`` slicing is a
bash extension, not POSIX, and Debian/Ubuntu's ``/bin/sh`` is dash, which
rejects it outright with "Bad substitution" before this script runs at
all (verified directly against dash, not assumed). Named variables plus a
plain ``shift 3`` are POSIX and work identically in bash, dash, and
busybox's ash, which between them cover essentially every base image this
adapter is likely to be pointed at."""


async def detect_container_runtime(*, timeout: float = 10.0) -> str | None:
    """The runtime binary to use, or ``None`` if none is usable right now.

    Checks both that a binary exists on ``PATH`` (``docker`` preferred,
    ``podman`` as a fallback) *and* that its daemon actually answers, since a
    binary with no reachable daemon (a common state: the CLI is installed
    but nothing is running) is exactly as useless here as no binary at all.
    Used both by this adapter's own constructor and by
    ``tests/functional/test_sandbox_container.py`` to decide whether to skip.
    """
    for candidate in ("docker", "podman"):
        binary = shutil.which(candidate)
        if binary is None:
            continue
        try:
            # `version`, not `info`. Both fail when no daemon answers, which
            # is the whole question here, but `info` also enumerates plugins,
            # storage drivers and network state, and on a loaded CI runner it
            # took longer than the five seconds this used to allow -- so a host
            # with a perfectly good Docker was told it had none, and the
            # container backend skipped itself into looking absent.
            proc = await asyncio.create_subprocess_exec(
                binary,
                "version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            continue
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            # The probe outlived its welcome, so kill it and reap it before
            # moving on. Letting the timeout fall through left a live child and
            # an unclosed transport: `podman info` on a host where podman is
            # installed but unconfigured does not return, and this abandoned one
            # per probe per process. On a CI runner that meant orphaned podman
            # processes outliving the test session, a job that took twenty-six
            # minutes to report that it had skipped everything, and -- because
            # this project turns warnings into errors -- three tests failing on
            # the ResourceWarning from the transport nobody closed.
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            continue
        if returncode == 0:
            return binary
    return None


class ContainerSandbox:
    """``Sandbox`` over one container per execution. See the module docstring."""

    def __init__(
        self,
        *,
        runtime: str | None = None,
        image: str = "python:3.12-slim",
        container_python_bin: str = "/usr/local/bin/python3",
        default_limits: SandboxLimits | None = None,
        env_allowlist: Mapping[str, str] | None = None,
        run_as: tuple[int, int] = _NOBODY,
        extra_run_args: Sequence[str] = (),
    ) -> None:
        """Build an adapter. No container is created until ``run()`` is called.

        Args:
            runtime: the runtime binary to invoke (``docker`` or ``podman``,
                as a path or a bare name resolved on ``PATH``). Defaults to
                whatever ``detect_container_runtime()`` finds; raises
                immediately if that is nothing, since every ``run()`` would
                fail identically and a consumer configuring this in their
                own startup should learn that then, not on a customer's
                first request.
            image: pulled or built by the consumer ahead of time; this
                adapter never pulls one implicitly, so a missing image
                surfaces as ``SandboxSetupError`` rather than a silent,
                slow, unbounded download on someone's first request.
                Defaults to ``python:3.12-slim``, a Debian-based image with a
                real ``/bin/sh``; see the module docstring's note on why
                that matters for two of the five limits.
            container_python_bin: absolute path to the interpreter inside
                the image. The default matches where the official
                ``python:3.12-slim`` image installs it; a different image
                needs this set explicitly.
            default_limits: as ``SubprocessSandbox``.
            env_allowlist: as ``SubprocessSandbox``: an explicit allowlist,
                passed as ``-e KEY=VALUE``, never anything from this
                process's own environment.
            run_as: the ``--user uid:gid`` the container runs as. Defaults to
                "nobody", for the same reason as the subprocess adapter,
                though here it is defence in depth rather than load-bearing:
                ``--pids-limit`` and ``--memory`` are cgroup controls that do
                not have uid-0's carve-out the way ``RLIMIT_NPROC`` does.
            extra_run_args: appended to the ``docker run`` invocation
                verbatim, after every flag this adapter sets itself, so a
                consumer can add something this adapter has no opinion about
                (a specific cgroup driver flag, a seccomp profile path)
                without this class needing to grow an option for everything
                a runtime can do. Not validated; whatever is passed here is
                trusted the same as this adapter's own arguments.

        Raises:
            SandboxSetupError: ``runtime`` was not given and no usable
                container runtime could be found.
        """
        self._runtime = runtime
        """``None`` means "not yet resolved"; ``_resolve_runtime`` fills it in lazily
        on first ``run()`` and every call after that reuses the same one."""
        self._image = image
        self._container_python_bin = container_python_bin
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._env_allowlist = dict(env_allowlist or {})
        self._run_as = run_as
        self._extra_run_args = list(extra_run_args)

    async def _resolve_runtime(self) -> str:
        if self._runtime is not None:
            return self._runtime
        found = await detect_container_runtime()
        if found is None:
            raise SandboxSetupError(
                "no usable container runtime was found (checked for `docker` and "
                "`podman` on PATH, each with a reachable daemon). Pass runtime= "
                "explicitly, or use psych_runtime.sandbox.subprocess.SubprocessSandbox instead "
                "if a container runtime is not available in this deployment."
            )
        self._runtime = found
        return found

    async def describe(self) -> SandboxDescription:
        """Runtime reachable, image present, host able to mount the channel.

        Predicts the guarantees the runtime's own flags provide; each result
        then confirms ``network`` and ``filesystem`` from inside the
        container. Nothing here starts a container.
        """
        problems: list[str] = []
        if sys.platform == "win32":
            problems.append(
                "ContainerSandbox needs a POSIX worker: its channel is a bind-mounted "
                "Unix socket, which cannot be mounted from a Windows path"
            )
        runtime: str | None = None
        try:
            runtime = await self._resolve_runtime()
        except SandboxSetupError as err:
            problems.append(str(err))
        if runtime is not None and not await _image_present(runtime, self._image):
            problems.append(
                f"image {self._image!r} is not present locally and this adapter never "
                f"pulls implicitly; run `{Path(runtime).name} pull {self._image}` first"
            )
        enforced = Enforcement.ENFORCED
        guarantees = SandboxGuarantees(
            filesystem=enforced,
            network=enforced,
            process_tree=enforced,
            identity=enforced,
            cpu=enforced,
            memory=enforced,
            file_size=enforced,
            process_count=enforced,
            wall_clock=enforced,
            environment=enforced,
        )
        return SandboxDescription(
            backend="container",
            platform="linux",
            isolation=IsolationLevel.ISOLATED,
            guarantees=guarantees,
            mechanisms=(
                "container_namespaces",
                "cgroups",
                "network_none",
                "read_only_rootfs",
                "cap_drop_all",
                "no_new_privileges",
                "ulimit",
            ),
            network_grant_supported=True,
            artifacts_supported=True,
            ready=not problems,
            problems=tuple(problems),
            notes=(
                "cpu_seconds and file_size_bytes rely on the image having /bin/sh with "
                "ulimit; a distroless image gets neither",
                f"image: {self._image}",
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
        if sys.platform == "win32":
            raise SandboxSetupError(
                "ContainerSandbox cannot run from a Windows worker: its channel is a "
                "bind-mounted Unix socket. Run the worker on Linux or macOS, or use a "
                "remote sandbox."
            )
        runtime = await self._resolve_runtime()
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
        # A host directory for the container's working directory, only when
        # the caller wants the files back: the tmpfs default carries a size
        # cap that a bind mount does not. World-writable with the sticky bit
        # because the container's uid is deliberately not the worker's.
        workspace: Path | None = None
        if active_capture.collect_artifacts:
            workspace = Path(tempfile.mkdtemp(prefix="psych-sandbox-ws-"))
            workspace.chmod(0o1777)
        canary = Canary()
        # 0711, not 0755: the container's uid has to *traverse* this directory
        # to reach the socket, but nothing needs to list it. Dropping read
        # means another local account cannot enumerate the socket names of
        # executions in flight, which is what makes the socket's own mode
        # below tolerable.
        ipc_dir.chmod(0o711)
        socket_path = ipc_dir / "ipc.sock"
        name = f"psych-sandbox-{secrets.token_hex(8)}"

        try:
            server, connected, accepted = await _listen_for_one_connection(socket_path)
            # The container runs as `--user` (65534:65534 by default), which is
            # deliberately not this process's uid, and connect(2) on a Unix
            # socket needs write permission on the node. The socket lands at
            # 0755 from the default umask and owned by whoever runs the worker,
            # so the container's bootstrap got EACCES, exited, and this adapter
            # sat out its full connect timeout reporting a missing interpreter.
            # It could not have worked on any host at the default run_as.
            #
            # Relaxing the socket rather than raising the container's
            # privileges: the alternatives are running the program as the
            # worker's own uid, which is the property the subprocess adapter
            # refuses by default and for the same reason, or chowning to the
            # container's uid, which needs the worker to be root and so fails
            # exactly where this failed. What the mode gives away is bounded by
            # the directory above -- an unguessable name that cannot be listed
            # -- and by this server accepting a single connection and closing,
            # which the container's own bootstrap is already racing to take.
            socket_path.chmod(0o666)
            try:
                argv = _build_run_argv(
                    runtime=runtime,
                    name=name,
                    image=self._image,
                    container_python_bin=self._container_python_bin,
                    ipc_dir=ipc_dir,
                    bindings=bound,
                    limits=active_limits,
                    network=network,
                    env_allowlist=self._env_allowlist,
                    run_as=self._run_as,
                    extra_run_args=self._extra_run_args,
                    workspace=workspace,
                    canary=canary,
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as err:
                    raise SandboxSetupError(
                        f"could not invoke the container runtime {runtime!r}: {err}"
                    ) from err

                result = await _drive(
                    runtime=runtime,
                    name=name,
                    proc=proc,
                    connected=connected,
                    program=program,
                    bindings=bound,
                    limits=active_limits,
                    capture=active_capture,
                    started=started,
                    cancel=cancel,
                    workspace=workspace,
                    network=network,
                    run_as=self._run_as,
                )
            finally:
                await _close_listener(server, connected, accepted)
        finally:
            canary.close()
            shutil.rmtree(ipc_dir, ignore_errors=True)
            if workspace is not None:
                remove_tree(workspace)
        return withhold_if_weaker(result, isolation, backend="container")


async def _listen_for_one_connection(
    socket_path: Path,
) -> tuple[
    asyncio.AbstractServer,
    asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    list[asyncio.StreamWriter],
]:
    """Start listening before the container exists, so its connect() never races a bind().

    Every accepted connection is recorded, not just the first one that wins
    the future. A container that connects back after the adapter has stopped
    waiting for it still leaves a socket open on this side, and the only
    reference to it would otherwise be the callback frame that is already
    gone. ``_close_listener`` needs all of them.
    """
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
    accepted: Sequence[asyncio.StreamWriter],
) -> None:
    """Close the connections first, then the listener. That order is the point.

    Since 3.12, ``Server.wait_closed()`` waits for the connections the server
    accepted and not merely for the listening socket. Closing the listener
    while still holding the container's connection open therefore blocks here
    for as long as the container lives, and nothing in this module bounds it:
    the execution has already produced its result by the time this runs, so
    every guard in ``_drive`` is spent. Under 3.11 the same call returned at
    once, which is why this survived review and every local run: it needs a
    3.12 host *and* a reachable container runtime to show itself at all.

    What it looked like when it did show itself: all thirteen container tests
    hanging identically, no container left behind, and a daemon answering in
    21 milliseconds. The work was finished and only the cleanup was stuck.
    """
    server.close()
    if not connected.done():
        connected.cancel()
    for writer in accepted:
        writer.close()
        # A container killed mid-execution resets the connection rather than
        # shutting it down, so waiting on it raises rather than returning.
        with contextlib.suppress(OSError, asyncio.CancelledError):
            await asyncio.wait_for(writer.wait_closed(), timeout=_STOP_GRACE_SECONDS)
    with contextlib.suppress(OSError):
        await asyncio.wait_for(server.wait_closed(), timeout=_STOP_GRACE_SECONDS)


def _build_run_argv(
    *,
    runtime: str,
    name: str,
    image: str,
    container_python_bin: str,
    ipc_dir: Path,
    bindings: Mapping[str, HostBinding],
    limits: SandboxLimits,
    network: bool,
    env_allowlist: Mapping[str, str],
    run_as: tuple[int, int],
    extra_run_args: Sequence[str],
    workspace: Path | None = None,
    canary: Canary | None = None,
) -> list[str]:
    fsize_blocks = max(1, (limits.file_size_bytes + 511) // 512)
    cpu_seconds = max(1, int(limits.cpu_seconds))
    tmpfs_size = max(limits.file_size_bytes * 4, 16 * 1024 * 1024)
    uid, gid = run_as
    if workspace is None:
        workdir_mount = ["--tmpfs", f"{_CONTAINER_WORKDIR}:size={tmpfs_size},mode=1777"]
    else:
        workdir_mount = ["--volume", f"{workspace}:{_CONTAINER_WORKDIR}:rw"]

    argv = [
        runtime,
        "run",
        "--name",
        name,
        "--network",
        "bridge" if network else "none",
        "--memory",
        str(limits.address_space_bytes),
        "--memory-swap",
        str(limits.address_space_bytes),
        "--pids-limit",
        str(limits.process_count),
        "--cpus",
        "1",
        "--read-only",
        *workdir_mount,
        "--workdir",
        _CONTAINER_WORKDIR,
        "--volume",
        f"{ipc_dir}:{_CONTAINER_IPC_DIR}:rw",
        "--user",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--env",
        f"HOME={_CONTAINER_WORKDIR}",
        "--env",
        f"TMPDIR={_CONTAINER_WORKDIR}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        f"PSYCH_CPU_SECONDS={cpu_seconds}",
        "--env",
        f"PSYCH_CPU_HARD_SECONDS={cpu_seconds + 1}",
        "--env",
        f"PSYCH_FSIZE_BLOCKS={fsize_blocks}",
    ]
    if canary is not None:
        # The canary lives on the host and is not mounted; the child reporting
        # it unreadable is the filesystem guarantee, observed rather than
        # assumed from the runtime's flags.
        argv += ["--env", f"{CANARY_ENV}={canary.path}"]
    for key, value in env_allowlist.items():
        argv += ["--env", f"{key}={value}"]
    argv += list(extra_run_args)
    argv += [
        image,
        "sh",
        "-c",
        _ULIMIT_WRAPPER,
        "psych-sandbox",  # conventional $0; the wrapper never reads it
        container_python_bin,
        BOOTSTRAP_SOURCE,
        f"unix:{_CONTAINER_IPC_DIR}/ipc.sock",
        *bindings.keys(),
    ]
    return argv


async def _drive(
    *,
    runtime: str,
    name: str,
    proc: asyncio.subprocess.Process,
    connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    program: str,
    bindings: Mapping[str, HostBinding],
    limits: SandboxLimits,
    capture: OutputCapture,
    started: float,
    cancel: asyncio.Event | None,
    workspace: Path | None,
    network: bool,
    run_as: tuple[int, int],
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
                f"the container never connected back within {_CONNECT_TIMEOUT_SECONDS}s of "
                "starting. This usually means the image has no interpreter at the configured "
                "container_python_bin, or has no /bin/sh for the ulimit wrapper; check "
                "`docker logs` for the container, or run the image manually to diagnose."
            )
        else:
            talk = await converse(
                reader, writer, program, bindings, wall_seconds=limits.wall_seconds, cancel=cancel
            )
    finally:
        # Also on this task being cancelled from outside: the container is
        # stopped and removed before anything else happens.
        exit_info = await _teardown(runtime, name, proc, timed_out=talk.timed_out or talk.cancelled)

    stdout = await bounded(stdout_task)
    stderr = await bounded(stderr_task)

    if setup_error is not None:
        raise SandboxSetupError(setup_error)

    failure, limit_hit = _classify(
        exit_info=exit_info,
        done=talk.done,
        protocol_error=talk.protocol_error,
        timed_out=talk.timed_out,
        cancelled=talk.cancelled,
        wall_seconds=limits.wall_seconds,
        elapsed=time.monotonic() - started,
        cpu_seconds=limits.cpu_seconds,
    )
    value = talk.done.value if (talk.done is not None and failure is None) else None
    artifacts, omitted = (
        collect_artifacts(workspace, capture)
        if workspace is not None and failure is None
        else ((), 0)
    )
    observed = talk.ready if talk.ready is not None else ReadyFrame(network_denied=False)
    guarantees = _grade(observed, network_granted=network, run_as=run_as)
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


def _grade(
    ready: ReadyFrame, *, network_granted: bool, run_as: tuple[int, int]
) -> SandboxGuarantees:
    """What this execution's guarantees were worth.

    The runtime's flags back everything the child cannot see for itself
    (cgroups, capabilities, the read-only root); the child's own probe grades
    the two it can.
    """
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
    if ready.uid is None:
        identity = unverified
    else:
        identity = enforced if ready.uid == run_as[0] else unavailable
    return SandboxGuarantees(
        filesystem=filesystem,
        network=network,
        process_tree=enforced,
        identity=identity,
        cpu=enforced,
        memory=enforced,
        file_size=enforced,
        process_count=enforced,
        wall_clock=enforced,
        environment=enforced,
    )


async def _image_present(runtime: str, image: str) -> bool:
    """Whether ``image`` exists locally, asked of the runtime itself."""
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS * 4) == 0
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        return False


class _ExitInfo:
    """What ``docker inspect`` says happened, or ``None`` fields if it could not be asked."""

    __slots__ = ("exit_code", "oom_killed")

    def __init__(self, exit_code: int | None, oom_killed: bool) -> None:
        self.exit_code = exit_code
        self.oom_killed = oom_killed


async def _reap(proc: asyncio.subprocess.Process, *, timeout: float = _STOP_GRACE_SECONDS) -> None:
    """Wait for a teardown helper, and kill it if it will not finish.

    ``docker stop`` and ``docker rm -f`` are the two commands this adapter runs
    that nothing else bounds, and ``rm -f`` runs on every single execution. An
    unbounded wait on either hands the runtime's CLI an unlimited claim on the
    caller's time: if it blocks, so does the execution, past every guard in
    this module -- the 20-second connect and the wall clock both belong to the
    program, not to cleaning up after it.

    This is a bound the adapter was missing, not the cause of any failure seen
    so far. The hang it was first written to explain turned out to belong to
    ``_close_listener``; see there.
    """
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


async def _teardown(
    runtime: str, name: str, proc: asyncio.subprocess.Process, *, timed_out: bool
) -> _ExitInfo:
    """Stop the container if it is still running, inspect it, then always remove it.

    Inspecting before removing is why this adapter does not use ``docker run
    --rm``: removal deletes the runtime's own record of why the container
    died (in particular ``State.OOMKilled``), and this adapter's memory-limit
    reporting depends on reading that record while it still exists.
    """
    if timed_out:
        with contextlib.suppress(OSError):
            stop = await asyncio.create_subprocess_exec(
                runtime,
                "stop",
                "-t",
                str(int(_STOP_GRACE_SECONDS)),
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await _reap(stop)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)

    exit_info = await _inspect(runtime, name)

    with contextlib.suppress(OSError):
        remove = await asyncio.create_subprocess_exec(
            runtime,
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _reap(remove)

    return exit_info


async def _inspect(runtime: str, name: str) -> _ExitInfo:
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "inspect",
            "--format",
            "{{.State.ExitCode}}|{{.State.OOMKilled}}",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_STOP_GRACE_SECONDS)
    except (TimeoutError, OSError):
        return _ExitInfo(exit_code=None, oom_killed=False)

    text = stdout.decode().strip()
    code_text, _, oom_text = text.partition("|")
    exit_code = int(code_text) if code_text.lstrip("-").isdigit() else None
    return _ExitInfo(exit_code=exit_code, oom_killed=oom_text.strip() == "true")


def _classify(
    *,
    exit_info: _ExitInfo,
    done: DoneFrame | None,
    protocol_error: SandboxProtocolError | None,
    timed_out: bool,
    cancelled: bool,
    wall_seconds: float,
    elapsed: float,
    cpu_seconds: float,
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    stopped = _classify_stopped(cancelled=cancelled, timed_out=timed_out, wall_seconds=wall_seconds)
    if stopped is not None:
        return stopped

    if done is not None:
        return classify_done(done)

    if exit_info.oom_killed:
        return (
            SandboxFailure(
                kind="resource_limit",
                message="the container was killed by the kernel for exceeding its memory limit",
            ),
            SandboxLimit.ADDRESS_SPACE_BYTES,
        )

    # A process cannot have burned `cpu_seconds` of CPU in less than that much
    # wall clock, so an unattributed kill this early is not RLIMIT_CPU's.
    signalled = _classify_exit_code(exit_info.exit_code, cpu_time_reachable=elapsed >= cpu_seconds)
    if signalled is not None:
        return signalled

    if protocol_error is not None:
        return SandboxFailure(kind="protocol_violation", message=str(protocol_error)), None

    exit_note = "" if exit_info.exit_code is None else f" (exit code {exit_info.exit_code})"
    return (
        SandboxFailure(
            kind="protocol_violation",
            message=f"the container ended without completing the protocol{exit_note}",
        ),
        None,
    )


def _classify_stopped(
    *, cancelled: bool, timed_out: bool, wall_seconds: float
) -> tuple[SandboxFailure, SandboxLimit | None] | None:
    """The two endings the host itself caused, before anything the child did."""
    if cancelled:
        return (
            SandboxFailure(
                kind="cancelled",
                message="the execution was cancelled and its container was removed",
            ),
            None,
        )
    if timed_out:
        return (
            SandboxFailure(
                kind="timeout",
                message=f"execution exceeded its {wall_seconds}s wall-clock limit",
            ),
            SandboxLimit.WALL_SECONDS,
        )
    return None


def _classify_exit_code(
    exit_code: int | None, *, cpu_time_reachable: bool
) -> tuple[SandboxFailure, SandboxLimit | None] | None:
    """Recognise the runtime's own "died from signal N" convention: 128 + N.

    Standard behaviour for Docker and Podman alike when a container's main
    process is terminated by a signal (the same convention a POSIX shell's
    own ``$?`` uses). This is what lets a CPU-limit or file-size-limit kill,
    both delivered as signals by the ``ulimit`` wrapper rather than reported
    through the protocol (the process is dead before it can send a ``done``
    frame), be told apart from an unrelated ``SIGKILL``.
    """
    if exit_code is None or exit_code < 128:
        return None
    received = exit_code - 128
    # These are signals from the Linux container, not the client host. Windows
    # has no SIGXCPU/SIGXFSZ attributes; supported Linux images use 24/25/9.
    if received == 24:  # SIGXCPU
        return (
            SandboxFailure(
                kind="resource_limit", message="the program exceeded its CPU time limit"
            ),
            SandboxLimit.CPU_SECONDS,
        )
    if received == 25:  # SIGXFSZ
        return (
            SandboxFailure(
                kind="resource_limit", message="the program exceeded its file size limit"
            ),
            SandboxLimit.FILE_SIZE_BYTES,
        )
    if received == 9:  # SIGKILL
        # Two things in this adapter SIGKILL a container: the memory cgroup and
        # RLIMIT_CPU's hard limit, one second behind the soft one. `oom_killed`
        # is meant to tell them apart and is checked before this runs, but it
        # is not dependable -- `docker inspect` reports OOMKilled false for a
        # genuine cgroup-v2 kill often enough that CI saw it on one run and not
        # the next, from an identical tree. Falling back to CPU unconditionally
        # meant an out-of-memory program was reported as having exceeded a CPU
        # limit it never came close to, which is worse than saying nothing.
        #
        # Elapsed wall clock settles it in one direction only: RLIMIT_CPU
        # measures CPU time, and a process cannot have consumed `cpu_seconds`
        # of it in less than `cpu_seconds` of wall clock. Below that, a CPU
        # kill is impossible and memory is the only remaining explanation this
        # adapter arranges. Above it, either is possible and CPU stays the
        # answer, as before.
        #
        # A signal this adapter did not arrange (an operator running `docker
        # kill` by hand) still cannot be told from these.
        if not cpu_time_reachable:
            return (
                SandboxFailure(
                    kind="resource_limit",
                    message="the program was killed for exceeding its memory limit",
                ),
                SandboxLimit.ADDRESS_SPACE_BYTES,
            )
        return (
            SandboxFailure(
                kind="resource_limit",
                message="the program was killed, most likely by the CPU time hard limit",
            ),
            SandboxLimit.CPU_SECONDS,
        )
    return (
        SandboxFailure(
            kind="terminated", message=f"the container was terminated by signal {received}"
        ),
        None,
    )
