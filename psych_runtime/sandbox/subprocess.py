"""The subprocess ``Sandbox`` adapter: a fresh CPython child per execution.

DESIGN.md §18. This module owns everything
about how one execution becomes one child process: rlimits, environment,
working directory, network denial, teardown, and the framed protocol that
carries host bindings back and forth.

## What each layer of containment actually is

- **``RLIMIT_CORE = (0, 0)``.** Applied first, before any other limit,
  because the limits set after it are the ones that kill: a program that hits
  ``RLIMIT_CPU`` gets ``SIGXCPU``, whose default disposition dumps core, and a
  core file is a large, memory-bearing artifact nobody asked to write.
- **``RLIMIT_CPU``.** Soft limit at ``limits.cpu_seconds``, hard limit one
  second later. The soft limit's default signal, ``SIGXCPU``, terminates the
  process; the hard limit is a backstop for a program that traps ``SIGXCPU``
  and keeps burning CPU regardless. CPU time, not wall time: a program
  blocked on a host binding's reply is not charged for the wait.
- **``RLIMIT_AS``.** The virtual address space cap. On Linux this surfaces
  to the program as an ordinary ``MemoryError`` when an allocation fails,
  not a killed process, because CPython's allocator checks malloc's return
  value. Skipped on any platform other than Linux, Darwin especially: some
  platforms map more into every process at exec than a useful cap would
  allow, and the child would fail to start at all.
- **``RLIMIT_FSIZE``.** Default 10 MiB. CPython installs ``SIG_IGN`` for
  ``SIGXFSZ`` at interpreter startup (the same reason it ignores
  ``SIGPIPE``: so an ordinary file operation reports a catchable error
  instead of killing the interpreter), so exceeding this surfaces as a plain
  ``OSError`` with ``errno.EFBIG``, not a signal death. Counted per open
  file, not as a total across every file the program writes.
- **``RLIMIT_NPROC``.** Default 64. Two things about this limit that are
  easy to get wrong:

  1. **It is a per-real-uid limit, not a per-process-tree one**, and Linux
     exempts uid 0 from it entirely (the kernel's fork-time check skips the
     limit outright for ``INIT_USER``). A sandbox that runs as root and
     relies on this limit for its fork-bomb defence is not defended. That is
     why this adapter drops privileges (see below) whenever it can: without
     that, ``RLIMIT_NPROC`` is decorative.
  2. **It is shared by everything else running as the same uid.** Every
     concurrent execution that drops to the same unprivileged uid (the
     default is always the same uid, "nobody") shares one counter. A limit
     of 64 chosen with one execution in mind can trip early under enough
     concurrent sandboxed executions, or if the host already runs unrelated
     processes as that uid. A consumer running many concurrent sandboxes who
     wants this limit to mean "per execution" should pass distinct
     ``run_as`` uids per worker.

- **Wall clock.** Not a resource limit on the process; enforced by this
  adapter killing the process group directly (see "Teardown" below) once
  ``limits.wall_seconds`` of real time has passed since spawn.

## Dropping privileges

When constructed by a process running as root (``os.geteuid() == 0``, true
of most container entrypoints), this adapter defaults to running every child
as uid/gid 65534 ("nobody"), for the ``RLIMIT_NPROC`` reason above and as
defence in depth generally: a sandboxed program that somehow escapes its
resource caps still only has an unprivileged user's access to the host, not
root's. Privileges drop last, in the child, after every rlimit is set and
after the network-denial attempt (both of which may need capabilities the
dropped-to uid will not have). Pass ``run_as=None`` to disable this (a
consumer whose worker process already runs unprivileged has nothing to
drop), or a specific ``(uid, gid)`` to use a different account, for example a
distinct uid per worker so concurrent executions do not share one
``RLIMIT_NPROC`` counter.

The interpreter this adapter execs must be reachable by the account it drops
to: every directory on the way to ``python_bin`` needs to stay traversable
for that uid. A user-owned virtualenv under a locked-down home directory
usually is not; a system interpreter usually is. A wrong ``python_bin`` for
the chosen ``run_as`` surfaces as ``SandboxSetupError`` at spawn time, not
as a silent fallback to some other interpreter.

## Network denial: what this does and does not guarantee

Unless ``network=True`` is passed to ``run()``, the child attempts
``os.unshare(CLONE_NEWNET)`` on itself before running the model's program.
On success this puts the process in a fresh network namespace with nothing
but loopback: no route to anywhere else exists, which is a real kernel-level
guarantee, not a firewall rule the program might find a way around.

**This can fail, and when it does, this adapter does not fail the whole
execution over it.** ``unshare(CLONE_NEWNET)`` needs ``CAP_NET_ADMIN`` in the
namespace the caller is in; as root that is automatic, but a consumer whose
worker process is *not* root will typically see this fail with
``PermissionError`` on most hosts. When it fails, the child simply runs with
whatever network access its process already had. **This adapter does not
promise network denial as a floor; it promises to attempt it and to report
the truth about whether it held**, which is what
``SandboxResult.network_denied`` is for: the child verifies its own state
after the attempt (a UDP "connect" to an address on TEST-NET-1, RFC 5737,
which raises immediately if there is no route at all, and never actually
reaches anywhere since that block is never routed) and reports what it
found, not what was requested. A consumer that must have network denial as a
hard guarantee, not a best effort, wants ``psych_runtime.sandbox.container`` instead:
``--network=none`` on a container runtime does not depend on the sandboxed
process's own privilege level.

## Teardown

Every child is spawned in its own session (``start_new_session=True``,
equivalent to ``setsid()``), so ``os.killpg`` reaches every process the
child's own program spawned, not just the directly tracked one. This matters
concretely for a fork bomb: killing only the tracked pid would leave every
process it forked still running. Teardown always signals the whole group,
whether the execution finished cleanly, hit a limit, or timed out:
``SIGTERM``, a grace period, then ``SIGKILL`` if anything is still alive.
This runs unconditionally after every execution, not only on failure,
because "one program per execution" (DESIGN.md §18) means nothing from this
child should still be running once ``run()`` returns.

## Wire protocol

One JSON object per line (``psych_runtime.sandbox.protocol``), on a channel
completely separate from the child's stdout and stderr: a socket pair
created before spawn, with the child's end passed through as an inherited
file descriptor. The obvious design fixes that descriptor at a literal fd 3
so the child knows where to look. This adapter does not,
because forcing a specific low fd number in a child spawned through
``subprocess``/``asyncio`` needs a ``dup2`` inside ``preexec_fn``, and
``close_fds`` runs *before* ``preexec_fn`` in CPython's own child-spawn
sequence, which closes a freshly-``dup2``'d low fd unless its number
happens to already be in ``pass_fds``, an easy way to build something that
looks right and silently loses the channel. Instead, the socket keeps
whatever fd number it already has in this process (``pass_fds`` only
exempts it from closing; fork duplicates the fd table verbatim, so the
number carries over unchanged), and that number is handed to the bootstrap
script as ``fd:<n>`` on its command line. What matters for the isolation
goal, a channel distinct from stdio and validated on every inbound frame, is
unaffected by which number it happens to be.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

if sys.platform != "win32":
    # POSIX-only, and imported conditionally rather than at the top because
    # psych_runtime.sandbox.__init__ imports this adapter eagerly: an unconditional
    # import here makes every module that reaches psych_runtime.sandbox.port -- which
    # is psych_runtime.runtime.execute, so the whole runtime -- unimportable on
    # Windows, over a symbol only ever touched inside the child's preexec_fn.
    # The adapter still refuses to construct there; see __init__.
    import resource

from psych_runtime.sandbox._bootstrap import BOOTSTRAP_SOURCE
from psych_runtime.sandbox.port import (
    HostBinding,
    SandboxFailure,
    SandboxLimit,
    SandboxLimits,
    SandboxResult,
    SandboxSetupError,
)
from psych_runtime.sandbox.protocol import DoneFrame, SandboxProtocolError, run_protocol

__all__ = ["SubprocessSandbox"]

# Defaults, and why each number is what it is:
#
# cpu_seconds=10: generous for a short data-munging or API-calling program,
# short enough that a runaway loop does not tie up a worker for long. Real
# work happens in host bindings, which do not count against this at all.
#
# address_space_bytes=512 MiB: room for a CPython interpreter plus a few
# hundred megabytes of working data, well under what a shared host can spare
# for one execution.
#
# file_size_bytes=10 MiB: generous for incidental scratch output (a CSV, a
# small report) while still catching an accidental multi-gigabyte write
# before it fills the host's disk.
#
# process_count=64: CPython's own asyncio machinery in the bootstrap does not
# need threads for this protocol, so this budget is almost entirely the
# model program's own to spend; 64 comfortably covers a program that spawns
# a handful of helper processes while still killing a fork bomb in well
# under a second (measured: a few hundredths of a second against this
# default, see tests/functional/test_sandbox_subprocess.py).
#
# wall_seconds=30: covers a cpu_seconds=10 program that spends most of its
# time waiting on host bindings rather than computing, without leaving a
# hung program occupying a worker for minutes.
_DEFAULT_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)

_GRACE_SECONDS: Final = 3.0
"""Between SIGTERM and SIGKILL at teardown, and the bound on draining stdout
and stderr once the process group has been signalled. Long enough for a
program to run its ``finally`` blocks, short enough that a program ignoring
SIGTERM does not hold the worker."""

_MAX_CAPTURED_BYTES: Final = 1024 * 1024
"""A safety valve on this adapter's own memory, independent of the four
rlimits above: those bound what the *child* can do, not how much of its
output this process is willing to buffer. Not one of ``SandboxLimit``'s
members and not reported as ``limit_hit``; only ``stdout_truncated`` /
``stderr_truncated`` reflect it."""

_NOBODY: Final = (65534, 65534)
"""The conventional "nobody" uid/gid on Linux. Used as the default account to
drop into when this adapter is constructed by a root process; see the module
docstring's "Dropping privileges" section."""


class _Auto:
    """Sentinel distinguishing "decide automatically" from ``run_as=None``.

    ``None`` is a meaningful value here (never drop privileges), so it
    cannot also mean "no value given"; a distinct sentinel type is worth it
    for how much confusion it avoids at call sites that pass ``run_as=None``
    on purpose.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "AUTO"


_AUTO: Final = _Auto()


class SubprocessSandbox:
    """``Sandbox`` over a fresh CPython child process. See the module docstring."""

    def __init__(
        self,
        *,
        python_bin: str | None = None,
        default_limits: SandboxLimits | None = None,
        env_allowlist: Mapping[str, str] | None = None,
        run_as: tuple[int, int] | _Auto | None = _AUTO,
        allow_same_uid: bool = False,
        require_network_denial: bool = False,
    ) -> None:
        """Build an adapter. No process is spawned until ``run()`` is called.

        Args:
            python_bin: absolute path to the interpreter each child execs.
                Resolved once, here, rather than per run: a child spawned
                with a scrubbed ``PATH`` cannot re-resolve a bare name, so
                resolving late would make what runs depend on the host's
                ``PATH`` at call time rather than on this adapter's own
                configuration. Defaults to ``sys.executable``. Must already
                be absolute, or resolvable via the *current* ``PATH``
                (resolved immediately, once); a bare name this process
                cannot find is a configuration error caught here rather than
                surfacing as a confusing failure on the first ``run()``.
            default_limits: used when ``run()`` is not given ``limits``
                explicitly. Defaults to this module's own defaults; see
                their definition above for what each number is and why.
            env_allowlist: extra environment variables the child receives,
                verbatim. Never a starting point of ``os.environ`` minus a
                blocklist: this adapter always builds the child's
                environment from nothing but this mapping plus the handful
                of variables (``HOME``, ``TMPDIR``, ``PATH``,
                ``PYTHONDONTWRITEBYTECODE``) it sets itself, so a credential
                nobody explicitly allowed here never reaches the child no
                matter what gets added to this process's own environment
                later.
            run_as: ``(uid, gid)`` to drop the child to, ``None`` to run it
                as whatever account this process itself runs as, or the
                default, which drops to "nobody" when this process is root
                and does nothing otherwise. See "Dropping privileges" above.
            allow_same_uid: permit a child that runs as the worker's own uid.
                Refused by default, because the environment this adapter
                carefully builds from nothing is readable anyway from
                ``/proc/<ppid>/environ`` by a same-uid process -- Yama's
                ``ptrace_scope`` restricts attaching, not reading -- so the
                scrubbing is not a boundary at all in that configuration.
                Verified on this project's own host: a program reading its
                parent's ``environ`` got the worker's variables back.
            require_network_denial: fail an execution whose ``network=False``
                could not actually be enforced, rather than running it with
                whatever access the process already had.
                ``unshare(CLONE_NEWNET)`` needs ``CAP_NET_ADMIN``, which an
                unprivileged worker does not have, and the child reports
                honestly that denial did not hold -- but nothing read that
                report, so "no network" silently meant "full network". Off by
                default because turning it on makes every unprivileged
                deployment's ``run_code`` fail; on is right wherever the
                denial is being relied on.

        Raises:
            SandboxSetupError: the child would run as the worker's own uid and
                ``allow_same_uid`` was not passed.
            SandboxSetupError: this process runs on Windows, where the
                confinement below is not available at all; or ``python_bin``
                does not resolve to a file this process can see, or resolves
                to something that no longer exists.
        """
        if sys.platform == "win32":
            # Refused here rather than at run(): every mechanism this adapter
            # confines the child with -- setrlimit, start_new_session,
            # os.killpg -- is POSIX-only, so there is no degraded mode worth
            # offering. A sandbox that silently stopped bounding CPU, memory
            # and process count would be worse than no sandbox, because
            # DESIGN.md §18 lets callers run untrusted programs on the
            # strength of it.
            raise SandboxSetupError(
                "SubprocessSandbox requires a POSIX host: it confines the child with "
                "setrlimit, start_new_session and os.killpg, none of which exist on "
                "Windows. Use the container backend, or run on Linux."
            )
        resolved = python_bin or sys.executable
        if not Path(resolved).is_absolute():
            found = shutil.which(resolved)
            if found is None:
                raise SandboxSetupError(
                    f"python_bin {resolved!r} is not an absolute path and is not on this "
                    "process's own PATH either. Resolve it explicitly: a child spawned with "
                    "a scrubbed environment cannot look it up itself."
                )
            resolved = found
        if not Path(resolved).is_file():
            raise SandboxSetupError(f"python_bin {resolved!r} does not exist")
        self._python_bin = resolved
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._env_allowlist = dict(env_allowlist or {})
        self._require_network_denial = require_network_denial
        if isinstance(run_as, _Auto):
            self._run_as: tuple[int, int] | None = _NOBODY if os.geteuid() == 0 else None
        else:
            self._run_as = run_as
        self._same_uid_as_worker = self._run_as is None or self._run_as[0] == os.geteuid()
        if self._same_uid_as_worker and not allow_same_uid:
            raise SandboxSetupError(
                "this SubprocessSandbox would run the model's program as the same uid "
                f"({os.geteuid()}) as the worker itself. A same-uid child can read the "
                "worker's own environment through /proc/<ppid>/environ -- every API key, "
                "DSN and credential the worker holds -- and can signal the worker. "
                "Run the worker as root so the default drop to 'nobody' applies, pass "
                "run_as=(uid, gid) for a dedicated account, or use the container backend. "
                "Pass allow_same_uid=True only for a host where the worker holds nothing "
                "worth reading, such as a test."
            )

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, HostBinding] | None = None,
        limits: SandboxLimits | None = None,
        network: bool = False,
    ) -> SandboxResult:
        bound: dict[str, HostBinding] = dict(bindings or {})
        active_limits = limits or self._default_limits
        started = time.monotonic()

        workdir = Path(tempfile.mkdtemp(prefix="psych-sandbox-"))
        try:
            if self._run_as is not None:
                os.chown(workdir, *self._run_as)
            workdir.chmod(0o700)

            parent_sock, child_sock = socket.socketpair()
            os.set_inheritable(child_sock.fileno(), True)
            child_fd = child_sock.fileno()

            argv = [
                self._python_bin,
                "-I",
                "-B",
                "-u",
                "-c",
                BOOTSTRAP_SOURCE,
                f"fd:{child_fd}",
                *bound.keys(),
            ]
            env = self._build_env(workdir)
            preexec = _make_preexec(active_limits, network=network, run_as=self._run_as)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workdir),
                    env=env,
                    pass_fds=(child_fd,),
                    preexec_fn=preexec,
                    start_new_session=True,
                )
            except OSError as err:
                hint = (
                    " This adapter is configured to drop the child to "
                    f"uid={self._run_as[0]}, gid={self._run_as[1]}; a permission error "
                    "here usually means that account cannot traverse the directories "
                    "leading to python_bin. Pass an interpreter path that account can "
                    "reach, or run_as=None if this process is already unprivileged."
                    if self._run_as is not None
                    else ""
                )
                raise SandboxSetupError(
                    f"could not spawn the sandbox subprocess at {self._python_bin!r}: {err}.{hint}"
                ) from err
            except BaseException:
                # The spawn failed, so _drive never runs and never closes the
                # parent half. Without this the socketpair leaks one descriptor
                # per failed spawn, which is exactly the path a misconfigured
                # python_bin takes on every single call.
                parent_sock.close()
                raise
            finally:
                child_sock.close()

            result = await _drive(
                proc, parent_sock, program, bound, limits=active_limits, started=started
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if not network and self._require_network_denial and not result.network_denied:
            # The child reported honestly that unshare(CLONE_NEWNET) did not
            # hold, so this execution had whatever network access the worker
            # has -- and nothing on the ordinary path reads that report, so
            # "network=False" silently meant "network". A consumer who turned
            # this on is relying on the denial, so the honest answer is a
            # failed execution rather than a successful one that ran with
            # access it was told it would not have.
            return result.model_copy(
                update={
                    "value": None,
                    # The plain reason, not model-facing guidance: `psych_runtime.tools.code`
                    # is where a SandboxFailure becomes text for the model, and it
                    # applies `sandbox_failure_guidance` itself. This layer may not
                    # import that module anyway (psych_runtime.sandbox sits below
                    # psych_runtime.tools; import-linter enforces it).
                    "failure": SandboxFailure(
                        kind="setup",
                        message=(
                            "network access could not be denied for this execution: "
                            "unshare(CLONE_NEWNET) needs CAP_NET_ADMIN, which this worker "
                            "does not have. The program's result was withheld rather than "
                            "returned from an execution that had network access it was "
                            "configured not to have."
                        ),
                    ),
                }
            )
        return result

    def _build_env(self, workdir: Path) -> dict[str, str]:
        env = dict(self._env_allowlist)
        env["HOME"] = str(workdir)
        env["TMPDIR"] = str(workdir)
        env.setdefault("PATH", "/usr/bin:/bin")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env


def _make_preexec(
    limits: SandboxLimits, *, network: bool, run_as: tuple[int, int] | None
) -> Callable[[], None]:
    """Build the ``preexec_fn`` for one spawn: rlimits, network, then privilege drop.

    Runs in the forked child, before ``execve`` replaces its image, which is
    the earliest point any of this can take effect and the last point it can
    happen while the process still has whatever privilege it was spawned
    with. Order matters: rlimits and the network-namespace attempt both may
    need capabilities the dropped-to account will not have, so privilege
    drop is always last.
    """

    def _preexec() -> None:
        _clamp_rlimit(resource.RLIMIT_CORE, 0, 0)

        cpu = max(1, int(limits.cpu_seconds))
        _clamp_rlimit(resource.RLIMIT_CPU, cpu, cpu + 1)

        if sys.platform == "linux":
            _clamp_rlimit(
                resource.RLIMIT_AS, limits.address_space_bytes, limits.address_space_bytes
            )

        _clamp_rlimit(resource.RLIMIT_FSIZE, limits.file_size_bytes, limits.file_size_bytes)
        _clamp_rlimit(resource.RLIMIT_NPROC, limits.process_count, limits.process_count)

        os.umask(0o077)

        if not network and sys.platform == "linux" and hasattr(os, "unshare"):
            with contextlib.suppress(OSError, AttributeError):
                os.unshare(os.CLONE_NEWNET)

        if run_as is not None:
            uid, gid = run_as
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    return _preexec


def _clamp_rlimit(which: int, soft: int, hard: int) -> None:
    """Set ``which`` to the strictest of what was requested and what was inherited.

    A requested limit above what this process already runs under would
    *raise* the effective limit, which is the opposite of containment. Soft
    and hard are clamped independently against the corresponding inherited
    side, and only then reconciled against each other, so a caller cannot
    widen one by asking for an impossible pair.
    """
    current_soft, current_hard = resource.getrlimit(which)
    new_hard = _min_allowing_infinity(hard, current_hard)
    new_soft = _min_allowing_infinity(soft, current_soft)
    new_soft = _min_allowing_infinity(new_soft, new_hard)
    resource.setrlimit(which, (new_soft, new_hard))


def _min_allowing_infinity(a: int, b: int) -> int:
    if a == resource.RLIM_INFINITY:
        return b
    if b == resource.RLIM_INFINITY:
        return a
    return min(a, b)


async def _drive(
    proc: asyncio.subprocess.Process,
    parent_sock: socket.socket,
    program: str,
    bindings: Mapping[str, HostBinding],
    *,
    limits: SandboxLimits,
    started: float,
) -> SandboxResult:
    """Run the protocol conversation, enforce the wall clock, and tear down.

    Teardown (killing the whole process group) always runs before this
    returns, whatever happened during the conversation: a clean ``done``
    frame, a protocol violation, or a timeout. See the module docstring's
    "Teardown" section for why this must not be conditional on failure.
    """
    reader, writer = await asyncio.open_connection(sock=parent_sock)
    stdout_task = asyncio.ensure_future(_read_capped(proc.stdout))
    stderr_task = asyncio.ensure_future(_read_capped(proc.stderr))

    timed_out = False
    protocol_error: SandboxProtocolError | None = None
    network_denied = False
    done: DoneFrame | None = None

    try:
        network_denied, done = await asyncio.wait_for(
            run_protocol(reader, writer, program, bindings), timeout=limits.wall_seconds
        )
    except TimeoutError:
        timed_out = True
    except SandboxProtocolError as err:
        protocol_error = err

    await _terminate_process_group(proc)

    stdout_text, stdout_truncated = await _bounded(stdout_task)
    stderr_text, stderr_truncated = await _bounded(stderr_task)
    # Closing the writer starts the transport teardown but does not finish it,
    # and the socketpair's parent half stays open until it does. Under
    # filterwarnings=error an unclosed socket is a test failure rather than a
    # quiet leak, which is how this was found; in production it is a file
    # descriptor lost per execution, which is worse.
    with contextlib.suppress(OSError):
        writer.close()
    with contextlib.suppress(OSError, ConnectionError):
        await writer.wait_closed()
    with contextlib.suppress(OSError):
        parent_sock.close()
    returncode = await proc.wait()

    failure, limit_hit = _classify(
        returncode=returncode,
        done=done,
        protocol_error=protocol_error,
        timed_out=timed_out,
        wall_seconds=limits.wall_seconds,
    )
    value = done.value if (done is not None and failure is None) else None

    return SandboxResult(
        stdout=stdout_text,
        stderr=stderr_text,
        value=value,
        failure=failure,
        duration_seconds=time.monotonic() - started,
        limit_hit=limit_hit,
        network_denied=network_denied,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def _read_capped(stream: asyncio.StreamReader | None) -> tuple[str, bool]:
    if stream is None:
        return "", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if total >= _MAX_CAPTURED_BYTES:
            truncated = True
            continue
        keep = chunk[: _MAX_CAPTURED_BYTES - total]
        chunks.append(keep)
        total += len(keep)
        if len(keep) < len(chunk):
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


async def _bounded(task: asyncio.Future[tuple[str, bool]]) -> tuple[str, bool]:
    """Wait for a reader task, bounded, after the process group is already dead.

    A fallback, not the normal path: once ``_terminate_process_group`` has
    run, every holder of the stdout/stderr pipes is dead, so these should
    resolve almost immediately. Bounded anyway in case teardown itself could
    not reach something (a process this account has no permission to
    signal, in a misconfigured deployment).
    """
    try:
        return await asyncio.wait_for(task, timeout=_GRACE_SECONDS)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return "", True


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the whole process group, then SIGKILL after a grace period.

    Aimed at the group (``os.killpg``), not just the tracked pid, because a
    process this tracked one forked (deliberately, or as a fork bomb) is in
    the same group by default and must die too. Idempotent: called even when
    the process has already exited cleanly, in which case there is nothing
    to signal and this returns immediately.
    """
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
    *,
    returncode: int | None,
    done: DoneFrame | None,
    protocol_error: SandboxProtocolError | None,
    timed_out: bool,
    wall_seconds: float,
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    """Turn what happened into a failure (or none) and which limit, if any."""
    if timed_out:
        return (
            SandboxFailure(
                kind="timeout",
                message=f"execution exceeded its {wall_seconds}s wall-clock limit",
            ),
            SandboxLimit.WALL_SECONDS,
        )

    if done is not None:
        if done.error_kind is None:
            return None, None
        limit = None
        if done.resource_limit is not None:
            with contextlib.suppress(ValueError):
                limit = SandboxLimit(done.resource_limit)
        return (
            SandboxFailure(
                kind=done.error_kind,
                message=done.error_message or "the program failed",
                traceback=done.error_traceback,
            ),
            limit,
        )

    if returncode is not None and returncode < 0:
        return _classify_signal(-returncode)

    if protocol_error is not None:
        return SandboxFailure(kind="protocol_violation", message=str(protocol_error)), None

    return (
        SandboxFailure(
            kind="protocol_violation",
            message="the sandboxed process ended without completing the protocol",
        ),
        None,
    )


def _classify_signal(received: int) -> tuple[SandboxFailure, SandboxLimit | None]:
    """The process died from a signal before ever sending a ``done`` frame."""
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
        # The only other hard kill this adapter installs is RLIMIT_CPU's hard
        # limit, one second behind the soft one, for a program that traps
        # SIGXCPU and keeps running. A SIGKILL not attributable to our own
        # timeout is reported as that; a host-level out-of-memory kill
        # unrelated to this execution's own limits is the one thing this
        # cannot tell apart from it.
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
