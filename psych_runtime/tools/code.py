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
- **Output is budgeted, never dropped.** What the model sees of stdout, stderr
  and the returned value is a deterministic head-and-tail preview inside a byte
  budget the Spec sets (``OutputPolicy.preview_bytes``). Everything the backend
  captured beyond that is kept as a ``ResultAttachment`` on the record, inline
  or in the ``BlobStore``, and the model reads it by handle through
  ``read_tool_output`` one window at a time. The payload says plainly which of
  three things happened to each stream: it fit, it was kept and can be read
  back, or it was not kept (``*_omitted_bytes``) because no ``BlobStore`` is
  wired or the Spec said not to.

## Why the model gets a program instead of more tools

A model that can write a program can loop, branch and combine results without a
round trip per step. The cost is that a program is arbitrary code, which is why
DESIGN.md §18 rejects in-process execution outright and this module cannot run
anything itself: it hands the program to a ``Sandbox`` and reports what came back.

## What the model is told about a failure

Everything: stdout, stderr, and the traceback. A model shown only "it failed"
rewrites the program from scratch and usually reproduces the same mistake. A
model shown the traceback fixes the line.

## What the model is not told

The host path of the workspace, the backend's name, the worker's platform, and
the per-guarantee enforcement report. Those are recorded (the ``execution``
attachment carries the full report for ``psych_runtime.report`` and a console)
but they cost context on every turn the result stays in the conversation and
change nothing about what the model should do next. The model sees the
isolation level it got and whether the network was denied, because those two
do change what it should do.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

from psych_runtime.core.code_execution import (
    DEFAULT_BINDING_BUDGET,
    RUN_CODE_TOOL_NAME,
    BindingBudget,
    OutputPreservation,
)
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.tools.bindings import BindingCounter, EffectiveBindings, budgeted
from psych_runtime.tools.guidance import sandbox_failure_guidance

__all__ = [
    "SLOT_PLACEHOLDER",
    "TOOL_NAME",
    "CodeExecutionOutcome",
    "PendingAttachment",
    "SandboxLike",
    "attachment_handle",
    "describe_bindings",
    "make_run_code",
    "preview_bytes",
    "run_code_definition",
]


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
        bindings: Any = None,
        limits: Any = None,
        network: bool = False,
        isolation: Any = None,
        capture: Any = None,
        cancel: Any = None,
    ) -> Any: ...


TOOL_NAME: Final = RUN_CODE_TOOL_NAME
"""Matches the entry in ``psych_runtime.core.spec.RESERVED_TOOL_NAMES``.

Defined in ``psych_runtime.core.code_execution`` and re-exported here: the
reducer has to recognise a program's calls while folding, and ``core``
may not import from ``tools``."""

DEFAULT_PREVIEW_BYTES: Final = 4_000
"""Per stream, when no ``OutputPolicy`` was given. Matches the Spec default."""

_TRACEBACK_PREVIEW_BYTES: Final = 6_000
"""A traceback is the one output the model must read whole to fix the line, so
its budget is separate from the streams' and a little larger; past it the
head and tail survive, which is where the error and the failing frame are."""

_BINARY_SAMPLE: Final = 8_192
_OMITTED_MARKER: Final = "\n…[{omitted} bytes omitted]…\n"
_TEXT_TYPE: Final = "text/plain; charset=utf-8"

SLOT_PLACEHOLDER: Final = "__slot__:"
"""Where a handle belongs in the payload before the call id is known. This
module never sees the call id (the agent loop mints it), so it writes
``__slot__:<slot>`` and ``psych_runtime.runtime.agent`` substitutes
``attachment_handle(call_id, slot)`` when it records the result."""
_SLOT_PLACEHOLDER: Final = SLOT_PLACEHOLDER

HostCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""How a binding reaches a host tool: the tool's name and its arguments.

Supplied by the runtime, which is what routes the call through Policy, the
egress seam and the record log. This module never calls a tool itself, which is
the whole of "there is no privileged back door".
"""

Refusal = tuple[str, str]
"""``(kind, message)`` for an execution the runtime already knows cannot run
under the terms the Spec asked for. The executor returns it as data without
touching the sandbox."""


@dataclass(frozen=True, slots=True)
class PendingAttachment:
    """Bytes a ``run_code`` call produced that the model did not see whole.

    Built here, stored by the agent loop: ``psych_runtime.runtime.agent`` decides
    whether the bytes sit inline in the record or in the ``BlobStore`` and
    writes the ``ResultAttachment``, because only it holds the journal and the
    Run's scope. This module only knows what was produced.

    Attributes:
        slot: a short token unique within the call: ``stdout``, ``stderr``,
            ``value``, ``execution``, ``file1``... Folded into the handle and
            into the blob address.
        name: what a reader sees: the slot, or ``file:<relative path>``.
        data: the captured bytes.
        observed_bytes: how many bytes the program produced. Larger than
            ``len(data)`` when the backend's capture cap cut it short.
        content_type: how to read ``data`` back.
        must_keep: whether losing these bytes is an error when no
            ``BlobStore`` is wired (``OutputPreservation.REQUIRED``).
    """

    slot: str
    name: str
    data: bytes
    observed_bytes: int
    content_type: str
    must_keep: bool


@dataclass(frozen=True, slots=True)
class CodeExecutionOutcome:
    """What ``run_code`` hands the agent loop: the payload plus the bytes
    behind it.

    ``payload`` is exactly what the model sees and what the record's
    ``result`` holds. ``attachments`` are stored beside it under the handles
    ``payload`` already names, so a model reading ``stdout_handle`` finds the
    bytes there. ``preserve`` is the Spec's own instruction about what must
    happen to those bytes.
    """

    payload: dict[str, Any]
    attachments: tuple[PendingAttachment, ...]
    preserve: OutputPreservation


def attachment_handle(call_id: str, slot: str) -> str:
    """The handle a ``run_code`` attachment is read back under.

    ``out_`` rather than ``res_`` so it can never collide with the call's own
    elided-result handle, and the call id inside it so two calls producing the
    same stream name get distinct handles. ``psych_runtime.tools.large_results``
    derives the blob address from the same pieces.
    """
    return f"out_{call_id}_{slot}"


_BINDING_LIST_BUDGET: Final = 1_200
"""Characters of binding names this description will spend, at most.

An agent connected to a large MCP server may have hundreds of callable tools.
Naming them all here would put a catalogue in every prompt of every turn, which
is the cost code execution exists to avoid -- the tool would pay for itself in
schemas before the model wrote a line. Past this budget the names stop and the
model is pointed at ``TOOLS`` inside the program, which costs nothing."""


def describe_bindings(bindings: EffectiveBindings) -> str:
    """The sentence naming what a program may call, inside a fixed budget.

    Names the ones a program can write as plain calls, because that is what a
    model copies. Everything else -- and everything past the budget -- is
    reachable through ``call_tool`` and listed in ``TOOLS``, which the program
    can read without any of it entering the prompt.
    """
    if not bindings.tools:
        return " No host tools are available inside the program."

    aliases = bindings.aliases
    shown: list[str] = []
    spent = 0
    for name in aliases:
        cost = len(name) + 8
        if spent + cost > _BINDING_LIST_BUDGET:
            break
        shown.append(name)
        spent += cost

    total = len(bindings.tools)
    text = (
        f"\n\nInside the program, `await call_tool(name, {{...}})` calls any of the "
        f"{total} tools you can call directly; `TOOLS` is the list of their names, and "
        "a failed call raises `ToolError` with a `.kind`."
    )
    if shown:
        listed = ", ".join(f"`{name}(...)`" for name in shown)
        text += f" These can also be awaited by name: {listed}."
    remaining = total - len(shown)
    if remaining > 0:
        text += (
            f" {remaining} more are in `TOOLS`; use `list_tools`/`get_tool_info` for "
            "arguments you do not know."
        )
    # One sentence, and it earns its place: without it a program written
    # against a large result crashes on the first line that touches it, and the
    # model's next move is usually to rewrite the program rather than to read
    # the window it was offered.
    text += (
        " A result too large to send arrives as a handle instead: read it with "
        "`await handle.read(offset=..., limit=...)` or search it with "
        "`await handle.read(pattern=...)`, and use the value it returns rather "
        "than the handle."
    )
    return text


def run_code_definition(
    bindings: EffectiveBindings,
    *,
    network: bool = False,
    preview_bytes_budget: int = DEFAULT_PREVIEW_BYTES,
    wall_seconds: float | None = None,
) -> ToolDefinition:
    """Describe ``run_code`` to the model, naming the bindings it may call.

    Compact on purpose: this text is billed on every turn of every Run that
    has code execution, so it says what changes the model's behaviour and
    nothing that does not. The binding list is in the description rather
    than in the schema because a program calls them as functions, not as
    arguments. A model that is not told what it may call will either invent a
    name and get a ``NameError``, or use nothing and write a program that
    cannot do anything.

    The guidance about *when* to write a program is here for the same reason
    and kept to two lines. A model that reaches for a program to make one
    lookup pays a sandbox for nothing; one that makes forty lookups one turn at
    a time pays forty prompts for a number it could have computed in a loop.
    Neither mistake is worth a second model call to decide, so the tool that is
    already in the prompt says which is which.
    """
    network_text = "Network is available." if network else "No network."
    wall_text = f" Wall clock limit: {wall_seconds:g}s." if wall_seconds is not None else ""

    return ToolDefinition(
        name=TOOL_NAME,
        description=(
            "Run one Python program in an isolated sandbox. Top-level `await` and "
            "`return` are allowed; the returned value (JSON-shaped) is the result. Each "
            "call is a fresh process with an empty working directory: nothing carries "
            f"over, so return what you need later. {network_text}{wall_text}\n\n"
            "Use this for many calls at once, dependent sequences, filtering, joins, "
            "aggregation, exact arithmetic, and pulling a small answer out of large "
            "intermediate results -- the intermediates stay in the program. Call a tool "
            "directly instead for a single lookup, for anything needing approval or a "
            "person's input, and for a destructive action you want reviewed on its own."
            f"\n\nOutput beyond {preview_bytes_budget} bytes per stream is previewed head "
            "and tail; read the rest with `read_tool_output` using the handle in the "
            "result. Files written to the working directory come back as artifacts with "
            "handles.\n\nIf the program raises you get the traceback: fix the line rather "
            "than rewriting." + describe_bindings(bindings)
        ),
        input_schema={
            "type": "object",
            "properties": {"program": {"type": "string", "description": "Python source."}},
            "required": ["program"],
            "additionalProperties": False,
        },
        annotations=frozenset({"write"}),
    )


def make_run_code(
    sandbox: Any,
    host_call: HostCall | None = None,
    *,
    bindings: EffectiveBindings | None = None,
    budget: BindingBudget = DEFAULT_BINDING_BUDGET,
    binding_counter: BindingCounter | None = None,
    limits: Any | None = None,
    network: bool = False,
    isolation: Any | None = None,
    capture: Any | None = None,
    preview_bytes_budget: int = DEFAULT_PREVIEW_BYTES,
    preserve: OutputPreservation = OutputPreservation.WHEN_AVAILABLE,
    refusal: Refusal | None = None,
    cancel: asyncio.Event | None = None,
    legacy_signature: bool = False,
) -> Callable[[Mapping[str, Any]], Awaitable[CodeExecutionOutcome]]:
    """Build the ``run_code`` executor for one Run.

    Args:
        sandbox: where the program runs, anything shaped like ``SandboxLike``
            (or the earlier three-option port, with ``legacy_signature``).
            Never this process (DESIGN.md §18). ``None`` only together with
            ``refusal``.
        host_call: how a binding reaches a host tool. ``None`` means the program
            gets no bindings, which is right for a Run whose Spec grants no tools.
        bindings: which tools the program may call this turn, and which of the
            ones its Spec named are not available. Each callable name becomes a
            binding routed back through ``host_call``; the unavailable ones are
            reported in the result rather than quietly dropped, because a model
            that asked for ``billing__refund`` and got silence will assume it
            ran (DESIGN.md §10.7).
        budget: how many host tool calls this program may make and how many
            bytes they may move. Applied per execution, which is what makes it
            a bound: a counter built once per turn would be shared by two
            programs in one assistant message.
        binding_counter: the Run's running total, shared across executions, so
            a model cannot buy a fresh allowance by writing a second program.
        limits: resource caps. ``None`` uses the adapter's defaults.
        network: whether the program may reach the network. Off by default, and
            granting it does not hand the program an HTTP client: anything it
            should fetch belongs behind a binding that goes through the egress
            seam (DESIGN.md §14).
        isolation: the minimum ``IsolationLevel`` the execution must achieve.
        capture: an ``OutputCapture`` bounding what the backend keeps.
        preview_bytes_budget: per stream, how much reaches the model.
        preserve: what must happen to bytes beyond the preview.
        refusal: ``(kind, message)`` when the runtime already knows the request
            cannot be met. The executor returns it as data and runs nothing.
        cancel: the Run's cancellation signal, handed to the backend.
        legacy_signature: the sandbox predates ``isolation``/``capture``/
            ``cancel`` and must be called with the three original options only.

    Returns:
        An async callable taking the tool's arguments and returning a
        ``CodeExecutionOutcome`` the agent loop records.
    """
    offered = bindings if bindings is not None else EffectiveBindings()
    run_total = binding_counter if binding_counter is not None else BindingCounter()

    async def execute(arguments: Mapping[str, Any]) -> CodeExecutionOutcome:
        program = arguments.get("program")
        if not isinstance(program, str) or not program.strip():
            return CodeExecutionOutcome(
                payload={
                    "ok": False,
                    "error": (
                        "run_code needs a `program`: a string of Python source. Send the "
                        "code you want to run."
                    ),
                    "error_type": "invalid_arguments",
                },
                attachments=(),
                preserve=preserve,
            )

        if refusal is not None or sandbox is None:
            kind, message = (
                refusal
                if refusal is not None
                else (
                    "backend_not_ready",
                    "no sandbox is configured for this agent's profile",
                )
            )
            return CodeExecutionOutcome(
                payload={
                    "ok": False,
                    "error": sandbox_failure_guidance(kind, message),
                    "error_type": kind,
                },
                attachments=(),
                preserve=preserve,
            )

        # Built here, once per execution, so the call and byte budgets bound
        # one program rather than one turn.
        bound = _build_bindings(
            None if host_call is None else budgeted(host_call, budget, run_total),
            offered.names,
        )
        options: dict[str, Any] = {"bindings": bound, "limits": limits, "network": network}
        if not legacy_signature:
            options.update(isolation=isolation, capture=capture, cancel=cancel)
        result = await sandbox.run(program, **options)
        return _outcome(
            result,
            preview_bytes_budget=preview_bytes_budget,
            preserve=preserve,
            limits=limits,
            requested_isolation=isolation,
            unavailable=offered.missing,
        )

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


# ---------------------------------------------------------------------------
# Building the payload from a SandboxResult
# ---------------------------------------------------------------------------


def _outcome(
    result: Any,
    *,
    preview_bytes_budget: int,
    preserve: OutputPreservation,
    limits: Any | None = None,
    requested_isolation: Any | None = None,
    unavailable: Sequence[str] = (),
) -> CodeExecutionOutcome:
    pending: list[PendingAttachment] = []
    must_keep = preserve is OutputPreservation.REQUIRED
    payload: dict[str, Any] = {"ok": result.failure is None}

    for stream in ("stdout", "stderr"):
        _stream_section(
            result,
            stream,
            preview_bytes_budget,
            must_keep=must_keep,
            payload=payload,
            pending=pending,
        )
    if result.failure is None:
        _value_section(
            result, preview_bytes_budget, must_keep=must_keep, payload=payload, pending=pending
        )

    payload["duration_seconds"] = round(float(result.duration_seconds), 4)
    if result.limit_hit is not None:
        payload["limit_hit"] = str(result.limit_hit)
    isolation = getattr(result, "isolation", None)
    payload["isolation"] = str(isolation) if isolation is not None else "unverified"
    if not getattr(result, "network_denied", False):
        payload["network_denied"] = False
    if getattr(result, "cancelled", False):
        payload["cancelled"] = True

    if unavailable:
        # Named, never substituted. A Spec that asked for a tool which is gone,
        # renamed, denied or on a server that did not answer has to be told so:
        # a program that finds no such binding and a model that is told nothing
        # both end up reporting work that never happened (DESIGN.md §10.7).
        payload["unavailable_bindings"] = list(unavailable)

    _artifacts_section(result, must_keep, payload, pending)
    if result.failure is not None:
        _failure_section(result, payload)
    pending.append(
        _execution_report(result, payload, limits=limits, requested_isolation=requested_isolation)
    )
    return CodeExecutionOutcome(payload=payload, attachments=tuple(pending), preserve=preserve)


def _stream_section(
    result: Any,
    stream: str,
    budget: int,
    *,
    must_keep: bool,
    payload: dict[str, Any],
    pending: list[PendingAttachment],
) -> None:
    data = _stream_bytes(result, stream)
    observed = max(int(getattr(result, f"{stream}_size", 0) or 0), len(data))
    capture_truncated = bool(getattr(result, f"{stream}_truncated", False))
    if not data and not capture_truncated:
        return
    text, fits, binary = preview_bytes(data, budget)
    payload[stream] = f"[binary output, {observed} bytes]" if binary else text
    if not fits or binary:
        payload[f"{stream}_bytes"] = observed
        payload[f"{stream}_handle"] = f"{_SLOT_PLACEHOLDER}{stream}"
        pending.append(
            PendingAttachment(
                slot=stream,
                name=stream,
                data=data,
                observed_bytes=observed,
                content_type="application/octet-stream" if binary else _TEXT_TYPE,
                must_keep=must_keep,
            )
        )
    if capture_truncated:
        payload[f"{stream}_truncated"] = True


def _value_section(
    result: Any,
    budget: int,
    *,
    must_keep: bool,
    payload: dict[str, Any],
    pending: list[PendingAttachment],
) -> None:
    rendered = json.dumps(result.value, separators=(",", ":"), default=str).encode("utf-8")
    if len(rendered) <= budget:
        payload["value"] = result.value
        return
    text, _fits, _binary = preview_bytes(rendered, budget)
    payload["value_preview"] = text
    payload["value_bytes"] = len(rendered)
    payload["value_handle"] = f"{_SLOT_PLACEHOLDER}value"
    pending.append(
        PendingAttachment(
            slot="value",
            name="value",
            data=rendered,
            observed_bytes=len(rendered),
            content_type="application/json",
            must_keep=must_keep,
        )
    )


def _artifacts_section(
    result: Any, must_keep: bool, payload: dict[str, Any], pending: list[PendingAttachment]
) -> None:
    artifacts = tuple(getattr(result, "artifacts", ()) or ())
    omitted = int(getattr(result, "artifacts_omitted", 0) or 0)
    if not artifacts and not omitted:
        return
    listed: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts, start=1):
        slot = f"file{index}"
        content_type = (
            artifact.content_type
            or mimetypes.guess_type(artifact.path)[0]
            or "application/octet-stream"
        )
        entry: dict[str, Any] = {
            "path": artifact.path,
            "bytes": artifact.size_bytes,
            "content_type": content_type,
            "handle": f"{_SLOT_PLACEHOLDER}{slot}",
        }
        if artifact.truncated:
            entry["truncated"] = True
        listed.append(entry)
        pending.append(
            PendingAttachment(
                slot=slot,
                name=f"file:{artifact.path}",
                data=bytes(artifact.data),
                observed_bytes=artifact.size_bytes,
                content_type=content_type,
                must_keep=must_keep,
            )
        )
    payload["artifacts"] = listed
    if omitted:
        payload["artifacts_omitted"] = omitted


def _failure_section(result: Any, payload: dict[str, Any]) -> None:
    # The whole point of §18's "failures are data": the model reads this and
    # fixes the line. A summary would make it guess. The traceback is not
    # passed into sandbox_failure_guidance here: it stays its own "traceback"
    # field below, and folding it into "error" too would print it twice in
    # the same payload.
    payload["error"] = sandbox_failure_guidance(result.failure.kind, result.failure.message)
    payload["error_type"] = result.failure.kind
    if result.failure.traceback:
        text, _fits, _binary = preview_bytes(
            result.failure.traceback.encode("utf-8"), _TRACEBACK_PREVIEW_BYTES
        )
        payload["traceback"] = text


def _execution_report(
    result: Any,
    payload: dict[str, Any],
    *,
    limits: Any | None = None,
    requested_isolation: Any | None = None,
) -> PendingAttachment:
    """The full enforcement report, recorded for a person and kept out of the
    prompt (see the module docstring). Always present, so a report can say
    what an execution was worth even when the program produced nothing."""
    guarantees = getattr(result, "guarantees", None)
    dump = getattr(limits, "model_dump", None)
    report = {
        "isolation": payload["isolation"],
        "requested_isolation": str(requested_isolation) if requested_isolation else None,
        "limits": dump(mode="json") if callable(dump) else None,
        "network_denied": bool(getattr(result, "network_denied", False)),
        "guarantees": guarantees.model_dump(mode="json") if guarantees is not None else {},
        "limit_hit": payload.get("limit_hit"),
        "duration_seconds": payload["duration_seconds"],
        "stdout_bytes": max(
            int(getattr(result, "stdout_size", 0) or 0), len(_stream_bytes(result, "stdout"))
        ),
        "stderr_bytes": max(
            int(getattr(result, "stderr_size", 0) or 0), len(_stream_bytes(result, "stderr"))
        ),
        "cancelled": bool(getattr(result, "cancelled", False)),
    }
    data = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    return PendingAttachment(
        slot="execution",
        name="execution",
        data=data,
        observed_bytes=len(data),
        content_type="application/json",
        must_keep=False,
    )


def _stream_bytes(result: Any, stream: str) -> bytes:
    """The raw capture of one stream, or its text re-encoded for a backend
    that predates raw capture."""
    raw = getattr(result, f"{stream}_data", b"")
    if raw:
        return bytes(raw)
    text = getattr(result, stream, "") or ""
    return text.encode("utf-8") if isinstance(text, str) else bytes(text)


def _looks_binary(data: bytes) -> bool:
    sample = data[:_BINARY_SAMPLE]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Invalid UTF-8 is still text often enough (a stray Latin-1 byte in a log
    # line) that it is previewed with replacement characters rather than
    # declared binary; only a NUL, or a quarter of the sample being control
    # bytes, counts as binary.
    control = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return control > len(sample) // 4


def preview_bytes(data: bytes, budget: int) -> tuple[str, bool, bool]:
    """A deterministic head-and-tail preview of ``data`` within ``budget`` bytes.

    Returns ``(text, fits, binary)``. ``fits`` is true when the whole content is
    in ``text``; otherwise the first ~60% and last ~40% of the budget survive
    around an explicit omission marker naming how many bytes it hides, so a
    model can see how a stream ended (where the error usually is) as well as
    how it began. Cuts never split a multi-byte character: each slice is
    decoded ignoring a partial character at its cut edge. Binary content is
    not previewed at all; the caller says so and offers the handle instead.
    """
    if _looks_binary(data):
        return "", False, True
    if len(data) <= budget:
        return data.decode("utf-8", errors="replace"), True, False
    head_budget = (budget * 3) // 5
    tail_budget = budget - head_budget
    head = data[:head_budget].decode("utf-8", errors="ignore")
    tail = data[len(data) - tail_budget :].decode("utf-8", errors="ignore")
    omitted = len(data) - head_budget - tail_budget
    return head + _OMITTED_MARKER.format(omitted=omitted) + tail, False, False


def describe_result(payload: Mapping[str, Any]) -> str:
    """Render a result for a log line or a report. Not what the model sees."""
    return json.dumps(dict(payload), sort_keys=True, default=str)[:512]


def digest(data: bytes) -> str:
    """The checksum a ``ResultAttachment`` carries, one definition."""
    return hashlib.sha256(data).hexdigest()


StoredKind = Literal["inline", "blob", "preview_only"]
