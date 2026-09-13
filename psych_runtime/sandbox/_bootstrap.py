"""The child-side bootstrap: one static script, run with ``python -c`` or from a file.

DESIGN.md §18. This is stdlib-only Python source, generated once and handed to
every adapter that spawns a child (``psych_runtime.sandbox.subprocess``,
``psych_runtime.sandbox.windows``, ``psych_runtime.sandbox.namespaces``,
``psych_runtime.sandbox.container``): the same script runs whether the channel
back to the host is an inherited socket fd, a Unix socket path or a loopback
TCP port, because it does not know or care which. It receives that as its
first argument: ``fd:<n>``, ``unix:<path>`` or ``tcp:<host>:<port>:<token>``.

## Why this cannot import ``psych``

The child runs with ``-I`` (isolated mode): no ``PYTHONPATH``, no site
directories, and deliberately no way to reach anything installed on the
host's own Python environment. That is what makes the environment scrubbing
in the adapters mean something. So this script cannot import
``psych_runtime.sandbox.protocol`` and share code with the host side; the framing and
the hostile-input rebuilding this performs on frames *from the host* (which
this script trusts, since the host is not the untrusted party here) are
deliberately re-expressed in plain stdlib terms rather than shared.

## What the child checks about itself before the program runs

The ``ready`` frame carries observations made from inside the process that
is about to run the model's program, not promises made before spawning:

- ``network_denied``: a UDP "connect" to an unrouted address, which never
  sends a packet and raises immediately when the process has no route out.
- ``canary_readable``: whether a file the host placed *outside* the
  workspace can be read. The host sets ``PSYCH_SANDBOX_CANARY`` to its path;
  readable means the program will see the host's filesystem. ``None`` when
  the host set no canary.
- ``uid``: the effective uid where there is one, so the host can confirm a
  privilege drop actually happened.
- ``platform``: ``sys.platform``, for a remote or containerised child whose
  platform the host did not choose.

The canary variable is removed from the environment once checked, so the
program is not handed a host path for free.

## What runs before the model's program

Only the handful of lines below ``# --- model program below this line ---``.
Everything above it is bootstrap machinery this project wrote and reviewed;
none of it is reachable from inside the model's program except through the
bindings explicitly handed to it.

## What a program is given

Three names, and one function per tool it may call:

- ``call_tool(name, arguments)`` reaches **every** tool offered to this
  execution, by its model-facing name. It exists because a name is not a
  Python identifier: an MCP server may legitimately offer ``list-repos``,
  ``2fa`` or ``class``, and none of those can be written as a call.
- a direct alias per tool whose name *is* a usable identifier, because
  ``await search(query="x")`` is what a model writes without being told twice.
  Never a mangled one -- two servers can offer ``list-repos`` and
  ``list_repos``, and a mangling would make one answer for the other.
- ``TOOLS``, the sorted names, so a program can look before it calls.
- ``ToolError``, raised when a call does not produce a result, carrying the
  host's own ``kind`` so the program can tell a refusal from a server being
  down without matching on prose.

The names come in the ``run`` frame rather than on the command line: an agent
connected to a large server may be offered hundreds of tools, and every
operating system caps a command line somewhere different.
"""

from __future__ import annotations

from typing import Final

__all__ = ["BOOTSTRAP_SOURCE", "CANARY_ENV"]

CANARY_ENV: Final = "PSYCH_SANDBOX_CANARY"
"""The environment variable naming the filesystem canary. Set by an adapter,
read and removed by the bootstrap before the program runs."""


BOOTSTRAP_SOURCE: Final[str] = """
import ast
import asyncio
import errno
import json
import keyword
import os
import socket
import sys
import traceback


def _connect(spec):
    kind, _, rest = spec.partition(":")
    if kind == "fd":
        return socket.socket(fileno=int(rest))
    if kind == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(rest)
        return sock
    if kind == "tcp":
        host, _, port_and_token = rest.partition(":")
        port, _, token = port_and_token.partition(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, int(port)))
        sock.sendall((token + "\\n").encode())
        return sock
    raise RuntimeError("psych sandbox bootstrap: unknown connection spec " + repr(spec))


def _route_exists(family, address):
    # A UDP "connect" never sends a packet; it only asks the kernel to pick a
    # route. In a namespace with nothing but loopback (what a successful
    # ``unshare(CLONE_NEWNET)`` leaves behind) there is no route to a
    # non-loopback address at all, so this raises immediately rather than
    # depending on anything actually being reachable at the far end. The
    # addresses used are documentation ranges guaranteed never to be routed
    # anywhere real, so this never actually reaches out even when a route
    # exists.
    try:
        probe = socket.socket(family, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        probe.connect(address)
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def _network_denied():
    # Both families. A host with IPv6 configured and IPv4 unrouted is not a
    # host with no network, and reporting denial from the v4 probe alone would
    # grade an execution that could still reach the world -- including a
    # link-local metadata service -- as contained.
    if _route_exists(socket.AF_INET, ("192.0.2.1", 53)):  # TEST-NET-1, RFC 5737
        return False
    if _route_exists(socket.AF_INET6, ("2001:db8::1", 53)):  # RFC 3849
        return False
    return True


def _canary_readable():
    path = os.environ.pop("PSYCH_SANDBOX_CANARY", None)
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _describe_failure(exc):
    resource_limit = None
    if isinstance(exc, MemoryError):
        resource_limit = "address_space_bytes"
    elif isinstance(exc, OSError) and exc.errno == errno.EAGAIN:
        resource_limit = "process_count"
    elif isinstance(exc, OSError) and exc.errno == errno.EFBIG:
        resource_limit = "file_size_bytes"
    tbe = traceback.TracebackException.from_exception(exc)
    kept = []
    for frame in tbe.stack:
        if frame.filename == "<model>":
            kept.append(frame)
    tbe.stack[:] = kept
    formatted = "".join(tbe.format())
    return {
        "kind": "exception",
        "message": str(exc) or type(exc).__name__,
        "traceback": formatted,
        "resource_limit": resource_limit,
    }


class ToolError(RuntimeError):
    \"\"\"A host tool call that did not produce a result.

    ``kind`` is a stable token the program can branch on -- the host's own
    classification of what went wrong -- rather than a sentence it would have
    to match on. ``tool`` is the name that was called.
    \"\"\"

    def __init__(self, kind, tool, message):
        self.kind = kind
        self.tool = tool
        self.message = message
        super().__init__(message)


class ToolResultHandle:
    \"\"\"A reference to a tool result too large to send in one piece.

    The call succeeded and the whole result is in the Run's log; what did not
    happen is moving all of it into this process. Read it in windows with
    ``await handle.read(offset=..., limit=...)`` or search it with
    ``await handle.read(pattern=...)``.

    Every other way of touching it raises, deliberately. A truncated result
    that still behaves like a result is the one failure nobody can see: a
    program that does ``len(rows)`` or ``rows.get("total", 0)`` on a partial
    answer returns a confident wrong number. So this refuses to be indexed,
    iterated, measured or unpacked, and says what to do instead.
    \"\"\"

    __slots__ = ("handle", "tool", "size_bytes", "stored", "_read")

    def __init__(self, descriptor, read):
        self.handle = descriptor.get("handle")
        self.tool = descriptor.get("tool")
        self.size_bytes = descriptor.get("size_bytes")
        self.stored = descriptor.get("stored")
        self._read = read

    async def read(self, offset=0, limit=200, pattern=None):
        \"\"\"One bounded window of the result, or the lines matching a pattern.

        Returns the reader's own answer: ``content`` (or ``matches``) plus
        ``total_lines``, ``total_matches`` and ``truncated``, so a program can
        tell "there is no more" from "there is more past here".
        \"\"\"
        arguments = {"handle": self.handle, "offset": offset, "limit": limit}
        if pattern is not None:
            arguments["pattern"] = pattern
        return await self._read(arguments)

    def __repr__(self):
        return "<ToolResultHandle tool=%r size_bytes=%r>" % (self.tool, self.size_bytes)

    def _refuse(self, *_args, **_kwargs):
        raise ToolError(
            "result_not_inline",
            self.tool,
            "this result was too large to send into the program in one piece, so "
            "it is a handle rather than the value. Read it with "
            "await handle.read(offset=..., limit=...) or search it with "
            "await handle.read(pattern=...). It is "
            + repr(self.size_bytes)
            + " bytes.",
        )

    __getitem__ = _refuse
    __iter__ = _refuse
    __len__ = _refuse
    __contains__ = _refuse
    __getattr__ = _refuse
    get = _refuse
    keys = _refuse
    items = _refuse
    values = _refuse


async def _amain():
    spec = sys.argv[1]
    sock = _connect(spec)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()

    read_buffer = bytearray()

    async def _read_line():
        while b"\\n" not in read_buffer:
            chunk = await loop.sock_recv(sock, 65536)
            if not chunk:
                raise ConnectionError("psych sandbox: host closed the channel")
            read_buffer.extend(chunk)
        index = read_buffer.index(b"\\n")
        line = bytes(read_buffer[:index])
        del read_buffer[: index + 1]
        return line

    async def _write_frame(frame):
        await loop.sock_sendall(sock, (json.dumps(frame) + "\\n").encode())

    # Reported before the model's program runs at all, so the host learns the
    # true state even if the program never finishes (a timeout, an infinite
    # loop): this is ground truth from inside the process that will run it,
    # not a promise made before spawning.
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    await _write_frame(
        {
            "type": "ready",
            "network_denied": _network_denied(),
            "canary_readable": _canary_readable(),
            "uid": uid,
            "platform": sys.platform,
        }
    )

    first_line = await _read_line()
    first_frame = json.loads(first_line)
    if first_frame.get("type") == "abort":
        # The host read this child's own report of its containment and would
        # not run the program on those terms. Nothing is compiled and nothing
        # is executed: the point of answering `ready` before receiving the
        # program is that a refusal here still prevents everything.
        return
    if first_frame.get("type") != "run":
        raise RuntimeError("psych sandbox: expected a run frame first")
    program = first_frame["program"]
    # In the frame rather than in argv: an agent connected to a large MCP
    # server may be offered hundreds of tools, and a command line has a length
    # every operating system enforces differently.
    binding_names = [name for name in first_frame.get("bindings") or [] if isinstance(name, str)]

    pending = {}
    next_call_id = 0
    call_lock = asyncio.Lock()

    async def _reader_loop():
        while True:
            try:
                line = await _read_line()
            except ConnectionError:
                return
            frame = json.loads(line)
            if frame.get("type") != "reply":
                continue
            entry = pending.pop(frame.get("id"), None)
            if entry is None or entry[0].done():
                continue
            future, called = entry
            if frame.get("ok"):
                # The frame says which kind of answer this is. Read from the
                # envelope, never from the value, so no tool result can make
                # itself look like a handle by what it contains.
                future.set_result((frame.get("value"), frame.get("result") == "handle"))
            else:
                message = frame.get("message") or "host tool call failed"
                kind = frame.get("kind")
                future.set_exception(
                    ToolError(kind if isinstance(kind, str) else "tool_failed", called, message)
                )

    reader_task = asyncio.ensure_future(_reader_loop())

    async def _dispatch(name, arguments):
        nonlocal next_call_id
        future = loop.create_future()
        async with call_lock:
            call_id = next_call_id
            await _write_frame(
                {"type": "call", "id": call_id, "name": name, "arguments": arguments}
            )
            pending[call_id] = (future, name)
            next_call_id = call_id + 1
        answer, is_handle = await future
        # The *frame* said which of the two this is. Nothing inside the
        # value decides it, so a tool whose own result happens to contain
        # any particular key is still just a tool that returned a dict.
        if is_handle:
            return ToolResultHandle(answer, _read_handle)
        return answer

    async def _read_handle(arguments):
        return await _dispatch("read_tool_output", arguments)

    offered = set(binding_names)

    async def call_tool(name, arguments=None):
        # The one way to reach every tool, including the many whose names are
        # not usable as Python names: an MCP server may offer "list-repos",
        # "2fa" or "class". Checked here rather than at the host so a typo is
        # an exception the program can catch, not a protocol violation that
        # ends the execution; the host checks the name again regardless.
        if name not in offered:
            raise ToolError(
                "binding_not_available",
                name,
                "no tool named " + repr(name) + " is available to this program. "
                "Available: " + (", ".join(sorted(offered)) or "none"),
            )
        return await _dispatch(name, dict(arguments or {}))

    def _make_binding(binding_name):
        async def _binding(**kwargs):
            return await _dispatch(binding_name, kwargs)

        _binding.__name__ = binding_name
        return _binding

    reserved = ("call_tool", "ToolError", "ToolResultHandle", "TOOLS")
    namespace = {
        "call_tool": call_tool,
        "ToolError": ToolError,
        "ToolResultHandle": ToolResultHandle,
        "TOOLS": tuple(sorted(offered)),
    }
    # An alias only where the name is one a program can actually write. No
    # mangling: two servers can offer "list-repos" and "list_repos", and a
    # mangled alias would silently answer for the wrong one. A tool whose name
    # would shadow the universal API keeps its place in `call_tool` and loses
    # only the shorthand.
    for name in binding_names:
        if name in reserved or name.startswith("__"):
            continue
        if not name.isidentifier() or keyword.iskeyword(name) or keyword.issoftkeyword(name):
            continue
        namespace[name] = _make_binding(name)

    # --- model program below this line ---
    value = None
    error = None
    try:
        module = ast.parse(program, filename="<model>")
        body = module.body or [ast.Pass(lineno=1, col_offset=0)]
        wrapper = ast.AsyncFunctionDef(
            name="__psych_main__",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                vararg=None,
                kwonlyargs=[],
                kw_defaults=[],
                kwarg=None,
                defaults=[],
            ),
            body=body,
            decorator_list=[],
            returns=None,
            type_comment=None,
        )
        ast.copy_location(wrapper, body[0])
        wrapped = ast.Module(body=[wrapper], type_ignores=[])
        ast.fix_missing_locations(wrapped)
        code = compile(wrapped, "<model>", "exec", dont_inherit=True)
        exec(code, namespace)
        value = await namespace["__psych_main__"]()
    except BaseException as exc:
        error = _describe_failure(exc)
    # --- model program above this line ---

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass

    reader_task.cancel()
    try:
        await reader_task
    except (asyncio.CancelledError, Exception):
        pass

    done = {"type": "done"}
    if error is not None:
        done["error"] = error
    else:
        try:
            json.dumps(value)
        except TypeError as exc:
            done["error"] = {
                "kind": "value_not_serialisable",
                "message": "the program returned a value that is not JSON-serialisable: "
                + str(exc),
                "traceback": None,
                "resource_limit": None,
            }
        else:
            done["value"] = value
    await _write_frame(done)
    sock.close()


asyncio.run(_amain())
"""
