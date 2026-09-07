"""Delegation: a parent Run delegates to a subagent and the child shows up
in the report as its own Run, at its own depth, scoped to its own parent.

DESIGN.md §17. A subagent is not a function call inside the parent's own
loop -- it is a wholly separate Run, with its own log, that the parent
happens to have started and waits on. This scenario checks the properties
that only matter once delegation is real rather than simulated: the child
is reachable as its own Run, its depth is one more than its parent's, it
inherits the parent's Scope exactly (a delegation cannot widen tenancy), and
the parent's own turn budget caps how many children one turn may spawn.
"""

from __future__ import annotations

from typing import Any

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, drive, records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef, SubagentRef
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="delegation",
    title="Delegation",
    proves=(
        "A parent Run delegates to a subagent and the child runs as its own "
        "Run, one delegation depth deeper, inheriting the parent's Scope "
        "exactly; a turn that tries to spawn more children than the "
        "fanout cap allows is refused rather than silently allowed."
    ),
    design_ref="§17",
    requires=(),
)


def _registry(effects: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def lookup(order_id: str) -> str:
        """Look up an order."""
        effects.append(f"looked up {order_id}")
        return f"{order_id} is shipped"

    return registry


def _child_run_id(parent_state: Any) -> RunId:
    for result in parent_state.tool_results:
        if result.tool == "delegate" and isinstance(result.result, dict):
            run_id = result.result.get("run_id")
            if run_id is not None:
                return RunId(run_id)
    raise AssertionError("the parent's log recorded no successful delegation")


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    effects: list[str] = []
    registry = _registry(effects)

    child = AgentSpec(
        name="billing",
        instructions="Handle billing questions.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="lookup"),),
    )
    parent = AgentSpec(
        name="support",
        instructions="Help the customer; delegate billing questions.",
        model=ModelRef(model="fake-standard"),
        tools=(),
        subagents=(
            SubagentRef(
                name="billing",
                description="Handles billing questions and refunds end to end.",
                spec=child,
            ),
        ),
    )

    await emit("delegate", "a parent Run that delegates a billing question to a subagent")
    model = (
        FakeModel()
        .turn(tool_calls=[("delegate", {"subagent": "billing", "task": "check A1"})])
        .turn(tool_calls=[("lookup", {"order_id": "A1"})])
        .turn(text="A1 is shipped.")
        .turn(text="Billing says A1 is shipped.")
    )
    parent_run_id = await drive(store, parent, model, registry, message="how is order A1?")

    parent_report = await psych_runtime.report(store, parent_run_id)
    parent_terminal = parent_report.terminal_state.value if parent_report.terminal_state else None
    checks.require(
        "the parent Run completed, having delegated rather than answered directly",
        parent_report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={parent_terminal!r}",
    )
    checks.require(
        "the child's own tool actually ran -- delegation reaches real execution",
        effects == ["looked up A1"],
        f"effects={effects!r}",
    )

    parent_state = await psych_runtime.state(store, parent_run_id)
    child_run_id = _child_run_id(parent_state)
    child_state = await psych_runtime.state(store, child_run_id)
    checks.require(
        "the child is a wholly separate Run, one delegation depth deeper than the parent",
        child_state.delegation_depth == 1 and child_run_id != parent_run_id,
        f"child_run_id={child_run_id}, delegation_depth={child_state.delegation_depth}",
    )
    checks.require(
        "the child inherited the parent's Scope exactly -- delegation cannot widen tenancy",
        child_state.scope == SCOPE_A and child_state.parent_run_id == parent_run_id,
        f"child_scope={child_state.scope}, parent_run_id={child_state.parent_run_id}",
    )
    checks.require(
        "the child Run itself completed",
        child_state.terminal_state is TerminalState.COMPLETED,
        f"child terminal_state={child_state.terminal_state}",
    )

    await emit("fanout", "a second parent Run whose one turn tries to spawn two children")
    capped_parent = AgentSpec(
        name="support-capped",
        instructions="Help the customer; delegate billing questions.",
        model=ModelRef(model="fake-standard"),
        tools=(),
        limits=Limits(max_fanout_per_turn=1),
        subagents=(
            SubagentRef(
                name="billing",
                description="Handles billing questions and refunds end to end.",
                spec=child,
            ),
        ),
    )
    fanout_model = (
        FakeModel()
        .turn(
            tool_calls=[
                ("delegate", {"subagent": "billing", "task": "one"}),
                ("delegate", {"subagent": "billing", "task": "two"}),
            ]
        )
        .turn(text="child done")
        .turn(text="parent done")
    )
    capped_run_id = await drive(
        store, capped_parent, fanout_model, _registry([]), message="two things at once"
    )
    capped_state = await psych_runtime.state(store, capped_run_id)
    second_delegation = capped_state.tool_results[1]
    checks.require(
        "the second delegation in one turn was refused once the fanout cap was reached",
        second_delegation.outcome is ToolOutcome.ERROR
        and second_delegation.failure is not None
        and "already delegated" in second_delegation.failure.message,
        f"outcome={second_delegation.outcome}, "
        f"failure={second_delegation.failure.message if second_delegation.failure else None!r}",
    )

    log = await psych_runtime.records(store, parent_run_id)
    child_log = await psych_runtime.records(store, child_run_id)
    return checks.result(
        "a parent delegated to a real, separately-scoped child Run, and a turn that "
        "tried to spawn a second child was capped rather than allowed",
        run_ids=[str(parent_run_id), str(child_run_id), str(capped_run_id)],
        report={
            "parent_run": {"report": report_dict(parent_report), "records": records_dict(log)},
            "child_run": {"records": records_dict(child_log)},
        },
    )
