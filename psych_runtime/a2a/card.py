"""An Agent Card built from a Spec, because a Spec already says most of it.

§4.4.1 and §8. A card is name, description, what the agent can do, where to
reach it and how to authenticate. An ``AgentSpec`` already carries the first
three: it has a name, a description, and a tool set that is exactly "the
things this agent can do". The only parts a Spec cannot know are the ones that
belong to the deployment -- the URLs, the security schemes, the provider -- so
those are arguments here and everything else is derived.

## Skills are derived from tools, not invented

§4.4.5 describes a skill as "a distinct capability or function that an agent
can perform", "largely a descriptive concept". The temptation is to make a
consumer hand-write a skills list, which then goes stale the first time
somebody adds a tool. Deriving one skill per granted tool keeps the card
honest by construction: what the card advertises and what the agent can
actually call come from the same field of the same Spec.

Two consequences worth stating rather than hiding:

- An MCP server's tools are **not** enumerated. A Spec names servers and an
  allow list, and what those servers currently offer is a runtime question
  (``psych_runtime.tools.mcp``) that a card generated from a Spec cannot answer
  without connecting. Each granted server therefore contributes one skill
  describing the server, not one per tool. A card that listed tools it had
  not confirmed would be advertising an ability that may have disappeared,
  which is the failure DESIGN.md §10.7 spends its length on.
- A subagent contributes a skill too, because from outside the agent that is
  precisely what it is: a thing this agent can do.

A consumer who wants a curated list passes ``skills=`` and gets exactly that;
the derived list is a default, not a policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psych_runtime.a2a.models import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    SecurityRequirement,
    SecurityScheme,
)
from psych_runtime.a2a.negotiation import PROTOCOL_VERSION
from psych_runtime.core.spec import AgentSpec, HttpTool

__all__ = ["DEFAULT_MEDIA_TYPES", "agent_card", "skills_of"]

DEFAULT_MEDIA_TYPES: tuple[str, ...] = ("text/plain",)
"""What a Psych agent reads and writes unless the consumer says otherwise.

An agent's model may well accept images; whether *this deployment* accepts
them is a consumer decision about their own upload path, not something a Spec
knows, so the conservative pair is the default and ``input_modes`` /
``output_modes`` override it.
"""


def _tag(*values: str) -> tuple[str, ...]:
    """Tags, deduplicated and ordered, since §4.4.5 wants keywords not noise."""
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)


def skills_of(spec: AgentSpec) -> tuple[AgentSkill, ...]:
    """One skill per thing the Spec grants, in a stable order.

    Order follows the Spec's own, which ``psych_runtime.core.spec`` already sorts at
    validation for tools, servers and subagents. So two publishes of the same
    Spec produce byte-identical skills -- which matters more than it looks,
    because a signed card whose skill order wandered would fail its own
    signature check for no reason (§8.4.1's canonicalisation is over the
    serialised document, and RFC 8785 sorts object keys but never array
    elements).
    """
    skills: list[AgentSkill] = []

    for tool in spec.tools:
        description = (
            tool.description
            if isinstance(tool, HttpTool)
            else f"The {tool.name} tool, as registered by this agent's operator."
        )
        skills.append(
            AgentSkill(
                id=tool.name,
                name=tool.name,
                description=description,
                tags=_tag("tool"),
            )
        )

    for server in spec.mcp_servers:
        allowed = ", ".join(server.allow) if server.allow else "every tool it offers"
        skills.append(
            AgentSkill(
                id=f"mcp:{server.name}",
                name=server.name,
                description=(
                    f"Tools reached through the connected {server.name!r} MCP server "
                    f"({allowed}). The exact set is whatever that server offers at the "
                    "time of the call."
                ),
                tags=_tag("mcp", "tool"),
            )
        )

    for subagent in spec.subagents:
        skills.append(
            AgentSkill(
                id=f"subagent:{subagent.name}",
                name=subagent.name,
                description=subagent.description,
                tags=_tag("subagent"),
            )
        )

    return tuple(skills)


def agent_card(
    spec: AgentSpec,
    *,
    interfaces: Sequence[AgentInterface],
    version: str,
    description: str | None = None,
    provider: AgentProvider | None = None,
    security_schemes: dict[str, SecurityScheme] | None = None,
    security_requirements: Sequence[SecurityRequirement] = (),
    skills: Sequence[AgentSkill] | None = None,
    extensions: Sequence[AgentExtension] = (),
    input_modes: Sequence[str] = DEFAULT_MEDIA_TYPES,
    output_modes: Sequence[str] = DEFAULT_MEDIA_TYPES,
    streaming: bool = True,
    push_notifications: bool = True,
    extended_agent_card: bool = False,
    documentation_url: str | None = None,
    icon_url: str | None = None,
) -> AgentCard:
    """Build the card for one published agent.

    Args:
        spec: the pinned ``AgentSpec``. Its name, description and grants
            become the card's name, description and skills.
        interfaces: where this agent is reachable, preferred first (§4.4.6:
            "Ordered list of supported interfaces. The first entry is
            preferred"). Required, and not defaulted: an agent card with a
            guessed URL is worse than none, because a client will call it.
        version: the *agent's* version, not the protocol's (§4.4.1). A
            consumer's release string, or the Spec's Version hash -- whichever
            their operators can act on when a card looks wrong.
        description: overrides the Spec's. An ``AgentSpec.description``
            defaults to empty and §4.4.1 marks the card's REQUIRED, so
            something has to fill the gap; a card that says nothing about what
            the agent does defeats discovery.
        streaming: §4.4.3. Declared true by default because Psych's log *is* a
            stream and the playground's SSE endpoint is a thin read of it.
            §3.3.4 makes this a promise with teeth -- an agent declaring it
            must answer ``SendStreamingMessage`` -- so a consumer without a
            streaming route must pass ``False``.
        push_notifications: §4.4.3, and the same promise: declaring it obliges
            the four config operations to work (§3.3.4).
        extended_agent_card: §13.3. Off by default, because declaring it and
            not configuring one is an error path (``Extended
            AgentCardNotConfiguredError``) rather than a nicety.

    Raises:
        ValueError: no interfaces, or no description anywhere. Both are
            REQUIRED fields in the proto, and failing here beats publishing a
            card that fails validation at a peer.
    """
    if not interfaces:
        raise ValueError(
            "an Agent Card must declare at least one interface (§4.4.1's "
            "supported_interfaces is REQUIRED); pass where this agent is reachable"
        )
    text = description if description is not None else spec.description
    if not text:
        raise ValueError(
            f"agent {spec.name!r} has no description; §4.4.1 makes the card's REQUIRED "
            "because it is what a client reads to decide whether to call this agent. "
            "Set AgentSpec.description or pass description="
        )

    # Empty repeated and map fields are left *unset* rather than passed as
    # empty collections. §8.4.1 canonicalises a repeated field with an empty
    # value away unless it is REQUIRED, and `wire_dict` implements that by
    # presence, so passing `extensions=()` here would put `"extensions": []`
    # into the signed bytes of every card that has none. `skills` is passed
    # unconditionally because the proto marks it REQUIRED, which §8.4.1 says to
    # include even when it is empty.
    capability_fields: dict[str, Any] = {
        "streaming": streaming,
        "push_notifications": push_notifications,
        "extended_agent_card": extended_agent_card,
    }
    if extensions:
        capability_fields["extensions"] = tuple(extensions)
    card_fields: dict[str, Any] = {
        "name": spec.name,
        "description": text,
        "supported_interfaces": tuple(interfaces),
        "version": version,
        "capabilities": AgentCapabilities(**capability_fields),
        "default_input_modes": tuple(input_modes),
        "default_output_modes": tuple(output_modes),
        "skills": tuple(skills) if skills is not None else skills_of(spec),
    }
    if provider is not None:
        card_fields["provider"] = provider
    if documentation_url is not None:
        card_fields["documentation_url"] = documentation_url
    if security_schemes:
        card_fields["security_schemes"] = dict(security_schemes)
    if security_requirements:
        card_fields["security_requirements"] = tuple(security_requirements)
    if icon_url is not None:
        card_fields["icon_url"] = icon_url
    return AgentCard(**card_fields)


def protocol_version() -> str:
    """The A2A version this implementation's interfaces declare (§4.4.6).

    A function rather than a re-export so that a consumer reading
    ``AgentInterface(protocol_version=protocol_version())`` in their own code
    is pointed at ``psych_runtime.a2a.negotiation``, where the negotiation rules that
    make the number meaningful actually live.
    """
    return PROTOCOL_VERSION
