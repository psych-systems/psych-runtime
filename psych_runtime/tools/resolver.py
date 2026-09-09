"""Resolving the tool set, once per turn.

DESIGN.md §10.2. A ``ToolResolver`` runs at the start of **every turn**, takes the
Run's Scope and pinned Version, and returns a concrete tool set. Nothing about
the tool set is fixed at process start.

## The freshness contract

Fresh at every turn boundary. Connect an MCP server and the next turn has its
tools, with no restart.

Mid-turn mutation is not offered, and that is a decision rather than a
limitation. Changing the tool set inside a turn rewrites the provider's cached
prompt prefix and contradicts what the model was told it could do at the start of
that turn. The prompt assembler depends on this holding (``psych_runtime.model.prompt``),
so the two decisions hold each other up.

The other obvious scope is once per conversation, which buys the same stability
inside a turn and then keeps a server connected mid-Run invisible until the Run
ends. Per turn is the smallest window that holds the model's view still for as
long as the model is looking at it.

## Everything narrows through one function

``psych_runtime.tools.narrowing.narrow`` is called here and by the publish-time
validator, and by nothing else. DESIGN.md §10.5 requires exactly that, because
two implementations of "what may this Run call" drift, and drift between the
validator and the runtime is the worst kind: it either blocks work that should
be allowed or allows work that should be blocked, and which one is not
predictable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import A2APeer, AgentSpec, HttpTool, McpServer, WorkflowSpec
from psych_runtime.tools.deferred import (
    DEFAULT_CATALOGUE_BUDGET_CHARS,
    ServerSummary,
    deferred_definitions,
    is_deferred,
    servers_advisory,
    summarise_description,
)
from psych_runtime.tools.failure_streak import advisory_message, assess
from psych_runtime.tools.mcp_names import mcp_tool_name
from psych_runtime.tools.narrowing import narrow
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["A2ACatalog", "McpCatalog", "ResolvedTools", "TenantToolPolicy", "ToolResolver"]


@runtime_checkable
class McpCatalog(Protocol):
    """What the resolver needs from MCP, and nothing more.

    A protocol rather than a direct import so the resolver can be tested without
    a server, and so the MCP implementation can change shape without this file
    moving. The resolver does not care how connections are pooled; it cares that
    asking for a Scope's view of a server returns that Scope's view.
    """

    async def tools_for(self, scope: Scope, server: McpServer) -> Sequence[ToolDefinition]:
        """Every tool ``server`` currently offers to ``scope``.

        Raises:
            Exception: the server is unreachable. The resolver decides what that
                means from the Spec's ``optional`` flag, so this raises rather
                than returning an empty list: an empty list is a server that
                answered and offers nothing, which is a different thing.
        """
        ...

    async def describe(self, scope: Scope, server: McpServer) -> str | None:
        """What ``server`` is for, or ``None`` when nobody has said.

        Runtime data, never Spec data: a description on ``McpServer`` would
        join the Version hash and make editing one republish every agent that
        names the server. Never raises, because a missing description is not a
        reason to fail a turn.
        """
        ...


@runtime_checkable
class A2ACatalog(Protocol):
    """What the resolver needs from A2A peers, and nothing more.

    Deliberately the same two methods as ``McpCatalog``, because from this
    file's point of view a peer agent and an MCP server are the same kind of
    thing: a remote system whose current offering has to be asked for at every
    turn boundary, narrowed by the same function, and reported as unavailable
    rather than silently dropped when it does not answer (DESIGN.md §10.7).
    """

    async def tools_for(self, scope: Scope, peer: A2APeer) -> Sequence[ToolDefinition]:
        """Every skill ``peer`` currently offers ``scope``, as tools.

        Raises when the peer cannot be reached, for the reason ``McpCatalog``
        gives: an empty list means "answered, offers nothing", which is a
        different fact.
        """
        ...

    async def describe(self, scope: Scope, peer: A2APeer) -> str | None:
        """What the peer says it is, or ``None``. Never raises."""
        ...


@runtime_checkable
class TenantToolPolicy(Protocol):
    """What the tenant permits, the middle plane of DESIGN.md §10.5.

    Separate from the consumer's ``Policy`` port because this answers "which
    tools exist for this tenant" while Policy answers "may this principal make
    this specific call with these arguments". The first shapes the prompt, the
    second gates a call.
    """

    async def permitted_tools(self, scope: Scope, server: str | None) -> Sequence[str]:
        """Tool name patterns this tenant permits. Empty means everything.

        ``server`` is the MCP server name, or ``None`` for code and HTTP tools.
        """
        ...


@dataclass(frozen=True, slots=True)
class ResolvedTools:
    """One turn's tool set, and what was left out of it.

    The omissions matter as much as the inclusions. DESIGN.md §10.7 is explicit
    that a tool silently disappearing produces an agent that confidently tells a
    customer it cannot issue refunds today, so anything dropped is recorded and
    said out loud.

    Attributes:
        definitions: what the model is offered. Not itself guaranteed
            byte-stable across processes -- an MCP server's own catalogue
            order is outside this resolver's control -- because the
            guarantee is enforced once, downstream, at
            ``psych_runtime.model.port.ModelRequest`` construction rather than here;
            see that module's ``psych_runtime.model.tool_normalize`` for why.
        advisories: what to add to the system prompt about tools that are gone.
        withheld: tools the failure-streak guard removed.
        unavailable_servers: optional MCP servers that did not answer.
    """

    definitions: tuple[ToolDefinition, ...]
    advisories: tuple[str, ...] = ()
    withheld: frozenset[str] = frozenset()
    unavailable_servers: tuple[str, ...] = ()
    names: frozenset[str] = field(default_factory=frozenset)
    deferred_servers: tuple[str, ...] = ()
    """Servers whose catalogue stayed out of the prompt this turn, reached
    through ``list_tools``/``get_tool_info``/``call_tool`` instead
    (``psych_runtime.tools.deferred``). Named here so a report can say which, and so
    the executor knows which servers those three tools may address."""


class ToolResolver:
    """Builds the tool set for one turn.

    Holds no per-Run state. Everything it needs arrives as an argument, so two
    Workers resolving the same turn of the same Run produce the same set, which
    is what makes a crash invisible to the model.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        catalog: McpCatalog | None = None,
        peers: A2ACatalog | None = None,
        tenant_policy: TenantToolPolicy | None = None,
        catalogue_budget_chars: int = DEFAULT_CATALOGUE_BUDGET_CHARS,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._peers = peers
        """The A2A catalog, or ``None`` for a Worker with no peer client wired.
        A Spec granting peers then resolves without them, exactly as it does
        for MCP with no catalog: the Worker cannot reach them, and pretending
        otherwise would offer the model a tool that could never run."""
        self._tenant_policy = tenant_policy
        self._catalogue_budget_chars = catalogue_budget_chars
        """How many characters of tool definitions one server may put in the
        prompt before its catalogue is deferred instead. Characters, never
        tokens; see ``psych_runtime.tools.deferred``."""

    async def resolve(
        self,
        spec: AgentSpec | WorkflowSpec,
        scope: Scope,
        *,
        failure_streaks: Mapping[str, int] | None = None,
        extra: Sequence[ToolDefinition] = (),
    ) -> ResolvedTools:
        """The concrete tool set for the turn about to start.

        Args:
            spec: the pinned Spec. Its grants are the third narrowing plane.
            scope: whose Run this is. Decides what the tenant permits and which
                pooled MCP connection answers.
            failure_streaks: consecutive failures per tool, from the reducer.
                Tools past the threshold are withheld and said out loud.
            extra: built-in tools the runtime adds for this turn, such as
                ``read_tool_output`` when a large result is outstanding. Passed
                in rather than assumed so a turn with nothing to read is not
                offered a tool that would do nothing.

        Returns:
            The tool set and everything that was left out of it.

        Raises:
            AccessDenied: a required MCP server did not answer. DESIGN.md §10.7
                makes this fail the Run by default, because the alternative is an
                agent quietly losing an ability mid-conversation.
        """
        streaks = failure_streaks or {}
        definitions: list[ToolDefinition] = []
        advisories: list[str] = []
        withheld: set[str] = set()
        unavailable: list[str] = []

        local = await self._local_tools(spec, scope)
        mcp, unavailable, mcp_advisories, servers = await self._mcp_tools(spec, scope)
        advisories.extend(mcp_advisories)
        peers, peer_unavailable, peer_advisories = await self._a2a_tools(spec, scope)
        unavailable.extend(peer_unavailable)
        advisories.extend(peer_advisories)
        deferred_servers = [server for server in servers if server.deferred]
        extra = (*extra, *deferred_definitions([server.name for server in deferred_servers]))
        advisories.extend(servers_advisory(servers))

        seen: set[str] = set()
        for definition in [*local, *mcp, *peers, *extra]:
            if definition.name in seen:
                raise AccessDenied(
                    f"tool catalogue name {definition.name!r}",
                    "more than one callable tool resolved to this name",
                )
            seen.add(definition.name)
            verdict = assess(
                definition.name,
                streaks.get(definition.name, 0),
                threshold=spec.limits.failure_streak_threshold,
                hard_stop=spec.limits.failure_streak_hard_stop,
            )
            if verdict.withhold:
                withheld.add(definition.name)
                advisories.append(advisory_message(definition.name, verdict.streak))
                continue
            definitions.append(definition)

        return ResolvedTools(
            definitions=tuple(definitions),
            advisories=tuple(advisories),
            withheld=frozenset(withheld),
            unavailable_servers=tuple(unavailable),
            names=frozenset(definition.name for definition in definitions),
            deferred_servers=tuple(server.name for server in deferred_servers),
        )

    async def _local_tools(
        self, spec: AgentSpec | WorkflowSpec, scope: Scope
    ) -> list[ToolDefinition]:
        """Code and HTTP tools: the Spec grants them and the tenant may narrow them.

        Code tools resolve through the registry so the model sees the schema
        derived from the live function rather than a copy in the Spec. HTTP tools
        carry their own definition, because they are data a user created at
        runtime and there is nothing registered to look up.
        """
        by_name: dict[str, ToolDefinition] = {}
        for tool in spec.tools:
            if isinstance(tool, HttpTool):
                by_name[tool.name] = ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    annotations=_http_annotations(tool),
                )
            else:
                registered = self._registry.get(tool.name)
                if registered is None:
                    # Publish-time validation catches this when the publishing
                    # process has the tool registered. It cannot catch a Worker
                    # that booted without registering it, so the tool is dropped
                    # here rather than crashing the turn, and the omission is
                    # visible in the resolved set.
                    continue
                by_name[tool.name] = registered.definition()

        permitted = await self._tenant_allows(scope, server=None)
        allowed = narrow(list(by_name), permitted, [])
        return [by_name[name] for name in allowed]

    async def _mcp_tools(
        self, spec: AgentSpec | WorkflowSpec, scope: Scope
    ) -> tuple[list[ToolDefinition], list[str], list[str], list[ServerSummary]]:
        """MCP tools, narrowed per server through the one narrowing function.

        Returns the definitions to offer, the optional servers that did not
        answer, the advisories to add to the prompt, and one summary per server
        that answered: its name, what this Run may reach on it, what it is for
        and whether its catalogue was deferred.
        """
        if self._catalog is None or not spec.mcp_servers:
            return [], [], [], []

        collected: list[ToolDefinition] = []
        unavailable: list[str] = []
        advisories: list[str] = []
        summaries: list[ServerSummary] = []

        for server in spec.mcp_servers:
            try:
                offered = await self._catalog.tools_for(scope, server)
            except Exception as err:
                if not server.optional:
                    raise AccessDenied(
                        f"MCP server {server.name!r}",
                        f"it did not answer at the start of this turn ({err}). Mark the "
                        "connection optional=True if the Run should continue without "
                        "its tools.",
                    ) from err
                unavailable.append(server.name)
                advisories.append(
                    f"The tools from {server.name!r} are unavailable right now because "
                    "the server did not answer. Do not claim you performed anything "
                    "that needed them. Say they are temporarily unavailable if the "
                    "user asks for something they would have covered."
                )
                continue

            by_name = {definition.name: definition for definition in offered}
            permitted = await self._tenant_allows(scope, server=server.name)
            allowed = narrow(list(by_name), permitted, list(server.allow))
            # Measured on what this Run may actually see rather than on the
            # server's whole surface: a Spec allowing six tools from a huge
            # server costs six tools' worth of prompt, so it should not be
            # deferred for the size of a catalogue it cannot reach.
            visible = [by_name[name] for name in allowed]
            deferred = is_deferred(server, visible, budget_chars=self._catalogue_budget_chars)
            summaries.append(
                ServerSummary(
                    name=server.name,
                    tool_count=len(allowed),
                    description=summarise_description(await self._describe(scope, server)),
                    deferred=deferred,
                )
            )
            if deferred:
                # The catalogue stays out of the prompt. What the model is told
                # is that the server exists and how to reach it, which is the
                # difference between a tool it can discover and one that
                # silently is not there (DESIGN.md §10.7).
                continue
            # A model tool call carries a name and arguments, but no MCP
            # server. Keep the owner explicit through execution and replay.
            collected.extend(
                definition.model_copy(update={"name": mcp_tool_name(server.name, definition.name)})
                for definition in visible
            )

        return collected, unavailable, advisories, summaries

    async def _a2a_tools(
        self, spec: AgentSpec | WorkflowSpec, scope: Scope
    ) -> tuple[list[ToolDefinition], list[str], list[str]]:
        """Peer skills, narrowed through the one narrowing function.

        A ``WorkflowSpec`` grants no peers -- delegation to another
        organisation's agent is an agent's decision, made with a model in the
        loop, and a workflow step that called out to one would be doing it
        without the judgement that makes it safe -- so this returns nothing
        for one rather than growing a field on that model.

        Peer catalogues are never deferred. A deferred MCP server exists
        because 351 tools cost 2.0 MB of prompt; a peer contributes one tool
        per declared skill, and a card with enough skills to matter is not a
        shape anyone is serving.
        """
        peers = spec.a2a_peers if isinstance(spec, AgentSpec) else ()
        if self._peers is None or not peers:
            return [], [], []

        collected: list[ToolDefinition] = []
        unavailable: list[str] = []
        advisories: list[str] = []

        for peer in peers:
            try:
                offered = await self._peers.tools_for(scope, peer)
            except Exception as err:
                if not peer.optional:
                    raise AccessDenied(
                        f"A2A peer {peer.name!r}",
                        f"it did not answer at the start of this turn ({err}). Mark the "
                        "peer optional=True if the Run should continue without its "
                        "skills.",
                    ) from err
                unavailable.append(peer.name)
                advisories.append(
                    f"The {peer.name!r} agent is unavailable right now because it did "
                    "not answer. Do not claim you asked it anything. Say it is "
                    "temporarily unreachable if the user asks for something it would "
                    "have handled."
                )
                continue
            collected.extend(offered)

        return collected, unavailable, advisories

    async def _describe(self, scope: Scope, server: McpServer) -> str | None:
        """The catalog's description of one server, never fatal.

        A catalog that raises here would fail a turn over prompt decoration,
        and a consumer's own lookup is the most likely thing in this path to
        be wrong. The server still appears in the advisory, without a
        description, which is exactly what it would have done before anyone
        wrote one.
        """
        if self._catalog is None:
            return None
        try:
            return await self._catalog.describe(scope, server)
        except Exception:
            # Deliberately broad: a description is never worth a failed Run.
            return None

    async def _tenant_allows(self, scope: Scope, server: str | None) -> list[str]:
        if self._tenant_policy is None:
            return []
        return list(await self._tenant_policy.permitted_tools(scope, server))


def _http_annotations(tool: HttpTool) -> frozenset[str]:
    """Infer an annotation for an HTTP tool from its method.

    A GET reads. Anything else writes. DESIGN.md §10.9 treats an unannotated tool
    as ``write``, so this only ever moves a tool to the safer classification when
    the method proves it is a read; it never claims a write is a read.
    """
    return frozenset({"read-only"}) if tool.method == "GET" else frozenset({"write"})
