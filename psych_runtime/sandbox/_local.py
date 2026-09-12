"""What every local backend shares: bounded capture, artifacts, the canary.

Private to ``psych_runtime.sandbox``. The subprocess, Windows job-object and
namespace adapters all spawn a child on this machine and read its streams,
collect what it wrote, and grade the guarantees they applied; keeping those
three in one place is what stops the adapters from disagreeing about what
"truncated" or "an artifact" means, which the contract suite would otherwise
catch one backend at a time.

## Bounded capture

A program may print a gigabyte. ``read_capped`` reads a pipe to its end no
matter how much arrives, so the child never blocks on a full pipe (the
classic deadlock: the host waits for exit, the child waits for someone to
drain stdout), but it keeps only the first ``cap`` bytes and counts the rest,
so this process's memory is bounded by the cap and the result can still say
how much was actually produced.

## Artifacts, and what is refused

Regular files under the workspace, by their path relative to it, within a
count and a byte budget. Nothing else: a symbolic link, a junction, any
reparse point on Windows, a device, a socket or a FIFO is skipped without
being opened, and a name that resolves outside the workspace is skipped too.
The walk never follows a link into a directory either, so a program cannot
point the collector at the host's filesystem by dropping a link in its own
working directory.

## The canary

A file with random contents placed outside the workspace before the child
starts. The bootstrap tries to read it and reports the outcome in its
``ready`` frame; ``filesystem`` is ``ENFORCED`` only when the child could
not. Cheap, and an observation rather than a promise.
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.sandbox.port import (
    HostBinding,
    OutputCapture,
    SandboxArtifact,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimit,
    SandboxResult,
    achieved_level,
)
from psych_runtime.sandbox.protocol import (
    DoneFrame,
    ReadyFrame,
    SandboxProtocolError,
    run_protocol,
)

__all__ = [
    "DEFAULT_CAPTURE",
    "Canary",
    "Captured",
    "Conversation",
    "bounded",
    "classify_done",
    "collect_artifacts",
    "content_type_for",
    "converse",
    "read_capped",
    "remove_tree",
    "withhold_if_weaker",
]

DEFAULT_CAPTURE: Final = OutputCapture(stream_bytes=1024 * 1024, collect_artifacts=False)
"""What a backend keeps when the caller did not say: a megabyte per stream,
the number the adapters always used, and no artifacts."""

_READ_CHUNK: Final = 65_536
_GRACE_SECONDS: Final = 3.0


@dataclass(slots=True)
class Captured:
    """One stream, read to the end, kept to the cap."""

    data: bytes
    observed: int
    truncated: bool


async def read_capped(stream: asyncio.StreamReader | None, cap: int) -> Captured:
    """Drain ``stream`` completely, keeping at most ``cap`` bytes."""
    if stream is None:
        return Captured(b"", 0, False)
    chunks: list[bytes] = []
    kept = 0
    observed = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        observed += len(chunk)
        if kept >= cap:
            continue
        piece = chunk[: cap - kept]
        chunks.append(piece)
        kept += len(piece)
    return Captured(b"".join(chunks), observed, observed > kept)


async def bounded(task: asyncio.Future[Captured], *, grace: float = _GRACE_SECONDS) -> Captured:
    """Wait for a reader task, bounded, after the process tree is already dead.

    A fallback, not the normal path: once the tree has been killed every
    holder of the pipes is gone and these resolve at once. Bounded anyway in
    case teardown could not reach something (a process this account has no
    permission to signal, in a misconfigured deployment).
    """
    try:
        return await asyncio.wait_for(task, timeout=grace)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return Captured(b"", 0, True)


@dataclass(slots=True)
class Conversation:
    """How the protocol conversation with one child ended.

    Exactly one of the three endings is set: a ``done`` frame (with the
    ``ready`` frame that preceded it), a protocol error, or the wall clock or
    the caller stopping it. A child that died mid-conversation is a protocol
    error too, because its channel closed without a ``done``.
    """

    ready: ReadyFrame | None = None
    done: DoneFrame | None = None
    protocol_error: SandboxProtocolError | None = None
    timed_out: bool = False
    cancelled: bool = False


async def converse(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    program: str,
    bindings: Mapping[str, HostBinding],
    *,
    wall_seconds: float,
    cancel: asyncio.Event | None,
) -> Conversation:
    """Run the protocol against the wall clock and the caller's cancellation.

    Shared by every local backend. The caller owns teardown: whatever this
    returns, and however this task itself is cancelled from outside, the
    backend kills the process tree afterwards, so nothing here has to know
    how that is done on this platform. An external ``CancelledError`` is
    re-raised after the conversation task is stopped, never swallowed.
    """
    outcome = Conversation()
    conversation = asyncio.ensure_future(run_protocol(reader, writer, program, bindings))
    waiters: list[asyncio.Future[Any]] = [conversation]
    cancel_wait: asyncio.Task[bool] | None = None
    if cancel is not None:
        cancel_wait = asyncio.ensure_future(cancel.wait())
        waiters.append(cancel_wait)
    try:
        finished, _ = await asyncio.wait(
            waiters, timeout=wall_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if conversation in finished:
            try:
                outcome.ready, outcome.done = conversation.result()
            except SandboxProtocolError as err:
                outcome.protocol_error = err
        elif cancel_wait is not None and cancel_wait in finished:
            outcome.cancelled = True
        else:
            outcome.timed_out = True
    finally:
        if cancel_wait is not None:
            cancel_wait.cancel()
        if not conversation.done():
            conversation.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await conversation
    return outcome


def classify_done(done: DoneFrame) -> tuple[SandboxFailure | None, SandboxLimit | None]:
    """A ``done`` frame's error, if any, as a failure and the limit it names.

    Shared by every backend: all of them receive the same frame from the
    same bootstrap.
    """
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


class Canary:
    """A secret file outside the workspace the child must not be able to read."""

    def __init__(self) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="psych-canary-"))
        self.path = self._dir / "canary"
        self.path.write_bytes(secrets.token_bytes(32))
        with contextlib.suppress(OSError):
            self._dir.chmod(0o755)
            self.path.chmod(0o644)

    def close(self) -> None:
        remove_tree(self._dir)


_KNOWN_TYPES: Final = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".html": "text/html",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}
"""The types a program's files usually have, fixed here so an artifact's
content type is the same on every platform: ``mimetypes`` consults the
Windows registry, where ``.csv`` can come back as a spreadsheet type."""


def content_type_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _KNOWN_TYPES:
        return _KNOWN_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse)


def _regular_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Every regular file under ``root``, never through a link of any kind."""
    real_root = os.path.realpath(root)
    pending: list[Path] = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or _is_reparse_point(entry):
                continue
            path = Path(entry.path)
            try:
                if os.path.commonpath([os.path.realpath(path), real_root]) != real_root:
                    continue
            except ValueError:
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                yield path, path.relative_to(root).as_posix()


def collect_artifacts(
    workdir: Path, capture: OutputCapture
) -> tuple[tuple[SandboxArtifact, ...], int]:
    """Regular files under ``workdir``, within the capture's budgets.

    Returns the artifacts and how many files were left behind past the count
    cap. Files are read in sorted path order so two executions writing the
    same files produce the same list.
    """
    if not capture.collect_artifacts or capture.artifact_count <= 0:
        return (), 0
    collected: list[SandboxArtifact] = []
    omitted = 0
    budget = capture.artifact_bytes
    for path, relative in _regular_files(workdir):
        if len(collected) >= capture.artifact_count:
            omitted += 1
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                data = handle.read(max(0, min(size, budget)))
        except OSError:
            continue
        truncated = len(data) < size
        budget -= len(data)
        collected.append(
            SandboxArtifact(
                path=relative,
                size_bytes=size,
                data=data,
                content_type=content_type_for(relative),
                truncated=truncated,
            )
        )
    return tuple(collected), omitted


def _clear_readonly(path: str) -> None:
    with contextlib.suppress(OSError):
        Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)


def remove_tree(root: Path) -> None:
    """Delete a workspace without following links out of it.

    ``shutil.rmtree`` refuses to descend into a symbolic link or, on Windows,
    a junction, which is exactly the property needed: a program that planted
    a link to the host's home directory in its workspace must not have that
    directory emptied by the cleanup. Read-only files a program left behind
    are made writable first, since a program can chmod its own files.
    """
    if root.is_symlink() or not root.exists():
        with contextlib.suppress(OSError):
            root.unlink()
        return

    def on_error(_func: object, path: str, _exc: object) -> None:
        _clear_readonly(path)
        with contextlib.suppress(OSError):
            Path(path).unlink()

    with contextlib.suppress(OSError):
        shutil.rmtree(root, onexc=on_error)


def withhold_if_weaker(
    result: SandboxResult, requested: IsolationLevel | None, *, backend: str
) -> SandboxResult:
    """Replace a result produced under weaker isolation than was asked for.

    Every local backend calls this last: the child has reported what it
    observed, the guarantees are graded, and if the level they add up to does
    not satisfy ``requested`` the program's output is withheld and the failure
    says why. Nothing produced under weaker terms than requested is ever
    returned as if it were not (DESIGN.md §18).
    """
    if requested is None:
        return result
    achieved = result.isolation
    if achieved is not None and achieved.satisfies(requested):
        return result
    offered = achieved.value if achieved is not None else "none"
    return result.model_copy(
        update={
            "value": None,
            "stdout": "",
            "stderr": "",
            "stdout_data": b"",
            "stderr_data": b"",
            "artifacts": (),
            "failure": SandboxFailure(
                kind="isolation_unavailable",
                message=(
                    f"this execution required {requested.value!r} isolation and the "
                    f"{backend} backend achieved {offered!r} on this host. The program's "
                    "output was withheld rather than returned from an execution that ran "
                    "under weaker terms than it was configured for."
                ),
            ),
        }
    )


def grade(
    guarantees: SandboxGuarantees, *, network_required: bool
) -> tuple[IsolationLevel | None, SandboxGuarantees]:
    """The level a set of guarantees adds up to, alongside the guarantees.

    A convenience so a backend builds its guarantees once and reports both
    the parts and the sum from the same object.
    """
    return achieved_level(guarantees, network_required=network_required), guarantees


def enforced_if(condition: bool) -> Enforcement:
    return Enforcement.ENFORCED if condition else Enforcement.UNAVAILABLE
