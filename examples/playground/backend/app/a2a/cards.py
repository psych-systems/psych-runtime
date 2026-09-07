"""Agent Cards for this deployment, and the key that signs them.

§8. Two things a card needs that a Spec cannot know: where the agent is
reachable, and how to authenticate to it. Both are deployment facts, so both
are decided here.

## Discovery, and the honest thing to say about the well-known path

§8.2 puts a card at `https://{server_domain}/.well-known/agent-card.json`,
which assumes one agent per origin. This console hosts many agents for many
accounts behind one origin, which is the case the proto's `tenant` field
exists for -- "an opaque string used for routing requests to a specific agent
... when multiple agents are served behind a single A2A endpoint".

So there are two ways to reach a card here and they return the same document:

- `/.well-known/agent-card.json?agent={id}`, the standard path with a
  selector. With no selector it answers for the account's only agent, and says
  so explicitly when there is more than one rather than picking.
- `/a2a/v1/agents/{id}/agent-card.json`, a direct per-agent URL that can be
  handed to a peer as-is. `psych_runtime.tools.a2a` accepts a URL ending in `.json` as
  a card URL, so this is the form to paste into another Psych deployment.

A production consumer with one agent per origin uses the well-known path alone
and none of this applies. The selector is here because pretending a
multi-tenant console is single-agent would produce a card that names the wrong
agent, which is worse than an extra query parameter.

## Cards here require authentication, and public ones do not

§8.2 imagines public discovery. These agents are private to the account that
published them, so every card route in this backend is authenticated like the
rest of `/a2a`. The consequence is worth naming: the card is not a public
discovery document here, it is a description served to a caller who is already
allowed to call the agent.

## The signing key

`psych_runtime.a2a.signing` ships HS256 and defines the seam for anything else. This
deployment generates one HMAC key on first boot and keeps it in a file beside
the rest of its state, so a card signed yesterday still verifies today. A
peer verifies it only if it has been given the same secret out of band, which
is exactly the deployment shape HS256 is right for, and is why the card
declares the signature rather than claiming public verifiability.

Set `PSYCH_PLAYGROUND_A2A_SIGNING_KEY` to pin the key instead -- which is what
two deployments that want to verify each other's cards actually do.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Final

from psych_runtime.a2a.card import agent_card
from psych_runtime.a2a.models import (
    AgentCard,
    AgentInterface,
    AgentProvider,
    HTTPAuthSecurityScheme,
    ProtocolBinding,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from psych_runtime.a2a.negotiation import PROTOCOL_VERSION
from psych_runtime.a2a.signing import HmacCardSigner, sign_card
from psych_runtime.core.spec import AgentSpec

__all__ = ["BEARER_SCHEME_NAME", "CardFactory", "load_signing_key"]

BEARER_SCHEME_NAME: Final = "consoleSession"
"""The name the card gives its one security scheme.

A `HTTPAuthSecurityScheme` with scheme `Bearer` (§4.5.3), because that is
literally what this backend accepts: the session token the console issues,
presented as a bearer token. Naming it after what it is keeps the card honest;
calling it `oauth2` because that sounds more serious would describe a flow this
deployment does not implement.
"""

_KEY_ENV: Final = "PSYCH_PLAYGROUND_A2A_SIGNING_KEY"
_KEY_ID: Final = "playground-hs256"


def load_signing_key(state_dir: Path) -> HmacCardSigner:
    """The HMAC signer for this installation's cards.

    The environment wins, so two deployments can be given the same key and
    verify each other. Otherwise a key is generated once and kept beside the
    other state files: regenerating it per boot would invalidate every
    signature a peer had cached, for no gain.
    """
    configured = os.environ.get(_KEY_ENV, "").strip()
    if configured:
        return HmacCardSigner(configured.encode("utf-8"), kid=_KEY_ID)
    path = state_dir / "a2a-signing-key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32))
        # The key is a secret sitting in a file the operator owns; narrowing
        # the mode is the least this example can do about that.
        path.chmod(0o600)
    return HmacCardSigner(path.read_text().strip().encode("utf-8"), kid=_KEY_ID)


class CardFactory:
    """Builds and signs the card for one published agent."""

    def __init__(
        self,
        *,
        base_url: str,
        signer: HmacCardSigner | None = None,
        organization: str = "Psych playground",
    ) -> None:
        """
        Args:
            base_url: this deployment's own origin, e.g.
                ``https://agents.example.test``. Interfaces are built from it,
                and a card whose URLs a peer cannot reach is worse than no card
                because a peer will try them.
            signer: omitted means an unsigned card, which is what a deployment
                that has not decided about keys should serve rather than a
                signature nobody can check.
        """
        self._base_url = base_url.rstrip("/")
        self._signer = signer
        self._organization = organization

    def interfaces(self, agent_id: str) -> tuple[AgentInterface, ...]:
        """Where this agent is reachable, preferred binding first (§4.4.6).

        JSON-RPC first because it is the binding with the least ambiguity in
        the specification (its `:subscribe` verb is not in dispute -- see
        `psych_runtime.a2a.rest`), and HTTP+JSON second because a client that prefers
        REST is explicitly allowed to choose it (§5.2). gRPC is absent because
        this deployment does not serve it, and §5.2 requires an agent to
        declare what it supports rather than what the protocol defines.
        """
        return (
            AgentInterface(
                # `/rpc`, not the bare door: `app.a2a.router` mounts the
                # JSON-RPC handler at `/a2a/v1/rpc` (tenant travels in the
                # request body, per `psych.tools.a2a`'s own `send_message`),
                # and a client posts straight to `interface.url` with no path
                # of its own to append. A card that named the bare door sent
                # every JSON-RPC call here to a 404 that nothing caught until
                # a real peer-to-peer round trip actually made one.
                url=f"{self._base_url}/a2a/v1/rpc",
                protocol_binding=ProtocolBinding.JSONRPC,
                tenant=agent_id,
                protocol_version=PROTOCOL_VERSION,
            ),
            AgentInterface(
                url=f"{self._base_url}/a2a/v1",
                protocol_binding=ProtocolBinding.HTTP_JSON,
                tenant=agent_id,
                protocol_version=PROTOCOL_VERSION,
            ),
        )

    def build(
        self, spec: AgentSpec, *, agent_id: str, version: str, extended: bool = False
    ) -> AgentCard:
        """One agent's card, signed when this deployment has a key.

        ``extended`` is §13.3's authenticated card: the same card with what
        the public one withholds -- the agent's instructions, which say how
        it behaves rather than only what it is called and can reach. Served
        only to a caller holding a token, from that caller's own agents.
        """
        description = spec.description or f"The {spec.name} agent."
        if extended and spec.instructions.strip():
            description = f"{description}\n\nInstructions:\n{spec.instructions.strip()}"
        card = agent_card(
            spec,
            interfaces=self.interfaces(agent_id),
            version=version,
            description=description,
            provider=AgentProvider(url=self._base_url, organization=self._organization),
            security_schemes={
                BEARER_SCHEME_NAME: SecurityScheme(
                    http_auth_security_scheme=HTTPAuthSecurityScheme(
                        scheme="Bearer",
                        description=(
                            "A console session token, presented as a bearer token. The "
                            "same token the browser holds in its session cookie."
                        ),
                    )
                )
            },
            security_requirements=(
                SecurityRequirement(schemes={BEARER_SCHEME_NAME: StringList()}),
            ),
            # Both are promises §3.3.4 gives teeth to, and this backend keeps
            # both: `/message:stream` and `:subscribe` are real, and the four
            # push-notification config operations are real.
            streaming=True,
            push_notifications=True,
            # Declared, and served: `GET /a2a/v1/extendedAgentCard` and the
            # JSON-RPC method both answer for a caller holding a token (§13.3).
            extended_agent_card=True,
            documentation_url=f"{self._base_url}/agents/{agent_id}",
        )
        return sign_card(card, self._signer) if self._signer is not None else card
