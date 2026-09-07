"""The outbound A2A client, against a real local peer on 127.0.0.1.

The one thing this file exists to prove, ahead of everything else: two Scopes
that differ by tenant or by credential never share a pooled peer connection,
and the credential that goes out on the wire for one Scope's call is never the
other Scope's. That is DESIGN.md §10.4's rule, tested here exactly as
`test_mcp.py` tests it for MCP, because an A2A peer takes a bearer token in a
header for the same reasons and would leak it in the same way.

The stub below is a real A2A agent over real sockets: it serves an Agent Card
at the well-known path, answers `SendMessage` over JSON-RPC and over REST, and
records what it was sent. Nothing about the client under test is mocked, and
nothing here reaches the network beyond loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field

import pytest

from psych_runtime.a2a.models import (
    AGENT_CARD_WELL_KNOWN_PATH,
    ProtocolBinding,
    TaskState,
)
from psych_runtime.a2a.negotiation import PROTOCOL_VERSION
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import A2APeer, AgentSpec, ModelRef
from psych_runtime.model.egress import HttpTransport
from psych_runtime.tools.a2a import (
    A2APeerUnreachable,
    A2APool,
    A2APoolKey,
    A2ATools,
    resolve_a2a_peer,
    tool_name_for,
)
from psych_runtime.tools.secrets import CredentialNotFound, InMemorySecretResolver

pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# A real A2A peer, on a real socket
# ---------------------------------------------------------------------------


@dataclass
class ReceivedCall:
    path: str
    authorization: str | None
    version: str | None
    extensions: str | None
    body: dict[str, object] = field(default_factory=dict)


class A2AStubPeer:
    """A minimal but genuine A2A agent, in one of the two HTTP bindings.

    Serves the card at `/.well-known/agent-card.json`, whose one interface
    declares `binding`. `SendMessage` answers with a completed Task carrying
    one artifact, which is the shape §3.7 asks for: the answer is an artifact,
    not a message.
    """

    def __init__(
        self,
        *,
        binding: str = ProtocolBinding.JSONRPC,
        skills: tuple[str, ...] = ("summarise", "translate"),
        tenant: str | None = None,
        require_token: str | None = None,
        state: TaskState = TaskState.COMPLETED,
    ) -> None:
        self.binding = binding
        self.skills = skills
        self.tenant = tenant
        self.require_token = require_token
        self.state = state
        self.calls: list[ReceivedCall] = []
        self.card_fetches = 0
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> A2AStubPeer:
        server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        self._server = server
        self._port = server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def card(self) -> dict[str, object]:
        interface: dict[str, object] = {
            "url": f"{self.url}/a2a/v1",
            "protocolBinding": self.binding,
            "protocolVersion": PROTOCOL_VERSION,
        }
        if self.tenant is not None:
            interface["tenant"] = self.tenant
        return {
            "name": "peer",
            "description": "A stub peer agent.",
            "supportedInterfaces": [interface],
            "version": "1.0.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": skill,
                    "name": skill,
                    "description": f"The peer can {skill}.",
                    "tags": ["peer"],
                }
                for skill in self.skills
            ],
        }

    def _task(self, text: str) -> dict[str, object]:
        return {
            "id": "peer-task-1",
            "contextId": "peer-context-1",
            "status": {"state": self.state.value},
            "artifacts": [
                {
                    "artifactId": "answer",
                    "parts": [{"text": f"peer handled: {text}"}],
                }
            ],
        }

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            method, path, headers, body = await _read_request(reader)
            await self._route(writer, method, path, headers, body)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _route(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        authorization = headers.get("authorization")
        if path.startswith(AGENT_CARD_WELL_KNOWN_PATH):
            self.card_fetches += 1
            await _write_json(writer, 200, self.card())
            return
        payload = json.loads(body) if body else {}
        self.calls.append(
            ReceivedCall(
                path=path,
                authorization=authorization,
                version=headers.get("a2a-version"),
                extensions=headers.get("a2a-extensions"),
                body=payload,
            )
        )
        if self.require_token is not None and authorization != f"Bearer {self.require_token}":
            await _write_json(
                writer,
                401,
                {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "no"}},
            )
            return
        if path.endswith("/rpc") or "jsonrpc" in payload:
            text = _text_of(payload.get("params"))
            await _write_json(
                writer,
                200,
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"task": self._task(text)}},
            )
            return
        if path.endswith("/message:send"):
            await _write_json(writer, 200, {"task": self._task(_text_of(payload))})
            return
        await _write_json(writer, 404, {"error": {"code": 404, "message": "no such route"}})


def _text_of(params: object) -> str:
    if not isinstance(params, dict):
        return ""
    message = params.get("message")
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes]:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
    header_bytes, _, rest = head.partition(b"\r\n\r\n")
    lines = header_bytes.decode("latin-1").split("\r\n")
    request_line = lines[0] if lines else ""
    parts = request_line.split(" ")
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else "/"
    headers: dict[str, str] = {}
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        if key:
            headers[key] = value.strip()
        if key == "content-length":
            length = int(value.strip())
    body = rest
    while len(body) < length:
        chunk = await reader.read(length - len(body))
        if not chunk:
            break
        body += chunk
    return method, path, headers, body


async def _write_json(writer: asyncio.StreamWriter, status: int, payload: object) -> None:
    body = json.dumps(payload).encode()
    head = (
        f"HTTP/1.1 {status} OK\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode()
    writer.write(head + body)
    await writer.drain()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


TENANT_A = Scope(tenant="acme", principal="alice")
TENANT_B = Scope(tenant="globex", principal="bob")


def _peer(url: str, **kwargs: object) -> A2APeer:
    return A2APeer.model_validate({"name": "research", "url": url, **kwargs})


def _spec(peer: A2APeer) -> AgentSpec:
    return AgentSpec(name="caller", model=ModelRef(model="gpt-4o"), a2a_peers=(peer,))


def _secrets() -> InMemorySecretResolver:
    secrets = InMemorySecretResolver()
    secrets.set(TENANT_A, "peer-token", "token-for-acme")
    secrets.set(TENANT_B, "peer-token", "token-for-globex")
    return secrets


# ---------------------------------------------------------------------------
# The isolation case, first
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_two_tenants_never_share_a_connection_or_a_token(self) -> None:
        """DESIGN.md §10.4, for A2A. Two Specs naming one peer URL, two
        tenants, two credentials: two pooled connections, and each call carries
        only its own tenant's token."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, credential="peer-token")
            tools = A2ATools(pool)

            await tools.call(_spec(spec_peer), TENANT_A, "research__summarise", {"message": "a"})
            await tools.call(_spec(spec_peer), TENANT_B, "research__summarise", {"message": "b"})

            keys = pool.keys()
            assert len({key.tenant for key in keys}) == 2
            assert len(keys) == 2

            sent = {_text_of(call.body.get("params")): call.authorization for call in peer.calls}
            assert sent["a"] == "Bearer token-for-acme"
            assert sent["b"] == "Bearer token-for-globex"

    async def test_the_pool_key_carries_the_tenant_and_the_credential_identity(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, credential="peer-token")
            await pool.get_or_connect(TENANT_A, spec_peer)
            await pool.get_or_connect(TENANT_B, spec_peer)
            keys = pool.keys()
            assert {key.tenant for key in keys} == {"acme", "globex"}
            assert len({key.credential_identity for key in keys}) == 2
            assert all(isinstance(key, A2APoolKey) for key in keys)

    async def test_one_tenant_reuses_its_own_connection(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, credential="peer-token")
            first = await pool.get_or_connect(TENANT_A, spec_peer)
            second = await pool.get_or_connect(TENANT_A, spec_peer)
            assert first is second

    async def test_a_missing_credential_is_a_configuration_error(self) -> None:
        """Not "unreachable": a Spec naming a credential nobody configured is a
        mistake to fix, not an outage to route around."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=InMemorySecretResolver())
            with pytest.raises(CredentialNotFound):
                await pool.get_or_connect(TENANT_A, _peer(peer.url, credential="peer-token"))


# ---------------------------------------------------------------------------
# Discovery and narrowing
# ---------------------------------------------------------------------------


class TestDiscoveryAndNarrowing:
    async def test_skills_become_tools_named_for_the_peer(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            resolution = await resolve_a2a_peer(pool, TENANT_A, _peer(peer.url))
            assert resolution.reachable
            assert [tool.name for tool in resolution.tools] == [
                "research__summarise",
                "research__translate",
            ]

    async def test_the_spec_narrows_what_the_model_sees(self) -> None:
        """The third plane of DESIGN.md §10.5, through the same `narrow`."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            resolution = await resolve_a2a_peer(
                pool, TENANT_A, _peer(peer.url, allow=("summarise",))
            )
            assert [tool.name for tool in resolution.tools] == ["research__summarise"]

    async def test_the_tenant_policy_narrows_further(self) -> None:
        class OnlyTranslate:
            async def permitted_tools(self, scope: Scope, server: str | None) -> list[str]:
                return ["translate"]

        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            tools = A2ATools(pool, tenant_policy=OnlyTranslate())
            offered = await tools.tools_for(TENANT_A, _peer(peer.url))
            assert [tool.name for tool in offered] == ["research__translate"]

    async def test_the_card_is_cached_but_the_narrowing_is_not(self) -> None:
        """DESIGN.md §10.2: what a peer offers is cached, what this Spec may
        use is recomputed every time."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            await resolve_a2a_peer(pool, TENANT_A, spec_peer)
            await resolve_a2a_peer(pool, TENANT_A, spec_peer)
            assert peer.card_fetches == 1
            narrowed = await resolve_a2a_peer(pool, TENANT_A, _peer(peer.url, allow=("translate",)))
            assert [tool.name for tool in narrowed.tools] == ["research__translate"]

    async def test_a_peer_tool_is_annotated_write(self) -> None:
        """DESIGN.md §10.9's default, and the right answer here: asking another
        organisation's agent to act is at least a write."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            resolution = await resolve_a2a_peer(pool, TENANT_A, _peer(peer.url))
            assert resolution.tools[0].annotations == frozenset({"write"})

    async def test_an_unreachable_required_peer_fails_the_turn(self) -> None:
        async with HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            with pytest.raises(A2APeerUnreachable):
                await resolve_a2a_peer(pool, TENANT_A, _peer("http://127.0.0.1:1"))

    async def test_an_unreachable_optional_peer_is_reported_not_raised(self) -> None:
        """DESIGN.md §10.7: the model is told, rather than silently losing the
        ability."""
        async with HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            resolution = await resolve_a2a_peer(
                pool, TENANT_A, _peer("http://127.0.0.1:1", optional=True)
            )
            assert not resolution.reachable
            assert resolution.tools == ()
            assert resolution.unavailable_reason


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------


class TestCalling:
    async def test_a_call_over_json_rpc_returns_the_peers_artifact(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            result = await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "the report"}
            )
            assert result.text == "peer handled: the report"
            assert result.state is TaskState.COMPLETED
            assert result.task_id == "peer-task-1"
            assert result.context_id == "peer-context-1"

    async def test_a_call_over_rest_reaches_the_rest_path(self) -> None:
        """§5.2: a client may use any binding the card declares, so both are
        implemented and the card decides."""
        async with A2AStubPeer(binding=ProtocolBinding.HTTP_JSON) as peer, HttpTransport() as t:
            pool = A2APool(transport=t, secrets=_secrets())
            spec_peer = _peer(peer.url)
            result = await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            assert result.text == "peer handled: x"
            assert peer.calls[-1].path.endswith("/message:send")

    async def test_every_call_declares_the_protocol_version(self) -> None:
        """§3.6.1: "Clients MUST send the A2A-Version header with each
        request"; absent means 0.3."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            assert peer.calls[-1].version == PROTOCOL_VERSION

    async def test_declared_extensions_are_opted_into(self) -> None:
        """§4.6.1's opt-in header, from the Spec's own declaration."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, extensions=("https://example.test/ext/v1",))
            await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            assert peer.calls[-1].extensions == "https://example.test/ext/v1"

    async def test_the_routing_tenant_travels_in_the_request(self) -> None:
        """§4.4.6: "clients MUST include this value in the tenant field of all
        request messages sent to this interface"."""
        async with A2AStubPeer(tenant="agent-7") as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, tenant="agent-7")
            await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            params = peer.calls[-1].body["params"]
            assert isinstance(params, dict)
            assert params["tenant"] == "agent-7"

    async def test_a_client_never_mints_a_task_id(self) -> None:
        """§3.4.2: "Client-provided taskId values for creating new tasks is NOT
        supported", so a first message carries none."""
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            params = peer.calls[-1].body["params"]
            assert isinstance(params, dict)
            assert "taskId" not in params["message"]

    async def test_continuing_a_task_sends_the_id_the_peer_issued(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            await A2ATools(pool).call(
                _spec(spec_peer),
                TENANT_A,
                "research__summarise",
                {"message": "more", "task_id": "peer-task-1"},
            )
            params = peer.calls[-1].body["params"]
            assert isinstance(params, dict)
            assert params["message"]["taskId"] == "peer-task-1"

    async def test_a_peer_needing_input_is_reported_with_its_state(self) -> None:
        """The model needs the state to know it must answer, not just prose."""
        async with A2AStubPeer(state=TaskState.INPUT_REQUIRED) as peer, HttpTransport() as t:
            pool = A2APool(transport=t, secrets=_secrets())
            spec_peer = _peer(peer.url)
            result = await A2ATools(pool).call(
                _spec(spec_peer), TENANT_A, "research__summarise", {"message": "x"}
            )
            assert result.state is TaskState.INPUT_REQUIRED
            assert result.task_id == "peer-task-1"

    async def test_a_skill_the_spec_excluded_cannot_be_called(self) -> None:
        from psych_runtime.core.errors import AccessDenied

        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url, allow=("summarise",))
            with pytest.raises(AccessDenied):
                await A2ATools(pool).call(
                    _spec(spec_peer), TENANT_A, "research__translate", {"message": "x"}
                )

    async def test_a_call_with_no_message_is_refused_before_the_wire(self) -> None:
        async with A2AStubPeer() as peer, HttpTransport() as transport:
            pool = A2APool(transport=transport, secrets=_secrets())
            spec_peer = _peer(peer.url)
            with pytest.raises(ValueError, match="message"):
                await A2ATools(pool).call(_spec(spec_peer), TENANT_A, "research__summarise", {})
            assert peer.calls == []

    async def test_the_tool_name_helper_matches_what_is_offered(self) -> None:
        peer_spec = _peer("http://127.0.0.1:1")
        assert tool_name_for(peer_spec, "summarise") == "research__summarise"
        assert tool_name_for(peer_spec, "sum/marise") == "research__sum_marise"
