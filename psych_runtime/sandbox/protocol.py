"""The host-binding wire protocol: framing and hostile-input parsing.

DESIGN.md §18. Shared by every adapter that talks to a child over a byte stream
(``psych_runtime.sandbox.subprocess`` over a socketpair fd, ``psych_runtime.sandbox.container``
over a bind-mounted Unix socket), so the framing and the parsing discipline
exist in exactly one place rather than drifting between two copies.

## Framing

One JSON object per line, newline-terminated, on the channel dedicated to
this protocol. Never mixed with the program's own stdout or stderr, which the
host reads from the process's ordinary stdio pipes instead: a program calling
``print`` cannot corrupt a frame here, because there is nothing for it to
write into on this channel in the first place.

## Why the host rebuilds every field instead of trusting the JSON

The child runs the model's program, which has the same access to this channel
that our own bootstrap does. A program that wants to can write anything to
its end of the socket: a forged frame, a call id chosen to collide with a
pending one, an out-of-range integer, a binding name that was never offered.
Parsing the JSON only proves it is well-formed JSON; it proves nothing about
whether the *shape* is one this host should act on. So every frame accepted
from a child is rebuilt field by field here rather than cast into a shape and
trusted: an unexpected type on any field, an id that is not the next one this
host is expecting, or a binding name outside the set actually offered, is a
protocol violation, not a value to pass along.

## Call ids: consecutive from zero, no gaps

The host answers a ``call`` only when its id is exactly one more than the id
it last answered. This bounds what the host must remember about in-flight
calls to a single integer, and it means a forged or replayed id is rejected
structurally rather than by a lookup that could be tricked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from psych_runtime.core.code_execution import ResultHandle
from psych_runtime.core.errors import PsychError
from psych_runtime.sandbox.port import HostBinding

__all__ = [
    "CallFrame",
    "DoneFrame",
    "ReadyFrame",
    "SandboxProtocolError",
    "binding_failure_kind",
    "build_abort_frame",
    "build_reply_frame",
    "build_run_frame",
    "parse_call_or_done_frame",
    "parse_ready_frame",
    "read_frame",
    "run_protocol",
    "write_frame",
]


class SandboxProtocolError(PsychError):
    """A child broke the framed wire protocol.

    Caught by the adapter that owns the channel and turned into a
    ``SandboxResult`` with ``failure.kind == "protocol_violation"``, never
    raised out of ``Sandbox.run()``: the model wrote the program running in
    that child, so a forged or malformed frame is the model's mistake to see
    and fix, the same as any other failure DESIGN.md §18 asks for as data.
    """


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read one newline-terminated JSON object.

    Raises:
        SandboxProtocolError: the channel closed with no frame, the line was
            not valid JSON, or the JSON was not an object.
    """
    try:
        line = await reader.readline()
    except OSError as err:
        # A peer killed mid-conversation reads as end-of-stream on POSIX and
        # as a connection reset on Windows; both mean the same thing here.
        raise SandboxProtocolError(
            f"the sandboxed process's protocol channel was reset: {err}"
        ) from err
    if not line:
        raise SandboxProtocolError(
            "the sandboxed process closed its protocol channel without sending a frame"
        )
    try:
        frame = json.loads(line)
    except json.JSONDecodeError as err:
        raise SandboxProtocolError(
            f"the sandboxed process sent a line that is not valid JSON: {err}"
        ) from err
    if not isinstance(frame, dict):
        raise SandboxProtocolError(
            f"a frame from the sandboxed process was a JSON {type(frame).__name__}, not an object"
        )
    return frame


async def write_frame(writer: asyncio.StreamWriter, frame: Mapping[str, Any]) -> None:
    """Write one frame, newline-terminated, and flush it.

    Raises:
        SandboxProtocolError: the channel was closed or reset by the peer.
    """
    try:
        writer.write(json.dumps(frame).encode() + b"\n")
        await writer.drain()
    except OSError as err:
        raise SandboxProtocolError(
            f"the sandboxed process's protocol channel was closed while writing: {err}"
        ) from err


def build_run_frame(program: str, bindings: Sequence[str] = ()) -> dict[str, Any]:
    """The frame that starts an execution, sent only once the host has read
    the child's ``ready`` report and accepted the terms it describes.

    Carries the names of the host tools this execution may call. In the frame
    rather than on the child's command line because an agent connected to a
    large MCP server may be offered hundreds of them, and every operating
    system caps a command line somewhere different -- a limit that would
    silently turn "your agent has many tools" into "the sandbox will not
    start".
    """
    return {"type": "run", "program": program, "bindings": list(bindings)}


def build_abort_frame(reason: str) -> dict[str, Any]:
    """The other answer to ``ready``: the terms are not what was asked for, so
    this child never receives the program.

    The point of the two-phase handshake. Grading a child's containment after
    it has already run the program tells you what happened, which is too late:
    the filesystem it touched, the address it reached and the file it wrote
    are not undone by discarding the output. The child reports what it
    observes about itself first, and the program is sent only if that is
    acceptable.
    """
    return {"type": "abort", "reason": reason}


@dataclass(frozen=True, slots=True)
class ReadyFrame:
    """What the child observed about itself before the program ran.

    Attributes:
        network_denied: the routing probe found no route out.
        canary_readable: the child could read the host's canary file, so the
            host filesystem is visible to it. ``None`` when no canary was set.
        uid: the child's effective uid, or ``None`` where there is none.
        platform: ``sys.platform`` inside the child.
    """

    network_denied: bool
    canary_readable: bool | None = None
    uid: int | None = None
    platform: str | None = None


def parse_ready_frame(raw: Mapping[str, Any]) -> ReadyFrame:
    """Validate the child's startup frame and return what it observed.

    Sent by the bootstrap before it reads the ``run`` frame, so before the
    model's program has run at all: unlike a ``call`` or ``done`` frame this
    one is not adversarial input (the model's code has not executed yet when
    it is sent), but the shape is still checked rather than assumed, on the
    general principle that nothing arriving over this channel is cast without
    being looked at first. The optional fields are rebuilt the same way: a
    remote service speaking this protocol may omit them, and a wrong type on
    one is a protocol violation rather than a value to pass along.

    Raises:
        SandboxProtocolError: the frame is not a well-formed ``ready`` frame.
    """
    network_denied = raw.get("network_denied")
    if raw.get("type") != "ready" or not isinstance(network_denied, bool):
        raise SandboxProtocolError(
            "expected a ready frame with a boolean network_denied field as the "
            f"child's first message, got {raw!r}"
        )
    canary = raw.get("canary_readable")
    if canary is not None and not isinstance(canary, bool):
        raise SandboxProtocolError("a ready frame's canary_readable was neither boolean nor null")
    uid = raw.get("uid")
    if uid is not None and (not isinstance(uid, int) or isinstance(uid, bool)):
        raise SandboxProtocolError("a ready frame's uid was neither an integer nor null")
    platform = raw.get("platform")
    if platform is not None and not isinstance(platform, str):
        raise SandboxProtocolError("a ready frame's platform was neither a string nor null")
    return ReadyFrame(
        network_denied=network_denied, canary_readable=canary, uid=uid, platform=platform
    )


def build_reply_frame(
    call_id: int,
    *,
    ok: bool,
    value: Any = None,
    message: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """The host's answer to one ``call`` frame, by id.

    An ``ok`` reply also says what *kind* of answer it is: an ordinary JSON
    value, or a handle naming a result too large to send whole
    (``result: "handle"``). That distinction is a field on the frame and
    never a key inside ``value``, because a tool's own result may legally
    contain any key at all.

    A failure carries a ``kind`` as well as a message, and the two are not the
    same thing. "The tool refused because a human would have to approve it",
    "the server is unreachable", "the tool itself returned an error" and "this
    program has spent its call budget" call for four different next moves, and
    a program handed one sentence can only tell them apart by matching on
    prose. The kind arrives in the program as ``ToolError.kind``.
    """
    if ok:
        if isinstance(value, ResultHandle):
            # Out of band, on the frame. A program is told what it was given
            # by the protocol rather than by anything inside the value, so a
            # tool that returns a dict shaped like a handle is still just a
            # tool that returned a dict.
            return {
                "type": "reply",
                "id": call_id,
                "ok": True,
                "result": "handle",
                "value": value.as_wire(),
            }
        return {"type": "reply", "id": call_id, "ok": True, "value": value}
    return {
        "type": "reply",
        "id": call_id,
        "ok": False,
        "message": message or "host tool call failed",
        "kind": kind or "tool_failed",
    }


def binding_failure_kind(err: BaseException) -> str:
    """The stable token a program sees for one failed binding call.

    A refusal Psych raised deliberately says what it is
    (``psych_runtime.core.errors.ProgramToolRefused.kind``). Everything else is
    named by its exception type, which is already the distinction that matters:
    ``McpToolError`` is a tool that ran and said no, ``McpServerUnreachable``
    is a server that is down, ``ValidationError`` is arguments that do not fit
    the schema. Read by ``getattr`` rather than by importing those types
    because this module sits under ``psych_runtime.sandbox`` and may not import
    ``psych_runtime.tools``, where most of them live (DESIGN.md §21).
    """
    kind = getattr(err, "kind", None)
    return kind if isinstance(kind, str) and kind else type(err).__name__


@dataclass(frozen=True, slots=True)
class CallFrame:
    """One validated host-binding invocation, rebuilt from an untrusted frame."""

    id: int
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DoneFrame:
    """One validated final settlement, rebuilt from an untrusted frame."""

    value: Any
    error_kind: str | None
    error_message: str | None
    error_traceback: str | None
    resource_limit: str | None


def parse_call_or_done_frame(
    raw: Mapping[str, Any],
    *,
    expected_call_id: int,
    known_bindings: AbstractSet[str],
) -> CallFrame | DoneFrame:
    """Validate and rebuild a child-to-host frame; never trust it by casting.

    Args:
        raw: the parsed JSON object, straight from ``read_frame``.
        expected_call_id: the only id a ``call`` frame may carry right now,
            enforcing the consecutive-from-zero, no-gaps discipline.
        known_bindings: the exact set of names offered to this execution. A
            ``call`` for anything else is rejected here rather than reaching
            a binding lookup that might behave oddly on an unknown key.

    Raises:
        SandboxProtocolError: the frame's declared type is neither ``call``
            nor ``done``, or a field does not have the shape that type
            requires.
    """
    frame_type = raw.get("type")

    if frame_type == "call":
        raw_id = raw.get("id")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id != expected_call_id:
            raise SandboxProtocolError(
                f"a call frame carried id {raw_id!r}, but the next expected id is "
                f"{expected_call_id}; call ids must be consecutive from 0 with no gaps"
            )
        name = raw.get("name")
        if not isinstance(name, str) or name not in known_bindings:
            raise SandboxProtocolError(
                f"a call frame named {name!r}, which is not one of the bindings "
                "offered to this execution"
            )
        raw_arguments = raw.get("arguments")
        if not isinstance(raw_arguments, dict):
            raise SandboxProtocolError("a call frame's arguments were not a JSON object")
        arguments = {str(key): value for key, value in raw_arguments.items()}
        return CallFrame(id=raw_id, name=name, arguments=arguments)

    if frame_type == "done":
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            raise SandboxProtocolError("a done frame's error was present but not a JSON object")
        if isinstance(error, dict):
            kind = error.get("kind")
            message = error.get("message")
            if not isinstance(kind, str) or not isinstance(message, str):
                raise SandboxProtocolError(
                    "a done frame's error did not carry a string kind and message"
                )
            tb = error.get("traceback")
            resource_limit = error.get("resource_limit")
            return DoneFrame(
                value=None,
                error_kind=kind,
                error_message=message,
                error_traceback=tb if isinstance(tb, str) else None,
                resource_limit=resource_limit if isinstance(resource_limit, str) else None,
            )
        return DoneFrame(
            value=raw.get("value"),
            error_kind=None,
            error_message=None,
            error_traceback=None,
            resource_limit=None,
        )

    raise SandboxProtocolError(
        f"a frame from the sandboxed process had unknown type {frame_type!r}"
    )


async def run_protocol(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    program: str,
    bindings: Mapping[str, HostBinding],
    admit: Callable[[ReadyFrame], str | None] | None = None,
) -> tuple[ReadyFrame, DoneFrame | None]:
    """Read the ready frame, accept or refuse it, then run and answer calls.

    Shared by every adapter (``psych_runtime.sandbox.subprocess``,
    ``psych_runtime.sandbox.container``): once a child is reachable as a pair of
    asyncio streams, the conversation from here on is identical no matter
    whether those streams sit on an inherited socket fd or an accepted Unix
    socket connection.

    Args:
        admit: given what the child reported about its own containment,
            returns ``None`` to send the program or a reason to refuse. A
            refusal sends an ``abort`` frame and the program is never sent,
            which is the only point at which refusing still prevents
            anything. Omitted, every child is admitted.

    Returns:
        What the child observed about itself before running anything (the
        ``ReadyFrame``), and the final ``DoneFrame`` -- or ``None`` for that
        second element when ``admit`` refused, because nothing ran.

    Raises:
        SandboxProtocolError: the child sent something that does not fit the
            protocol. Left for the caller to catch: this function does not
            know whether that should end the whole execution or just this
            attempt to talk to it.
    """
    ready = parse_ready_frame(await read_frame(reader))
    refusal = admit(ready) if admit is not None else None
    if refusal is not None:
        await write_frame(writer, build_abort_frame(refusal))
        return ready, None
    await write_frame(writer, build_run_frame(program, sorted(bindings)))

    known = bindings.keys()
    expected_id = 0
    while True:
        parsed = parse_call_or_done_frame(
            await read_frame(reader), expected_call_id=expected_id, known_bindings=known
        )
        if isinstance(parsed, DoneFrame):
            return ready, parsed

        expected_id += 1
        binding = bindings[parsed.name]
        try:
            value = await binding(parsed.arguments)
        except Exception as err:  # a binding's failure is data, relayed to the child, never raised
            await write_frame(
                writer,
                build_reply_frame(
                    parsed.id, ok=False, message=str(err), kind=binding_failure_kind(err)
                ),
            )
            continue
        # A handle is checked as the dict it will actually become: the
        # dataclass itself is not JSON, and checking it as one would reject
        # every handle as unserialisable.
        serialisable = value.as_wire() if isinstance(value, ResultHandle) else value
        try:
            json.dumps(serialisable)
        except TypeError as err:
            message = (
                f"host binding {parsed.name!r} returned a value that is not "
                f"JSON-serialisable: {err}"
            )
            await write_frame(
                writer,
                build_reply_frame(
                    parsed.id, ok=False, message=message, kind="result_not_serialisable"
                ),
            )
        else:
            await write_frame(writer, build_reply_frame(parsed.id, ok=True, value=value))
