"""Deferred tool disclosure: a big catalogue the model discovers instead of reading.

DESIGN.md §10.3. A connection marked not-to-preload contributes no tool
definitions to the prompt at all. In their place the model gets three tools --
list what a server offers, read one tool's schema, call one tool -- and one
line per deferred server saying it is there.

A deferred tool is addressed by its plain ``(server, tool)`` pair, not by an
opaque handle the runtime hands out. The model already knows both names from
the listing, so a handle would add a lookup table, a lifetime and a way for a
handle to outlive the catalogue it came from, and buy nothing.

## Why this exists, in numbers from a real server

A production MCP server in front of this project offered 351 tools whose JSON
Schemas came to 2.0 MB. Preloaded, every turn of every Run sent all of it:
past the request limit of several providers outright, and where it was
accepted, paid for on every turn of every conversation, for a set of tools the
model uses two or three of. The agent did not fail in a way that named this;
it failed with whatever the provider says about a request that large.

## The rule for when it applies

``McpServer.preload`` is three-valued and defaults to ``None``, meaning
*decide from the catalogue*: a server whose tools would cost more than the
configured budget is deferred, one that fits is not. That default is the
honest one because the Spec's author cannot know what a server offers -- the
catalogue is discovered from the server, at run time, and it changes. ``True``
and ``False`` pin the decision for an author who does know.

## The budget is measured in characters, and that word is deliberate

Not tokens. A token count needs a tokenizer, tokenizers differ per provider,
and the same number would then mean different things depending on which model
an agent happens to name. A character count is exact, provider-independent and
needs no dependency. It is a coarser proxy for cost than tokens, and that is
an acceptable trade for a threshold whose only job is to separate "a handful
of tools" from "a directory".

The number is never scaled to resemble a token estimate. A figure that looks
like a token count but is not would be worse than an honest character count,
so every place a person can see this number says *characters*.

An earlier version counted tools instead, deferring above forty. Count is a
proxy for the thing that actually costs money and a poor one: ten tools
carrying 50 KB schemas each are far worse for a prompt than eighty tools with
three fields apiece, and a count-based rule gets that case exactly backwards.
The argument for count was that a server editing its descriptions could flip
the decision between two runs. True, but count has the same instability (a
server can add tools) and buys it by measuring the wrong quantity.

## What does not change

Narrowing. ``call_tool`` resolves its target through the same
``psych_runtime.tools.narrowing.narrow`` the resolver applies, against the same three
planes, so a tool the Spec's ``allow`` list excludes is refused however the
model reached it. Discovery is not access: ``list_tools`` shows a deferred
server's *permitted* names, not its catalogue.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, McpServer

__all__ = [
    "CALL_TOOL",
    "DEFAULT_CATALOGUE_BUDGET_CHARS",
    "DEFERRED_TOOL_NAMES",
    "GET_TOOL_INFO",
    "LIST_TOOLS",
    "MAX_SERVER_DESCRIPTION_CHARS",
    "DeferredCatalog",
    "DeferredDiscovery",
    "ServerSummary",
    "catalogue_chars",
    "deferred_definitions",
    "is_deferred",
    "servers_advisory",
    "summarise",
    "summarise_description",
]

LIST_TOOLS: Final = "list_tools"
GET_TOOL_INFO: Final = "get_tool_info"
CALL_TOOL: Final = "call_tool"

DEFERRED_TOOL_NAMES: Final = frozenset({LIST_TOOLS, GET_TOOL_INFO, CALL_TOOL})
"""Reserved alongside Psych's other built-ins, so a Spec cannot define a tool
that shadows one and leave the model unable to address either."""

DEFAULT_CATALOGUE_BUDGET_CHARS: Final = 20_000
"""How many **characters** of tool definitions a server may contribute to the
prompt before ``preload=None`` defers it. Characters, not tokens: see this
module's docstring for why, and never scale this number to look like a token
count.

Twenty thousand is roughly where a catalogue stops being something worth
sending every turn and starts being a directory worth looking things up in.
It comfortably fits the servers most consumers connect (a handful of tools,
usually a few thousand characters all told) and defers the ones that are
really an API surface. The server this was built against measured 2,088,315
characters across 351 tools, which is a hundred times the budget.

A deployment with a different prompt budget sets its own on ``Runtime``.
"""

MAX_SERVER_DESCRIPTION_CHARS: Final = 400
"""How much of a connected server's description reaches the prompt.

**Characters, not tokens**, for the same reason the catalogue budget is (see
this module's docstring).

Capped because this text goes into the system prompt on every turn of every
Run against every agent that names the server, and an uncapped field is an
invitation to paste a README into the cached prefix. Four hundred characters
is two or three sentences: enough to say what a server is for and when to
reach for it, not enough to explain how to use it. Explaining how to use it is
what the tool descriptions are for.
"""

_DESCRIPTION_CAP: Final = 200
"""How much of a tool's description ``list_tools`` returns. The point of a
listing is to let the model choose which tool to read properly, and a listing
carrying full descriptions is the catalogue it was meant to avoid sending."""


def catalogue_chars(tools: Sequence[ToolDefinition]) -> int:
    """What these tools would cost the prompt, in characters.

    Every part a provider is actually sent: the name, the description, and the
    input schema serialised compactly. Schemas dominate, which is the whole
    reason this is measured rather than counted -- a tool with one string
    parameter and a tool with a forty-field nested object are one tool each and
    nothing alike.

    Compact separators because that is how the request is serialised on the
    wire; measuring pretty-printed JSON would overstate every schema by the
    whitespace nobody sends.
    """
    total = 0
    for tool in tools:
        total += len(tool.name) + len(tool.description)
        total += len(json.dumps(tool.input_schema, separators=(",", ":"), default=str))
    return total


def is_deferred(
    server: McpServer,
    tools: Sequence[ToolDefinition],
    *,
    budget_chars: int = DEFAULT_CATALOGUE_BUDGET_CHARS,
) -> bool:
    """Whether ``server``'s catalogue stays out of the prompt this turn.

    ``tools`` is what this Run may actually see, already narrowed, so a Spec
    that allows six tools from a huge server is measured on its six rather
    than on the server's whole surface.
    """
    if server.preload is not None:
        return not server.preload
    return catalogue_chars(tools) > budget_chars


def summarise(description: str) -> str:
    """One tool's description, capped for a listing."""
    text = description.strip().splitlines()[0] if description.strip() else ""
    if len(text) <= _DESCRIPTION_CAP:
        return text
    return f"{text[:_DESCRIPTION_CAP]}…"


@dataclass(frozen=True, slots=True)
class ServerSummary:
    """One connected server, as the model is told about it.

    Attributes:
        name: the Spec-local name, which is what ``list_tools`` is called with.
        tool_count: how many tools this Run may see, after narrowing. Not the
            server's whole surface, which the Run may not be granted.
        description: what the server is for, already capped. ``None`` when
            nobody described it and the server did not describe itself.
        deferred: whether its tools are absent from the prompt.
    """

    name: str
    tool_count: int
    description: str | None = None
    deferred: bool = False


def deferred_definitions(servers: Sequence[str]) -> tuple[ToolDefinition, ...]:
    """The three tools a model uses to reach a deferred server.

    Empty when nothing is deferred: a Run whose servers are all preloaded must
    not be offered tools that could only ever answer "no such server".
    """
    if not servers:
        return ()
    listed = ", ".join(repr(name) for name in servers)
    return (
        ToolDefinition(
            name=LIST_TOOLS,
            description=(
                "List the tools one connected server offers, by name and a short "
                f"description. Servers you can list: {listed}. Call this first when "
                "you need something one of them does; their tools are not listed "
                "above."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "which server to list"},
                    "search": {
                        "type": "string",
                        "description": (
                            "optional: only tools whose name or description contains this"
                        ),
                    },
                },
                "required": ["server"],
                "additionalProperties": False,
            },
            annotations=frozenset({"read-only"}),
        ),
        ToolDefinition(
            name=GET_TOOL_INFO,
            description=(
                "Read one tool's full description and input schema, from a server "
                f"listed by {LIST_TOOLS!r}. Do this before calling a tool you have "
                "not called before: the arguments are not guessable from the name."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "tool": {"type": "string"},
                },
                "required": ["server", "tool"],
                "additionalProperties": False,
            },
            annotations=frozenset({"read-only"}),
        ),
        ToolDefinition(
            name=CALL_TOOL,
            description=(
                "Call one tool on a connected server. Use the exact tool name and "
                f"the argument shape {GET_TOOL_INFO!r} returned for it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server": {"type": "string"},
                    "tool": {"type": "string"},
                    "arguments": {
                        "type": "object",
                        "description": "the tool's own arguments",
                        "additionalProperties": True,
                    },
                },
                "required": ["server", "tool"],
                "additionalProperties": False,
            },
            # Unannotated-is-write (DESIGN.md §10.9), and the tool being reached
            # is not known until the call is made. The narrowing and approval
            # decision that matters happens against the *target* tool's own
            # annotations inside the executor, not against this wrapper.
            annotations=frozenset({"write"}),
        ),
    )


def summarise_description(text: str | None) -> str | None:
    """A server description, whitespace-collapsed and capped.

    Collapsed because a description pasted from a README arrives with newlines
    that would break the one-line-per-server shape the advisory block relies
    on, and because a blank line in the middle of a system prompt reads as a
    section boundary that is not there.
    """
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= MAX_SERVER_DESCRIPTION_CHARS:
        return collapsed
    return f"{collapsed[:MAX_SERVER_DESCRIPTION_CHARS].rstrip()}…"


def servers_advisory(servers: Sequence[ServerSummary]) -> list[str]:
    """One advisory line per connected server, plus how to reach a deferred one.

    A list rather than one block, because ``psych_runtime.model.prompt.advisories_block``
    renders each advisory as its own bullet under a single "Notices" heading.
    Returning a multi-line string with a heading of its own produced
    ``- ## Connected systems`` followed by orphaned bullets: broken Markdown,
    and a model reading it sees a section that starts inside a list item.

    Two things this has to do, and they pull the same way. A model told
    nothing about a deferred server will not think to look, which is
    DESIGN.md §10.7's failure exactly: an agent confidently telling a customer
    something cannot be done. And a model connected to three servers it can
    only tell apart by name cannot choose between them, which is the same
    failure by a different route.

    Empty when there is nothing worth saying: every server preloaded and none
    described. The tool list already tells the model what those servers offer,
    and a paragraph repeating it is prompt nobody is paying for on purpose.
    """
    if not servers:
        return []
    any_deferred = any(server.deferred for server in servers)
    any_described = any(server.description for server in servers)
    if not any_deferred and not any_described:
        return []

    lines: list[str] = []
    for server in servers:
        # "in your tool list", not "above" or "below": tool definitions travel
        # in their own field of the request rather than in this prompt, so
        # there is no position for them to be at relative to this sentence.
        listed = "not in your tool list" if server.deferred else "in your tool list"
        line = f"Connected system `{server.name}`: {server.tool_count} tool"
        line += "" if server.tool_count == 1 else "s"
        line += f", {listed}."
        if server.description:
            line += f" {server.description}"
        lines.append(line)

    if any_deferred:
        lines.append(
            f"For a connected system whose tools are not in your tool list, use "
            f"`{LIST_TOOLS}` to see what it offers, `{GET_TOOL_INFO}` to read one "
            f"tool's arguments, and `{CALL_TOOL}` to call it. Do not tell the user "
            "something cannot be done before checking."
        )
    return lines


def listing(tools: Sequence[ToolDefinition], search: str | None = None) -> list[dict[str, Any]]:
    """``list_tools``' answer: names and capped descriptions, optionally filtered."""
    needle = search.lower().strip() if search else ""
    return [
        {"name": tool.name, "description": summarise(tool.description)}
        for tool in tools
        if not needle or needle in tool.name.lower() or needle in tool.description.lower()
    ]


@runtime_checkable
class DeferredCatalog(Protocol):
    """What discovery needs from MCP: the tools one server offers this Run,
    already narrowed, and a way to call one.

    Structural for the same reason ``psych_runtime.tools.resolver.McpCatalog`` is: the
    executor should not have to hold an ``McpTools`` to know what discovery
    means, and a test should be able to supply two functions.
    """

    async def list_for(
        self, spec: AgentSpec, scope: Scope, server_name: str
    ) -> list[ToolDefinition]: ...

    async def call(
        self, spec: AgentSpec, scope: Scope, name: str, arguments: dict[str, Any]
    ) -> Any: ...


class DeferredDiscovery:
    """Executes ``list_tools``, ``get_tool_info`` and ``call_tool``.

    Bound to one Run's Spec and Scope by the runtime, the same way the MCP
    caller is: the model sends a server name and a tool name, and both have to
    be checked against what *this* Run is granted rather than against whatever
    the pool happens to hold.
    """

    def __init__(self, catalog: DeferredCatalog, scope: Scope) -> None:
        self._catalog = catalog
        self._scope = scope

    async def call(self, spec: AgentSpec, name: str, arguments: dict[str, Any]) -> Any:
        server = str(arguments.get("server", ""))
        match name:
            case _ if name == LIST_TOOLS:
                tools = await self._catalog.list_for(spec, self._scope, server)
                search = arguments.get("search")
                return {
                    "server": server,
                    "tools": listing(tools, search if isinstance(search, str) else None),
                    "total": len(tools),
                }
            case _ if name == GET_TOOL_INFO:
                wanted = str(arguments.get("tool", ""))
                tools = await self._catalog.list_for(spec, self._scope, server)
                found = next((tool for tool in tools if tool.name == wanted), None)
                if found is None:
                    raise AccessDenied(
                        f"tool {wanted!r}",
                        f"server {server!r} does not offer a tool of that name to this "
                        f"run. Call {LIST_TOOLS!r} for what it does offer.",
                    )
                return {
                    "server": server,
                    "name": found.name,
                    "description": found.description,
                    "input_schema": found.input_schema,
                    "annotations": sorted(found.annotations),
                }
            case _:
                target = str(arguments.get("tool", ""))
                raw = arguments.get("arguments")
                # The target's own name goes to the MCP caller, which narrows
                # it again against all three planes. Reaching a tool the Spec
                # excluded is refused there, whichever door the model used.
                return await self._catalog.call(
                    spec, self._scope, target, dict(raw) if isinstance(raw, dict) else {}
                )
