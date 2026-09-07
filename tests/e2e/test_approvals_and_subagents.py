"""End to end: suspension, approvals and subagent delegation.

DESIGN.md §11 (suspend and resume), §10.9 (approvals) and §17 (subagents).

The property common to all three: a Run that stops to wait does not hold anything
in memory. The decision, the depth and the child's whole log are in the store, so
the Worker that resumes need not be the Worker that asked.
"""

from __future__ import annotations

from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.records import (
    Suspended,
    SuspendReason,
    TerminalState,
    ToolOutcome,
)
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef, SubagentRef
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.subagent import (
    check_depth,
    check_fanout,
    child_tool_names,
    delegation_definition,
    resolve_child_depth,
)
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.policy import Decision
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


def build_registry(log: list[str] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    sink = log if log is not None else []

    @registry.register(annotations={"destructive"})
    def refund(order_id: str) -> str:
        """Refund an order."""
        sink.append(f"refunded {order_id}")
        return f"refunded {order_id}"

    @registry.register(annotations={"read-only"})
    def lookup(order_id: str) -> str:
        """Look up an order."""
        sink.append(f"looked up {order_id}")
        return f"{order_id} is shipped"

    return registry


def agent(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"), CodeTool(name="refund", interruptible=False)),
    }
    base.update(kwargs)
    return AgentSpec(**base)


class DenyRefunds:
    """A Policy that refuses one tool, to prove Psych asks rather than decides."""

    async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
        _ = scope, args
        if tool == "refund":
            return Decision.deny("this principal cannot issue refunds")
        return Decision.allow()

    async def allow_run(self, scope: Scope, version: Any) -> Decision:
        _ = scope, version
        return Decision.allow()


async def run_agent(
    store: InMemoryStore,
    spec: AgentSpec,
    model: FakeModel,
    registry: ToolRegistry,
    **runtime_kwargs: Any,
) -> tuple[Any, Journal]:
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
    journal = await Journal.open(store, dispatched.run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=registry, **runtime_kwargs)
    header = await store.get_run(dispatched.run_id)
    assert header is not None

    await runtime(journal, header, AbortSignal())
    return dispatched, journal


class TestPolicyDenial:
    async def test_a_denied_tool_becomes_a_result_the_model_can_act_on(self) -> None:
        """Psych asks the consumer and reports the answer. It does not decide,
        and it does not turn a denial into a crash."""
        store = InMemoryStore()
        calls: list[str] = []
        model = (
            FakeModel()
            .turn(tool_calls=[("refund", {"order_id": "A1"})])
            .turn(text="I am not able to issue that refund.")
        )
        dispatched, _ = await run_agent(
            store, agent(), model, build_registry(calls), policy=DenyRefunds()
        )

        state = await psych_runtime.state(store, dispatched.run_id)
        assert state.tool_results[0].outcome is ToolOutcome.ERROR
        assert state.tool_results[0].failure is not None
        assert "cannot issue refunds" in state.tool_results[0].failure.message
        assert calls == []  # the tool never ran

    async def test_a_permitted_tool_still_runs(self) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        model = (
            FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="It shipped.")
        )
        await run_agent(store, agent(), model, build_registry(calls), policy=DenyRefunds())
        assert calls == ["looked up A1"]


class TestApprovals:
    async def test_a_destructive_tool_suspends_for_a_decision(self) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        model = FakeModel().turn(tool_calls=[("refund", {"order_id": "A1"})])
        dispatched, _ = await run_agent(
            store,
            agent(),
            model,
            build_registry(calls),
            approval_selectors=("@destructive",),
        )

        state = await psych_runtime.state(store, dispatched.run_id)
        assert state.suspended
        assert state.suspend_reason is SuspendReason.APPROVAL
        assert state.pending_approval_call_id is not None
        assert calls == []  # nothing happened while waiting
        assert not state.settled

    async def test_a_read_only_tool_does_not_suspend(self) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        model = (
            FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="It shipped.")
        )
        dispatched, _ = await run_agent(
            store,
            agent(),
            model,
            build_registry(calls),
            approval_selectors=("@destructive",),
        )
        state = await psych_runtime.state(store, dispatched.run_id)
        assert not state.suspended
        assert calls == ["looked up A1"]

    async def test_approving_runs_the_call_on_a_later_worker(self) -> None:
        """The decision is in the log, so the Worker that resumes need not be the
        one that asked."""
        store = InMemoryStore()
        calls: list[str] = []
        registry = build_registry(calls)
        spec = agent()

        first_model = FakeModel().turn(tool_calls=[("refund", {"order_id": "A1"})])
        dispatched, _ = await run_agent(
            store, spec, first_model, registry, approval_selectors=("@destructive",)
        )

        await psych_runtime.resume(store, dispatched.run_id, approved=True)

        # A completely fresh Worker, with its own model and its own journal.
        second_model = FakeModel().turn(text="Refund issued.")
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_2", attempt_number=2)
        runtime = Runtime(store=store, model=second_model, registry=registry)
        header = await store.get_run(dispatched.run_id)
        assert header is not None

        await runtime(journal, header, AbortSignal())

        assert calls == ["refunded A1"]
        state = await psych_runtime.state(store, dispatched.run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.tool_results[0].outcome is ToolOutcome.OK

    async def test_declining_records_a_denial_and_does_not_run_the_tool(self) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        registry = build_registry(calls)

        first_model = FakeModel().turn(tool_calls=[("refund", {"order_id": "A1"})])
        dispatched, _ = await run_agent(
            store, agent(), first_model, registry, approval_selectors=("@destructive",)
        )

        await psych_runtime.resume(store, dispatched.run_id, approved=False)

        second_model = FakeModel().turn(text="That was not approved.")
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_2", attempt_number=2)
        runtime = Runtime(store=store, model=second_model, registry=registry)
        header = await store.get_run(dispatched.run_id)
        assert header is not None

        await runtime(journal, header, AbortSignal())

        assert calls == []
        state = await psych_runtime.state(store, dispatched.run_id)
        result = state.tool_results[0]
        assert result.outcome is ToolOutcome.ERROR
        assert result.failure is not None
        assert result.failure.kind == "denied"

    async def test_the_suspension_carries_an_expiry(self) -> None:
        """DESIGN.md §11: suspensions expire rather than waiting forever on a
        user who left."""
        store = InMemoryStore()
        model = FakeModel().turn(tool_calls=[("refund", {"order_id": "A1"})])
        dispatched, _ = await run_agent(
            store, agent(), model, build_registry(), approval_selectors=("@destructive",)
        )
        records = await store.read(dispatched.run_id)
        suspension = next(r for r in records if isinstance(r, Suspended))
        assert suspension.expires_at > suspension.at

    async def test_resuming_a_run_that_is_not_suspended_is_refused(self) -> None:
        store = InMemoryStore()
        model = FakeModel().turn(text="done")
        dispatched, _ = await run_agent(store, agent(), model, build_registry())
        with pytest.raises(psych_runtime.RunNotSuspended, match="not suspended"):
            await psych_runtime.resume(store, dispatched.run_id, approved=True)


class TestSubagentDepth:
    """DESIGN.md §17. Depth is monotone: options may deepen it, never lower it."""

    def test_a_child_is_always_deeper_than_its_parent(self) -> None:
        assert resolve_child_depth(0) == 1
        assert resolve_child_depth(3) == 4

    def test_a_request_to_run_shallower_is_ignored(self) -> None:
        """The bug worth naming: a resumed child arriving with fresh options must
        not be counted from zero, or it delegates as though it were top-level and
        the recursion budget is defeated."""
        assert resolve_child_depth(5, requested=0) == 6
        assert resolve_child_depth(5, requested=3) == 6

    def test_a_request_to_run_deeper_is_honoured(self) -> None:
        assert resolve_child_depth(1, requested=9) == 9

    def test_the_depth_cap_is_enforced(self) -> None:
        check_depth(3, max_depth=3)
        with pytest.raises(Exception, match="limit is 3"):
            check_depth(4, max_depth=3)

    def test_the_fanout_cap_is_enforced(self) -> None:
        check_fanout(0, max_fanout=2)
        check_fanout(1, max_fanout=2)
        with pytest.raises(Exception, match="already delegated 2"):
            check_fanout(2, max_fanout=2)


class TestSubagentNarrowing:
    def test_a_child_cannot_reach_a_tool_its_parent_lacks(self) -> None:
        """A child cannot be used to launder access the parent did not have."""
        child = agent(name="child", tools=(CodeTool(name="refund"),))
        assert child_tool_names(["lookup"], child) == []

    def test_a_child_gets_the_intersection(self) -> None:
        child = agent(name="child", tools=(CodeTool(name="lookup"), CodeTool(name="refund")))
        assert child_tool_names(["lookup", "other"], child) == ["lookup"]

    def test_the_delegation_tool_advertises_what_the_child_will_actually_hold(
        self,
    ) -> None:
        """A model routing on a tool list the child will not get routes badly."""
        child = agent(name="billing", tools=(CodeTool(name="refund"),))
        ref = SubagentRef(
            name="billing",
            description="Handles billing questions and refunds end to end.",
            spec=child,
        )
        definition = delegation_definition([ref], parent_tools=["lookup"])
        assert definition is not None
        assert "no tools" in definition.description

    def test_no_subagents_means_no_delegation_tool(self) -> None:
        assert delegation_definition([], parent_tools=["lookup"]) is None


class TestDelegation:
    async def test_a_parent_delegates_and_the_child_gets_its_own_run(self) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        registry = build_registry(calls)

        child = agent(name="billing", tools=(CodeTool(name="lookup"),))
        parent = agent(
            subagents=(
                SubagentRef(
                    name="billing",
                    description="Handles billing questions and refunds end to end.",
                    spec=child,
                ),
            )
        )

        model = (
            FakeModel()
            # The parent delegates.
            .turn(tool_calls=[("delegate", {"subagent": "billing", "task": "check A1"})])
            # The child looks something up, then answers.
            .turn(tool_calls=[("lookup", {"order_id": "A1"})])
            .turn(text="A1 is shipped.")
            # The parent answers its user.
            .turn(text="Billing says A1 is shipped.")
        )
        dispatched, _ = await run_agent(store, parent, model, registry)

        parent_state = await psych_runtime.state(store, dispatched.run_id)
        assert parent_state.terminal_state is TerminalState.COMPLETED
        assert calls == ["looked up A1"]

        # The child is a separate Run, at depth 1, naming its parent.
        child_id = _child_run_id(parent_state)
        child_state = await psych_runtime.state(store, child_id)
        assert child_state.delegation_depth == 1
        assert child_state.parent_run_id == dispatched.run_id
        assert child_state.terminal_state is TerminalState.COMPLETED

    async def test_the_child_inherits_the_parents_scope(self) -> None:
        """Tenancy cannot widen across a delegation."""
        store = InMemoryStore()
        child = agent(name="billing", tools=())
        parent = agent(
            tools=(),
            subagents=(
                SubagentRef(
                    name="billing",
                    description="Handles billing questions and refunds end to end.",
                    spec=child,
                ),
            ),
        )
        model = (
            FakeModel()
            .turn(tool_calls=[("delegate", {"subagent": "billing", "task": "x"})])
            .turn(text="child done")
            .turn(text="parent done")
        )
        dispatched, _ = await run_agent(store, parent, model, build_registry())
        parent_state = await psych_runtime.state(store, dispatched.run_id)
        child_state = await psych_runtime.state(store, _child_run_id(parent_state))
        assert child_state.scope == SCOPE

    async def test_delegating_to_an_unknown_subagent_names_the_real_ones(self) -> None:
        store = InMemoryStore()
        child = agent(name="billing", tools=())
        parent = agent(
            tools=(),
            subagents=(
                SubagentRef(
                    name="billing",
                    description="Handles billing questions and refunds end to end.",
                    spec=child,
                ),
            ),
        )
        model = (
            FakeModel()
            .turn(tool_calls=[("delegate", {"subagent": "nowhere", "task": "x"})])
            .turn(text="Let me try the right one.")
        )
        dispatched, _ = await run_agent(store, parent, model, build_registry())
        state = await psych_runtime.state(store, dispatched.run_id)
        failure = state.tool_results[0].failure
        assert failure is not None
        assert "billing" in failure.message

    async def test_the_fanout_cap_stops_a_turn_spawning_a_tree(self) -> None:
        store = InMemoryStore()
        child = agent(name="billing", tools=())
        parent = agent(
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
        model = (
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
        dispatched, _ = await run_agent(store, parent, model, build_registry())
        state = await psych_runtime.state(store, dispatched.run_id)
        second = state.tool_results[1]
        assert second.outcome is ToolOutcome.ERROR
        assert second.failure is not None
        assert "already delegated 1" in second.failure.message


def _child_run_id(parent_state: Any) -> Any:
    for result in parent_state.tool_results:
        if result.tool == "delegate" and isinstance(result.result, dict):
            return result.result["run_id"]
    raise AssertionError("the parent recorded no delegation")
