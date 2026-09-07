"""The Spec model, its normalisation, and publish-time validation.

DESIGN.md §4 and §16. The invariant under test everywhere here is that a Spec is
data: serialisable, hashable, and holding no live objects.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    CodeTool,
    CompactionPolicy,
    HttpTool,
    Limits,
    McpServer,
    ModelRef,
    Skill,
    Spec,
    SubagentRef,
    ToolStep,
    WorkflowSpec,
)
from psych_runtime.core.validation import ValidationContext, collect_issues, validate_spec
from psych_runtime.core.version import compute_hash

pytestmark = pytest.mark.unit

SPEC_ADAPTER: TypeAdapter[Spec] = TypeAdapter(Spec)


def agent(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {"name": "support", "model": ModelRef(model="gpt-4o")}
    base.update(kwargs)
    return AgentSpec(**base)


class TestNoCallables:
    """The invariant that forks the runtime into two execution models if broken."""

    def test_a_function_where_a_tool_name_belongs_is_refused(self) -> None:
        def refund(amount: int) -> str:
            return "done"

        with pytest.raises(ValidationError):
            CodeTool(name=refund)  # type: ignore[arg-type]

    def test_a_callable_hidden_in_a_schema_dict_is_refused(self) -> None:
        """input_schema is typed dict[str, Any], which is exactly the hole the
        explicit walk exists to close."""
        with pytest.raises(ValidationError, match="holds a callable"):
            HttpTool(
                name="lookup",
                description="look something up",
                url="https://example.invalid/x",
                input_schema={"type": "object", "properties": {"a": {"default": len}}},
            )

    def test_a_callable_nested_in_step_arguments_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="holds a callable"):
            ToolStep(name="s", tool="t", arguments={"nested": {"fn": print}})

    def test_a_plain_spec_survives_the_walk(self) -> None:
        assert agent(tools=(CodeTool(name="refund"),)).tools[0].name == "refund"


class TestRoundTrip:
    def test_an_agent_spec_round_trips_through_json_losslessly(self) -> None:
        original = agent(
            instructions="Be helpful.",
            tools=(
                CodeTool(name="refund"),
                HttpTool(
                    name="lookup",
                    description="Look up an order.",
                    url="https://example.invalid/orders",
                    input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
                ),
            ),
            skills=(Skill(name="tone", description="How to write.", body="Be brief."),),
            mcp_servers=(McpServer(name="crm", url="https://mcp.invalid"),),
        )
        restored = SPEC_ADAPTER.validate_python(json.loads(SPEC_ADAPTER.dump_json(original)))
        assert restored == original

    def test_a_workflow_spec_round_trips_through_json_losslessly(self) -> None:
        original = WorkflowSpec(
            name="nightly",
            steps=(
                ToolStep(name="fetch", tool="pull", arguments={"since": "yesterday"}),
                AgentStep(name="summarise", spec=agent(name="summariser")),
            ),
        )
        restored = SPEC_ADAPTER.validate_python(json.loads(SPEC_ADAPTER.dump_json(original)))
        assert restored == original

    def test_the_discriminator_picks_the_right_shape(self) -> None:
        parsed = SPEC_ADAPTER.validate_python(
            {"kind": "workflow", "name": "w", "steps": [{"kind": "tool", "name": "s", "tool": "t"}]}
        )
        assert isinstance(parsed, WorkflowSpec)

    def test_an_unknown_field_fails_loudly_rather_than_being_dropped(self) -> None:
        """A typo in a YAML file that silently does nothing produces an agent
        that quietly ignores a setting its author believed was applied."""
        with pytest.raises(ValidationError, match="Extra inputs"):
            AgentSpec(name="a", model=ModelRef(model="m"), instrucions="typo")  # type: ignore[call-arg]


class TestNormalisation:
    def test_tool_order_does_not_survive_validation(self) -> None:
        """Tool order carries no meaning, so it must not reach the hasher."""
        one = agent(tools=(CodeTool(name="b"), CodeTool(name="a")))
        two = agent(tools=(CodeTool(name="a"), CodeTool(name="b")))
        assert one == two

    def test_workflow_step_order_does_survive(self) -> None:
        """'Sequences deterministically' is the whole definition of a workflow."""
        one = WorkflowSpec(
            name="w",
            steps=(ToolStep(name="a", tool="t"), ToolStep(name="b", tool="t")),
        )
        two = WorkflowSpec(
            name="w",
            steps=(ToolStep(name="b", tool="t"), ToolStep(name="a", tool="t")),
        )
        assert one != two

    def test_model_fallback_order_survives_because_it_is_tried_in_order(self) -> None:
        one = ModelRef(model="m", fallbacks=("a", "b"))
        two = ModelRef(model="m", fallbacks=("b", "a"))
        assert one != two

    def test_mcp_allow_patterns_are_a_set(self) -> None:
        server = McpServer(name="s", url="https://x.invalid", allow=("b", "a", "b"))
        assert server.allow == ("a", "b")

    def test_windows_line_endings_normalise(self) -> None:
        """The same agent authored in a YAML file on Windows and through chat."""
        assert agent(instructions="a\r\nb").instructions == "a\nb"

    def test_trailing_whitespace_is_content_and_does_not_normalise(self) -> None:
        """A model reading an instruction block can be affected by it, so two
        Specs differing here really are two different agents."""
        assert agent(instructions="a  \nb").instructions == "a  \nb"

    def test_duplicate_tool_names_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="duplicate name"):
            agent(tools=(CodeTool(name="x"), CodeTool(name="x")))

    def test_a_tool_may_not_claim_a_builtin_name(self) -> None:
        with pytest.raises(ValidationError, match="reserved"):
            agent(tools=(CodeTool(name="load_skill"),))


class TestFieldRules:
    def test_a_literal_authorization_header_is_refused(self) -> None:
        """Specs get exported, reviewed in git and stored unencrypted."""
        with pytest.raises(ValidationError, match="SecretResolver"):
            HttpTool(
                name="x",
                description="d",
                url="https://x.invalid",
                headers={"Authorization": "Bearer sk-live-1234"},
            )

    def test_a_credential_name_is_fine_because_it_is_not_a_secret(self) -> None:
        tool = HttpTool(name="x", description="d", url="https://x.invalid", credential="stripe_key")
        assert tool.credential == "stripe_key"

    def test_a_short_subagent_description_is_refused(self) -> None:
        """DESIGN.md §17: vague descriptions are the root cause of bad routing."""
        with pytest.raises(ValidationError, match="at least 20 characters"):
            SubagentRef(name="billing", description="billing", spec=agent(name="b"))

    def test_a_hard_stop_below_the_advisory_threshold_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="told to change approach"):
            Limits(failure_streak_threshold=5, failure_streak_hard_stop=3)

    def test_a_spec_is_frozen(self) -> None:
        spec = agent()
        with pytest.raises(ValidationError, match="frozen"):
            spec.name = "other"  # type: ignore[misc]

    def test_a_workflow_needs_at_least_one_step(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 item"):
            WorkflowSpec(name="w", steps=())


class TestValidation:
    def test_an_unregistered_code_tool_names_itself(self) -> None:
        spec = agent(tools=(CodeTool(name="refund"),))
        with pytest.raises(SpecValidationError, match="'refund' is not registered"):
            validate_spec(spec, ValidationContext(registered_tools={"lookup"}))

    def test_a_registered_code_tool_passes(self) -> None:
        spec = agent(tools=(CodeTool(name="refund"),))
        validate_spec(spec, ValidationContext(registered_tools={"refund"}))

    def test_an_unknown_model_names_itself(self) -> None:
        with pytest.raises(SpecValidationError, match="'gpt-4o' is not one"):
            validate_spec(agent(), ValidationContext(known_models={"known-model"}))

    def test_an_empty_known_models_set_means_do_not_check(self) -> None:
        """A consumer pointing at a proxy can reach models Psych never heard of."""
        validate_spec(agent(), ValidationContext())

    def test_a_dangling_skill_link_fails_publication(self) -> None:
        """DESIGN.md §16."""
        spec = agent(
            instructions="Read [[skill:missing]] first.",
            skills=(Skill(name="present", description="d", body="b"),),
        )
        with pytest.raises(SpecValidationError, match=r"\[\[skill:missing\]\]"):
            validate_spec(spec)

    def test_a_skill_may_link_to_another_skill(self) -> None:
        spec = agent(
            skills=(
                Skill(name="a", description="d", body="see [[skill:b]]"),
                Skill(name="b", description="d", body="body"),
            ),
        )
        validate_spec(spec)

    def test_a_dangling_link_inside_a_skill_body_is_found(self) -> None:
        spec = agent(skills=(Skill(name="a", description="d", body="see [[skill:nope]]"),))
        issues = collect_issues(spec)
        assert len(issues) == 1
        assert issues[0].path == "support.skills.a.body"

    def test_an_unreachable_required_mcp_server_fails(self) -> None:
        """§10.7: silent tool disappearance produces an agent that confidently
        tells a customer it cannot issue refunds today."""
        spec = agent(mcp_servers=(McpServer(name="crm", url="https://mcp.invalid"),))
        with pytest.raises(SpecValidationError, match="did not answer at publish"):
            validate_spec(spec, ValidationContext(reachable_mcp_servers={"other"}))

    def test_an_unreachable_optional_mcp_server_passes(self) -> None:
        spec = agent(mcp_servers=(McpServer(name="crm", url="https://mcp.invalid", optional=True),))
        validate_spec(spec, ValidationContext(reachable_mcp_servers={"other"}))

    def test_a_malformed_schema_names_the_field(self) -> None:
        spec = agent(
            tools=(
                HttpTool(
                    name="x",
                    description="d",
                    url="https://x.invalid",
                    input_schema={"type": "object", "properties": {}, "required": ["missing"]},
                ),
            )
        )
        issues = collect_issues(spec)
        assert any("requires ['missing']" in issue.message for issue in issues)

    def test_a_schema_with_no_type_is_reported(self) -> None:
        spec = agent(
            tools=(
                HttpTool(
                    name="x",
                    description="d",
                    url="https://x.invalid",
                    input_schema={"properties": {"a": {"type": "string"}}},
                ),
            )
        )
        assert any("has no 'type'" in issue.message for issue in collect_issues(spec))

    def test_every_problem_is_reported_at_once(self) -> None:
        """Fixing one typo per publish attempt is a bad way to spend an evening."""
        spec = agent(
            instructions="[[skill:one]] and [[skill:two]]",
            tools=(CodeTool(name="unregistered"),),
        )
        issues = collect_issues(spec, ValidationContext(registered_tools={"other"}))
        assert len(issues) == 3

    def test_a_workflow_step_calling_an_ungranted_tool_is_reported(self) -> None:
        spec = WorkflowSpec(name="w", steps=(ToolStep(name="s", tool="nowhere"),))
        issues = collect_issues(spec, ValidationContext(registered_tools={"elsewhere"}))
        assert issues[0].path == "w.steps.s"

    def test_a_subagent_tree_deeper_than_its_cap_is_reported(self) -> None:
        leaf = agent(name="leaf")
        middle = agent(
            name="middle",
            subagents=(
                SubagentRef(name="leaf", description="Handles leaf work end to end.", spec=leaf),
            ),
        )
        root = agent(
            name="root",
            limits=Limits(max_delegation_depth=1),
            subagents=(
                SubagentRef(name="middle", description="Handles middle work overall.", spec=middle),
            ),
        )
        issues = collect_issues(root)
        assert any("can never run" in issue.message for issue in issues)

    def test_validation_descends_into_subagents(self) -> None:
        child = agent(name="child", tools=(CodeTool(name="ghost"),))
        root = agent(
            subagents=(
                SubagentRef(
                    name="child", description="A child that does child things.", spec=child
                ),
            ),
        )
        issues = collect_issues(root, ValidationContext(registered_tools={"real"}))
        assert issues[0].path == "support.subagents.child.tools.ghost"


class TestCompactionIsPartOfWhatAnAgentIs:
    """An agent that summarises its own history shows the model a
    different conversation on a long Run, so the switch belongs in the Version
    hash rather than in Runtime wiring."""

    def test_it_is_off_by_default(self) -> None:
        assert agent().compaction is None

    def test_turning_it_on_makes_a_different_version(self) -> None:
        plain = agent()
        compacting = agent(compaction=CompactionPolicy(trigger_tokens=100_000))
        assert compute_hash(plain) != compute_hash(compacting)

    def test_two_agents_asking_for_the_same_compaction_are_one_version(self) -> None:
        first = agent(compaction=CompactionPolicy(trigger_tokens=100_000, keep_recent_turns=2))
        second = agent(compaction=CompactionPolicy(trigger_tokens=100_000, keep_recent_turns=2))
        assert compute_hash(first) == compute_hash(second)

    def test_changing_only_the_summariser_model_makes_a_different_version(self) -> None:
        cheap = agent(compaction=CompactionPolicy(trigger_tokens=100_000, model="gpt-4o-mini"))
        same = agent(compaction=CompactionPolicy(trigger_tokens=100_000))
        assert compute_hash(cheap) != compute_hash(same)

    def test_a_trigger_has_to_be_chosen(self) -> None:
        """No default, because Psych has no context window to take a fraction
        of: the port speaks to a proxy that reaches models it was never told
        about."""
        with pytest.raises(ValidationError):
            CompactionPolicy.model_validate({})

    def test_an_unknown_summariser_model_is_caught_at_publish(self) -> None:
        spec = agent(compaction=CompactionPolicy(trigger_tokens=100_000, model="gpt-9o"))
        issues = collect_issues(spec, ValidationContext(known_models={"gpt-4o"}))
        assert any(issue.path.endswith("compaction.model") for issue in issues)
