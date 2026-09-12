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


def _network_denied():
    # A UDP "connect" never sends a packet; it only asks the kernel to pick a
    # route. In a namespace with nothing but loopback (what a successful
    # ``unshare(CLONE_NEWNET)`` leaves behind) there is no route to a
    # non-loopback address at all, so this raises immediately rather than
    # depending on anything actually being reachable at the far end. TEST-NET-1
    # (RFC 5737) is used because it is guaranteed never to be routed anywhere
    # real, so this never actually reaches out even when a route exists.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 53))
    except OSError:
        return True
    else:
        return False
    finally:
        probe.close()


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


async def _amain():
    spec = sys.argv[1]
    binding_names = sys.argv[2:]
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
    if first_frame.get("type") != "run":
        raise RuntimeError("psych sandbox: expected a run frame first")
    program = first_frame["program"]

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
            future = pending.pop(frame.get("id"), None)
            if future is None or future.done():
                continue
            if frame.get("ok"):
                future.set_result(frame.get("value"))
            else:
                message = frame.get("message") or "host binding failed"
                future.set_exception(RuntimeError(message))

    reader_task = asyncio.ensure_future(_reader_loop())

    async def _dispatch(name, arguments):
        nonlocal next_call_id
        future = loop.create_future()
        async with call_lock:
            call_id = next_call_id
            await _write_frame(
                {"type": "call", "id": call_id, "name": name, "arguments": arguments}
            )
            pending[call_id] = future
            next_call_id = call_id + 1
        return await future

    def _make_binding(binding_name):
        async def _binding(**kwargs):
            return await _dispatch(binding_name, kwargs)

        _binding.__name__ = binding_name
        return _binding

    namespace = {name: _make_binding(name) for name in binding_names}

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
