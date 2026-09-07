"""Other agents, as a tool source. The outbound half of A2A.

``psych_runtime.a2a`` holds the protocol; this holds the client. From inside a running
agent, a peer agent is not a special kind of collaborator -- it is a thing the
model can call, described in the prompt, gated by the same policy, recorded in
the same log. So this module is shaped exactly like ``psych_runtime.tools.mcp``: a
pool, a connection, a catalogue, and a narrowed set of ``ToolDefinition``s per
turn.

## The rule that matters more than anything else in this module

**Never pool a peer connection by URL.** DESIGN.md §10.4 calls URL pooling the
bug that ends the project, and every word of it applies here: two Scopes whose
Specs happen to name the same peer URL are not the same tenant, and a
credential resolved for one is not interchangeable with the other's. An A2A
call carries a bearer token in a header exactly as MCP does, so pooling by URL
would eventually send tenant A's token on tenant B's call.

``A2APoolKey`` therefore keys on the Scope's tenant and principal (through
``Scope.pool_key``), the peer's URL, its routing tenant, and the *resolved
credential's identity* -- never the Spec's credential name, never the secret.
All five fields are named and keyword-only through ``from_scope``, so there is
no positional shortcut that drops the tenancy half.

The cached Agent Card lives inside the connection a pool key already owns, so
one tenant's view of a peer -- including an extended card served only to an
authenticated caller (§13.3) -- cannot reach another tenant. There is no
second cache for it to leak into.

## What a peer's skills become

One ``ToolDefinition`` per skill the card declares, named
``{peer}__{skill id}``. The peer alias is in the name because two peers may
legitimately both declare a ``search`` skill, and a model cannot address two
tools with one name. Every generated tool takes the same arguments -- the
message to send, and optionally the task to continue -- because that is what
A2A actually offers: §3.1.1 has exactly one way to ask an agent for something.
The skill is expressed by which tool the model picks, and travels to the peer
as the message's context, not as a separate protocol field.

## Why calls block by default

``SendMessageConfiguration.return_immediately`` defaults to ``false`` in the
proto, meaning the call returns when the task is terminal or interrupted
(§3.2.2). This client sends it explicitly rather than relying on the peer's
default, and keeps it false: a tool call that returned "submitted" and nothing
else would hand the model a task id and no answer, and the model's only
recourse would be to poll in a loop that costs a turn each time. A peer that
needs input comes back ``INPUT_REQUIRED``, and the returned task id is how the
model answers -- which is the same mechanism as the terminal case, not an
exception to it.

## Every call goes through the egress seam

``A2ATransport`` is structurally the same Protocol ``psych_runtime.tools.mcp`` defines
for the same reason: ``psych_runtime.tools`` and ``psych_runtime.model`` sit in one
import-linter layer and neither may import the other, so the real
``HttpTransport`` is passed in by the runtime and satisfies this by shape.
There is no ``httpx.AsyncClient`` in this module. DESIGN.md §14 requires every
outbound call to go through one seam, and a client that opened its own
connection would be the fourth of four routes a consumer's egress policy did
not cover.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from psych_runtime.a2a.errors import A2AError, InvalidAgentResponseError
from psych_runtime.a2a.jsonrpc import JSONRPC_VERSION, A2AMethod
from psych_runtime.a2a.models import (
    AGENT_CARD_WELL_KNOWN_PATH,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    ProtocolBinding,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SendMessageResponse,
    Task,
    TaskState,
    wire_dict,
)
from psych_runtime.a2a.negotiation import (
    A2A_EXTENSIONS_HEADER,
    A2A_VERSION_HEADER,
    PROTOCOL_VERSION,
    format_extensions_header,
)
from psych_runtime.a2a.rest import REST_ROUTES
from psych_runtime.core.errors import AccessDenied, PsychError
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import A2APeer, AgentSpec
from psych_runtime.tools.narrowing import narrow
from psych_runtime.tools.secrets import CredentialNotFound, ResolvedCredential, SecretResolver

__all__ = [
    "DEFAULT_CARD_TTL_SECONDS",
    "A2ACallResult",
    "A2AConnection",
    "A2APeerResolution",
    "A2APeerUnreachable",
    "A2APool",
    "A2APoolKey",
    "A2ATools",
    "A2ATransport",
    "resolve_a2a_peer",
    "resolve_a2a_peers",
    "tool_name_for",
]

DEFAULT_CARD_TTL_SECONDS: Final = 300.0
"""How long a fetched Agent Card is trusted before it is fetched again.

§8.6 leaves caching to the implementation and points at HTTP caching headers.
Five minutes is short enough that a peer that withdrew a skill stops being
advertised within a turn or two, and long enough that a Run making several
calls does not refetch the card for each. The freshness rule that matters more
is the one Psych already has: which skills *this Spec* may use is re-narrowed
on every resolve (DESIGN.md §10.2), never cached.
"""

_TOOL_NAME_SEPARATOR: Final = "__"
_UNSAFE_NAME_CHARS: Final = re.compile(r"[^a-zA-Z0-9_.-]")


class A2APeerUnreachable(PsychError):
    """A peer did not answer, or answered with something unusable.

    Distinct from ``InvalidAgentResponseError``: this is "I could not talk to
    it", which for an ``optional`` peer means carry on without its skills, and
    that one is "it talked and broke the protocol", which is the peer's bug and
    is reported to the model as a tool failure.
    """

    def __init__(self, peer: str, reason: str) -> None:
        super().__init__(f"A2A peer {peer!r} is unreachable: {reason}")
        self.peer = peer
        self.reason = reason


@runtime_checkable
class A2ATransport(Protocol):
    """The outbound HTTP capability this client needs (the egress seam).

    Structurally typed against ``psych_runtime.model.egress.HttpTransport`` for the
    layering reason in the module docstring. Identical in shape to
    ``psych_runtime.tools.mcp.McpTransport``, and deliberately so: two seams with
    slightly different shapes is how one of them ends up bypassed.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response: ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> AbstractAsyncContextManager[httpx.Response]: ...


@dataclass(frozen=True, slots=True)
class A2APoolKey:
    """Identifies one pooled peer connection.

    Five named fields, all required. See the module docstring for why this
    type exists rather than a tuple or an f-string.

    ``routing_tenant`` is the peer's own opaque ``tenant`` value (§4.4.6), not
    ours: one peer URL may front several agents, and two Specs pointing at
    different ones must not share a cached card.
    """

    tenant: str
    principal: str | None
    peer_url: str
    routing_tenant: str | None
    credential_identity: str | None

    @classmethod
    def from_scope(
        cls, *, scope: Scope, peer: A2APeer, credential_identity: str | None
    ) -> A2APoolKey:
        tenant, principal = scope.pool_key
        return cls(
            tenant=tenant,
            principal=principal,
            peer_url=peer.url,
            routing_tenant=peer.tenant,
            credential_identity=credential_identity,
        )


class A2ACallResult(BaseModel):
    """What one call to a peer produced, as the model reads it.

    A model rather than a bare string because a peer's answer carries state
    the model must act on: ``INPUT_REQUIRED`` with a task id means "answer
    this", and a plain string would leave the model to infer that from prose.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    peer: str
    task_id: str | None = None
    context_id: str | None = None
    state: TaskState | None = None
    text: str = ""
    """The peer's answer: its artifacts' text parts, or its message's, joined.
    §3.7 puts outputs in artifacts and communication in messages, so artifacts
    come first and a status message is used when there are none."""


def tool_name_for(peer: A2APeer, skill_id: str) -> str:
    """The tool name one peer skill is offered to the model under.

    ``{peer}__{skill}``, with anything outside Psych's own name pattern
    replaced: a skill id is an arbitrary string chosen by another
    organisation, and it reaches the model as a JSON schema property name and
    the log as an identifier (``psych_runtime.core.spec``'s ``_NAME_PATTERN``).
    """
    return f"{peer.name}{_TOOL_NAME_SEPARATOR}{_UNSAFE_NAME_CHARS.sub('_', skill_id)}"


def _definition_for(peer: A2APeer, skill: AgentSkill) -> ToolDefinition:
    examples = "\n".join(f"- {example}" for example in skill.examples)
    description = skill.description
    if examples:
        description = f"{description}\n\nThe peer gives these examples:\n{examples}"
    return ToolDefinition(
        name=tool_name_for(peer, skill.id),
        description=(
            f"Ask the {peer.name!r} agent to do this, over A2A. {description}\n\n"
            "The call returns when that agent finishes or needs something from you. "
            "If it comes back needing input, send the next message with the same "
            "task_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to ask the peer agent, in full sentences.",
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Only when continuing a task this peer previously returned as "
                        "needing input. Never invent one."
                    ),
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        # A2A skills carry no MCP-style annotations, and DESIGN.md §10.9's
        # default for an unannotated tool is `write`. That is the right answer
        # here and not merely the default one: asking another organisation's
        # agent to act is at least a write, and a consumer whose approval
        # selectors say `@write` should be asked before it happens.
        annotations=frozenset({"write"}),
    )


@dataclass(frozen=True, slots=True)
class _CachedCard:
    card: AgentCard
    interface: AgentInterface
    fetched_at: float


class A2AConnection:
    """One tenant's connection to one peer, for one credential.

    Constructed and cached by ``A2APool``. Nothing outside this module should
    build one: a hand-built connection is a connection outside the pool key,
    which is the isolation guarantee itself.
    """

    def __init__(
        self,
        *,
        scope: Scope,
        peer: A2APeer,
        transport: A2ATransport,
        credential: ResolvedCredential | None,
        card_ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._scope = scope
        self._peer = peer
        self._transport = transport
        self._credential = credential
        self._ttl = card_ttl_seconds
        self._timeout = timeout_seconds
        self._cached: _CachedCard | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Never the credential value, only its identity: this object is the
        # one most likely to be printed while debugging a pooling problem,
        # which is exactly when a leaked secret would be most damaging.
        identity = self._credential.identity if self._credential is not None else None
        return (
            f"A2AConnection(peer={self._peer.name!r}, url={self._peer.url!r}, "
            f"tenant={self._scope.tenant!r}, principal={self._scope.principal!r}, "
            f"credential_identity={identity!r})"
        )

    @property
    def credential_identity(self) -> str | None:
        return self._credential.identity if self._credential is not None else None

    @property
    def card(self) -> AgentCard | None:
        """The cached card, or ``None`` before the first fetch."""
        return self._cached.card if self._cached is not None else None

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            # §3.6.1: "Clients MUST send the A2A-Version header with each
            # request". Omitting it would tell the peer we speak 0.3.
            A2A_VERSION_HEADER: PROTOCOL_VERSION,
        }
        if self._peer.extensions:
            headers[A2A_EXTENSIONS_HEADER] = format_extensions_header(self._peer.extensions)
        if self._credential is not None:
            headers["Authorization"] = (
                f"{self._peer.scheme} {self._credential.secret.get_secret_value()}"
            )
        return headers

    def card_url(self) -> str:
        """Where this peer's Agent Card is (§8.2).

        A URL that already names a card is used as given -- a consumer may
        point at a card served from a path of their own -- and anything else
        is treated as the agent's origin, which is what the well-known path is
        relative to.
        """
        url = self._peer.url
        if url.endswith(".json"):
            return url
        parsed = httpx.URL(url)
        return str(parsed.copy_with(raw_path=AGENT_CARD_WELL_KNOWN_PATH.encode()))

    async def fetch_card(self, *, force: bool = False) -> AgentCard:
        """The peer's Agent Card, from cache or from the peer.

        Raises:
            A2APeerUnreachable: the peer did not answer, answered with a
                non-2xx status, or returned something that is not an Agent
                Card. All three mean the same thing to a caller deciding
                whether an optional peer is available.
        """
        async with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None and now - self._cached.fetched_at < self._ttl:
                return self._cached.card
            url = self.card_url()
            try:
                response = await self._transport.request(
                    "GET", url, scope=self._scope, headers=self._headers(), timeout=self._timeout
                )
            except Exception as err:
                raise A2APeerUnreachable(self._peer.name, f"{url}: {err}") from err
            if response.status_code >= 400:
                raise A2APeerUnreachable(
                    self._peer.name,
                    f"{url} answered HTTP {response.status_code}",
                )
            try:
                card = AgentCard.model_validate(response.json())
            except (ValidationError, ValueError) as err:
                raise A2APeerUnreachable(
                    self._peer.name, f"{url} did not return a valid Agent Card: {err}"
                ) from err
            interface = _select_interface(self._peer, card)
            self._cached = _CachedCard(card=card, interface=interface, fetched_at=now)
            return card

    async def skills(self, *, force: bool = False) -> tuple[AgentSkill, ...]:
        """Every skill the peer currently declares."""
        card = await self.fetch_card(force=force)
        return card.skills

    async def _interface(self) -> AgentInterface:
        await self.fetch_card()
        cached = self._cached
        if cached is None:  # pragma: no cover - fetch_card sets it or raises
            raise A2APeerUnreachable(self._peer.name, "its Agent Card could not be read")
        return cached.interface

    async def send_message(
        self, text: str, *, task_id: str | None = None, context_id: str | None = None
    ) -> A2ACallResult:
        """Ask the peer to do something, and wait for it to finish or ask.

        Raises:
            A2APeerUnreachable: the peer could not be reached.
            A2AError: the peer answered with a protocol error, re-raised as
                the typed error its code names, so a caller can tell "no such
                task" from "this agent is broken".
            InvalidAgentResponseError: the peer answered with something that
                is not a ``SendMessageResponse`` (§5.4's own error for this).
        """
        interface = await self._interface()
        # Fields are set only when they have a value rather than passed as
        # None: §5.7 makes an explicit null different from an omitted field,
        # and a first message must carry no `taskId` at all (§3.4.2) rather
        # than a null one a strict peer could read as an attempt to mint one.
        message_fields: dict[str, Any] = {
            "message_id": str(uuid4()),
            "role": Role.USER,
            "parts": (Part.from_text(text),),
        }
        if task_id is not None:
            message_fields["task_id"] = task_id
        if context_id is not None:
            message_fields["context_id"] = context_id
        if self._peer.extensions:
            message_fields["extensions"] = self._peer.extensions
        request_fields: dict[str, Any] = {
            "message": Message(**message_fields),
            "configuration": SendMessageConfiguration(
                accepted_output_modes=("text/plain",),
                return_immediately=False,
            ),
        }
        if self._peer.tenant is not None:
            request_fields["tenant"] = self._peer.tenant
        request = SendMessageRequest(**request_fields)
        payload = await self._call(interface, A2AMethod.SEND_MESSAGE, wire_dict(request))
        try:
            response = SendMessageResponse.model_validate(payload)
        except ValidationError as err:
            raise InvalidAgentResponseError(
                f"peer {self._peer.name!r} answered SendMessage with something that is "
                f"neither a task nor a message: {err}"
            ) from err
        return _result_of(self._peer.name, response)

    async def _call(
        self, interface: AgentInterface, method: A2AMethod, body: Mapping[str, Any]
    ) -> Any:
        """One request, over whichever binding the chosen interface declares.

        Both bindings are implemented because a peer declares which it offers
        and §5.2 lets a client pick any of them; a client that spoke only one
        would be unable to call a perfectly conformant agent.
        """
        if interface.protocol_binding == ProtocolBinding.JSONRPC:
            return await self._call_jsonrpc(interface, method, body)
        return await self._call_rest(interface, method, body)

    async def _call_jsonrpc(
        self, interface: AgentInterface, method: A2AMethod, body: Mapping[str, Any]
    ) -> Any:
        envelope = {
            "jsonrpc": JSONRPC_VERSION,
            "id": str(uuid4()),
            "method": method.value,
            "params": dict(body),
        }
        payload = await self._post(interface.url, envelope)
        if not isinstance(payload, dict):
            raise InvalidAgentResponseError(
                f"peer {self._peer.name!r} answered with {type(payload).__name__}, not a "
                "JSON-RPC response object"
            )
        error = payload.get("error")
        if error is not None:
            raise _error_from_jsonrpc(self._peer.name, error)
        if "result" not in payload:
            raise InvalidAgentResponseError(
                f"peer {self._peer.name!r} answered with neither a result nor an error"
            )
        return payload["result"]

    async def _call_rest(
        self, interface: AgentInterface, method: A2AMethod, body: Mapping[str, Any]
    ) -> Any:
        _, path = REST_ROUTES[method]
        url = interface.url.rstrip("/") + path
        return await self._post(url, dict(body))

    async def _post(self, url: str, body: Mapping[str, Any]) -> Any:
        try:
            response = await self._transport.request(
                "POST",
                url,
                scope=self._scope,
                headers=self._headers(),
                json=dict(body),
                timeout=self._timeout,
            )
        except Exception as err:
            raise A2APeerUnreachable(self._peer.name, f"{url}: {err}") from err
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as err:
            raise InvalidAgentResponseError(
                f"peer {self._peer.name!r} answered HTTP {response.status_code} with a body "
                f"that is not JSON: {err}"
            ) from err
        if response.status_code >= 400:
            raise _error_from_rest(self._peer.name, response.status_code, payload)
        return payload


def _select_interface(peer: A2APeer, card: AgentCard) -> AgentInterface:
    """The interface to talk to, honouring the card's own preference order.

    §4.4.6: "Ordered list of supported interfaces. The first entry is
    preferred", and §5.2 lets a client choose any declared protocol. So the
    first interface whose binding this client speaks wins, and gRPC entries
    are skipped rather than attempted -- see ``psych_runtime.a2a`` for why gRPC is not
    implemented.

    Raises:
        A2APeerUnreachable: the card declares no HTTP binding. Reported as
            unreachable rather than as a protocol error because that is what
            it means for the caller: there is no way to talk to this peer, and
            an optional peer should carry on without it.
    """
    speakable = {ProtocolBinding.JSONRPC.value, ProtocolBinding.HTTP_JSON.value}
    for interface in card.supported_interfaces:
        if interface.protocol_binding in speakable:
            return interface
    declared = ", ".join(sorted({i.protocol_binding for i in card.supported_interfaces}))
    raise A2APeerUnreachable(
        peer.name,
        f"its Agent Card declares only {declared or 'no'} bindings, and this client "
        "speaks JSONRPC and HTTP+JSON",
    )


def _result_of(peer_name: str, response: SendMessageResponse) -> A2ACallResult:
    """Flatten a peer's answer into what the model reads.

    Artifacts before messages, per §3.7: "Messages SHOULD NOT be used to
    deliver task outputs. Results SHOULD BE returned using Artifacts". A task
    that finished with neither is reported with its state and no text, which
    is honest -- inventing a sentence about an empty answer would be Psych
    speaking for another organisation's agent.
    """
    if response.message is not None:
        message = response.message
        return A2ACallResult(
            peer=peer_name,
            task_id=message.task_id,
            context_id=message.context_id,
            text=message.text,
        )
    task = response.task
    if task is None:  # pragma: no cover - the oneof validator forbids this
        raise InvalidAgentResponseError(f"peer {peer_name!r} sent an empty SendMessageResponse")
    return A2ACallResult(
        peer=peer_name,
        task_id=task.id,
        context_id=task.context_id,
        state=task.status.state,
        text=_task_text(task),
    )


def _task_text(task: Task) -> str:
    chunks = [
        part.text for artifact in task.artifacts for part in artifact.parts if part.text is not None
    ]
    if chunks:
        return "\n\n".join(chunks)
    status_message = task.status.message
    return status_message.text if status_message is not None else ""


_JSONRPC_ERRORS: Final[Mapping[int, type[A2AError]]] = {
    error.jsonrpc_code: error for error in A2AError.__subclasses__()
}
"""Code to type, built from the classes themselves so it cannot fall behind
``psych_runtime.a2a.errors``. An unknown code becomes a plain ``A2AError``, which is
what a peer inventing its own code deserves: reported, not guessed at."""


def _error_from_jsonrpc(peer_name: str, error: object) -> A2AError:
    if not isinstance(error, dict):
        return InvalidAgentResponseError(
            f"peer {peer_name!r} answered with a malformed JSON-RPC error object"
        )
    code = error.get("code")
    message = error.get("message") or "the peer reported an error with no message"
    kind = _JSONRPC_ERRORS.get(code) if isinstance(code, int) else None
    text = f"peer {peer_name!r}: {message}"
    return kind(text) if kind is not None else A2AError(text)


def _error_from_rest(peer_name: str, status: int, payload: object) -> A2AError:
    """§11.6's ``google.rpc.Status`` body, read back into a typed error.

    The ``ErrorInfo`` reason is what distinguishes the several A2A errors that
    share one HTTP status, which is exactly why §11.6 requires it, so it is
    what this matches on before falling back to the status code.
    """
    message = f"peer {peer_name!r} answered HTTP {status}"
    reason: str | None = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = f"peer {peer_name!r}: {error.get('message', message)}"
            for detail in error.get("details") or []:
                if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                    reason = detail["reason"]
                    break
    if reason is not None:
        for kind in A2AError.__subclasses__():
            if kind.reason == reason:
                return kind(message)
    for kind in A2AError.__subclasses__():
        if kind.http_status == status:
            return kind(message)
    return A2AError(message)


class A2APool:
    """Pools ``A2AConnection``s by ``A2APoolKey``.

    One pool per process, shared by every Run of every tenant, with isolation
    coming entirely from the key -- the same shape as ``McpPool``, and for the
    same reason: a pool per tenant just moves the one-line mistake to whoever
    wires the pools up.
    """

    def __init__(
        self,
        *,
        transport: A2ATransport,
        secrets: SecretResolver,
        card_ttl_seconds: float = DEFAULT_CARD_TTL_SECONDS,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self._ttl = card_ttl_seconds
        self._connections: dict[A2APoolKey, A2AConnection] = {}
        self._locks: dict[A2APoolKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get_or_connect(self, scope: Scope, peer: A2APeer) -> A2AConnection:
        """The connection for ``(scope, peer, credential)``, creating one if
        needed.

        Raises:
            CredentialNotFound: ``peer.credential`` names a credential this
                Scope has no value for. Raised regardless of ``optional``,
                because a missing credential is a configuration mistake rather
                than a peer being down, and treating it as an outage would
                hide it.
        """
        credential = await self._resolve_credential(scope, peer)
        key = A2APoolKey.from_scope(
            scope=scope,
            peer=peer,
            credential_identity=credential.identity if credential is not None else None,
        )
        lock = await self._lock_for(key)
        async with lock:
            existing = self._connections.get(key)
            if existing is not None:
                return existing
            connection = A2AConnection(
                scope=scope,
                peer=peer,
                transport=self._transport,
                credential=credential,
                card_ttl_seconds=self._ttl,
            )
            self._connections[key] = connection
            return connection

    def peek(self, scope: Scope, peer: A2APeer) -> A2AConnection | None:
        """The pooled connection, without connecting or resolving a secret.

        Best-effort by construction: it can only find a connection whose
        credential identity is already known to this process, so it answers
        ``None`` for a peer whose credential has not been resolved yet rather
        than resolving one. Used for display, never for access.
        """
        tenant, principal = scope.pool_key
        for key, connection in self._connections.items():
            if (
                key.tenant == tenant
                and key.principal == principal
                and key.peer_url == peer.url
                and key.routing_tenant == peer.tenant
            ):
                return connection
        return None

    async def evict(self, scope: Scope, peer: A2APeer) -> None:
        """Forget this Scope's connection to one peer, card and all."""
        tenant, principal = scope.pool_key
        doomed = [
            key
            for key in self._connections
            if key.tenant == tenant
            and key.principal == principal
            and key.peer_url == peer.url
            and key.routing_tenant == peer.tenant
        ]
        for key in doomed:
            self._connections.pop(key, None)
            self._locks.pop(key, None)

    async def _resolve_credential(self, scope: Scope, peer: A2APeer) -> ResolvedCredential | None:
        if peer.credential is None:
            return None
        credential = await self._secrets.resolve(scope, peer.credential)
        if credential is None:
            raise CredentialNotFound(scope, peer.credential)
        return credential

    async def _lock_for(self, key: A2APoolKey) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def keys(self) -> tuple[A2APoolKey, ...]:
        """Every pooled key, for a consumer displaying connections and for the
        test that proves two tenants never share one."""
        return tuple(self._connections)


@dataclass(frozen=True, slots=True)
class A2APeerResolution:
    """One peer's contribution to one turn's tool set.

    The sibling of ``McpServerResolution``, field for field, so a consumer
    handling both does not have to learn two shapes.
    """

    peer: str
    reachable: bool
    tools: tuple[ToolDefinition, ...]
    unavailable_reason: str | None = None


async def resolve_a2a_peer(pool: A2APool, scope: Scope, peer: A2APeer) -> A2APeerResolution:
    """Fetch one peer's card and narrow its skills for this Spec.

    Fresh on every call by design (DESIGN.md §10.2): the card is what the
    connection caches, never which skills this Spec may use, so ``peer.allow``
    is re-applied to the live card every turn rather than pinned at first use.

    Raises:
        A2APeerUnreachable: ``peer.optional`` is False and the peer did not
            answer (DESIGN.md §10.7).
        CredentialNotFound: as ``A2APool.get_or_connect``.
    """
    try:
        connection = await pool.get_or_connect(scope, peer)
        skills = await connection.skills()
    except A2APeerUnreachable as err:
        if peer.optional:
            return A2APeerResolution(
                peer=peer.name, reachable=False, tools=(), unavailable_reason=str(err)
            )
        raise
    granted = set(narrow([skill.id for skill in skills], spec_grants=peer.allow))
    tools = tuple(_definition_for(peer, skill) for skill in skills if skill.id in granted)
    return A2APeerResolution(peer=peer.name, reachable=True, tools=tools)


async def resolve_a2a_peers(
    pool: A2APool, scope: Scope, peers: Sequence[A2APeer]
) -> tuple[A2APeerResolution, ...]:
    """``resolve_a2a_peer`` for every peer a Spec declares, in order."""
    return tuple([await resolve_a2a_peer(pool, scope, peer) for peer in peers])


class A2ATools:
    """The seam between a pool of peer connections and a running agent.

    Two halves that must narrow identically, on one object for the reason
    ``McpTools`` gives: wiring the resolver's view and the executor's view
    from two places is how a model ends up able to call a peer the Spec
    excluded.
    """

    def __init__(self, pool: A2APool, *, tenant_policy: Any = None) -> None:
        """
        Args:
            pool: keyed by ``(tenant, principal, peer url, routing tenant,
                credential identity)``, which is what makes these lookups
                tenant-safe rather than merely tenant-shaped.
            tenant_policy: the middle narrowing plane, matching
                ``psych_runtime.tools.resolver.TenantToolPolicy``. Structural rather
                than imported, so this module does not depend on the resolver
                that consumes it. ``None`` means the tenant permits every
                skill the peer declares.
        """
        self._pool = pool
        self._tenant_policy = tenant_policy

    async def tools_for(self, scope: Scope, peer: A2APeer) -> Sequence[ToolDefinition]:
        """Every skill ``peer`` offers ``scope`` right now, as tools.

        Raises rather than returning an empty list when the peer cannot be
        reached, because the resolver distinguishes the two exactly as it does
        for MCP: empty means "answered, offers nothing".
        """
        connection = await self._pool.get_or_connect(scope, peer)
        skills = await connection.skills()
        granted = set(
            narrow(
                [skill.id for skill in skills],
                await self._tenant_allows(scope, peer.name),
                list(peer.allow),
            )
        )
        return [_definition_for(peer, skill) for skill in skills if skill.id in granted]

    async def describe(self, scope: Scope, peer: A2APeer) -> str | None:
        """What the peer says it is, from its card, or ``None``.

        Never connects and never raises: this decorates a prompt, and failing
        a turn over a description would be absurd. A peer nobody has talked to
        yet simply has no card cached to describe.
        """
        connection = self._pool.peek(scope, peer)
        card = connection.card if connection is not None else None
        return card.description if card is not None else None

    async def call(
        self, spec: AgentSpec, scope: Scope, name: str, arguments: dict[str, Any]
    ) -> A2ACallResult:
        """Run one peer-skill tool call.

        Raises:
            AccessDenied: no granted peer offers a callable skill of that
                name. The model chose the name, so this is the same narrowing
                check ``tools_for`` made when the name was offered.
            A2APeerUnreachable: the peer that offers it did not answer.
                Propagated rather than folded into ``AccessDenied``, because
                "that agent is down" and "no such tool" call for different
                next moves and the second would be a lie.
        """
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"{name} needs a 'message' string saying what to ask the peer agent")
        task_id = arguments.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError(f"{name}'s task_id must be the string a previous call returned")

        unreachable: Exception | None = None
        for peer in spec.a2a_peers:
            if not name.startswith(f"{peer.name}{_TOOL_NAME_SEPARATOR}"):
                continue
            try:
                connection = await self._pool.get_or_connect(scope, peer)
                skills = await connection.skills()
            except A2APeerUnreachable as err:
                if unreachable is None:
                    unreachable = err
                continue
            callable_here = {
                tool_name_for(peer, skill_id)
                for skill_id in narrow(
                    [skill.id for skill in skills],
                    await self._tenant_allows(scope, peer.name),
                    list(peer.allow),
                )
            }
            if name in callable_here:
                return await connection.send_message(message, task_id=task_id)

        if unreachable is not None:
            raise unreachable
        raise AccessDenied(
            f"tool {name!r}",
            "no A2A peer granted to this Run offers a callable skill of that name",
        )

    async def _tenant_allows(self, scope: Scope, peer: str) -> list[str]:
        if self._tenant_policy is None:
            return []
        permitted = await self._tenant_policy.permitted_tools(scope, peer)
        return list(permitted)

    def caller(self, spec: AgentSpec, scope: Scope) -> Callable[..., Awaitable[Any]]:
        """``call`` bound to one Run, in the shape ``ToolExecutor`` expects.

        The Spec and Scope are closed over here for the reason
        ``McpTools.caller`` gives: both are fixed for a Run's life, and
        binding them per Run is what stops one Run's executor reaching
        another's peers.
        """

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            return await self.call(spec, scope, name, arguments)

        return call
