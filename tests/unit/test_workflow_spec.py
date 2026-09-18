"""The composite step kinds: what the Spec accepts, refuses and hashes.

A workflow's shape is data, so every rule about it is a validation rule on the
Spec rather than a runtime surprise, and the Version hash has to be stable
across the JSON round trip that a stored Spec makes.
"""

from __future__ import annotations

import pytest

import psych_runtime
from psych_runtime.core.spec import (
    BranchCase,
    BranchStep,
    Condition,
    ForEachStep,
    HumanStep,
    LiteralValue,
    LoopStep,
    MapStep,
    ParallelStep,
    RetryPolicy,
    SetStateStep,
    SleepStep,
    ToolStep,
    ValuePath,
    WaitStep,
    WorkflowSpec,
    WorkflowStepRef,
    iter_steps,
)
from psych_runtime.core.validation import ValidationContext, collect_issues
from psych_runtime.core.version import compute_hash

pytestmark = pytest.mark.unit


def _tool(name: str) -> ToolStep:
    return ToolStep(name=name, tool="t")


def _everything() -> WorkflowSpec:
    inner = WorkflowSpec(
        name="inner", steps=(_tool("i1"), MapStep(name="i2", output={"x": lit(1)}))
    )
    return WorkflowSpec(
        name="all",
        tools=(psych_runtime.CodeTool(name="t"),),
        initial_state={"count": 0},
        input_schema={"type": "object"},
        output={"done": lit(True), "last": ref("steps.h.output.answer")},
        retry=RetryPolicy(max_attempts=2, backoff_seconds=1.5),
        steps=(
            ToolStep(name="a", tool="t", arguments_from={"n": ref("input.n")}, timeout_seconds=5),
            ParallelStep(
                name="p", branches=(_tool("b1"), _tool("b2")), on_branch_failure="wait_all"
            ),
            BranchStep(
                name="br",
                cases=(BranchCase(name="c1", when=Condition(path="input.n"), step=_tool("x1")),),
                otherwise=_tool("x2"),
                mode="all",
            ),
            ForEachStep(name="fe", items=ref("input.items"), body=_tool("body"), concurrency=3),
            LoopStep(
                name="lp",
                body=SetStateStep(name="s", values={"count": ref("iteration")}),
                until=Condition(path="state.count", op="gte", value=2),
            ),
            MapStep(name="m", output={"a": ref("steps.a.output.result")}),
            SleepStep(name="z", seconds=1),
            WaitStep(name="w", event="paid", payload_schema={"type": "object"}, timeout_seconds=60),
            HumanStep(name="h", prompt="Approve?", when=Condition(path="state.count", op="exists")),
            WorkflowStepRef(
                name="nested", spec=inner, input={"n": ref("input.n")}, on_failure="continue"
            ),
        ),
    )


def ref(path: str) -> ValuePath:
    return ValuePath(path=path)


def lit(value: object) -> LiteralValue:
    return LiteralValue(value=value)


class TestShape:
    def test_every_kind_round_trips_through_json_with_the_same_hash(self) -> None:
        spec = _everything()
        again = WorkflowSpec.model_validate_json(spec.model_dump_json())
        assert again == spec
        assert compute_hash(again) == compute_hash(spec)

    def test_iter_steps_walks_the_whole_tree_but_not_a_nested_workflow(self) -> None:
        names = [step.name for step in iter_steps(_everything().steps)]
        assert "b1" in names
        assert "x2" in names
        assert "body" in names
        assert "s" in names
        assert "i1" not in names

    def test_names_must_be_unique_across_branches_and_bodies(self) -> None:
        with pytest.raises(ValueError, match="duplicate name"):
            WorkflowSpec(
                name="w",
                steps=(_tool("a"), ParallelStep(name="p", branches=(_tool("a"),))),
            )

    def test_a_nested_workflow_is_its_own_namespace(self) -> None:
        inner = WorkflowSpec(name="inner", steps=(_tool("a"),))
        WorkflowSpec(name="w", steps=(_tool("a"), WorkflowStepRef(name="n", spec=inner)))

    def test_a_loop_has_exactly_one_exit(self) -> None:
        with pytest.raises(ValueError, match="exactly one of `until` or `while`"):
            LoopStep(name="l", body=_tool("b"))
        with pytest.raises(ValueError, match="exactly one of `until` or `while`"):
            LoopStep(
                name="l",
                body=_tool("b"),
                until=Condition(path="input.x"),
                while_=Condition(path="input.y"),
            )

    def test_while_is_spelled_while_in_data(self) -> None:
        loop = LoopStep.model_validate(
            {
                "name": "l",
                "body": {"kind": "tool", "name": "b", "tool": "t"},
                "while": {"path": "input.go"},
            }
        )
        assert loop.while_ is not None
        assert LoopStep.model_validate_json(loop.model_dump_json()) == loop

    def test_a_sleep_has_exactly_one_wake(self) -> None:
        with pytest.raises(ValueError, match="exactly one of `seconds` or `until`"):
            SleepStep(name="z")
        with pytest.raises(ValueError, match="exactly one of `seconds` or `until`"):
            SleepStep(name="z", seconds=1, until=ref("input.at"))

    def test_branch_case_names_are_unique(self) -> None:
        with pytest.raises(ValueError, match="duplicate name"):
            BranchStep(
                name="b",
                cases=(
                    BranchCase(name="c", when=Condition(path="input.x"), step=_tool("s1")),
                    BranchCase(name="c", when=Condition(path="input.y"), step=_tool("s2")),
                ),
            )

    def test_a_value_path_is_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="pattern"):
            ValuePath(path="input.x y")

    def test_retry_delay_grows_and_caps(self) -> None:
        policy = RetryPolicy(
            max_attempts=5, backoff_seconds=2, multiplier=3, max_backoff_seconds=10
        )
        assert policy.delay_before(1) == 0
        assert policy.delay_before(2) == 2
        assert policy.delay_before(3) == 6
        assert policy.delay_before(4) == 10
        assert RetryPolicy().delay_before(2) == 0


class TestPublishValidation:
    def test_a_tool_inside_a_branch_arm_must_be_granted(self) -> None:
        spec = WorkflowSpec(
            name="w",
            steps=(
                BranchStep(
                    name="b",
                    cases=(
                        BranchCase(
                            name="c",
                            when=Condition(path="input.x"),
                            step=ToolStep(name="deep", tool="never_registered"),
                        ),
                    ),
                ),
            ),
        )
        issues = collect_issues(spec, ValidationContext(registered_tools={"other"}))
        assert any("deep" in issue.path and "never_registered" in issue.message for issue in issues)

    def test_a_granted_tool_deep_in_the_tree_passes(self) -> None:
        spec = WorkflowSpec(
            name="w",
            tools=(psych_runtime.CodeTool(name="t"),),
            steps=(ForEachStep(name="f", items=ref("input.items"), body=_tool("body")),),
        )
        assert collect_issues(spec, ValidationContext(registered_tools={"t"})) == []
