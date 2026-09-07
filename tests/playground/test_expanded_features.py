"""The features this parity pass added, driven exactly the way a person or
another agent would reach them.

`test_backend.py` covers the contract the backend already had; this covers
what it did not: the rest of `AgentSpec` (description, HTTP tools, the
subagent roster, the spawn envelope, sampling options, suspension expiries),
workflows as a first-class publishable thing, the send/state/records/text
routes, per-account runtime settings, and A2A end to end -- including a real
peer-to-peer call between two agents of one account, over a real TCP
connection, because a feature that is only ever asserted through its own
mapping layer is not proven to work.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.playground.conftest import (
    StubProvider,
    Turn,
    sign_up,
    wait_for,
)

pytestmark = pytest.mark.functional

AGENT = {
    "name": "support",
    "instructions": "Help the customer with their order.",
    "model": "test-model",
    "tools": ["lookup_order"],
}


async def _publish(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/agents", json={**AGENT, **overrides})
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestFullAgentSpecParity:
    """Every field on `AgentSpec` reachable through `POST /api/agents`, and
    read back whole through `GET /api/agents/{agent_id}`."""

    async def test_description_round_trips(self, client: httpx.AsyncClient) -> None:
        created = await _publish(client, description="Answers order questions.")
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["description"] == "Answers order questions."

    async def test_model_options_round_trip(self, client: httpx.AsyncClient) -> None:
        created = await _publish(
            client,
            model_options={
                "top_p": 0.9,
                "max_output_tokens": 512,
                "reasoning_effort": "low",
                "fallbacks": ["backup-model"],
            },
        )
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["model_options"] == {
            "top_p": 0.9,
            "max_output_tokens": 512,
            "reasoning_effort": "low",
            "fallbacks": ["backup-model"],
        }

    async def test_http_tool_round_trips_and_never_carries_a_literal_auth_header(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await _publish(
            client,
            http_tools=[
                {
                    "name": "get_weather",
                    "description": "Today's weather for a city.",
                    "url": "https://api.example.com/weather/{city}",
                    "method": "GET",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                    "credential": "weather_key",
                }
            ],
        )
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["http_tools"][0]["name"] == "get_weather"
        assert agent["http_tools"][0]["credential"] == "weather_key"

        # The library itself refuses a literal Authorization header in a
        # published tool -- a Spec is exported and reviewed in git.
        rejected = await client.post(
            "/api/agents",
            json={
                **AGENT,
                "name": "leaky",
                "http_tools": [
                    {
                        "name": "bad",
                        "description": "x",
                        "url": "https://api.example.com/x",
                        "headers": {"Authorization": "Bearer literal-secret"},
                    }
                ],
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "literal-secret" not in rejected.text

    async def test_subagent_roster_is_embedded_and_pinned(self, client: httpx.AsyncClient) -> None:
        child = await _publish(client, name="researcher")
        parent = await _publish(
            client,
            name="lead",
            subagents=[
                {
                    "name": "researcher",
                    "description": "Deep research over the internal wiki, one question at a time.",
                    "agent_id": child["agent_id"],
                }
            ],
        )
        agent = (await client.get(f"/api/agents/{parent['agent_id']}")).json()
        assert agent["subagents"][0]["agent_id"] == child["agent_id"]
        assert agent["subagents"][0]["version_hash"] == child["version_hash"]

    async def test_a_short_subagent_description_is_refused_at_publish(
        self, client: httpx.AsyncClient
    ) -> None:
        child = await _publish(client, name="helper")
        rejected = await client.post(
            "/api/agents",
            json={
                **AGENT,
                "name": "lead2",
                "subagents": [
                    {"name": "helper", "description": "too short", "agent_id": child["agent_id"]}
                ],
            },
        )
        # 422: FastAPI's own request-model validation catches this before
        # anything reaches `psych.publish`, since `SubagentRefIn.description`
        # already declares the same 20-character floor.
        assert rejected.status_code == 422, rejected.text

    async def test_spawn_envelope_round_trips(self, client: httpx.AsyncClient) -> None:
        created = await _publish(
            client,
            subagents_enabled=True,
            spawn={
                "tools": ["lookup_order"],
                "models": [],
                "max_depth": 3,
                "max_alive": 5,
                "may_message": False,
            },
        )
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["subagents_enabled"] is True
        assert agent["spawn"]["max_depth"] == 3
        assert agent["spawn"]["max_alive"] == 5
        assert agent["spawn"]["may_message"] is False

    async def test_suspension_expiries_round_trip(self, client: httpx.AsyncClient) -> None:
        created = await _publish(
            client,
            suspension={
                "approval_expires_seconds": 3600,
                "question_expires_seconds": 7200,
                "external_expires_seconds": 86400,
                "children_expires_seconds": 600,
            },
        )
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["suspension"]["approval_expires_seconds"] == 3600
        assert agent["suspension"]["children_expires_seconds"] == 600

    async def test_limits_round_trip_on_the_summary(self, client: httpx.AsyncClient) -> None:
        created = await _publish(client, limits={"max_turns": 7})
        agent = (await client.get(f"/api/agents/{created['agent_id']}")).json()
        assert agent["limits"]["max_turns"] == 7


class TestSendStateRecordsText:
    """The rest of the public API surface the runs routes now cover."""

    async def test_send_refuses_after_settlement(self, client: httpx.AsyncClient) -> None:
        agent_id = await _publish_id(client)
        run_id = await dispatch_default(client, agent_id)
        # No stub model wired for `client` (its provider points at a closed
        # port), so this run never settles on its own during the test. We
        # only need a run id that exists to prove `/send` reaches the
        # library and reports its refusal honestly; that is asserted in
        # `TestLiveConversation` against a run that really does settle.
        response = await client.post(
            f"/api/runs/{run_id}/send", json={"message": "hello", "queue": "steer"}
        )
        # Still running (no model answers), so a steer should be accepted.
        assert response.status_code == 200, response.text
        assert response.json()["queue"] == "steer"

    async def test_state_and_records_are_readable(self, client: httpx.AsyncClient) -> None:
        agent_id = await _publish_id(client)
        run_id = await dispatch_default(client, agent_id)

        state = await client.get(f"/api/runs/{run_id}/state")
        assert state.status_code == 200, state.text
        assert "turn" in state.json()

        records = await client.get(f"/api/runs/{run_id}/records")
        assert records.status_code == 200, records.text
        assert len(records.json()) >= 1
        assert records.json()[0]["type"] == "run_admitted"


async def _publish_id(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/agents", json=AGENT)
    assert response.status_code == 201, response.text
    return str(response.json()["agent_id"])


async def dispatch_default(client: httpx.AsyncClient, agent_id: str) -> str:
    response = await client.post("/api/runs", json={"agent_id": agent_id, "message": "look up A1"})
    assert response.status_code == 201, response.text
    return str(response.json()["run_id"])


class TestWorkflows:
    """DESIGN.md §5's other authoring surface: a fixed order of steps,
    published, listed, and driven to completion by the same worker."""

    async def test_publish_list_get_delete(self, client: httpx.AsyncClient) -> None:
        created = await client.post(
            "/api/workflows",
            json={
                "name": "two_lookups",
                "description": "Looks up two orders.",
                "steps": [
                    {
                        "kind": "tool",
                        "name": "first",
                        "tool": "lookup_order",
                        "arguments": {"order_id": "A1"},
                    },
                    {
                        "kind": "tool",
                        "name": "second",
                        "tool": "lookup_order",
                        "arguments": {"order_id": "A2"},
                    },
                ],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["created"] is True

        listed = await client.get("/api/workflows")
        assert listed.status_code == 200
        assert any(w["workflow_id"] == body["workflow_id"] for w in listed.json())

        fetched = await client.get(f"/api/workflows/{body['workflow_id']}")
        assert fetched.status_code == 200
        assert len(fetched.json()["steps"]) == 2

        deleted = await client.delete(f"/api/workflows/{body['workflow_id']}")
        assert deleted.status_code == 200
        assert (await client.get("/api/workflows")).json() == []

    async def test_a_step_naming_an_unregistered_tool_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/workflows",
            json={
                "name": "bad",
                "steps": [{"kind": "tool", "name": "s", "tool": "does_not_exist", "arguments": {}}],
            },
        )
        assert response.status_code == 400, response.text

    async def test_an_agent_step_embeds_the_named_agent(self, client: httpx.AsyncClient) -> None:
        agent = await client.post("/api/agents", json=AGENT)
        agent_id = str(agent.json()["agent_id"])
        version_hash = str(agent.json()["version_hash"])

        created = await client.post(
            "/api/workflows",
            json={
                "name": "with_agent",
                "steps": [{"kind": "agent", "name": "ask", "agent_id": agent_id}],
            },
        )
        assert created.status_code == 201, created.text
        fetched = (await client.get(f"/api/workflows/{created.json()['workflow_id']}")).json()
        assert fetched["steps"][0]["version_hash"] == version_hash

    async def test_dispatch_runs_a_workflow_to_completion(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        """Dispatched exactly like an agent, and driven by the same worker:
        this is what proves a workflow is not merely publishable but
        actually executable through the ordinary `/api/runs` door."""
        stub_provider.says(Turn(text="An agent step ran too."))
        agent = await talking.post(
            "/api/agents",
            json={
                "name": "wrap",
                "instructions": "Summarise briefly.",
                "model": "stub-model",
                "tools": [],
            },
        )
        created = await talking.post(
            "/api/workflows",
            json={
                "name": "pipeline",
                "steps": [
                    {
                        "kind": "tool",
                        "name": "look_up",
                        "tool": "lookup_order",
                        "arguments": {"order_id": "A1"},
                    },
                    {"kind": "agent", "name": "wrap_up", "agent_id": agent.json()["agent_id"]},
                ],
            },
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["workflow_id"]

        dispatched = await talking.post(
            "/api/runs", json={"workflow_id": workflow_id, "message": "go"}
        )
        assert dispatched.status_code == 201, dispatched.text
        run_id = dispatched.json()["run_id"]

        status = await wait_for(talking, run_id)
        assert status["lifecycle"] == "done", status

        listed = (await talking.get("/api/runs")).json()
        row = next(r for r in listed if r["run_id"] == run_id)
        assert row["kind"] == "workflow"
        assert row["workflow_id"] == workflow_id


class TestRuntimeSettings:
    """The `Runtime` knobs that are per-account rather than per-agent: none
    of them should move any published agent's version hash."""

    async def test_round_trips_and_reports_sandbox_availability(
        self, client: httpx.AsyncClient
    ) -> None:
        before = (await client.get("/api/settings")).json()["runtime"]
        assert "sandbox_available" in before

        updated = await client.put(
            "/api/settings/runtime",
            json={
                "cost_policy": "computed",
                "blob_offload_bytes": 123456,
                "catalogue_budget_chars": 5000,
                "sandbox_enabled": False,
                "sandbox_limits": {
                    "cpu_seconds": 2,
                    "address_space_bytes": 1024 * 1024,
                    "file_size_bytes": 1024,
                    "process_count": 4,
                    "wall_seconds": 5,
                },
                "egress_allow": ["api.example.com"],
                "denied_tools": ["issue_refund"],
            },
        )
        assert updated.status_code == 200, updated.text
        runtime = updated.json()["runtime"]
        assert runtime["cost_policy"] == "computed"
        assert runtime["blob_offload_bytes"] == 123456
        assert runtime["egress_allow"] == ["api.example.com"]
        assert runtime["denied_tools"] == ["issue_refund"]

    async def test_publishing_an_agent_is_unaffected_by_runtime_settings(
        self, client: httpx.AsyncClient
    ) -> None:
        """Runtime settings are execution, not identity: two publishes of the
        same agent must share one version hash whatever this account's
        runtime settings say."""
        before_hash = (await _publish(client))["version_hash"]
        await client.put(
            "/api/settings/runtime",
            json={
                "cost_policy": "provider_only",
                "blob_offload_bytes": 1,
                "catalogue_budget_chars": 1,
                "sandbox_enabled": True,
                "sandbox_limits": {
                    "cpu_seconds": 1,
                    "address_space_bytes": 1,
                    "file_size_bytes": 1,
                    "process_count": 1,
                    "wall_seconds": 1,
                },
                "egress_allow": ["nowhere.example.com"],
                "denied_tools": ["lookup_order"],
            },
        )
        after = await client.post("/api/agents", json={**AGENT, "agent_id": None})
        assert after.json()["version_hash"] == before_hash


class TestA2ADiscovery:
    """Agent Cards, with and without a credential (§8.2, §13.3)."""

    async def test_the_public_card_needs_no_credential(self, client: httpx.AsyncClient) -> None:
        created = await _publish(client, description="A support agent.")
        # `client` carries a signed-in session cookie for `/api/*`, but the
        # A2A door sits outside `/api` and authenticates with a bearer token
        # alone (`app.a2a.router._authenticate`), which this request never
        # sends. So this is already the unauthenticated case the public card
        # exists for -- no second client needed.
        card = await client.get(f"/a2a/v1/agents/{created['agent_id']}/agent-card.json")
        assert card.status_code == 200, card.text
        body = card.json()
        assert body["name"] == "support"
        # The public card never carries instructions.
        assert "Help the customer" not in body["description"]

    async def test_the_extended_card_adds_instructions_for_an_authenticated_caller(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await _publish(client, description="A support agent.")
        minted = await client.post("/api/settings/a2a/tokens", json={"secret_name": "test-token"})
        assert minted.status_code == 201, minted.text
        token = minted.json()["token"]

        response = await client.post(
            "/a2a/v1/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "GetExtendedAgentCard",
                "params": {"tenant": created["agent_id"]},
            },
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
        )
        assert response.status_code == 200, response.text
        result = response.json().get("result")
        assert result is not None, response.text
        assert "Help the customer" in result["description"]


class TestA2ARoundTrip:
    """Two agents of one account, talking to each other over a real HTTP
    connection: the whole protocol path, not a mock of it."""

    async def test_one_agent_calls_another_over_a2a(
        self, live_playground: tuple[str, httpx.AsyncClient], stub_provider: StubProvider
    ) -> None:
        base_url, client = live_playground
        await sign_up(client, "owner@example.com")

        target = await client.post(
            "/api/agents",
            json={
                "name": "orders",
                "description": "Looks up an order's shipping status.",
                "instructions": "Answer questions about orders.",
                "model": "stub-model",
                "tools": ["lookup_order"],
            },
        )
        assert target.status_code == 201, target.text
        target_agent_id = target.json()["agent_id"]

        added = await client.post(
            "/api/settings/a2a/peers/local",
            json={"agent_id": target_agent_id, "name": "orders_peer", "secret_name": "self-token"},
        )
        assert added.status_code == 200, added.text
        peers = added.json()["a2a_peers"]
        peer = next(p for p in peers if p["name"] == "orders_peer")
        assert peer["url"] == f"{base_url}/a2a/v1/agents/{target_agent_id}/agent-card.json"
        assert peer["credential"] == "self-token"
        assert peer["tenant"] == target_agent_id

        caller = await client.post(
            "/api/agents",
            json={
                "name": "front_desk",
                "instructions": "Use the orders_peer skill for anything about an order.",
                "model": "stub-model",
                "tools": [],
                "a2a": [
                    {
                        "name": "orders_peer",
                        "url": peer["url"],
                        "credential": "self-token",
                        "scheme": "Bearer",
                        # This deployment serves several agents behind one
                        # A2A door, and `tenant` is what tells it which one
                        # (§4.4.6). `add_local_peer` already put the target's
                        # agent id here on the saved preset; a published
                        # agent's own copy needs it too.
                        "tenant": peer["tenant"],
                    }
                ],
            },
        )
        assert caller.status_code == 201, caller.text

        # The tool name the caller's model is offered is `{peer}__{skill_id}`,
        # and a code tool's skill id is the tool's own name
        # (`app.a2a.cards.skills_of`, mirrored from `psych.a2a.card`). The
        # call itself is not a re-invocation of the peer's own argument
        # shape: an A2A peer skill is asked in natural language
        # (`psych.tools.a2a._definition_for`), so the model sends a
        # `message`, and the peer's own agent decides how to satisfy it.
        stub_provider.says(
            Turn(
                tool_calls=(
                    ("orders_peer__lookup_order", {"message": "What is the status of order A1?"}),
                ),
                prompt_tokens=10,
            ),
            Turn(text="Order A1 has shipped.", prompt_tokens=10, completion_tokens=5),
        )

        dispatched = await client.post(
            "/api/runs",
            json={"agent_id": caller.json()["agent_id"], "message": "What is the status of A1?"},
        )
        assert dispatched.status_code == 201, dispatched.text
        run_id = dispatched.json()["run_id"]

        status = await wait_for(client, run_id)
        assert status["lifecycle"] == "done", status

        report = (await client.get(f"/api/runs/{run_id}/report")).json()
        tool_calls = report["tool_calls"]
        assert any(
            call["tool"] == "orders_peer__lookup_order" and call["outcome"] == "ok"
            for call in tool_calls
        ), tool_calls

    async def test_minting_a_token_makes_it_a_usable_secret(
        self, live_playground: tuple[str, httpx.AsyncClient]
    ) -> None:
        _, client = live_playground
        await sign_up(client, "owner@example.com")

        minted = await client.post(
            "/api/settings/a2a/tokens", json={"secret_name": "peer-token", "ttl_days": 30}
        )
        assert minted.status_code == 201, minted.text
        assert minted.json()["secret_name"] == "peer-token"
        assert minted.json()["token"]

        settings = (await client.get("/api/settings")).json()
        assert "peer-token" in settings["secrets"]
        # Never the value, only the name.
        assert minted.json()["token"] not in (await client.get("/api/settings")).text
