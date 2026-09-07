"""``run_code``: the model writes one program, the sandbox runs it.

DESIGN.md §18. The contract, and every clause of it matters:

- **One program per execution.** No state carries between calls. A model that
  wants a value in its next program puts it in the program.
- **Host bindings.** The program calls host-provided functions as ordinary Python
  calls, and those calls route back through the normal tool path with the same
  Policy, egress and recording. There is no privileged back door: a binding is
  the same tool the model could have called directly, reached a different way.
- **Failures are data.** A traceback comes back as part of the result so the
  model can read it and fix the program, never raised so it kills the turn.

## Why the model gets a program instead of more tools

A model that can write a program can loop, branch and combine results without a
round trip per step. The cost is that a program is arbitrary code, which is why
DESIGN.md §18 rejects in-process execution outright and this module cannot run
anything itself: it hands the program to a ``Sandbox`` and reports what came back.

## What the model is told about a failure

Everything: stdout, stderr, and the traceback. A model shown only "it failed"
rewrites the program from scratch and usually reproduces the same mistake. A
model shown the traceback fixes the line.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, Protocol, runtime_checkable

from psych_runtime.core.messages import ToolDefinition
from psych_runtime.tools.guidance import sandbox_failure_guidance

__all__ = ["TOOL_NAME", "SandboxLike", "make_run_code", "run_code_definition"]


@runtime_checkable
class SandboxLike(Protocol):
    """What this module needs from a sandbox, declared rather than imported.

    ``psych_runtime.tools`` and ``psych_runtime.sandbox`` are siblings under import-linter's
    layering contract and neither may import the other, so this matches
    ``psych_runtime.sandbox.port.Sandbox`` structurally. The runtime layer, which imports
    both, hands a real one in. ``psych_runtime.tools.mcp`` does the same for the egress
    transport, for the same reason.

    Declaring the shape rather than reaching across also keeps this module
    testable against a stand-in without pulling a subprocess into a unit test.
    """

    async def run(
        self,
        program: str,
        *,
        bindings: Mapping[str, Any] | None = None,
        limits: Any | None = None,
        network: bool = False,
    ) -> Any: ...


TOOL_NAME: Final = "run_code"
"""Matches the entry in ``psych_runtime.core.spec.RESERVED_TOOL_NAMES``."""

_MAX_OUTPUT_CHARS: Final = 8_000
"""How much of stdout or stderr reaches the model directly.

A program that prints a megabyte should not fill the context window with it. The
whole result is in the log either way, and a program that genuinely needs to
produce something large should return it so large-result elision handles it
(DESIGN.md §10.8).
"""

HostCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""How a binding reaches a host tool: the tool's name and its arguments.

Supplied by the runtime, which is what routes the call through Policy, the
egress seam and the record log. This module never calls a tool itself, which is
the whole of "there is no privileged back door".
"""


def run_code_definition(binding_names: Sequence[str]) -> ToolDefinition:
    """Describe ``run_code`` to the model, naming the bindings it may call.

    The binding list is in the description rather than in the schema because a
    program calls them as functions, not as arguments. A model that is not told
    what it may call will either invent a name and get a ``NameError``, or use
    nothing and write a program that cannot do anything.
    """
    if binding_names:
        available = "\n".join(f"- `{name}(...)`" for name in sorted(binding_names))
        bindings_text = (
            "\n\nInside the program you can call these host functions directly. "
            "They are `await`ed like any coroutine:\n\n" + available
        )
    else:
        bindings_text = "\n\nNo host functions are available in this program."

    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Run one Python program in an isolated process and get back what it "
            "printed and what it returned.\n\n"
            "The program's top-level code runs inside an implicit async function, "
            "so a bare `await` and a bare `return` are both legal. Whatever you "
            "return becomes the result.\n\n"
            "Each call is a fresh process: nothing carries over from a previous "
            "one, so a value you need later must be returned, not left in a "
            "variable. There is no network unless you were told otherwise, and "
            "the process is resource-capped.\n\n"
            "If the program raises, you get the traceback back as the result. "
            "Read it and fix the line rather than rewriting from scratch." + bindings_text
        ),
        input_schema={
            "type": "object",
            "properties": {
                "program": {
                    "type": "string",
                    "description": "The Python source to run.",
                }
            },
            "required": ["program"],
            "additionalProperties": False,
        },
        annotations=frozenset({"write"}),
    )


def make_run_code(
    sandbox: SandboxLike,
    host_call: HostCall | None = None,
    *,
    binding_names: Sequence[str] = (),
    limits: Any | None = None,
    network: bool = False,
) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """Build the ``run_code`` executor for one Run.

    Args:
        sandbox: where the program runs. Never this process (DESIGN.md §18).
        host_call: how a binding reaches a host tool. ``None`` means the program
            gets no bindings, which is right for a Run whose Spec grants no tools.
        binding_names: which tools the program may call. Each becomes a function
            in the program's namespace that routes back through ``host_call``.
        limits: resource caps. ``None`` uses the adapter's defaults.
        network: whether the program may reach the network. Off by default, and
            granting it does not hand the program an HTTP client: anything it
            should fetch belongs behind a binding that goes through the egress
            seam (DESIGN.md §14).

    Returns:
        An async callable taking the tool's arguments and returning a result the
        model can read.
    """
    bindings = _build_bindings(host_call, binding_names)

    async def execute(arguments: Mapping[str, Any]) -> dict[str, Any]:
        program = arguments.get("program")
        if not isinstance(program, str) or not program.strip():
            return {
                "ok": False,
                "error": (
                    "run_code needs a `program`: a string of Python source. Send the "
                    "code you want to run."
                ),
            }

        result = await sandbox.run(program, bindings=bindings, limits=limits, network=network)

        payload: dict[str, Any] = {
            "ok": result.failure is None,
            "stdout": _cap(result.stdout),
            "stderr": _cap(result.stderr),
            "value": result.value,
            "duration_seconds": round(result.duration_seconds, 4),
        }
        if result.stdout_truncated or len(result.stdout) > _MAX_OUTPUT_CHARS:
            payload["stdout_truncated"] = True
        if result.stderr_truncated or len(result.stderr) > _MAX_OUTPUT_CHARS:
            payload["stderr_truncated"] = True
        if result.limit_hit is not None:
            payload["limit_hit"] = str(result.limit_hit)

        if result.failure is not None:
            # The whole point of §18's "failures are data": the model reads this
            # and fixes the line. A summary would make it guess. The traceback
            # is not passed into sandbox_failure_guidance here: it stays its own
            # "traceback" field below, and folding it into "error" too would
            # print it twice in the same payload.
            payload["error"] = sandbox_failure_guidance(result.failure.kind, result.failure.message)
            payload["error_type"] = result.failure.kind
            if result.failure.traceback:
                payload["traceback"] = _cap(result.failure.traceback)

        return payload

    return execute


def _build_bindings(
    host_call: HostCall | None, names: Sequence[str]
) -> dict[str, Callable[[Mapping[str, Any]], Awaitable[Any]]]:
    """Turn tool names into callables the program can use.

    Each binding closes over its own name so the program's ``lookup(...)`` becomes
    ``host_call("lookup", {...})``. Routing every one through the same callback is
    what keeps a binding from being a shortcut past Policy or the record log.
    """
    if host_call is None:
        return {}

    def bind(name: str) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
        async def call(arguments: Mapping[str, Any]) -> Any:
            # One positional mapping, not **kwargs: the sandbox marshals the
            # program's keyword arguments into a single dict before they cross
            # the process boundary, so this is the shape that actually arrives.
            return await host_call(name, dict(arguments))

        call.__name__ = name
        return call

    return {name: bind(name) for name in names}


def _cap(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    kept = text[:_MAX_OUTPUT_CHARS]
    dropped = len(text) - _MAX_OUTPUT_CHARS
    return f"{kept}\n\n[{dropped} more characters, not shown]"


def describe_result(payload: Mapping[str, Any]) -> str:
    """Render a result for a log line or a report. Not what the model sees."""
    return json.dumps(dict(payload), sort_keys=True, default=str)[:512]
