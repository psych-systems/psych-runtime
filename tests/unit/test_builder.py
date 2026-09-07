"""The typed builder: the fluent API, and the invariants it must uphold.

DESIGN.md §4 and §23. The property that matters most: the same agent built
with ``AgentBuilder`` and the same agent built from a dict hash identically,
because the builder is one of four authoring forms Psych privileges none of,
and if it produced a structurally different Spec it would be a fifth thing
wearing the name of one of the four.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from psych_runtime.builder import AgentBuilder, BuilderError, WorkflowBuilder, agent, workflow
from psych_runtime.builder.shared import SharedBuilder
from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    CodeTool,
    HttpTool,
    Limits,
    McpServer,
    ModelRef,
    SubagentRef,
    ToolStep,
    WorkflowSpec,
    WorkflowStepRef,
)
from psych_runtime.core.validation import validate_spec
from psych_runtime.core.version import compute_hash
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by id."""
    return {"order_id": order_id, "status": "shipped"}


def issue_refund(order_id: str, cents: int) -> str:
    """Issue a refund."""
    return f"refunded {cents} for {order_id}"


# ---------------------------------------------------------------------------
# The acceptance test that matters: builder output hashes like a dict Spec.
# ---------------------------------------------------------------------------


class TestBuilderHashesLikeADict:
    def test_a_minimal_agent_hashes_like_the_equivalent_dict(self) -> None:
        from_builder = AgentBuilder("support").model("gpt-4o").build()
        from_dict = AgentSpec.model_validate(
            {"kind": "agent", "name": "support", "model": {"model": "gpt-4o"}}
        )
        assert compute_hash(from_builder) == compute_hash(from_dict)

    def test_a_fully_populated_agent_hashes_like_the_equivalent_dict(self) -> None:
        registry = ToolRegistry()
        builder = (
            AgentBuilder("support", registry=registry)
            .description("Handles order questions.")
            .instructions("Be helpful. See [[skill:policy]].")
            .model(
                "gpt-4o",
                temperature=0.2,
                top_p=0.9,
                max_output_tokens=512,
                reasoning_effort="low",
                fallbacks=("gpt-4o-mini",),
            )
            .tool(lookup_order)
            .http_tool(
                "issue_refund_http",
                description="Issue a refund.",
                url="https://api.example.com/refunds/{order_id}",
                method="POST",
                input_schema={"type": "object", "properties": {"cents": {"type": "integer"}}},
                credential="payments-key",
            )
            .mcp_server("crm", "https://mcp.example.com/crm", allow=["read_*"])
            .skill("policy", "Refund policy", body="Refunds within 30 days.")
            .limits(max_steps=10, deadline_seconds=120.0)
            .suspension(approval_expires_seconds=3600.0)
        )
        from_builder = builder.build()

        from_dict = AgentSpec.model_validate(
            {
                "kind": "agent",
                "name": "support",
                "description": "Handles order questions.",
                "instructions": "Be helpful. See [[skill:policy]].",
                "model": {
                    "model": "gpt-4o",
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "max_output_tokens": 512,
                    "reasoning_effort": "low",
                    "fallbacks": ["gpt-4o-mini"],
                },
                "tools": [
                    {"kind": "code", "name": "lookup_order"},
                    {
                        "kind": "http",
                        "name": "issue_refund_http",
                        "description": "Issue a refund.",
                        "url": "https://api.example.com/refunds/{order_id}",
                        "method": "POST",
                        "input_schema": {
                            "type": "object",
                            "properties": {"cents": {"type": "integer"}},
                        },
                        "credential": "payments-key",
                    },
                ],
                "mcp_servers": [
                    {"name": "crm", "url": "https://mcp.example.com/crm", "allow": ["read_*"]}
                ],
                "skills": [
                    {
                        "name": "policy",
                        "description": "Refund policy",
                        "body": "Refunds within 30 days.",
                    }
                ],
                "limits": {"max_steps": 10, "deadline_seconds": 120.0},
                "suspension": {"approval_expires_seconds": 3600.0},
            }
        )

        assert compute_hash(from_builder) == compute_hash(from_dict)

    def test_a_subagent_hashes_like_the_equivalent_dict(self) -> None:
        child = AgentBuilder("billing").model("gpt-4o-mini")
        from_builder = (
            AgentBuilder("router")
            .model("gpt-4o")
            .subagent("billing", "Handles billing questions specifically.", child)
            .build()
        )
        from_dict = AgentSpec.model_validate(
            {
                "kind": "agent",
                "name": "router",
                "model": {"model": "gpt-4o"},
                "subagents": [
                    {
                        "name": "billing",
                        "description": "Handles billing questions specifically.",
                        "spec": {
                            "kind": "agent",
                            "name": "billing",
                            "model": {"model": "gpt-4o-mini"},
                        },
                    }
                ],
            }
        )
        assert compute_hash(from_builder) == compute_hash(from_dict)

    def test_a_workflow_hashes_like_the_equivalent_dict(self) -> None:
        registry = ToolRegistry()
        from_builder = (
            WorkflowBuilder("onboard", registry=registry)
            .tool(issue_refund)
            .tool_step("refund", "issue_refund", {"order_id": "A1", "cents": 500})
            .agent_step("greet", AgentBuilder("greeter").model("gpt-4o-mini"))
            .build()
        )
        from_dict = WorkflowSpec.model_validate(
            {
                "kind": "workflow",
                "name": "onboard",
                "tools": [{"kind": "code", "name": "issue_refund"}],
                "steps": [
                    {
                        "kind": "tool",
                        "name": "refund",
                        "tool": "issue_refund",
                        "arguments": {"order_id": "A1", "cents": 500},
                    },
                    {
                        "kind": "agent",
                        "name": "greet",
                        "spec": {
                            "kind": "agent",
                            "name": "greeter",
                            "model": {"model": "gpt-4o-mini"},
                        },
                    },
                ],
            }
        )
        assert compute_hash(from_builder) == compute_hash(from_dict)

    def test_top_level_factory_functions_produce_the_same_spec_as_the_class(self) -> None:
        via_class = AgentBuilder("support").model("gpt-4o").build()
        via_factory = agent("support").model("gpt-4o").build()
        assert compute_hash(via_class) == compute_hash(via_factory)

        via_class_workflow = WorkflowBuilder("w").tool_step("s", "t").build()
        via_factory_workflow = workflow("w").tool_step("s", "t").build()
        assert compute_hash(via_class_workflow) == compute_hash(via_factory_workflow)


# ---------------------------------------------------------------------------
# A callable never reaches the Spec.
# ---------------------------------------------------------------------------


class TestACallableNeverReachesTheSpec:
    def test_tool_puts_only_the_registered_name_in_the_spec(self) -> None:
        spec = AgentBuilder("support").model("gpt-4o").tool(lookup_order).build()
        assert spec.tools == (CodeTool(name="lookup_order"),)
        # The tool is a plain, frozen, extra-forbid Pydantic model holding a
        # str name: there is no field a callable could have landed in.
        assert isinstance(spec.tools[0].name, str)

    def test_the_function_is_registered_and_callable_from_the_registry(self) -> None:
        builder = AgentBuilder("support").model("gpt-4o").tool(lookup_order)
        registered = builder.registry.get("lookup_order")
        assert registered is not None
        assert registered.fn is lookup_order

    def test_a_spec_serialises_to_json_with_no_trace_of_the_callable(self) -> None:
        import json

        spec = AgentBuilder("support").model("gpt-4o").tool(lookup_order).build()
        payload = spec.model_dump(mode="json")
        serialised = json.dumps(payload)
        assert "function" not in serialised.lower()
        assert "lookup_order" in serialised  # the name, which is the point

    def test_a_custom_name_and_description_are_honoured(self) -> None:
        builder = (
            AgentBuilder("support")
            .model("gpt-4o")
            .tool(lookup_order, name="find_order", description="Find an order by id.")
        )
        spec = builder.build()
        assert spec.tools == (CodeTool(name="find_order"),)
        registered = builder.registry.get("find_order")
        assert registered is not None
        assert registered.description == "Find an order by id."

    def test_tool_by_name_grants_without_registering(self) -> None:
        builder = AgentBuilder("support").model("gpt-4o").tool_by_name("remote_tool")
        spec = builder.build()
        assert spec.tools == (CodeTool(name="remote_tool"),)
        assert builder.registry.get("remote_tool") is None


# ---------------------------------------------------------------------------
# Sorted collections normalise, workflow order does not.
# ---------------------------------------------------------------------------


class TestNormalisation:
    def test_tools_added_in_a_different_order_hash_the_same(self) -> None:
        one = (
            AgentBuilder("a").model("gpt-4o").tool_by_name("b_tool").tool_by_name("a_tool").build()
        )
        two = (
            AgentBuilder("a").model("gpt-4o").tool_by_name("a_tool").tool_by_name("b_tool").build()
        )
        assert compute_hash(one) == compute_hash(two)
        assert [t.name for t in one.tools] == ["a_tool", "b_tool"]

    def test_mcp_servers_added_in_a_different_order_hash_the_same(self) -> None:
        one = (
            AgentBuilder("a")
            .model("gpt-4o")
            .mcp_server("z", "https://z.example.com")
            .mcp_server("a", "https://a.example.com")
            .build()
        )
        two = (
            AgentBuilder("a")
            .model("gpt-4o")
            .mcp_server("a", "https://a.example.com")
            .mcp_server("z", "https://z.example.com")
            .build()
        )
        assert compute_hash(one) == compute_hash(two)

    def test_skills_added_in_a_different_order_hash_the_same(self) -> None:
        one = (
            AgentBuilder("a")
            .model("gpt-4o")
            .skill("z", "z desc", "z body")
            .skill("a", "a desc", "a body")
            .build()
        )
        two = (
            AgentBuilder("a")
            .model("gpt-4o")
            .skill("a", "a desc", "a body")
            .skill("z", "z desc", "z body")
            .build()
        )
        assert compute_hash(one) == compute_hash(two)

    def test_workflow_step_order_is_preserved_and_changes_the_hash(self) -> None:
        one = WorkflowBuilder("w").tool_step("first", "t").tool_step("second", "t").build()
        two = WorkflowBuilder("w").tool_step("second", "t").tool_step("first", "t").build()
        assert [step.name for step in one.steps] == ["first", "second"]
        assert [step.name for step in two.steps] == ["second", "first"]
        assert compute_hash(one) != compute_hash(two)

    def test_model_fallbacks_are_order_preserving(self) -> None:
        spec = AgentBuilder("a").model("gpt-4o", fallbacks=("second", "first")).build()
        assert spec.model.fallbacks == ("second", "first")


# ---------------------------------------------------------------------------
# The API surface, end to end.
# ---------------------------------------------------------------------------


class TestAgentBuilderSurface:
    def test_a_minimal_agent_builds(self) -> None:
        spec = AgentBuilder("support").model("gpt-4o").build()
        assert spec.name == "support"
        assert spec.model.model == "gpt-4o"
        assert spec.limits == Limits()

    def test_build_without_a_model_raises_builder_error(self) -> None:
        with pytest.raises(BuilderError, match="no model"):
            AgentBuilder("support").build()

    def test_a_built_spec_passes_structural_validation(self) -> None:
        spec = (
            AgentBuilder("support")
            .instructions("See [[skill:policy]].")
            .model("gpt-4o")
            .skill("policy", "desc", "body")
            .build()
        )
        validate_spec(spec)  # raises on failure

    def test_a_dangling_skill_link_fails_validation(self) -> None:
        spec = (
            AgentBuilder("support").instructions("See [[skill:missing]].").model("gpt-4o").build()
        )
        with pytest.raises(SpecValidationError):
            validate_spec(spec)

    def test_http_tool_defaults_match_the_spec_model(self) -> None:
        spec = (
            AgentBuilder("support")
            .model("gpt-4o")
            .http_tool("lookup", description="d", url="https://x.example.com")
            .build()
        )
        tool = spec.tools[0]
        assert isinstance(tool, HttpTool)
        assert tool.method == "POST"
        assert tool.timeout_seconds == 30.0
        assert tool.input_schema == {}
        assert tool.headers == {}

    def test_a_literal_authorization_header_is_rejected_by_the_spec_model(self) -> None:
        with pytest.raises(ValidationError):
            (
                AgentBuilder("support")
                .model("gpt-4o")
                .http_tool(
                    "lookup",
                    description="d",
                    url="https://x.example.com",
                    headers={"Authorization": "Bearer secret"},
                )
                .build()
            )

    def test_limits_layer_onto_previous_calls_rather_than_replacing_them(self) -> None:
        spec = (
            AgentBuilder("support")
            .model("gpt-4o")
            .limits(max_steps=10)
            .limits(deadline_seconds=42.0)
            .build()
        )
        assert spec.limits.max_steps == 10
        assert spec.limits.deadline_seconds == 42.0

    def test_subagent_accepts_a_prebuilt_agentspec(self) -> None:
        child_spec = AgentSpec(name="billing", model=ModelRef(model="gpt-4o-mini"))
        spec = (
            AgentBuilder("router")
            .model("gpt-4o")
            .subagent("billing", "Handles billing questions specifically.", child_spec)
            .build()
        )
        assert spec.subagents == (
            SubagentRef(
                name="billing",
                description="Handles billing questions specifically.",
                spec=child_spec,
            ),
        )

    def test_registry_is_shared_across_builders_when_passed_explicitly(self) -> None:
        registry = ToolRegistry()
        AgentBuilder("a", registry=registry).model("gpt-4o").tool(lookup_order)
        assert "lookup_order" in registry

    def test_agent_builder_extends_shared_builder(self) -> None:
        assert isinstance(AgentBuilder("a"), SharedBuilder)


class TestWorkflowBuilderSurface:
    def test_a_minimal_workflow_builds(self) -> None:
        spec = WorkflowBuilder("w").tool_step("only", "some_tool").build()
        assert spec.name == "w"
        assert len(spec.steps) == 1
        assert isinstance(spec.steps[0], ToolStep)

    def test_build_with_no_steps_raises_builder_error(self) -> None:
        with pytest.raises(BuilderError, match="no steps"):
            WorkflowBuilder("w").build()

    def test_agent_step_accepts_a_builder_and_calls_build_on_it(self) -> None:
        spec = (
            WorkflowBuilder("w")
            .agent_step("greet", AgentBuilder("greeter").model("gpt-4o-mini"))
            .build()
        )
        step = spec.steps[0]
        assert isinstance(step, AgentStep)
        assert step.spec.name == "greeter"

    def test_workflow_step_nests_a_workflow_and_recurses(self) -> None:
        inner = WorkflowBuilder("inner").tool_step("only", "some_tool")
        spec = WorkflowBuilder("outer").workflow_step("nested", inner).build()
        step = spec.steps[0]
        assert isinstance(step, WorkflowStepRef)
        assert step.spec.name == "inner"

    def test_workflow_builder_extends_shared_builder(self) -> None:
        assert isinstance(WorkflowBuilder("w"), SharedBuilder)

    def test_mcp_server_is_granted_on_a_workflow(self) -> None:
        spec = (
            WorkflowBuilder("w")
            .tool_step("only", "some_tool")
            .mcp_server("crm", "https://mcp.example.com", allow=["read_*"])
            .build()
        )
        assert spec.mcp_servers == (
            McpServer(name="crm", url="https://mcp.example.com", allow=("read_*",)),
        )
