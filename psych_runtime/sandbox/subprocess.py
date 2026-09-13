"""The subprocess ``Sandbox`` adapter: a fresh CPython child per execution.

DESIGN.md §18. This module owns everything about how one execution becomes
one child process on a POSIX host: rlimits, environment, working directory,
network denial, teardown, and the framed protocol that carries host bindings
back and forth. On Windows the equivalent is ``psych_runtime.sandbox.windows``,
and ``psych_runtime.sandbox.local`` picks between them.

## What this backend is, and is not

It is ``IsolationLevel.PROCESS``: a process boundary with kernel-enforced
resource limits, a scrubbed environment, a temporary working directory and
whole-tree teardown. It is **not** ``ISOLATED``: the child shares the host's
filesystem view (it can read whatever the account it runs as can read), and
network denial is attempted rather than guaranteed. Every result grades
itself honestly on both (``SandboxResult.guarantees``, checked from inside
the child), and an execution that asked for ``ISOLATED`` is refused with its
output withheld rather than run at this level. For a kernel boundary use
``psych_runtime.sandbox.namespaces`` (Linux, bubblewrap) or
``psych_runtime.sandbox.container``.

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
  allow, and the child would fail to start at all. Reported as
  ``memory: unavailable`` there, which is why this backend never reaches
  ``ISOLATED`` on macOS whatever else it does.
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
     that, ``RLIMIT_NPROC`` is decorative, and the guarantees say so.
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

Unless ``network=True`` is passed to ``run()``, the child attempts to put
itself in a fresh network namespace before running the model's program:
``unshare(CLONE_NEWNET)`` first, which needs ``CAP_NET_ADMIN`` and so works
as root, then ``unshare(CLONE_NEWUSER | CLONE_NEWNET)``, which an
unprivileged process may do on a kernel that allows unprivileged user
namespaces. On success there is no route to anywhere but loopback, which is
a real kernel-level guarantee, not a firewall rule the program might find a
way around.

**This can fail, and when it does, this adapter does not fail the whole
execution over it** unless told to. Many hosts disable unprivileged user
namespaces; there the child simply runs with whatever network access its
process already had. **This adapter does not promise network denial as a
floor; it promises to attempt it and to report the truth about whether it
held**, which is what ``SandboxResult.network_denied`` and
``SandboxResult.guarantees.network`` are for: the child verifies its own
state after the attempt (a UDP "connect" to an address on TEST-NET-1, RFC
5737, which raises immediately if there is no route at all, and never
actually reaches anywhere since that block is never routed) and reports what
it found, not what was requested. ``require_network_denial=True`` refuses to
return a result from an execution where denial did not hold, and an
execution that asked for ``ISOLATED`` is refused the same way.

On macOS, ``seatbelt`` (below) is the mechanism instead.

## macOS: the seatbelt profile, when it is available

macOS has no namespaces, but it does have a kernel-enforced sandbox reached
through ``sandbox-exec``, an interface the vendor has marked deprecated and
still ships and enforces. When ``seatbelt`` is on (the default is to probe
for it), the child is launched under a generated profile that denies
network access, denies writes outside the workspace and the system
temporary directories, and denies reads of home directories and of the
canary. Reads of system files stay allowed: the profile language cannot
allow a subpath without first allowing reads generally, so a fully private
root is not something this mechanism can express.

The probe at construction runs a trivial program under the profile; if that
fails (the interface removed, the profile rejected), seatbelt is off and the
description says so. The child's own ``ready`` frame is what grades the
execution either way, so a profile that silently stopped applying would
show up as ``network: unavailable`` on the next result rather than as a
promise nobody checked.

## Teardown

Every child is spawned in its own session (``start_new_session=True``,
equivalent to ``setsid()``), so ``os.killpg`` reaches every process the
child's own program spawned, not just the directly tracked one. This matters
concretely for a fork bomb: killing only the tracked pid would leave every
process it forked still running. Teardown always signals the whole group,
whether the execution finished cleanly, hit a limit, timed out or was
cancelled: ``SIGTERM``, a grace period, then ``SIGKILL`` if anything is still
alive. This runs unconditionally after every execution, not only on failure,
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
import subprocess
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

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox._bootstrap import BOOTSTRAP_SOURCE, CANARY_ENV
from psych_runtime.sandbox._local import (
    DEFAULT_CAPTURE,
    Canary,
    Captured,
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

__all__ = ["SubprocessSandbox", "classify_done"]

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

_NOBODY: Final = (65534, 65534)
"""The conventional "nobody" uid/gid on Linux. Used as the default account to
drop into when this adapter is constructed by a root process; see the module
docstring's "Dropping privileges" section."""

_PROBE_LIMITS: Final = SandboxLimits(
    cpu_seconds=2.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=10.0,
)
"""What ``describe()``'s probe runs under: small, because it runs ``return 0``."""


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
        seatbelt: bool | None = None,
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
                whatever access the process already had. Off by default
                because turning it on makes every unprivileged deployment's
                ``run_code`` fail on a host without user namespaces; on is
                right wherever the denial is being relied on. An execution
                requesting ``IsolationLevel.ISOLATED`` is always refused
                here regardless, since this backend cannot reach it.
            seatbelt: macOS only. ``None`` probes for ``sandbox-exec`` and
                uses it when the probe passes; ``True`` requires it (setup
                fails otherwise); ``False`` never uses it. Ignored elsewhere.

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
            # offering. Windows has its own adapter with its own mechanisms.
            raise SandboxSetupError(
                "SubprocessSandbox requires a POSIX host: it confines the child with "
                "setrlimit, start_new_session and os.killpg, none of which exist on "
                "Windows. Use psych_runtime.sandbox.windows.WindowsJobSandbox, or "
                "psych_runtime.sandbox.local.local_sandbox() to pick the right one."
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
        self._seatbelt_bin: str | None = None
        self._seatbelt_note: str | None = None
        if sys.platform == "darwin" and seatbelt is not False:
            self._seatbelt_bin = _probe_seatbelt(self._python_bin)
            if self._seatbelt_bin is None:
                if seatbelt:
                    raise SandboxSetupError(
                        "seatbelt=True but sandbox-exec is not usable on this host: the "
                        "probe under a generated profile did not run"
                    )
                self._seatbelt_note = (
                    "sandbox-exec is not usable here, so no filesystem or network "
                    "restriction applies on macOS"
                )

    # -- capability report ----------------------------------------------------

    async def describe(self) -> SandboxDescription:
        """What this backend can promise here, from a probe execution.

        Runs ``return 0`` under the smallest limits and reads the child's
        own ``ready`` frame, so ``network`` and ``filesystem`` are observed
        rather than predicted. A probe that cannot spawn is ``ready=False``
        with the reason.
        """
        problems: list[str] = []
        notes: list[str] = []
        if self._seatbelt_note:
            notes.append(self._seatbelt_note)
        if self._run_as is None:
            notes.append(
                "the program runs as the worker's own account; RLIMIT_NPROC is shared "
                "with everything else running as it"
            )
        try:
            probe = await self.run("return 0", limits=_PROBE_LIMITS)
        except SandboxSetupError as err:
            probe = None
            problems.append(str(err))
        if probe is not None and probe.failure is not None:
            problems.append(f"the probe program failed: {probe.failure.message}")
        guarantees = probe.guarantees if probe is not None else self._predicted_guarantees()
        mechanisms = ["rlimit", "setsid", "scrubbed_env"]
        if guarantees.network is Enforcement.ENFORCED and sys.platform == "linux":
            mechanisms.append("netns")
        if self._seatbelt_bin is not None:
            mechanisms.append("seatbelt")
        if self._run_as is not None:
            mechanisms.append("setuid")
        return SandboxDescription(
            backend="subprocess",
            platform=sys.platform,
            isolation=achieved_level(guarantees, network_required=True),
            guarantees=guarantees,
            mechanisms=tuple(mechanisms),
            network_grant_supported=True,
            artifacts_supported=True,
            ready=not problems,
            problems=tuple(problems),
            notes=tuple(notes),
        )

    def _predicted_guarantees(self) -> SandboxGuarantees:
        return _grade(
            ReadyFrame(network_denied=False, canary_readable=None, uid=None),
            run_as=self._run_as,
            network_granted=False,
            seatbelt=self._seatbelt_bin is not None,
        )

    # -- one execution ---------------------------------------------------------

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
            return _cancelled_before_start(started)

        workdir = Path(tempfile.mkdtemp(prefix="psych-sandbox-"))
        canary = Canary()
        try:
            if self._run_as is not None:
                os.chown(workdir, *self._run_as)
            workdir.chmod(0o700)

            parent_sock, child_sock = socket.socketpair()
            os.set_inheritable(child_sock.fileno(), True)
            child_fd = child_sock.fileno()

            argv = self._argv(child_fd, workdir=workdir, canary=canary, network=network)
            env = self._build_env(workdir, canary)
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
                proc,
                parent_sock,
                program,
                bound,
                limits=active_limits,
                capture=active_capture,
                started=started,
                cancel=cancel,
                workdir=workdir,
                run_as=self._run_as,
                network=network,
                seatbelt=self._seatbelt_bin is not None,
                isolation=isolation,
            )
        finally:
            canary.close()
            remove_tree(workdir)

        if not network and self._require_network_denial and not result.network_denied:
            # The child reported honestly that network denial did not hold,
            # so this execution had whatever network access the worker has. A
            # consumer who turned this on is relying on the denial, so the
            # honest answer is a failed execution rather than a successful one
            # that ran with access it was told it would not have.
            return result.model_copy(
                update={
                    "value": None,
                    "failure": SandboxFailure(
                        kind="setup",
                        message=(
                            "network access could not be denied for this execution: no "
                            "network namespace could be created for the child on this "
                            "host. The program's result was withheld rather than returned "
                            "from an execution that had network access it was configured "
                            "not to have."
                        ),
                    ),
                }
            )
        return withhold_if_weaker(result, isolation, backend="subprocess")

    def _argv(
        self,
        child_fd: int,
        *,
        workdir: Path,
        canary: Canary,
        network: bool,
    ) -> list[str]:
        # The binding names travel in the run frame, not here: a command
        # line has a length limit and a tool catalogue does not.
        python = [self._python_bin, "-I", "-B", "-u", "-c", BOOTSTRAP_SOURCE, f"fd:{child_fd}"]
        if self._seatbelt_bin is None:
            return python
        profile = _seatbelt_profile(workdir, canary.path.parent, network=network)
        return [self._seatbelt_bin, "-p", profile, *python]

    def _build_env(self, workdir: Path, canary: Canary) -> dict[str, str]:
        env = dict(self._env_allowlist)
        env["HOME"] = str(workdir)
        env["TMPDIR"] = str(workdir)
        env.setdefault("PATH", "/usr/bin:/bin")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env[CANARY_ENV] = str(canary.path)
        return env


# ---------------------------------------------------------------------------
# The child's own confinement, applied between fork and exec
# ---------------------------------------------------------------------------


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
            _try_network_namespace()

        if run_as is not None:
            uid, gid = run_as
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    return _preexec


def _try_network_namespace() -> None:
    """A fresh network namespace, as root or through an unprivileged user
    namespace. Failure is silent here; the child's own probe reports it."""
    try:
        os.unshare(os.CLONE_NEWNET)
    except OSError:
        with contextlib.suppress(OSError, AttributeError):
            os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)


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


# ---------------------------------------------------------------------------
# macOS seatbelt
# ---------------------------------------------------------------------------


def _sbpl_string(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt_profile(workdir: Path, canary_dir: Path, *, network: bool) -> str:
    """A deny-by-default profile that still lets CPython start.

    Reads are allowed generally (the profile language cannot allow a subpath
    without that), then home directories and the canary are denied; writes
    are allowed only under the workspace and the system temporary roots;
    network is denied unless granted. Real paths, because the sandbox
    matches on the resolved path and ``/tmp`` is a link on macOS.
    """
    work = Path(os.path.realpath(workdir))
    canary = Path(os.path.realpath(canary_dir))
    lines = [
        "(version 1)",
        '(deny default (with message "psych-sandbox"))',
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow process-info* (target same-sandbox))",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm)",
        "(allow ipc-posix-sem)",
        "(allow user-preference-read)",
        '(allow file-ioctl (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/random") (literal "/dev/urandom") (literal "/dev/tty"))',
        '(allow file-write-data (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/random") (literal "/dev/urandom") (literal "/dev/tty"))',
        "(allow file-read*)",
        '(deny file-read* (subpath "/Users"))',
        '(deny file-read* (subpath "/private/var/root"))',
        f"(deny file-read* (subpath {_sbpl_string(canary)}))",
        f"(allow file-read* (subpath {_sbpl_string(work)}))",
        f"(allow file-write* (subpath {_sbpl_string(work)}))",
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/private/var/folders"))',
        "(allow network*)" if network else '(deny network* (with message "psych-sandbox"))',
    ]
    return "\n".join(lines)


def _probe_seatbelt(python_bin: str) -> str | None:
    """``sandbox-exec``'s path if a generated profile runs a program under it."""
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return None
    probe_dir = Path(tempfile.mkdtemp(prefix="psych-seatbelt-probe-"))
    try:
        profile = _seatbelt_profile(probe_dir, probe_dir / "canary", network=False)
        try:
            completed = subprocess.run(
                [binary, "-p", profile, python_bin, "-I", "-c", "import os, socket, json"],
                capture_output=True,
                timeout=15,
                check=False,
                cwd=str(probe_dir),
                env={"HOME": str(probe_dir), "TMPDIR": str(probe_dir), "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return binary if completed.returncode == 0 else None
    finally:
        remove_tree(probe_dir)


# ---------------------------------------------------------------------------
# Driving one execution to a result
# ---------------------------------------------------------------------------


def _cancelled_before_start(started: float) -> SandboxResult:
    return SandboxResult(
        duration_seconds=time.monotonic() - started,
        failure=SandboxFailure(
            kind="cancelled", message="the execution was cancelled before the program started"
        ),
        cancelled=True,
    )


async def _drive(
    proc: asyncio.subprocess.Process,
    parent_sock: socket.socket,
    program: str,
    bindings: Mapping[str, HostBinding],
    *,
    limits: SandboxLimits,
    capture: OutputCapture,
    started: float,
    cancel: asyncio.Event | None,
    workdir: Path,
    run_as: tuple[int, int] | None,
    network: bool,
    seatbelt: bool,
    isolation: IsolationLevel | None,
) -> SandboxResult:
    """Run the protocol conversation, enforce the wall clock, and tear down.

    Teardown (killing the whole process group) always runs before this
    returns, whatever happened during the conversation: a clean ``done``
    frame, a protocol violation, a timeout, a cancellation, or this task
    itself being cancelled from outside. See the module docstring's
    "Teardown" section for why this must not be conditional on failure.
    """
    reader, writer = await asyncio.open_connection(sock=parent_sock)
    stdout_task = asyncio.ensure_future(read_capped(proc.stdout, capture.stream_bytes))
    stderr_task = asyncio.ensure_future(read_capped(proc.stderr, capture.stream_bytes))

    try:
        talk = await converse(
            reader,
            writer,
            program,
            bindings,
            wall_seconds=limits.wall_seconds,
            cancel=cancel,
            # Before the program is sent, not after it has run: see
            # `psych_runtime.sandbox._local.admission`.
            admit=admission(
                lambda ready: _grade(
                    ready, run_as=run_as, network_granted=network, seatbelt=seatbelt
                ),
                isolation,
                network_required=not network,
                backend="subprocess",
            ),
        )
    except asyncio.CancelledError:
        # The caller's task was cancelled (a Worker shutting down, an abort).
        # Nothing from the child may outlive that either.
        await _terminate_process_group(proc)
        raise
    finally:
        await _terminate_process_group(proc)

    stdout = await bounded(stdout_task)
    stderr = await bounded(stderr_task)
    await _close_channel(writer, parent_sock)
    returncode = await proc.wait()

    failure, limit_hit = _classify(
        returncode=returncode,
        done=talk.done,
        protocol_error=talk.protocol_error,
        timed_out=talk.timed_out,
        cancelled=talk.cancelled,
        wall_seconds=limits.wall_seconds,
    )
    value = talk.done.value if (talk.done is not None and failure is None) else None
    artifacts, omitted = collect_artifacts(workdir, capture) if failure is None else ((), 0)

    observed = talk.ready if talk.ready is not None else ReadyFrame(network_denied=False)
    guarantees = _grade(observed, run_as=run_as, network_granted=network, seatbelt=seatbelt)
    return _result(
        stdout,
        stderr,
        value=value,
        failure=failure,
        started=started,
        limit_hit=limit_hit,
        guarantees=guarantees,
        network_denied=observed.network_denied,
        network_granted=network,
        artifacts=artifacts,
        artifacts_omitted=omitted,
        cancelled=talk.cancelled,
    )


async def _close_channel(writer: asyncio.StreamWriter, parent_sock: socket.socket) -> None:
    """Close the protocol channel completely.

    Closing the writer starts the transport teardown but does not finish it,
    and the socketpair's parent half stays open until it does. Under
    ``filterwarnings=error`` an unclosed socket is a test failure rather than
    a quiet leak, which is how this was found; in production it is a file
    descriptor lost per execution, which is worse.
    """
    with contextlib.suppress(OSError):
        writer.close()
    with contextlib.suppress(OSError, ConnectionError):
        await writer.wait_closed()
    with contextlib.suppress(OSError):
        parent_sock.close()


def _result(
    stdout: Captured,
    stderr: Captured,
    *,
    value: object,
    failure: SandboxFailure | None,
    started: float,
    limit_hit: SandboxLimit | None,
    guarantees: SandboxGuarantees,
    network_denied: bool,
    network_granted: bool,
    artifacts: tuple[object, ...],
    artifacts_omitted: int,
    cancelled: bool,
) -> SandboxResult:
    return SandboxResult(
        stdout=stdout.data.decode("utf-8", errors="replace"),
        stderr=stderr.data.decode("utf-8", errors="replace"),
        value=value,
        failure=failure,
        duration_seconds=time.monotonic() - started,
        limit_hit=limit_hit,
        network_denied=network_denied and not network_granted,
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        stdout_data=stdout.data,
        stderr_data=stderr.data,
        stdout_size=stdout.observed,
        stderr_size=stderr.observed,
        isolation=achieved_level(guarantees, network_required=not network_granted),
        guarantees=guarantees,
        artifacts=artifacts,  # type: ignore[arg-type]
        artifacts_omitted=artifacts_omitted,
        cancelled=cancelled,
    )


def _grade(
    ready: ReadyFrame, *, run_as: tuple[int, int] | None, network_granted: bool, seatbelt: bool
) -> SandboxGuarantees:
    """What this execution's guarantees were worth, from what the child saw."""
    enforced, unavailable, unverified = (
        Enforcement.ENFORCED,
        Enforcement.UNAVAILABLE,
        Enforcement.UNVERIFIED,
    )
    network = unavailable
    if not network_granted and ready.network_denied:
        network = enforced
    # A readable canary is proof of exposure; an unreadable one is not proof
    # of confinement. Under seatbelt the profile allows reads generally and
    # then denies specific subtrees, so failing to read one deliberately
    # denied file says that denial worked and nothing about the rest of the
    # filesystem -- which is most of it. That is `unverified`: a mechanism is
    # in place and this execution did not contradict it. Without seatbelt
    # there is no filesystem mechanism at all, so an unreadable canary is
    # still `unavailable` rather than a guarantee.
    filesystem = unverified if seatbelt else unavailable
    if ready.canary_readable:
        filesystem = unavailable
    if run_as is None:
        identity = unavailable
    elif ready.uid is None:
        identity = unverified
    else:
        identity = enforced if ready.uid == run_as[0] else unavailable
    child_is_root = ready.uid == 0 or (ready.uid is None and run_as is None and os.geteuid() == 0)
    return SandboxGuarantees(
        filesystem=filesystem,
        network=network,
        process_tree=enforced,
        identity=identity,
        cpu=enforced,
        memory=enforced if sys.platform == "linux" else unavailable,
        file_size=enforced,
        process_count=unavailable if child_is_root else enforced,
        wall_clock=enforced,
        environment=enforced,
    )


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
    cancelled: bool,
    wall_seconds: float,
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    """Turn what happened into a failure (or none) and which limit, if any."""
    if cancelled:
        return (
            SandboxFailure(
                kind="cancelled",
                message="the execution was cancelled and its process tree was ended",
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

    if done is not None:
        return classify_done(done)

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
