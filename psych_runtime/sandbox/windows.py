"""The Windows ``Sandbox`` adapter: a fresh CPython child inside a job object.

DESIGN.md §18. Windows has no ``setrlimit``, no process groups a signal can
reach, and no ``unshare``. What it does have is the **job object**: a kernel
object a process tree is assigned to, whose limits the kernel enforces on
every process in the tree and whose closure kills all of them at once. This
adapter builds one per execution and everything below rests on it.

## What this backend is, and is not

It is ``IsolationLevel.PROCESS``, for trusted code. The child runs as the
worker's own account with the worker's own view of the filesystem, and
nothing here denies it a network route. Every result says so
(``SandboxResult.guarantees``: ``filesystem``, ``network`` and ``identity``
are ``unavailable``, and the child confirms the first two from inside), and
an execution that asked for ``ISOLATED`` is refused with its output withheld
rather than run at this level. ``ISOLATED`` on a Windows host means a
container (``psych_runtime.sandbox.container`` on a Linux worker, or Docker
Desktop reached through ``psych_runtime.sandbox.remote``) or a remote service.

## What each layer of containment actually is, and who backs it

- **Process tree.** ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when the last
  handle to the job closes, every process in it is terminated by the kernel.
  Teardown calls ``TerminateJobObject`` explicitly and then closes the
  handle, so a clean finish, a timeout, a cancellation and a worker that
  dies mid-execution all end the same way: nothing the program started
  survives. A child cannot leave the job (``JOB_OBJECT_LIMIT_BREAKAWAY_OK``
  is never set), so grandchildren and detached processes are in it too.
- **Memory.** ``JOB_OBJECT_LIMIT_JOB_MEMORY`` caps committed memory across
  the whole tree at ``limits.address_space_bytes``. A commit past it fails,
  which CPython reports as an ordinary ``MemoryError`` in the program, the
  same shape ``RLIMIT_AS`` gives on Linux.
- **Process count.** ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` at
  ``limits.process_count``. A ``CreateProcess`` past it fails, which the
  program sees as an ``OSError``. Per job, not per user, so concurrent
  executions never share a counter.
- **CPU time.** ``JOB_OBJECT_LIMIT_JOB_TIME`` at ``limits.cpu_seconds`` of
  user-mode time summed across the tree. When it is exceeded the kernel
  terminates every process in the job; the adapter reads the job's
  accounting afterwards to attribute the kill to the CPU limit rather than
  guess from an exit code.
- **Wall clock.** Enforced by this adapter terminating the job.
- **User interface.** ``JOB_OBJECT_UILIMIT_*`` restrictions stop the tree
  reaching the desktop, the clipboard, global atoms, system parameters and
  ``ExitWindows``: a program cannot log the user out or read what they
  copied.
- **File size.** Windows has no per-process file-size cap. Reported
  ``unavailable``; the wall clock, the memory cap and the workspace's own
  removal are what bound a runaway writer.
- **Environment.** Built from the allowlist plus the handful of variables
  CPython needs to start on Windows (``SYSTEMROOT``, a ``PATH`` holding the
  interpreter's directory, ``TEMP``/``TMP``/``USERPROFILE`` pointing at the
  workspace). Never ``os.environ`` minus a blocklist.
- **Workspace.** A fresh directory per execution, removed afterwards by a
  deletion that never follows a junction or symbolic link out of it, so a
  program that plants a link to the user's profile cannot have the profile
  emptied by cleanup. Collected artifacts skip every reparse point.

## The race that is not one

A process cannot be created directly inside a job through ``subprocess``, so
the child is spawned first and assigned to the job by pid immediately after.
Between those two calls the child is not yet contained -- but the child is
this project's own bootstrap, which does nothing until it receives the
``run`` frame, and the adapter sends that frame only after the assignment
succeeded. The model's program never executes an instruction outside the
job. An assignment that fails aborts the execution before the program is
sent.

## Argument handling

The bootstrap is written to a private file beside the workspace (not inside
it, where the program could overwrite it) and run as ``python -I -B -u
<file>``, rather than passed through ``-c``: a multi-kilobyte script with
quotes and newlines survives ``CreateProcess``'s single command-line string
far more reliably as a path than as an argument. Binding names are plain
identifiers by construction and pass through ``subprocess``'s own quoting.

## Wire protocol

Windows sockets are handles, not descriptors, and inheriting one through
``subprocess`` is fragile, so the channel is a loopback TCP listener bound to
``127.0.0.1`` on an ephemeral port. The child's first line on the connection
is a random token the adapter generated for this execution; the listener
accepts exactly one connection, and drops it unless the token matches, so a
local process guessing at the port gets nothing. Everything after the token
is the same framed protocol every other adapter speaks.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final

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

__all__ = ["WindowsJobSandbox"]

_DEFAULT_LIMITS: Final = SandboxLimits(
    cpu_seconds=10.0,
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
"""Identical to the subprocess adapter's defaults, for the same reasons."""

_GRACE_SECONDS: Final = 3.0
_CONNECT_TIMEOUT_SECONDS: Final = 20.0
_PROBE_LIMITS: Final = SandboxLimits(
    cpu_seconds=2.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=10.0,
)

# Job object constants, from the Windows SDK headers.
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS: Final = 0x00000008
_JOB_OBJECT_LIMIT_JOB_TIME: Final = 0x00000004
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: Final = 0x00000400
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_OBJECT_LIMIT_JOB_MEMORY: Final = 0x00000200
_JOB_OBJECT_UILIMIT_DESKTOP: Final = 0x00000040
_JOB_OBJECT_UILIMIT_DISPLAYSETTINGS: Final = 0x00000010
_JOB_OBJECT_UILIMIT_EXITWINDOWS: Final = 0x00000080
_JOB_OBJECT_UILIMIT_GLOBALATOMS: Final = 0x00000020
_JOB_OBJECT_UILIMIT_HANDLES: Final = 0x00000001
_JOB_OBJECT_UILIMIT_READCLIPBOARD: Final = 0x00000002
_JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS: Final = 0x00000008
_JOB_OBJECT_UILIMIT_WRITECLIPBOARD: Final = 0x00000004
_JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS: Final = 4
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS: Final = 1
_PROCESS_SET_QUOTA: Final = 0x0100
_PROCESS_TERMINATE: Final = 0x0001
_TERMINATED_BY_HOST: Final = 0xC000013A  # STATUS_CONTROL_C_EXIT, "ended from outside"


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicUiRestrictions(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _is_windows() -> bool:
    return sys.platform == "win32"


def _kernel32() -> Any:
    if not _is_windows():  # pragma: no cover - guarded by the constructor
        raise SandboxSetupError("WindowsJobSandbox is only available on Windows")
    # Reached through getattr so the module type-checks on every platform:
    # the Windows-only names are absent from ctypes' stubs elsewhere.
    win_dll = getattr(ctypes, "WinDLL")  # noqa: B009 - absent from POSIX stubs
    return win_dll("kernel32", use_last_error=True)


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


class _Job:
    """One job object: created, limited, assigned, terminated, closed."""

    def __init__(self, limits: SandboxLimits) -> None:
        self._k32 = _kernel32()
        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            raise SandboxSetupError(
                f"CreateJobObject failed: error {_last_error()}. The worker "
                "cannot create job objects on this host."
            )
        self.handle = wintypes.HANDLE(handle)
        self._closed = False
        try:
            self._apply_limits(limits)
        except BaseException:
            self.close()
            raise

    def _apply_limits(self, limits: SandboxLimits) -> None:
        info = _ExtendedLimitInformation()
        basic = info.BasicLimitInformation
        basic.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_TIME
        )
        basic.ActiveProcessLimit = int(limits.process_count)
        # 100-nanosecond units, summed over every process in the job.
        basic.PerJobUserTimeLimit = int(limits.cpu_seconds * 10_000_000)
        info.JobMemoryLimit = int(limits.address_space_bytes)
        ok = self._k32.SetInformationJobObject(
            self.handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise SandboxSetupError(
                f"SetInformationJobObject (limits) failed: error {_last_error()}"
            )
        ui = _BasicUiRestrictions()
        ui.UIRestrictionsClass = (
            _JOB_OBJECT_UILIMIT_DESKTOP
            | _JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
            | _JOB_OBJECT_UILIMIT_EXITWINDOWS
            | _JOB_OBJECT_UILIMIT_GLOBALATOMS
            | _JOB_OBJECT_UILIMIT_HANDLES
            | _JOB_OBJECT_UILIMIT_READCLIPBOARD
            | _JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
            | _JOB_OBJECT_UILIMIT_WRITECLIPBOARD
        )
        ok = self._k32.SetInformationJobObject(
            self.handle,
            _JOB_OBJECT_BASIC_UI_RESTRICTIONS_CLASS,
            ctypes.byref(ui),
            ctypes.sizeof(ui),
        )
        if not ok:
            raise SandboxSetupError(
                f"SetInformationJobObject (UI restrictions) failed: error {_last_error()}"
            )

    def assign(self, pid: int) -> None:
        process = self._k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise SandboxSetupError(
                f"OpenProcess({pid}) failed: error {_last_error()}; the child "
                "could not be placed in its job object"
            )
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process)):
                raise SandboxSetupError(
                    f"AssignProcessToJobObject failed: error {_last_error()}. "
                    "The worker itself may be in a job that forbids nesting; the child "
                    "was not run."
                )
        finally:
            self._k32.CloseHandle(wintypes.HANDLE(process))

    def user_time_seconds(self) -> float | None:
        info = _BasicAccountingInformation()
        ok = self._k32.QueryInformationJobObject(
            self.handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            return None
        return int(info.TotalUserTime) / 10_000_000

    def terminate(self) -> None:
        if not self._closed:
            self._k32.TerminateJobObject(self.handle, _TERMINATED_BY_HOST)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._k32.CloseHandle(self.handle)


class WindowsJobSandbox:
    """``Sandbox`` over a CPython child in a job object. See the module docstring."""

    _python_bin: Path
    _default_limits: SandboxLimits
    _env_allowlist: dict[str, str]

    def __init__(
        self,
        *,
        python_bin: str | None = None,
        default_limits: SandboxLimits | None = None,
        env_allowlist: Mapping[str, str] | None = None,
    ) -> None:
        """Build an adapter. No process is spawned until ``run()`` is called.

        Args:
            python_bin: the interpreter each child runs. Defaults to
                ``sys.executable``. Must exist; resolved once, here.
            default_limits: used when ``run()`` is not given ``limits``.
            env_allowlist: extra environment variables the child receives,
                verbatim, on top of the few CPython needs to start.

        Raises:
            SandboxSetupError: not Windows, or ``python_bin`` does not exist.
        """
        if not _is_windows():
            raise SandboxSetupError(
                "WindowsJobSandbox requires Windows: it confines the child with a job "
                "object. Use psych_runtime.sandbox.subprocess.SubprocessSandbox on a POSIX "
                "host, or psych_runtime.sandbox.local.local_sandbox() to pick the right one."
            )
        resolved = Path(python_bin or sys.executable)
        if not resolved.is_file():
            raise SandboxSetupError(f"python_bin {str(resolved)!r} does not exist")
        self._python_bin = resolved
        self._default_limits = default_limits or _DEFAULT_LIMITS
        self._env_allowlist = dict(env_allowlist or {})

    async def describe(self) -> SandboxDescription:
        problems: list[str] = []
        try:
            probe = await self.run("return 0", limits=_PROBE_LIMITS)
        except SandboxSetupError as err:
            probe = None
            problems.append(str(err))
        if probe is not None and probe.failure is not None:
            problems.append(f"the probe program failed: {probe.failure.message}")
        guarantees = (
            probe.guarantees
            if probe is not None
            else _grade(ReadyFrame(network_denied=False), network_granted=False)
        )
        return SandboxDescription(
            backend="windows-job",
            platform="win32",
            isolation=achieved_level(guarantees, network_required=True),
            guarantees=guarantees,
            mechanisms=("job_object", "ui_restrictions", "scrubbed_env"),
            network_grant_supported=True,
            artifacts_supported=True,
            ready=not problems,
            problems=tuple(problems),
            notes=(
                "the program runs as the worker's own account and sees its files; "
                "for untrusted code use a container or a remote sandbox",
                "no per-file size cap exists on Windows; file_size is unavailable",
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

        root = Path(tempfile.mkdtemp(prefix="psych-sandbox-"))
        workdir = root / "workspace"
        workdir.mkdir()
        bootstrap = root / "bootstrap.py"
        bootstrap.write_text(BOOTSTRAP_SOURCE, encoding="utf-8")
        canary = Canary()
        job: _Job | None = None
        try:
            job = _Job(active_limits)
            token = secrets.token_hex(16)
            server, connected, accepted = await _listen_for_one_connection(token)
            try:
                port = server.sockets[0].getsockname()[1]
                argv = [
                    str(self._python_bin),
                    "-I",
                    "-B",
                    "-u",
                    str(bootstrap),
                    f"tcp:127.0.0.1:{port}:{token}",
                ]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=str(workdir),
                        env=self._build_env(workdir, canary),
                        creationflags=_creation_flags(),
                    )
                except OSError as err:
                    raise SandboxSetupError(
                        f"could not spawn the sandbox process at {str(self._python_bin)!r}: {err}"
                    ) from err
                try:
                    job.assign(proc.pid)
                except SandboxSetupError:
                    proc.kill()
                    with contextlib.suppress(ProcessLookupError):
                        await proc.wait()
                    raise
                result = await _drive(
                    proc,
                    job,
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
            if job is not None:
                job.terminate()
                job.close()
            canary.close()
            remove_tree(root)
        return withhold_if_weaker(result, isolation, backend="windows-job")

    def _build_env(self, workdir: Path, canary: Canary) -> dict[str, str]:
        env = dict(self._env_allowlist)
        # CPython on Windows needs SYSTEMROOT to initialise its random seed
        # and to load system DLLs; without it the interpreter aborts at start.
        system_root = _system_root()
        if system_root:
            env["SYSTEMROOT"] = system_root
        env.setdefault("PATH", str(self._python_bin.parent))
        env["TEMP"] = str(workdir)
        env["TMP"] = str(workdir)
        env["USERPROFILE"] = str(workdir)
        env["HOME"] = str(workdir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env[CANARY_ENV] = str(canary.path)
        return env


def _system_root() -> str | None:
    return os.environ.get("SYSTEMROOT") or "C:\\Windows"


def _creation_flags() -> int:
    # A new process group so a console event aimed at the worker never
    # reaches the child, and no window: nothing here should ever paint one.
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


async def _listen_for_one_connection(
    token: str,
) -> tuple[
    asyncio.Server,
    asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
    list[asyncio.StreamWriter],
]:
    """A loopback listener that hands over the first connection presenting ``token``."""
    loop = asyncio.get_running_loop()
    connected: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        loop.create_future()
    )
    accepted: list[asyncio.StreamWriter] = []
    expected = (token + "\n").encode()

    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(writer)
        try:
            first = await asyncio.wait_for(reader.readline(), timeout=_CONNECT_TIMEOUT_SECONDS)
        except (TimeoutError, OSError):
            writer.close()
            return
        if first != expected or connected.done():
            writer.close()
            return
        connected.set_result((reader, writer))

    server = await asyncio.start_server(_on_client, host="127.0.0.1", port=0)
    return server, connected, accepted


async def _close_listener(
    server: asyncio.Server,
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
    job: _Job,
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
                f"the sandbox process never connected back within "
                f"{_CONNECT_TIMEOUT_SECONDS}s of starting; the interpreter at the configured "
                "python_bin may not run with a scrubbed environment on this host"
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
                    lambda ready: _grade(ready, network_granted=network),
                    isolation,
                    network_required=not network,
                    backend="windows-job",
                ),
            )
    finally:
        # Whatever happened, including this task being cancelled from outside:
        # the job is terminated, so nothing the program started survives.
        job.terminate()

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_GRACE_SECONDS)
    if proc.returncode is None:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()

    stdout = await bounded(stdout_task)
    stderr = await bounded(stderr_task)
    if setup_error is not None:
        raise SandboxSetupError(setup_error)

    failure, limit_hit = _classify(
        returncode=proc.returncode,
        done=talk.done,
        protocol_error=talk.protocol_error,
        timed_out=talk.timed_out,
        cancelled=talk.cancelled,
        wall_seconds=limits.wall_seconds,
        cpu_seconds=limits.cpu_seconds,
        user_time=job.user_time_seconds(),
    )
    value = talk.done.value if (talk.done is not None and failure is None) else None
    artifacts, omitted = collect_artifacts(workdir, capture) if failure is None else ((), 0)
    observed = talk.ready if talk.ready is not None else ReadyFrame(network_denied=False)
    guarantees = _grade(observed, network_granted=network)
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


def _grade(ready: ReadyFrame, *, network_granted: bool) -> SandboxGuarantees:
    enforced, unavailable = Enforcement.ENFORCED, Enforcement.UNAVAILABLE
    network = unavailable
    if not network_granted and ready.network_denied:
        network = enforced
    filesystem = unavailable
    if ready.canary_readable is False:
        filesystem = enforced
    return SandboxGuarantees(
        filesystem=filesystem,
        network=network,
        process_tree=enforced,
        identity=unavailable,
        cpu=enforced,
        memory=enforced,
        file_size=unavailable,
        process_count=enforced,
        wall_clock=enforced,
        environment=enforced,
    )


def _classify(
    *,
    returncode: int | None,
    done: DoneFrame | None,
    protocol_error: SandboxProtocolError | None,
    timed_out: bool,
    cancelled: bool,
    wall_seconds: float,
    cpu_seconds: float,
    user_time: float | None,
) -> tuple[SandboxFailure | None, SandboxLimit | None]:
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
    if user_time is not None and user_time >= cpu_seconds:
        # The job's own accounting says the tree burned its CPU budget, which
        # is the one reason the kernel terminates a job on its own.
        return (
            SandboxFailure(
                kind="resource_limit", message="the program exceeded its CPU time limit"
            ),
            SandboxLimit.CPU_SECONDS,
        )
    if protocol_error is not None:
        return SandboxFailure(kind="protocol_violation", message=str(protocol_error)), None
    note = "" if returncode is None else f" (exit code {returncode})"
    return (
        SandboxFailure(
            kind="protocol_violation",
            message=f"the sandboxed process ended without completing the protocol{note}",
        ),
        None,
    )
