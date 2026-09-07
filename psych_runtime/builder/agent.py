"""The typed builder for an Agent Spec.

DESIGN.md §4: the Python builder is one of four authoring forms, and none is
privileged. What this produces, ``AgentBuilder.build()``, is exactly the same
``AgentSpec`` a consumer would get from validating an equivalent dict, so it
hashes the same (``psych_runtime.core.version.compute_hash``) and runs identically.
"""

from __future__ import annotations

from typing import Literal, Self

from psych_runtime.builder.errors import BuilderError
from psych_runtime.builder.shared import SharedBuilder
from psych_runtime.core.spec import (
    AgentSpec,
    Limits,
    ModelRef,
    Skill,
    SubagentRef,
    SuspensionPolicy,
)
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["AgentBuilder", "agent"]


def agent(name: str, *, registry: ToolRegistry | None = None) -> AgentBuilder:
    """Start building an Agent Spec named ``name``."""
    return AgentBuilder(name, registry=registry)


class AgentBuilder(SharedBuilder):
    """Fluent construction of an ``AgentSpec``.

    ```python
    spec = (
        AgentBuilder("support")
        .instructions("Help the customer with their order.")
        .model("gpt-4o", temperature=0.2, fallbacks=["gpt-4o-mini"])
        .tool(lookup_order)
        .http_tool(
            "issue_refund",
            description="Issue a refund for an order.",
            url="https://api.example.com/orders/{order_id}/refund",
            method="POST",
            credential="payments-api-key",
        )
        .skill("refund-policy", "When a refund is allowed", body="...")
        .limits(max_steps=32)
        .build()
    )
    ```

    Every method returns ``self``, so a Spec reads as the sequence of grants
    that built it. Nothing here is stateful beyond this one builder and the
    ``ToolRegistry`` it owns (or was handed): calling ``.build()`` any number
    of times replays the same fields into a fresh, equally valid ``AgentSpec``.
    """

    def __init__(self, name: str, *, registry: ToolRegistry | None = None) -> None:
        super().__init__(registry=registry)
        self._name = name
        self._description = ""
        self._instructions = ""
        self._model: ModelRef | None = None
        self._skills: list[Skill] = []
        self._subagents: list[SubagentRef] = []

    def description(self, text: str) -> Self:
        """A short summary, shown to a caller choosing between agents (for
        example a parent's delegation tool listing its subagents)."""
        self._description = text
        return self

    def instructions(self, text: str) -> Self:
        """The system prompt body. May reference a skill with
        ``[[skill:name]]``; publish-time validation rejects a dangling one
        (DESIGN.md §16)."""
        self._instructions = text
        return self

    def model(
        self,
        model: str,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: Literal["low", "medium", "high"] | None = None,
        fallbacks: tuple[str, ...] = (),
    ) -> Self:
        """Which model, and how it is called. ``fallbacks`` is failover for a
        provider outage, tried in order on transient failure, never routing
        on cost or capability (DESIGN.md §19)."""
        self._model = ModelRef(
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            fallbacks=fallbacks,
        )
        return self

    def skill(self, name: str, description: str, body: str) -> Self:
        """An instruction pack loaded on demand (DESIGN.md §16): ``description``
        sits in the system prompt always, ``body`` loads only when the model
        calls ``load_skill``."""
        self._skills.append(Skill(name=name, description=description, body=body))
        return self

    def subagent(self, name: str, description: str, spec: AgentSpec | AgentBuilder) -> Self:
        """A nested agent this agent may delegate to (DESIGN.md §17).
        ``description`` must be at least 20 characters: DESIGN.md §17 traces
        bad routing back to vague descriptions more often than anything else,
        and the parent's delegation tool shows this text to decide between
        subagents."""
        resolved = spec.build() if isinstance(spec, AgentBuilder) else spec
        self._subagents.append(SubagentRef(name=name, description=description, spec=resolved))
        return self

    def build(self) -> AgentSpec:
        """Validate and produce the ``AgentSpec``.

        Raises:
            BuilderError: no model was set. Every other field has a sensible
                default; the model does not, because there is no default
                model Psych could pick that would not surprise someone.
        """
        if self._model is None:
            raise BuilderError(
                f"agent {self._name!r} has no model; call .model(...) before .build()"
            )
        return AgentSpec(
            name=self._name,
            description=self._description,
            instructions=self._instructions,
            model=self._model,
            tools=tuple(self._tools),
            mcp_servers=tuple(self._mcp_servers),
            skills=tuple(self._skills),
            subagents=tuple(self._subagents),
            limits=self._limits if self._limits is not None else Limits(),
            suspension=self._suspension if self._suspension is not None else SuspensionPolicy(),
        )
