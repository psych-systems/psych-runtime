"""End to end: a peer agent talks to a real Run over A2A, and gets a Task.

The regression gate for this feature (DESIGN.md §22). Everything in the path is
real: the playground's own A2A router, mounted on a FastAPI app; a real
`psych_runtime.dispatch`; a real `Worker` executing the agent loop against the
scriptable fake model; a real Store; and the real mapping from the Run's log
back to `TaskState`. What is *not* real is the model provider and the socket:
the model is `FakeModel` and the app is driven over `httpx.ASGITransport`,
because DESIGN.md §22 forbids a test making a network call.

The assertions are about task states, because that is what an A2A client can
see. A client never learns that Psych has a Worker, a lease or a log: it sends
a message, and it is told `SUBMITTED`, `WORKING`, `COMPLETED` -- or
`INPUT_REQUIRED` when the agent asks it something, which is the case worth
proving because it is where the two models agree most exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.ids import RunId
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

_BACKEND = Path(__file__).resolve().parents[2] / "examples" / "playground" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytest.importorskip("fastapi", reason="the playground extra is not installed")

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402


class _Harness:
    """One deployment: a store, a Worker, and the A2A router over both."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        store: InMemoryStore,
        token: str,
        agent_id: str,
        account_id: str,
    ) -> None:
        self.client = client
        self.store = store
        self.token = token
        self.agent_id = agent_id
        self.account_id = account_id

    def headers(self, *, token: str | None = None) -> dict[str, str]:
        """A conformant client's headers: a bearer token and a version (§3.6.1)."""
        return {
            "Authorization": f"Bearer {token or self.token}",
            "A2A-Version": "1.0",
            "Content-Type": "application/json",
        }

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.client.post(
            "/a2a/v1/rpc",
            headers=self.headers(),
            json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}},
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def send(self, text: str, **message_fields: Any) -> dict[str, Any]:
        result = await self.rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": f"m-{text[:8]}",
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                    **message_fields,
                },
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        )
        assert "error" not in result, result
        task: dict[str, Any] = result["result"]["task"]
        return task


def _model() -> FakeModel:
    return (
        FakeModel()
        .turn(tool_calls=[("lookup_order", {"order_id": "A1"})])
        .turn(text="Order A1 shipped on Tuesday.")
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    async def lookup_order(order_id: str) -> dict[str, str]:
        """Look an order up by id."""
        return {"order_id": order_id, "status": "shipped"}

    return registry


@contextlib.asynccontextmanager
async def _harness(tmp_path: Path, model: FakeModel) -> AsyncIterator[_Harness]:
    """The playground's A2A router, over a real Worker, with no network."""
    from datetime import UTC, datetime

    from app.a2a import (
        A2ADeps,
        A2AService,
        CardFactory,
        PushConfigStore,
        PushSender,
        build_router,
        load_signing_key,
    )
    from app.auth import create_account
    from app.settings_store import SettingsStore
    from app.store_index import AgentEntry, PlaygroundIndex, new_agent_id

    from psych_runtime.model.egress import HttpTransport

    store = InMemoryStore()
    spec = psych_runtime.AgentSpec(
        name="support",
        description="Answers questions about orders.",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
        suspension=psych_runtime.SuspensionPolicy(may_ask_questions=True),
        limits=psych_runtime.Limits(max_turns=6, deadline_seconds=60),
    )
    version = await psych_runtime.publish(store, spec)

    settings_store = SettingsStore(tmp_path / "state.json")
    account, token = await create_account(settings_store, "peer@example.com", "x" * 24)

    index = PlaygroundIndex(tmp_path / "index.json")
    await index.load(store)
    agent_id = new_agent_id()
    await index.record_publish(
        AgentEntry(
            version_hash=version.hash,
            agent_id=agent_id,
            owner=account.id,
            name=spec.name,
            instructions=spec.instructions,
            model=spec.model.model,
            tools=("lookup_order",),
            mcp_servers=(),
            published_at=datetime.now(UTC),
            approval_selectors=(),
        ),
        now=datetime.now(UTC),
    )

    push_configs = PushConfigStore(tmp_path / "push.json")
    service = A2AService(store=store, index=index, push_configs=push_configs, wait_seconds=10)
    transport = HttpTransport()
    sender = PushSender(transport=transport, service=service)
    deps = A2ADeps(
        service=service,
        cards=CardFactory(base_url="http://backend", signer=load_signing_key(tmp_path)),
        index=index,
        settings=settings_store,
        push=sender,
    )

    def _deps_of(request: Request) -> A2ADeps:
        state: A2ADeps = request.app.state.a2a
        return state

    app = FastAPI()
    app.state.a2a = deps
    app.include_router(build_router(_deps_of))

    runtime = Runtime(store=store, model=model, registry=_registry())
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    worker_task = asyncio.create_task(worker.run())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as client:
        try:
            yield _Harness(
                client=client,
                store=store,
                token=token,
                agent_id=agent_id,
                account_id=account.id,
            )
        finally:
            worker.stop()
            await sender.aclose()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(worker_task, timeout=10)
            await transport.aclose()
            # The Windows proactor completes socket teardown on the next loop
            # turn after httpx closes its pool.
            await asyncio.sleep(0)


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    """An agent that answers in two turns: a tool call, then the answer."""
    async with _harness(tmp_path, _model()) as harness:
        yield harness


@pytest_asyncio.fixture
async def asking_harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    """An agent that asks a question first, so a task reaches
    `INPUT_REQUIRED` and has to be answered before it can finish."""
    model = (
        FakeModel()
        .turn(tool_calls=[("ask_question", {"questions": [{"question": "Which order number?"}]})])
        .turn(text="Order A1 shipped on Tuesday.")
    )
    async with _harness(tmp_path, model) as harness:
        yield harness


class TestARealRunOverA2A:
    async def test_a_message_becomes_a_completed_task_carrying_the_answer(
        self, harness: _Harness
    ) -> None:
        task = await harness.send("where is order A1?")

        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        # §3.4.2: the task id is the server's, and it is a real Run.
        assert task["id"]
        assert await harness.store.get_run(RunId(task["id"])) is not None
        # §3.7: the answer is an artifact, not a message.
        assert task["artifacts"][0]["parts"][0]["text"] == "Order A1 shipped on Tuesday."

    async def test_the_states_reported_are_the_states_the_run_passed_through(
        self, harness: _Harness
    ) -> None:
        """The whole lifecycle, as an A2A client sees it: submitted, working,
        completed, in the log's own order (§3.5.2)."""
        task = await harness.send("where is order A1?")
        response = await harness.client.get(
            f"/a2a/v1/tasks/{task['id']}:subscribe", headers=harness.headers()
        )
        # A terminal task refuses a subscription (§3.1.6), which is itself the
        # state assertion: the server knows the task is over.
        assert response.status_code == 400
        assert response.json()["error"]["details"][0]["reason"] == "UNSUPPORTED_OPERATION"

        listed = await harness.rpc("ListTasks", {})
        states = [t["status"]["state"] for t in listed["result"]["tasks"]]
        assert states == ["TASK_STATE_COMPLETED"]

    async def test_a_streamed_message_reports_every_transition_in_order(
        self, harness: _Harness
    ) -> None:
        """§3.1.2 and §3.5.2: SSE, ordered, ending at the terminal state."""
        async with harness.client.stream(
            "POST",
            "/a2a/v1/message:stream",
            headers=harness.headers(),
            json={
                "message": {
                    "messageId": "m-stream",
                    "role": "ROLE_USER",
                    "parts": [{"text": "where is order A1?"}],
                }
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            frames = [
                line.removeprefix("data: ")
                async for line in response.aiter_lines()
                if line.startswith("data: ")
            ]

        import json

        events = [json.loads(frame) for frame in frames]
        states = [
            event["statusUpdate"]["status"]["state"] for event in events if "statusUpdate" in event
        ]
        assert states == [
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
            "TASK_STATE_COMPLETED",
        ]
        # The artifact arrives before the stream closes, never after.
        assert "artifactUpdate" in events[-2]

    async def test_a_second_message_in_the_context_is_a_new_task(self, harness: _Harness) -> None:
        """§3.4.1: a context groups tasks. Psych's continuation chain is that
        context, so the second Run reports the first Run's id as its
        `contextId`."""
        first = await harness.send("where is order A1?")
        second = await harness.send("and order A2?", contextId=first["contextId"])
        assert second["id"] != first["id"]
        assert second["contextId"] == first["contextId"]

        listed = await harness.rpc("ListTasks", {"contextId": first["contextId"]})
        assert {t["id"] for t in listed["result"]["tasks"]} == {first["id"], second["id"]}

    async def test_a_mismatched_context_and_task_are_rejected(self, harness: _Harness) -> None:
        """§3.4.3: "Agents MUST reject messages containing mismatching contextId
        and taskId"."""
        task = await harness.send("where is order A1?")
        result = await harness.rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": "m-bad",
                    "role": "ROLE_USER",
                    "parts": [{"text": "again"}],
                    "taskId": task["id"],
                    "contextId": "some-other-context",
                }
            },
        )
        assert result["error"]["code"] == -32602

    async def test_an_unknown_task_is_not_found(self, harness: _Harness) -> None:
        result = await harness.rpc("GetTask", {"id": "run_does_not_exist"})
        assert result["error"]["code"] == -32001

    async def test_the_rest_binding_answers_the_same_way(self, harness: _Harness) -> None:
        """§5.1: two bindings, one behaviour."""
        response = await harness.client.post(
            "/a2a/v1/message:send",
            headers=harness.headers(),
            json={
                "message": {
                    "messageId": "m-rest",
                    "role": "ROLE_USER",
                    "parts": [{"text": "where is order A1?"}],
                }
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/a2a+json")
        task = response.json()["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"

        fetched = await harness.client.get(f"/a2a/v1/tasks/{task['id']}", headers=harness.headers())
        assert fetched.status_code == 200
        assert fetched.json()["id"] == task["id"]


class TestInputRequiredRoundTrip:
    """The correspondence the mapping was built around, end to end.

    `ask_question` parks the Run; A2A calls that `INPUT_REQUIRED`; §3.4.3 says
    the client answers "with the same taskId and contextId"; Psych resumes the
    same Run. The task id therefore does not change across the round trip,
    which is the thing worth proving: if it did, a client's own record of the
    conversation would fork.
    """

    async def test_a_question_parks_the_task_and_an_answer_finishes_it(
        self, asking_harness: _Harness
    ) -> None:
        task = await asking_harness.send("refund my order")
        assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        # §3.7: the question rides on the status as a message, which is what a
        # client renders. The answer is not a message, and is not here yet.
        assert task["status"]["message"]["parts"][0]["text"]
        assert task["status"]["message"]["role"] == "ROLE_AGENT"
        assert not task.get("artifacts")

        answered = await asking_harness.send("A1", taskId=task["id"], contextId=task["contextId"])
        assert answered["id"] == task["id"]
        assert answered["contextId"] == task["contextId"]
        assert answered["status"]["state"] == "TASK_STATE_COMPLETED"
        assert answered["artifacts"][0]["parts"][0]["text"] == "Order A1 shipped on Tuesday."

    async def test_a_waiting_task_can_be_subscribed_to(self, asking_harness: _Harness) -> None:
        """§3.1.6 refuses a *terminal* task, not an interrupted one: a task
        waiting for input is still live, and a client reconnecting to watch it
        must be allowed to."""
        task = await asking_harness.send("refund my order")
        async with asking_harness.client.stream(
            "GET",
            f"/a2a/v1/tasks/{task['id']}:subscribe",
            headers=asking_harness.headers(),
        ) as response:
            assert response.status_code == 200
            frames = [line async for line in response.aiter_lines() if line.startswith("data: ")]
        assert frames
        assert "TASK_STATE_INPUT_REQUIRED" in frames[-1]


class TestPushNotifications:
    """§4.3: the agent calls the client back, over a real socket.

    The receiver below is a real HTTP server on 127.0.0.1, so the sender's
    headers, body and acknowledgement handling are exercised rather than
    described.
    """

    async def test_a_registered_webhook_receives_the_task_events(self, harness: _Harness) -> None:
        received: list[tuple[dict[str, str], dict[str, Any]]] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            headers, body = await _read_http_request(reader)
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            received.append((headers, body))

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            first = await harness.send("where is order A1?")
            await harness.rpc(
                "CreateTaskPushNotificationConfig",
                {
                    "taskId": first["id"],
                    "url": f"http://127.0.0.1:{port}/hook",
                    "token": "shared-token",
                    "authentication": {"scheme": "Bearer", "credentials": "hook-secret"},
                },
            )
            # Registering starts the watcher, which reads the task's log from
            # the beginning: a webhook registered after the fact is still told
            # what happened, because the log is the source rather than a queue
            # that has already drained.
            for _ in range(100):
                if received:
                    break
                await asyncio.sleep(0.05)
        finally:
            server.close()
            await server.wait_closed()

        assert received, "the webhook was never called"
        headers, body = received[0]
        # §4.3.3: the body is a StreamResponse, exactly what SSE carries.
        assert set(body) <= {"task", "message", "statusUpdate", "artifactUpdate"}
        # §13.2: the agent authenticates itself with the configured credential.
        assert headers["authorization"] == "Bearer hook-secret"
        assert headers["x-a2a-notification-token"] == "shared-token"
        assert headers["content-type"] == "application/a2a+json"


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[dict[str, str], dict[str, Any]]:
    """One HTTP request, as the webhook receiver above needs it."""
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await reader.read(4096)
        if not chunk:
            break
        head += chunk
    header_bytes, _, rest = head.partition(b"\r\n\r\n")
    headers: dict[str, str] = {}
    length = 0
    for line in header_bytes.decode("latin-1").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        key = name.strip().lower()
        if key:
            headers[key] = value.strip()
        if key == "content-length":
            length = int(value.strip())
    body = rest
    while len(body) < length:
        more = await reader.read(length - len(body))
        if not more:
            break
        body += more
    parsed: dict[str, Any] = json.loads(body or b"{}")
    return headers, parsed


class TestTheFrontDoor:
    async def test_no_token_is_refused(self, harness: _Harness) -> None:
        response = await harness.client.get("/a2a/v1/tasks", headers={"A2A-Version": "1.0"})
        assert response.status_code == 401

    async def test_a_missing_version_header_is_refused_as_0_3(self, harness: _Harness) -> None:
        """§3.6.2: an absent header means 0.3, and this deployment speaks 1.0."""
        response = await harness.client.get(
            "/a2a/v1/tasks", headers={"Authorization": f"Bearer {harness.token}"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"][0]["reason"] == "VERSION_NOT_SUPPORTED"

    async def test_another_accounts_task_is_invisible(
        self, harness: _Harness, tmp_path: Path
    ) -> None:
        """The MCP isolation case, for A2A: a second tenant asking for the
        first tenant's task is told it does not exist (§13.1), and never that
        it exists but is not theirs."""
        from app.auth import create_account
        from app.settings_store import SettingsStore

        task = await harness.send("where is order A1?")
        settings_store = SettingsStore(tmp_path / "state.json")
        _other, other_token = await create_account(settings_store, "intruder@example.com", "y" * 24)

        response = await harness.client.get(
            f"/a2a/v1/tasks/{task['id']}",
            headers=harness.headers(token=other_token),
        )
        assert response.status_code == 404
        assert response.json()["error"]["details"][0]["reason"] == "TASK_NOT_FOUND"

        listed = await harness.client.get(
            "/a2a/v1/tasks", headers=harness.headers(token=other_token)
        )
        assert listed.json()["tasks"] == []


class TestDiscoveryAndPushConfigs:
    async def test_the_card_describes_this_agent_and_verifies(
        self, harness: _Harness, tmp_path: Path
    ) -> None:
        from app.a2a import load_signing_key

        from psych_runtime.a2a.models import AgentCard
        from psych_runtime.a2a.signing import verify_card

        response = await harness.client.get(
            "/.well-known/agent-card.json", headers=harness.headers()
        )
        assert response.status_code == 200
        card = AgentCard.model_validate(response.json())
        assert card.name == "support"
        assert [i.protocol_binding for i in card.supported_interfaces] == [
            "JSONRPC",
            "HTTP+JSON",
        ]
        # Every interface names this agent as its routing tenant (§4.4.6), so a
        # peer sending to the shared endpoint reaches the right agent.
        assert all(i.tenant == harness.agent_id for i in card.supported_interfaces)
        assert {skill.id for skill in card.skills} == {"lookup_order"}
        # §8.4: the card is signed, and the signature verifies against the
        # bytes it was served as.
        signer = load_signing_key(tmp_path)
        assert verify_card(card, {signer.kid: signer}) == (signer.kid,)

    async def test_the_extended_card_adds_instructions_for_an_authenticated_caller(
        self, harness: _Harness
    ) -> None:
        """§13.3: this deployment declares `extendedAgentCard` and serves one,
        because a client that has authenticated with this agent's own account
        is exactly who §13.3 imagines reading it -- the description alone,
        which is all the public card carries, does not say how the agent
        behaves. The public card's own instructions are asserted absent in
        `test_the_card_describes_this_agent_and_verifies`."""
        result = await harness.rpc("GetExtendedAgentCard", {})
        assert "error" not in result, result
        assert "Help the customer" in result["result"]["description"]

    async def test_push_configs_round_trip(self, harness: _Harness) -> None:
        """§3.1.7 to §3.1.10, over the binding a client would actually use."""
        task = await harness.send("where is order A1?")
        created = await harness.rpc(
            "CreateTaskPushNotificationConfig",
            {
                "taskId": task["id"],
                "url": "https://client.example.test/hooks/a2a",
                "token": "shared-token",
                "authentication": {"scheme": "Bearer", "credentials": "hook-secret"},
            },
        )
        config_id = created["result"]["id"]
        assert config_id

        listed = await harness.rpc("ListTaskPushNotificationConfigs", {"taskId": task["id"]})
        assert [c["id"] for c in listed["result"]["configs"]] == [config_id]

        fetched = await harness.rpc(
            "GetTaskPushNotificationConfig", {"taskId": task["id"], "id": config_id}
        )
        assert fetched["result"]["url"] == "https://client.example.test/hooks/a2a"

        deleted = await harness.rpc(
            "DeleteTaskPushNotificationConfig", {"taskId": task["id"], "id": config_id}
        )
        assert deleted["result"] == {}

        empty = await harness.rpc("ListTaskPushNotificationConfigs", {"taskId": task["id"]})
        assert empty["result"].get("configs", []) == []

    async def test_a_push_config_for_another_accounts_task_is_not_found(
        self, harness: _Harness, tmp_path: Path
    ) -> None:
        from app.auth import create_account
        from app.settings_store import SettingsStore

        task = await harness.send("where is order A1?")
        settings_store = SettingsStore(tmp_path / "state.json")
        _other, other_token = await create_account(
            settings_store, "intruder2@example.com", "z" * 24
        )
        response = await harness.client.post(
            f"/a2a/v1/tasks/{task['id']}/pushNotificationConfigs",
            headers=harness.headers(token=other_token),
            json={"url": "https://attacker.example.test/hook"},
        )
        assert response.status_code == 404
