"""End to end: an agent built with the typed builder, published, and run.

DESIGN.md §23, item one: an agent Spec built in Python and the same Spec built
from a dict must produce the same Version hash and run identically. The unit
suite (``tests/unit/test_builder.py``) proves the hash half of that; this file
proves the "runs identically" half, by running both forms through the real
``AgentLoop`` against the same scripted ``FakeModel`` and asserting the log
tells the same story either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from psych_runtime.builder import AgentBuilder
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import RECORD_ADAPTER, TerminalState
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec
from psych_runtime.core.validation import ValidationContext
from psych_runtime.core.version import Version, compute_hash
from psych_runtime.core.version import publish as make_version
from psych_runtime.runtime.agent import AgentLoop, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by id."""
    if order_id == "MISSING":
        raise ValueError("no such order")
    return {"order_id": order_id, "status": "shipped"}


def build_with_builder() -> tuple[AgentSpec, ToolRegistry]:
    """The agent, authored through ``AgentBuilder``.

    Grants an http tool and an optional MCP server alongside the code tool the
    script actually calls, so the builder's coverage of every DESIGN.md §10.1
    tool kind is exercised even though only the code tool is invoked.
    """
    builder = (
        AgentBuilder("support")
        .description("Answers order status questions.")
        .instructions("Help the customer with their order.")
        .model("fake-standard")
        .tool(lookup_order)
        .http_tool(
            "issue_refund_http",
            description="Issue a refund for an order.",
            url="https://api.example.com/refunds/{order_id}",
            method="POST",
            credential="payments-key",
        )
        .mcp_server("crm", "https://mcp.example.com/crm", optional=True)
        .limits(max_steps=10)
    )
    return builder.build(), builder.registry


def build_with_dict() -> tuple[AgentSpec, ToolRegistry]:
    """The structurally identical agent, authored as a validated dict.

    DESIGN.md §4: chat, builder, file and import all converge on the same
    Pydantic model. This is standing in for the "file" form: a dict is exactly
    what a loaded YAML or JSON document becomes before validation.
    """
    registry = ToolRegistry()
    registry.register(lookup_order)
    spec = AgentSpec.model_validate(
        {
            "kind": "agent",
            "name": "support",
            "description": "Answers order status questions.",
            "instructions": "Help the customer with their order.",
            "model": {"model": "fake-standard"},
            "tools": [
                {"kind": "code", "name": "lookup_order"},
                {
                    "kind": "http",
                    "name": "issue_refund_http",
                    "description": "Issue a refund for an order.",
                    "url": "https://api.example.com/refunds/{order_id}",
                    "method": "POST",
                    "credential": "payments-key",
                },
            ],
            "mcp_servers": [
                {"name": "crm", "url": "https://mcp.example.com/crm", "optional": True}
            ],
            "limits": {"max_steps": 10},
        }
    )
    return spec, registry


async def publish_spec(store: InMemoryStore, spec: AgentSpec, registry: ToolRegistry) -> Version:
    from psych_runtime.core.validation import validate_spec

    validate_spec(spec, ValidationContext(registered_tools=registry.names))
    version = make_version(spec)
    await store.put_version(version)
    return version


async def start_run(store: InMemoryStore, version: Version, message: str) -> RunId:
    """Admit a Run the way ``psych_runtime.dispatch`` will, so the log starts correctly."""
    from psych_runtime.core.ids import new_run_id

    run_id = new_run_id()
    now = datetime.now(UTC)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=SCOPE,
            version_hash=version.hash,
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=now + timedelta(seconds=version.spec.limits.deadline_seconds),
        )
    )
    await store.append(
        run_id,
        1,
        RECORD_ADAPTER.validate_python(
            {
                "type": "run_admitted",
                "run_id": run_id,
                "seq": 1,
                "at": now,
                "scope": SCOPE,
                "version_hash": version.hash,
                "input": {"message": message},
                "deadline_at": now + timedelta(seconds=version.spec.limits.deadline_seconds),
            }
        ),
    )
    return run_id


async def run_agent(
    spec: AgentSpec, registry: ToolRegistry, model: FakeModel, message: str = "where is A1?"
) -> tuple[TerminalState, dict[str, Any] | None, InMemoryStore, RunId]:
    """Publish, admit and run one agent end to end, and return what a caller
    checking the outcome cares about."""
    store = InMemoryStore()
    version = await publish_spec(store, spec, registry)
    run_id = await start_run(store, version, message)

    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    loop = AgentLoop(
        journal,
        spec,
        model,
        ToolResolver(registry),
        ToolExecutor(registry),
    )
    outcome = await loop.run()
    if not journal.state.settled and not journal.state.suspended:
        await journal.append(
            type="run_settled", state=outcome.state, output=outcome.output, failure=outcome.failure
        )
    return outcome.state, outcome.output, store, run_id


def script() -> FakeModel:
    return (
        FakeModel()
        .turn(text="Let me check.", tool_calls=[("lookup_order", {"order_id": "A1"})])
        .turn(text="Your order A1 has shipped.")
    )


class TestTheBuilderAndTheDictRunIdentically:
    async def test_the_same_hash(self) -> None:
        from_builder, _ = build_with_builder()
        from_dict, _ = build_with_dict()
        assert compute_hash(from_builder) == compute_hash(from_dict)

    async def test_both_forms_complete_the_run_with_the_same_outcome(self) -> None:
        spec_b, registry_b = build_with_builder()
        spec_d, registry_d = build_with_dict()

        state_b, output_b, store_b, run_id_b = await run_agent(spec_b, registry_b, script())
        state_d, output_d, store_d, run_id_d = await run_agent(spec_d, registry_d, script())

        assert state_b is TerminalState.COMPLETED
        assert state_b is state_d
        assert output_b == output_d == {"text": "Your order A1 has shipped."}

        records_b = await store_b.read(run_id_b)
        records_d = await store_d.read(run_id_d)
        state_view_b = reduce(records_b)
        state_view_d = reduce(records_d)
        assert state_view_b.turn == state_view_d.turn == 2
        assert [r.tool for r in state_view_b.tool_results] == ["lookup_order"]
        assert [r.tool for r in state_view_d.tool_results] == ["lookup_order"]
        assert state_view_b.tool_results[0].outcome == state_view_d.tool_results[0].outcome

    async def test_a_tool_failure_is_handled_identically_by_both_forms(self) -> None:
        failing_script = (
            FakeModel()
            .turn(tool_calls=[("lookup_order", {"order_id": "MISSING"})])
            .turn(text="I could not find that order.")
        )
        failing_script_2 = (
            FakeModel()
            .turn(tool_calls=[("lookup_order", {"order_id": "MISSING"})])
            .turn(text="I could not find that order.")
        )
        spec_b, registry_b = build_with_builder()
        spec_d, registry_d = build_with_dict()

        state_b, output_b, _, _ = await run_agent(spec_b, registry_b, failing_script)
        state_d, output_d, _, _ = await run_agent(spec_d, registry_d, failing_script_2)

        assert state_b is TerminalState.COMPLETED
        assert state_b is state_d
        assert output_b == output_d == {"text": "I could not find that order."}


class TestBuilderProducedAgentRunsOnItsOwn:
    async def test_the_run_completes_with_a_code_tool_call(self) -> None:
        spec, registry = build_with_builder()
        state, output, store, run_id = await run_agent(spec, registry, script())

        assert state is TerminalState.COMPLETED
        assert output == {"text": "Your order A1 has shipped."}

        state_view = reduce(await store.read(run_id))
        assert not state_view.has_dangling_tool_calls
        assert state_view.model_calls == 2

    async def test_the_granted_http_and_mcp_tools_reach_the_spec_without_being_called(
        self,
    ) -> None:
        spec, _ = build_with_builder()
        assert {tool.name for tool in spec.tools} == {"lookup_order", "issue_refund_http"}
        assert {server.name for server in spec.mcp_servers} == {"crm"}

    async def test_limits_set_through_the_builder_take_effect(self) -> None:
        spec, _ = build_with_builder()
        assert spec.limits.max_steps == 10


class TestEveryMcpServerFieldSurvivesBothForms:
    """DESIGN.md §23's first item is that the same agent built in Python and
    built from a dict produce the same Version hash.

    That property is not maintained by the hash function; it is maintained by
    the builder exposing every field the Spec has. A field added to
    ``McpServer`` without a matching builder parameter silently makes the two
    forms unable to express the same agent, and every existing parity test
    keeps passing, because they only compare agents that avoid the new field.

    ``McpServer.oauth`` was exactly that: a Spec could carry
    per-server OAuth configuration and the typed builder had no way to set it.
    """

    def _oauth(self) -> Any:
        from psych_runtime.core.spec import McpOAuth

        return McpOAuth(
            grant="client_credentials",
            preregistered_client_id="crm-client",
            client_secret_credential="crm-secret",
        )

    def test_per_server_oauth_set_through_the_builder_hashes_as_the_dict_form(self) -> None:
        from psych_runtime.core.spec import McpServer, ModelRef

        oauth = self._oauth()
        built = (
            AgentBuilder("support")
            .instructions("help")
            .model("gpt-test")
            .mcp_server("crm", "https://crm.example/mcp", credential="crm-token", oauth=oauth)
            .build()
        )
        direct = AgentSpec(
            name="support",
            instructions="help",
            model=ModelRef(model="gpt-test"),
            mcp_servers=(
                McpServer(
                    name="crm",
                    url="https://crm.example/mcp",
                    credential="crm-token",
                    oauth=oauth,
                ),
            ),
        )
        assert compute_hash(built) == compute_hash(direct)

    def test_the_builder_carries_the_oauth_config_rather_than_dropping_it(self) -> None:
        """A builder that silently ignored the argument would still pass the
        hash test above if the dict form were built the same way, so assert the
        configuration actually arrives."""
        oauth = self._oauth()
        built = (
            AgentBuilder("support")
            .instructions("help")
            .model("gpt-test")
            .mcp_server("crm", "https://crm.example/mcp", oauth=oauth)
            .build()
        )
        assert built.mcp_servers[0].oauth == oauth

    def test_two_servers_can_carry_different_identities_through_the_builder(self) -> None:
        """The constraint per-server OAuth removed, expressed in the builder."""
        from psych_runtime.core.spec import McpOAuth

        built = (
            AgentBuilder("support")
            .instructions("help")
            .model("gpt-test")
            .mcp_server(
                "crm",
                "https://crm.example/mcp",
                oauth=McpOAuth(grant="client_credentials", preregistered_client_id="crm-client"),
            )
            .mcp_server(
                "tickets",
                "https://tickets.example/mcp",
                oauth=McpOAuth(
                    grant="authorization_code", preregistered_client_id="tickets-client"
                ),
            )
            .build()
        )
        crm, tickets = built.mcp_servers
        assert crm.oauth is not None
        assert tickets.oauth is not None
        assert crm.oauth.grant == "client_credentials"
        assert tickets.oauth.grant == "authorization_code"
        assert crm.oauth.preregistered_client_id != tickets.oauth.preregistered_client_id

    def test_no_client_secret_literal_reaches_an_exported_version(self) -> None:
        """The Spec carries a credential *name*; the SecretResolver holds the
        secret. A Version is stored and shipped around, so a literal in one is
        a secret at rest in the consumer's database."""
        from psych_runtime.core.version import canonical_bytes

        built = (
            AgentBuilder("support")
            .instructions("help")
            .model("gpt-test")
            .mcp_server("crm", "https://crm.example/mcp", oauth=self._oauth())
            .build()
        )
        serialised = canonical_bytes(built).decode()
        assert "crm-secret" in serialised, "the credential name is what gets stored"
        assert "client_secret" not in serialised or "client_secret_credential" in serialised
