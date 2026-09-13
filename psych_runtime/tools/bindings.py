"""Which of a Run's tools a sandboxed program may call, and what it costs.

DESIGN.md §18 and §10.5. ``run_code`` hands the model a program; this module
decides which of the tools that model could have called directly the program
may call too, and bounds what a program may spend calling them.

## The rule is capability, not origin

A tool is code-bindable when it can answer a program, not when it happens to
have been written in Python. Four conditions, checked in this order:

1. It accepts a JSON-shaped request. Every tool the resolver offers does, by
   construction: a ``ToolDefinition`` carries a JSON Schema.
2. It completes with a JSON-shaped result, or with a handle that names one.
3. It runs inside the execution's own limits rather than outliving them.
4. It never suspends the Run. This is the load-bearing one. A suspension is
   a Run stopping and later resuming into a *turn*; there is no turn inside a
   running subprocess, and nothing can resume into one. A tool that suspends
   would leave a program blocked until its wall clock ran out, so it is
   refused before it runs and the model is told to call it directly.

Origin still matters for *how* a call is routed -- an MCP tool goes through the
scoped MCP caller, an HTTP tool through the egress seam -- so ``BindableTool``
records it. It is not what decides bindability.

## Why the set is rebuilt every turn

An MCP catalogue is a runtime fact. A server may rename a tool, a tenant policy
may withdraw one, an optional server may go away, and a failure streak may
withhold one between one turn and the next. A binding set computed once when
the Attempt started would keep offering a program access that had since been
taken away, which is the one direction DESIGN.md §10.5 does not allow. So the
runtime calls ``effective_bindings`` at every turn boundary against that turn's
freshly resolved set, and the ``run_code`` description the model reads is built
from the same answer.

## Budgets

A program compresses model turns; it does not get to compress the host's work.
One ``run_code`` call could otherwise issue a million MCP calls, or assemble a
gigabyte of arguments, without the model paying a turn for any of it. So every
binding call is counted and measured against a budget the deployment owns
(``psych_runtime.sandbox.profiles.SandboxProfile``), per execution and per Run,
and a program that exhausts one is told so as data.
"""

from __future__ import annotations

import json
import keyword
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from psych_runtime.core.code_execution import (
    BINDABLE_BUILTINS,
    DEFAULT_BINDING_BUDGET,
    EXCLUDED_BUILTINS,
    BindingBudget,
    ResultHandle,
)
from psych_runtime.core.errors import ProgramToolRefused
from psych_runtime.core.messages import ToolDefinition

__all__ = [
    "BINDABLE_BUILTINS",
    "DEFAULT_BINDING_BUDGET",
    "EXCLUDED_BUILTINS",
    "BindableTool",
    "BindingBudget",
    "BindingCounter",
    "EffectiveBindings",
    "ResultHandle",
    "ToolOrigin",
    "alias_for",
    "bindable_from_definitions",
    "budgeted",
    "effective_bindings",
    "exclusion_reason",
    "handle_descriptor",
    "is_handle_descriptor",
    "measure_bytes",
]

ToolOrigin = Literal["code", "http", "mcp", "a2a", "builtin"]
"""Where a bindable tool came from. Decides how a call is routed, never
whether it may be made."""


@dataclass(frozen=True, slots=True)
class BindableTool:
    """One tool a program could call, with what the runtime needs to gate it.

    Attributes:
        definition: the tool exactly as the model-facing catalogue holds it,
            under its model-facing name. Carries the annotations the approval
            selectors match on, which is why the whole definition travels
            rather than the name alone: looking annotations up in the code
            registry finds nothing for an MCP or HTTP tool, and a tool with no
            annotations is a tool no selector can gate.
        origin: which caller runs it.
        server: the owning MCP server or A2A peer alias, for a report and for
            grouping in an editor. ``None`` for code, HTTP and built-ins.
    """

    definition: ToolDefinition
    origin: ToolOrigin
    server: str | None = None

    @property
    def name(self) -> str:
        """The model-facing name. What a program passes to ``call_tool``."""
        return self.definition.name


def exclusion_reason(name: str) -> str | None:
    """Why ``name`` is not code-bindable, or ``None`` when it is.

    Only built-ins are excluded by name. A code, HTTP, MCP or A2A tool is
    bindable whenever the Run can call it at all, which is the point: a program
    gets what the agent has and never more.
    """
    return EXCLUDED_BUILTINS.get(name)


# ---------------------------------------------------------------------------
# Building the set
# ---------------------------------------------------------------------------


def bindable_from_definitions(
    definitions: Iterable[ToolDefinition], origin: ToolOrigin, server: str | None = None
) -> list[BindableTool]:
    """Wrap definitions the resolver already narrowed, dropping the excluded."""
    return [
        BindableTool(definition=definition, origin=origin, server=server)
        for definition in definitions
        if exclusion_reason(definition.name) is None
    ]


def alias_for(name: str) -> str | None:
    """The bare Python name a program may call ``name`` by, or ``None``.

    An MCP tool may legally be called ``list-repos``, ``2fa`` or ``class``.
    None of those can be a name in a program, so they are reachable only
    through ``call_tool``. A name that *is* a usable identifier gets an alias
    as well, because ``await search(query="x")`` is what a model writes without
    being told twice.

    Deliberately not a mangling: ``list-repos`` does not become ``list_repos``.
    Two servers can offer ``list-repos`` and ``list_repos``, and a mangling
    would make one silently answer for the other. A name that cannot be an
    identifier simply has no alias.
    """
    if not name.isidentifier() or keyword.iskeyword(name) or keyword.issoftkeyword(name):
        return None
    if name.startswith("__"):
        # Dunders in the program's globals collide with the machinery the
        # bootstrap puts there to run the program at all.
        return None
    return name


@dataclass(frozen=True, slots=True)
class EffectiveBindings:
    """What one execution may call, and what it asked for and did not get.

    Attributes:
        tools: the bindings, sorted by name. Every one is callable right now
            by this Run under every narrowing plane.
        missing: names an explicit ``CodeExecution.bindings`` asked for that
            are not callable this turn -- removed, renamed, denied by a tenant
            policy, withheld by a failure streak, or on an optional server that
            did not answer. Reported rather than silently dropped, and never
            substituted with something else (DESIGN.md §10.7).
    """

    tools: tuple[BindableTool, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Every callable binding name, sorted."""
        return tuple(tool.name for tool in self.tools)

    @property
    def aliases(self) -> tuple[str, ...]:
        """The names a program may call as bare functions, sorted."""
        return tuple(name for name in self.names if alias_for(name) is not None)

    def by_name(self) -> dict[str, BindableTool]:
        return {tool.name: tool for tool in self.tools}


def effective_bindings(
    bindable: Sequence[BindableTool], *, requested: frozenset[str] | None
) -> EffectiveBindings:
    """Intersect what the Run can call with what the Spec and policy asked for.

    Args:
        bindable: every tool this turn's resolution found callable, already
            narrowed by the deployment, the tenant policy, the MCP server's
            ``allow`` rules, the Spec's grants and the failure-streak guard.
        requested: the explicit binding filter from the Spec's
            ``CodeExecution.bindings``, intersected with any
            ``CodeExecutionPolicy`` narrowing. ``None`` means the Spec named no
            subset, so every bindable tool is offered.

    Returns:
        The bindings, and the requested names that are not available.

    This only ever removes. ``requested`` cannot introduce a name ``bindable``
    does not hold: a name in the filter that no longer resolves comes back in
    ``missing``, never as a binding.
    """
    available = {tool.name: tool for tool in bindable}
    if requested is None:
        chosen = sorted(available)
        missing: list[str] = []
    else:
        chosen = sorted(name for name in requested if name in available)
        missing = sorted(name for name in requested if name not in available)
    return EffectiveBindings(
        tools=tuple(available[name] for name in chosen), missing=tuple(missing)
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BindingCounter:
    """How much of a Run's per-Run binding allowance is already spent.

    Per execution *and* per Run, because the two bound different abuses: one
    runaway loop, and a model that writes a fresh program every turn to get a
    fresh allowance.

    The per-Run half has to survive the process that spent it. A Worker can die
    holding a lease and another can reclaim the same Attempt; an allowance kept
    in memory would reset at exactly that moment, so a Run that crashed its way
    through ten programs would get ten fresh allowances and the cap would bound
    nothing. So the real runtime passes ``source``: a reader over the Run's own
    folded log, which is the same number before and after a reclaim because it
    is derived from records rather than remembered.

    ``calls`` is the fallback for a caller with no log to read -- a unit test,
    or a direct user of :func:`budgeted`. When ``source`` is set it is the
    authority and ``calls`` is left alone, so there is no second, divergent
    tally to reconcile.

    Attributes:
        calls: in-process count, used only when ``source`` is None.
        source: reads the durable count of binding calls this Run has started.
    """

    calls: int = 0
    source: Callable[[], int] | None = None

    @property
    def spent(self) -> int:
        """Binding calls this Run has already started, from whichever
        authority this counter has."""
        return self.calls if self.source is None else self.source()

    def spend_call(self) -> None:
        """Record one more call, for a counter with no durable source.

        A no-op when ``source`` is set: the call's own ``tool_call_started``
        record is what spends the allowance there, and adding to a second
        tally would charge it twice.
        """
        if self.source is None:
            self.calls += 1


@dataclass(slots=True)
class _ExecutionSpend:
    calls: int = 0
    total_bytes: int = 0


def budgeted(
    host_call: Callable[[str, dict[str, Any]], Awaitable[Any]],
    budget: BindingBudget,
    counter: BindingCounter | None = None,
) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """Wrap one execution's host call with the budget it must stay inside.

    Host-side on purpose. Every backend reaches its bindings through this one
    callable -- a subprocess over a socket, a container over a bind mount, a
    remote service over NDJSON, a scripted double calling it directly -- so a
    counter here is a counter every backend has, and one buried in the framed
    wire protocol would leave the remote path uncounted.

    A budget that runs out raises ``ProgramToolRefused`` with a stable kind, so
    the program sees a typed failure it can catch rather than a wall-clock
    timeout it cannot explain.
    """
    spend = _ExecutionSpend()
    run_counter = counter if counter is not None else BindingCounter()

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if spend.calls >= budget.max_calls:
            raise ProgramToolRefused(
                "binding_calls_exhausted",
                f"tool {name!r}",
                f"this program has already made {budget.max_calls} host tool calls, which "
                "is all one program gets. Return what you have and do the rest in "
                "another program, or do less work per call.",
            )
        if run_counter.spent >= budget.max_calls_per_run:
            raise ProgramToolRefused(
                "binding_calls_exhausted_for_run",
                f"tool {name!r}",
                f"this Run has made {budget.max_calls_per_run} host tool calls from "
                "programs, which is all one Run gets. Answer with what you have.",
            )
        sent = _measure(arguments)
        if sent > budget.max_argument_bytes:
            raise ProgramToolRefused(
                "binding_arguments_too_large",
                f"tool {name!r}",
                f"the arguments are {sent} bytes and one call may send "
                f"{budget.max_argument_bytes}. Send a reference rather than the data.",
            )
        if spend.total_bytes + sent > budget.max_total_bytes:
            raise ProgramToolRefused(
                "binding_traffic_exhausted",
                f"tool {name!r}",
                f"this program has moved {spend.total_bytes} bytes through host tool "
                f"calls and may move {budget.max_total_bytes}. Filter inside the program "
                "rather than moving everything through it.",
            )

        spend.calls += 1
        run_counter.spend_call()
        spend.total_bytes += sent

        result = await host_call(name, arguments)

        received = _measure(result)
        if received > budget.max_result_bytes:
            raise ProgramToolRefused(
                "binding_result_too_large",
                f"tool {name!r}",
                f"it answered {received} bytes and one call may receive "
                f"{budget.max_result_bytes}. The call was made and is in the log; ask "
                "for less of it, or call it directly so the result can be paged.",
            )
        if spend.total_bytes + received > budget.max_total_bytes:
            raise ProgramToolRefused(
                "binding_traffic_exhausted",
                f"tool {name!r}",
                f"the answer would take this program past {budget.max_total_bytes} bytes "
                "of host tool traffic. The call was made and is in the log.",
            )
        spend.total_bytes += received
        return result

    return call


def measure_bytes(value: Any) -> int:
    """How many bytes this value is worth, for the traffic budget.

    Serialised rather than guessed, because a nested structure's cost is not
    visible from its top level and the wire cost is what the budget is about.
    A value that will not serialise is counted as zero here and refused a
    moment later by the protocol, which is where that failure belongs.
    """
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


_measure = measure_bytes


def handle_descriptor(*, tool: str, handle: str, size_bytes: int, stored: str) -> ResultHandle:
    """What a program gets instead of a result too large to send it.

    Opaque on purpose: a handle names a result inside one Run's own log and
    carries no key, path, URL or credential, so it is worth nothing to
    anything but the Run that minted it.

    Args:
        tool: the tool whose result this names. Included so a program that
            fanned out can tell its handles apart without bookkeeping.
        handle: the log's own handle for the stored result.
        size_bytes: how large the whole result is.
        stored: ``"log"`` or ``"blob"`` -- where the bytes live. Reported
            because it is the honest answer to "is all of it still there",
            and it is what a person reading a trace will ask."""
    return ResultHandle(handle=handle, tool=tool, size_bytes=size_bytes, stored=stored)


def is_handle_descriptor(value: Any) -> bool:
    """Whether ``value`` is a handle rather than a result.

    A type test, and never a look inside: nothing a tool puts in its own
    result can make that result one of these."""
    return isinstance(value, ResultHandle)
