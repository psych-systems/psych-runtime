"""The fluent surface ``AgentBuilder`` and ``WorkflowBuilder`` share.

DESIGN.md §5: an Agent and a Workflow are two shapes over one engine, and
``psych_runtime.core.spec.AgentSpec`` and ``WorkflowSpec`` both carry tool grants, MCP
servers, limits and a suspension policy. Rather than writing ``.tool()``,
``.http_tool()``, ``.mcp_server()``, ``.limits()`` and ``.suspension()`` twice,
they live once here and both builders inherit them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal, Self

from psych_runtime.core.spec import (
    AnyTool,
    CodeTool,
    HttpTool,
    Limits,
    McpOAuth,
    McpServer,
    SuspensionPolicy,
)
from psych_runtime.tools.registry import ToolRegistry, ToolSchemaError

__all__ = ["SharedBuilder"]


class SharedBuilder:
    """Tool grants, MCP servers, limits and suspension, shared by both builders.

    Holds a ``ToolRegistry``. A callable passed to ``.tool()`` is registered
    here, and only its registered *name* ever reaches a Spec (DESIGN.md §4):
    a builder may accept a function, but what lands in the Spec is the name.
    """

    def __init__(self, *, registry: ToolRegistry | None = None) -> None:
        self._tools: list[AnyTool] = []
        self._mcp_servers: list[McpServer] = []
        self._limits: Limits | None = None
        self._suspension: SuspensionPolicy | None = None
        self._registry = registry if registry is not None else ToolRegistry()

    @property
    def registry(self) -> ToolRegistry:
        """Where ``.tool()`` registers callables.

        Exposed so a consumer can register more tools directly on it, share
        one registry across several builders in the same process, or hand it
        to whatever assembles the ``Runtime`` that will execute Specs this
        builder produces (DESIGN.md §21). The registry is process state; the
        Spec only ever holds the names it produced.
        """
        return self._registry

    def tool(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        interruptible: bool = True,
        safe_to_retry: bool = False,
        annotations: frozenset[str] | set[str] | None = None,
    ) -> Self:
        """Register ``fn`` as a code tool and grant it.

        The schema is derived from ``fn``'s type hints (DESIGN.md §10.1).
        What lands in the Spec is the registered name, never ``fn`` itself:
        the moment a Spec holds a live object it stops being serialisable,
        hashable and storable, and this is the ergonomic path that is meant
        to never hit ``psych_runtime.core.spec``'s own rejection of one.
        """
        # The keyword-argument overload, not the bare-decorator one: mypy has
        # no overload for "fn plus keyword overrides" even though the runtime
        # supports it, so this goes through the decorator-factory form
        # instead, which type-checks and behaves identically.
        self._registry.register(
            name=name,
            description=description,
            interruptible=interruptible,
            safe_to_retry=safe_to_retry,
            annotations=annotations,
        )(fn)
        lookup_name = name if name is not None else getattr(fn, "__name__", None)
        registered = self._registry.get(lookup_name) if lookup_name is not None else None
        if registered is None:
            # register() above either raised already or put exactly this name
            # in the registry; this exists so mypy sees a concrete name below
            # rather than because it can actually be reached.
            raise ToolSchemaError(f"registering {fn!r} did not add a tool named {lookup_name!r}")

        self._tools.append(CodeTool(name=registered.name, interruptible=interruptible))
        return self

    def tool_by_name(self, name: str, *, interruptible: bool = True) -> Self:
        """Grant a code tool this builder did not register itself.

        For a tool registered directly against ``.registry``, or one another
        process registers: DESIGN.md §10.1 notes two Workers on two machines
        both need the same functions registered, which is a property of how
        the consumer boots and not something a builder can enforce. Publish-
        time validation is what catches a name nobody ever registers.
        """
        self._tools.append(CodeTool(name=name, interruptible=interruptible))
        return self

    def http_tool(
        self,
        name: str,
        *,
        description: str,
        url: str,
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST",
        input_schema: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        credential: str | None = None,
        timeout_seconds: float = 30.0,
        interruptible: bool = True,
    ) -> Self:
        """Grant an HTTP tool: a URL, a method, a schema and a credential
        name, entirely data (DESIGN.md §10.1), so an end user could have
        created this one at runtime instead of a developer at build time.
        """
        self._tools.append(
            HttpTool(
                name=name,
                description=description,
                url=url,
                method=method,
                input_schema=input_schema if input_schema is not None else {},
                headers=headers if headers is not None else {},
                credential=credential,
                timeout_seconds=timeout_seconds,
                interruptible=interruptible,
            )
        )
        return self

    def mcp_server(
        self,
        name: str,
        url: str,
        *,
        transport: Literal["http", "sse"] = "http",
        credential: str | None = None,
        oauth: McpOAuth | None = None,
        allow: Sequence[str] = (),
        optional: bool = False,
    ) -> Self:
        """Grant an MCP connection, narrowed to ``allow`` (DESIGN.md §10.5).
        An empty ``allow`` means every tool the server offers, still subject
        to whatever the tenant permits at resolution time.

        ``transport`` mirrors ``McpServer.transport`` exactly. See that
        field's docstring in ``psych_runtime.core.spec`` for what ``"http"`` and the
        deprecated ``"sse"`` mean, and why ``"stdio"`` is not an option.

        ``oauth`` is this server's own OAuth 2.1 configuration, so two servers
        in one Spec can sit behind different authorization servers under
        different grants. It takes a ``client_secret_credential`` *name* and
        never a secret, the same way ``credential`` does.

        It is a parameter here rather than only a field on ``McpServer``
        because DESIGN.md §23's first item is that the same agent built in
        Python and built from a dict produce the same Version hash. A field
        the builder cannot express makes that false for any Spec using it."""
        self._mcp_servers.append(
            McpServer(
                name=name,
                url=url,
                transport=transport,
                credential=credential,
                oauth=oauth,
                allow=tuple(allow),
                optional=optional,
            )
        )
        return self

    def limits(
        self,
        *,
        max_steps: int | None = None,
        max_turns: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        deadline_seconds: float | None = None,
        transient_retry_budget: int | None = None,
        max_delegation_depth: int | None = None,
        max_fanout_per_turn: int | None = None,
        failure_streak_threshold: int | None = None,
        failure_streak_hard_stop: int | None = None,
        stream_idle_seconds: float | None = None,
        large_result_bytes: int | None = None,
        max_history_records: int | None = None,
    ) -> Self:
        """Set budgets, layered onto whatever an earlier ``.limits()`` call
        set. Every ``Limits`` field has a default (DESIGN.md §3: an unset
        limit is an unbounded bill), so only the fields given here change;
        the rest keep their current value, starting from ``Limits()``'s own
        defaults on the first call.
        """
        current = self._limits if self._limits is not None else Limits()
        given: dict[str, int | float | None] = {
            "max_steps": max_steps,
            "max_turns": max_turns,
            "max_tool_calls_per_turn": max_tool_calls_per_turn,
            "deadline_seconds": deadline_seconds,
            "transient_retry_budget": transient_retry_budget,
            "max_delegation_depth": max_delegation_depth,
            "max_fanout_per_turn": max_fanout_per_turn,
            "failure_streak_threshold": failure_streak_threshold,
            "failure_streak_hard_stop": failure_streak_hard_stop,
            "stream_idle_seconds": stream_idle_seconds,
            "large_result_bytes": large_result_bytes,
            "max_history_records": max_history_records,
        }
        updates = {key: value for key, value in given.items() if value is not None}
        self._limits = current.model_copy(update=updates)
        return self

    def suspension(
        self,
        *,
        approval_expires_seconds: float | None = None,
        question_expires_seconds: float | None = None,
        external_expires_seconds: float | None = None,
    ) -> Self:
        """Set suspension expiries, layered the same way ``.limits()`` is."""
        current = self._suspension if self._suspension is not None else SuspensionPolicy()
        given: dict[str, float | None] = {
            "approval_expires_seconds": approval_expires_seconds,
            "question_expires_seconds": question_expires_seconds,
            "external_expires_seconds": external_expires_seconds,
        }
        updates = {key: value for key, value in given.items() if value is not None}
        self._suspension = current.model_copy(update=updates)
        return self
