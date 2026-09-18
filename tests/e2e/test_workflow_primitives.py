"""Every workflow primitive, dispatched through a real Worker.

Each test here is the regression gate for one feature: parallel branches,
conditional branches, foreach, loops with state, mapping, retries with a
backoff that parks the Run, sleep, waiting on an event, asking a person,
a tool step's approval, breakpoints, replay, timeouts, the step budget, and
crash recovery in the middle of a parallel step. All of it is asserted through
the public API: ``psych_runtime.state``, ``status``, ``report``, ``workflow_view``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.errors import AccessDenied, WorkflowRequired
from psych_runtime.core.ids import RunId, WorkerId
from psych_runtime.core.records import Suspended, SuspendReason, TerminalState
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    BranchCase,
    BranchStep,
    CodeTool,
    Condition,
    ForEachStep,
    HumanStep,
    Limits,
    LiteralValue,
    LoopStep,
    MapStep,
    ModelRef,
    ParallelStep,
    RetryPolicy,
    SetStateStep,
    SleepStep,
    ToolStep,
    ValuePath,
    WaitStep,
    WorkflowSpec,
    WorkflowStepRef,
)
from psych_runtime.core.workflow_view import StepViewStatus
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.policy import Decision
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")
OTHER = Scope(tenant="rival", principal="user-9")


def ref(path: str) -> ValuePath:
    return ValuePath(path=path)


def lit(value: Any) -> LiteralValue:
    return LiteralValue(value=value)


def _registry(calls: dict[str, int], *, quick: bool = False) -> ToolRegistry:
    """``quick`` makes ``slow`` return at once, for a re-run branch that must finish."""
    registry = ToolRegistry()

    @registry.register
    def double(n: int) -> int:
        """Double a number."""
        calls["double"] = calls.get("double", 0) + 1
        return n * 2

    @registry.register
    def flaky(label: str, succeed_on: int = 3) -> str:
        """Fail until the nth call."""
        calls[label] = calls.get(label, 0) + 1
        if calls[label] < succeed_on:
            raise RuntimeError(f"{label} is not ready")
        return f"{label} ok"

    @registry.register
    async def slow(label: str, seconds: float) -> str:
        """Take a while."""
        calls[label] = calls.get(label, 0) + 1
        if not quick:
            await asyncio.sleep(seconds)
        return f"{label} done"

    @registry.register
    def refund(order_id: str) -> str:
        """Refund an order."""
        calls["refund"] = calls.get("refund", 0) + 1
        return f"refunded {order_id}"

    return registry


TOOLS = (
    CodeTool(name="double"),
    CodeTool(name="flaky"),
    CodeTool(name="slow"),
    CodeTool(name="refund"),
)


class _Harness:
    def __init__(self, calls: dict[str, int] | None = None, **runtime: Any) -> None:
        self.store = InMemoryStore()
        self.calls: dict[str, int] = calls if calls is not None else {}
        self.registry = _registry(self.calls)
        self.runtime = Runtime(
            store=self.store,
            model=runtime.pop("model", FakeModel()),
            registry=self.registry,
            **runtime,
        )

    async def start(self, spec: WorkflowSpec, **dispatch: Any) -> RunId:
        version = await psych_runtime.publish(self.store, spec)
        run = await psych_runtime.dispatch(self.store, version, SCOPE, **dispatch)
        return run.run_id

    async def drive(
        self, run_id: RunId, *, until: Callable[[Any], bool] | None = None, timeout: float = 8.0
    ) -> Any:
        """Run a Worker until the Run settles, or until ``until(state)`` holds."""
        worker = Worker(self.store, self.runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        deadline = datetime.now(UTC) + timedelta(seconds=timeout)
        try:
            while datetime.now(UTC) < deadline:
                state = await psych_runtime.state(self.store, run_id)
                if state.settled or (until is not None and until(state)):
                    return state
                await asyncio.sleep(0.02)
            raise AssertionError(f"run {run_id} did not reach the expected state in {timeout}s")
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)


class TestComposition:
    async def test_parallel_branch_foreach_loop_map_and_output(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="compose",
            tools=TOOLS,
            initial_state={"total": 0},
            steps=(
                ToolStep(name="a", tool="double", arguments_from={"n": ref("input.n")}),
                ParallelStep(
                    name="par",
                    branches=(
                        ToolStep(
                            name="b1",
                            tool="double",
                            arguments_from={"n": ref("steps.a.output.result")},
                        ),
                        ToolStep(name="b2", tool="double", arguments={"n": 5}),
                    ),
                ),
                BranchStep(
                    name="pick",
                    cases=(
                        BranchCase(
                            name="big",
                            when=Condition(path="steps.par.output.b1.result", op="gt", value=100),
                            step=MapStep(name="big_map", output={"size": lit("big")}),
                        ),
                        BranchCase(
                            name="small",
                            when=Condition(path="steps.par.output.b1.result", op="lte", value=100),
                            step=MapStep(name="small_map", output={"size": lit("small")}),
                        ),
                    ),
                ),
                ForEachStep(
                    name="each",
                    items=ref("input.items"),
                    body=ToolStep(name="dbl", tool="double", arguments_from={"n": ref("item")}),
                    concurrency=2,
                ),
                LoopStep(
                    name="count",
                    body=SetStateStep(name="bump", values={"total": ref("iteration")}),
                    until=Condition(path="state.total", op="gte", value=3),
                ),
                MapStep(
                    name="shape",
                    output={
                        "first": ref("steps.each.output.items[0].result"),
                        "chosen": ref("steps.pick.output.chosen"),
                    },
                ),
            ),
            output={
                "size": ref("steps.pick.output.small_map.size"),
                "shape": ref("steps.shape.output"),
                "total": ref("state.total"),
                "b2": ref("steps.par.output.b2.result"),
            },
        )
        run_id = await h.start(spec, input={"n": 3, "items": [1, 2, 3]})
        state = await h.drive(run_id)

        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output == {
            "size": "small",
            "shape": {"first": 2, "chosen": ["small_map"]},
            "total": 3,
            "b2": 10,
        }
        assert h.calls == {"double": 6}

        view = await psych_runtime.workflow_view(h.store, run_id, scope=SCOPE)
        by_name = {s.name: s for s in view.steps}
        assert [c.status for c in by_name["pick"].children] == [
            StepViewStatus.SKIPPED,
            StepViewStatus.COMPLETED,
        ]
        assert [c.iteration for c in by_name["each"].children] == [0, 1, 2]
        assert by_name["each"].output == {
            "items": [{"result": 2}, {"result": 4}, {"result": 6}],
            "count": 3,
        }
        assert [c.iteration for c in by_name["count"].children] == [1, 2, 3]
        assert view.state == {"total": 3}
        assert (
            view.completed == len(view.steps) + 2 + 1 + 3 + 3
        )  # top level, branches, chosen arm, items, iterations

        report = await psych_runtime.report(h.store, run_id)
        starts = [s for s in report.steps if s.name == "dbl"]
        assert [s.iteration for s in starts] == [0, 1, 2]
        assert all(s.parent_step_id == by_name["each"].step_id for s in starts)
        assert all(s.started_at is not None and s.completed_at is not None for s in report.steps)

    async def test_a_step_may_end_the_workflow_early(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="early",
            tools=TOOLS,
            steps=(
                BranchStep(
                    name="gate",
                    cases=(
                        BranchCase(
                            name="nothing_to_do",
                            when=Condition(path="input.items", op="eq", value=[]),
                            step=MapStep(
                                name="done", output={"reason": lit("empty")}, ends_workflow=True
                            ),
                        ),
                    ),
                    ends_workflow=True,
                    when=Condition(path="input.items", op="eq", value=[]),
                ),
                ToolStep(name="work", tool="double", arguments={"n": 1}),
            ),
        )
        run_id = await h.start(spec, input={"items": []})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert h.calls == {}
        assert state.output is not None
        assert state.output["gate"]["done"] == {"reason": "empty"}
        view = await psych_runtime.workflow_view(h.store, run_id)
        assert view.steps[1].status is StepViewStatus.PENDING

        busy = await h.start(spec, input={"items": [1]})
        state = await h.drive(busy)
        assert state.terminal_state is TerminalState.COMPLETED
        assert h.calls == {"double": 1}

    async def test_a_nested_workflow_gets_its_own_namespace_and_shares_state(self) -> None:
        h = _Harness()
        inner = WorkflowSpec(
            name="inner",
            tools=TOOLS,
            steps=(
                ToolStep(name="a", tool="double", arguments_from={"n": ref("input.n")}),
                SetStateStep(name="mark", values={"inner_ran": lit(True)}),
            ),
            output={"doubled": ref("steps.a.output.result")},
        )
        outer = WorkflowSpec(
            name="outer",
            tools=TOOLS,
            steps=(
                ToolStep(name="a", tool="double", arguments={"n": 1}),
                WorkflowStepRef(
                    name="child", spec=inner, input={"n": ref("steps.a.output.result")}
                ),
                MapStep(
                    name="after",
                    output={
                        "seen": ref("state.inner_ran"),
                        "child": ref("steps.child.output.doubled"),
                    },
                ),
            ),
        )
        run_id = await h.start(outer)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output is not None
        assert state.output["after"] == {"seen": True, "child": 4}
        assert state.output["child"] == {"doubled": 4}

    async def test_an_agent_step_receives_its_mapped_input_as_the_user_message(self) -> None:
        model = FakeModel().turn(text="Hello Ada!")
        h = _Harness(model=model)
        greeter = AgentSpec(
            name="greeter", instructions="Greet.", model=ModelRef(model="fake-standard")
        )
        spec = WorkflowSpec(
            name="greet",
            steps=(
                MapStep(name="who", output={"name": ref("input.customer")}),
                AgentStep(
                    name="hello", spec=greeter, input={"message": ref("steps.who.output.name")}
                ),
            ),
        )
        run_id = await h.start(spec, input={"customer": "Ada"})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        request = model.requests[-1]
        user_messages = [m.content for m in request.messages if getattr(m, "role", "") == "user"]
        assert user_messages[-1] == "Ada"
        # The agent's turn is attributed to its step, so a trace can nest it.
        report = await psych_runtime.report(h.store, run_id)
        hello = next(s for s in report.steps if s.name == "hello")
        assert [call.step_id for call in report.model_calls] == [hello.step_id]


class TestFailureHandling:
    async def test_a_retry_with_backoff_parks_the_run_and_wakes_it(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="retrying",
            tools=TOOLS,
            steps=(
                ToolStep(
                    name="f",
                    tool="flaky",
                    arguments={"label": "f"},
                    retry=RetryPolicy(max_attempts=3, backoff_seconds=0.1),
                ),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert h.calls["f"] == 3
        records = await h.store.read(run_id)
        parked = [r for r in records if isinstance(r, Suspended)]
        assert [r.reason for r in parked] == [SuspendReason.TIMER, SuspendReason.TIMER]
        assert all(r.wake_at is not None and r.step_id is not None for r in parked)
        report = await psych_runtime.report(h.store, run_id)
        attempts = [s for s in report.steps if s.name == "f"]
        assert [s.attempt_number for s in attempts] == [1, 2, 3]
        assert [s.will_retry for s in attempts] == [True, True, False]
        assert state.attempt_count == 3, "each backoff released the lease and a claim woke it"

    async def test_exhausted_retries_fail_the_run_with_the_steps_failure(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="giveup",
            tools=TOOLS,
            retry=RetryPolicy(max_attempts=2),
            steps=(
                ToolStep(name="f", tool="flaky", arguments={"label": "f", "succeed_on": 99}),
                ToolStep(name="never", tool="double", arguments={"n": 1}),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "RuntimeError"
        assert h.calls == {"f": 2}
        view = await psych_runtime.workflow_view(h.store, run_id)
        assert [s.status for s in view.steps] == [StepViewStatus.FAILED, StepViewStatus.PENDING]

    async def test_on_failure_continue_carries_on_with_none(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="tolerant",
            tools=TOOLS,
            steps=(
                ToolStep(
                    name="f",
                    tool="flaky",
                    arguments={"label": "f", "succeed_on": 99},
                    on_failure="continue",
                ),
                MapStep(
                    name="after",
                    output={"got": ref("steps.f.output"), "status": ref("steps.f.status")},
                ),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output is not None
        assert state.output["after"] == {"got": None, "status": "failed"}

    async def test_a_step_timeout_is_its_own_failure_kind(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="slowpoke",
            tools=TOOLS,
            steps=(
                ToolStep(
                    name="s",
                    tool="slow",
                    arguments={"label": "s", "seconds": 5},
                    timeout_seconds=0.1,
                ),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "step_timeout"

    async def test_an_unresolved_path_names_the_step_and_the_path(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="typo",
            tools=TOOLS,
            steps=(ToolStep(name="a", tool="double", arguments_from={"n": ref("input.missing")}),),
        )
        run_id = await h.start(spec, input={"n": 1})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "unresolved_path"
        assert "input.missing" in state.failure.message

    async def test_a_guard_skips_and_a_schema_violation_fails(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="guarded",
            tools=TOOLS,
            input_schema={"type": "object", "required": ["n"]},
            steps=(
                ToolStep(
                    name="maybe", tool="double", arguments={"n": 1}, when=Condition(path="input.go")
                ),
                ToolStep(
                    name="checked",
                    tool="double",
                    arguments={"n": 1},
                    output_schema={"properties": {"result": {"type": "string"}}},
                ),
            ),
        )
        run_id = await h.start(spec, input={"n": 1, "go": False})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "schema_violation"
        view = await psych_runtime.workflow_view(h.store, run_id)
        assert view.steps[0].status is StepViewStatus.SKIPPED
        assert h.calls == {"double": 1}

        bad_input = await h.start(spec, input={})
        state = await h.drive(bad_input)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert "input_schema" in state.failure.message

    async def test_the_step_budget_bounds_a_loop(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="runaway",
            tools=TOOLS,
            limits=Limits(max_steps=5),
            steps=(
                LoopStep(
                    name="forever",
                    body=ToolStep(name="d", tool="double", arguments={"n": 1}),
                    until=Condition(path="input.never", op="exists"),
                    max_iterations=100,
                ),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "budget_exhausted"
        assert state.step_starts <= 6


class TestWaiting:
    async def test_a_sleep_releases_the_lease_and_wakes_on_time(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="nap",
            tools=TOOLS,
            steps=(
                SleepStep(name="z", seconds=0.2),
                ToolStep(name="a", tool="double", arguments={"n": 2}),
            ),
        )
        run_id = await h.start(spec)
        parked = await h.drive(run_id, until=lambda s: s.suspended)
        assert parked.suspend_reason is SuspendReason.TIMER
        header = await h.store.get_run(run_id)
        assert header is not None
        assert header.state is RunState.RUNNABLE, (
            "a timer parks a runnable Run rather than suspending it"
        )
        assert header.runnable_at is not None
        status = await psych_runtime.status(h.store, run_id)
        assert status.pending_wait is not None
        assert status.pending_wait.reason is SuspendReason.TIMER
        assert status.pending_wait.wake_at is not None
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output is not None
        assert state.output["z"] == {"woken_by": "timer"}

    async def test_a_wait_step_completes_with_the_delivered_payload(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="webhook",
            tools=TOOLS,
            steps=(
                WaitStep(
                    name="paid",
                    event="payment.settled",
                    payload_schema={"type": "object", "required": ["amount"]},
                ),
                MapStep(name="after", output={"amount": ref("steps.paid.output.amount")}),
            ),
        )
        run_id = await h.start(spec)
        waiting = await h.drive(run_id, until=lambda s: s.suspended)
        assert waiting.suspend_reason is SuspendReason.EXTERNAL
        status = await psych_runtime.status(h.store, run_id)
        assert status.pending_wait is not None
        assert status.pending_wait.event == "payment.settled"
        assert status.suspended_step_id == status.pending_wait.step_id

        await psych_runtime.resume(h.store, run_id, payload={"amount": 42}, scope=SCOPE)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.output is not None
        assert state.output["after"] == {"amount": 42}

    async def test_a_payload_that_breaks_the_schema_fails_the_step(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="webhook",
            tools=TOOLS,
            steps=(
                WaitStep(
                    name="paid",
                    event="paid",
                    payload_schema={"type": "object", "required": ["amount"]},
                ),
            ),
        )
        run_id = await h.start(spec)
        await h.drive(run_id, until=lambda s: s.suspended)
        await psych_runtime.resume(h.store, run_id, payload={"wrong": 1})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "schema_violation"

    async def test_a_human_step_asks_and_takes_the_answer_as_output(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="ask",
            steps=(
                HumanStep(name="confirm", prompt="Ship it?"),
                MapStep(name="after", output={"said": ref("steps.confirm.output.answer")}),
            ),
        )
        run_id = await h.start(spec)
        waiting = await h.drive(run_id, until=lambda s: s.suspended)
        assert waiting.suspend_reason is SuspendReason.QUESTION
        status = await psych_runtime.status(h.store, run_id)
        assert status.pending_question is not None
        assert status.pending_question.call_id is None
        assert status.pending_question.step_id is not None
        assert status.pending_question.summary == "Ship it?"
        await psych_runtime.resume(h.store, run_id, payload={"answer": "yes"}, by="ops")
        state = await h.drive(run_id)
        assert state.output is not None
        assert state.output["after"] == {"said": "yes"}

    async def test_a_tool_step_that_needs_approval_suspends_and_obeys_the_decision(self) -> None:
        class AskForRefunds:
            async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
                return (
                    Decision.ask("a refund needs a person")
                    if tool == "refund"
                    else Decision.allow()
                )

            async def allow_run(self, scope: Scope, version: Any) -> Decision:
                return Decision.allow()

        spec = WorkflowSpec(
            name="refunds",
            tools=TOOLS,
            steps=(
                ToolStep(
                    name="pay",
                    tool="refund",
                    arguments={"order_id": "A1"},
                    retry=RetryPolicy(max_attempts=3),
                ),
            ),
        )
        approved = _Harness(policy=AskForRefunds())
        run_id = await approved.start(spec)
        waiting = await approved.drive(run_id, until=lambda s: s.suspended)
        assert waiting.suspend_reason is SuspendReason.APPROVAL
        status = await psych_runtime.status(approved.store, run_id)
        assert status.pending_approval is not None
        assert status.pending_approval.call_id is None
        assert status.pending_approval.tool == "refund"
        assert status.pending_approval.arguments == {"order_id": "A1"}
        assert approved.calls == {}
        await psych_runtime.resume(approved.store, run_id, approved=True, by="manager")
        state = await approved.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert approved.calls == {"refund": 1}

        denied = _Harness(policy=AskForRefunds())
        run_id = await denied.start(spec)
        await denied.drive(run_id, until=lambda s: s.suspended)
        await psych_runtime.resume(denied.store, run_id, approved=False, by="manager")
        state = await denied.drive(run_id)
        assert state.terminal_state is TerminalState.FAILED
        assert state.failure is not None
        assert state.failure.kind == "denied"
        assert denied.calls == {}
        report = await psych_runtime.report(denied.store, run_id)
        assert len([s for s in report.steps if s.name == "pay"]) == 1, "a denial is not retried"


class TestDebugging:
    async def test_breakpoints_pause_before_a_step_and_step_mode_can_be_turned_off(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="stepped",
            tools=TOOLS,
            steps=tuple(
                ToolStep(name=f"s{i}", tool="double", arguments={"n": i}) for i in range(3)
            ),
        )
        run_id = await h.start(spec, breakpoints=("s1",))
        paused = await h.drive(run_id, until=lambda s: s.suspended)
        assert paused.suspend_reason is SuspendReason.BREAKPOINT
        assert h.calls == {"double": 1}
        view = await psych_runtime.workflow_view(h.store, run_id)
        # The paused step has no start record yet and still reads as waiting,
        # because the suspension names it.
        assert [s.status for s in view.steps] == [
            StepViewStatus.COMPLETED,
            StepViewStatus.WAITING,
            StepViewStatus.PENDING,
        ]
        assert view.waiting is not None
        assert view.waiting.name == "s1"
        status = await psych_runtime.status(h.store, run_id)
        assert status.pending_wait is not None
        assert status.pending_wait.step_name == "s1"

        # Continue, and ask to stop before every later step too.
        await psych_runtime.resume(h.store, run_id, payload={"step_mode": True})
        paused = await h.drive(run_id, until=lambda s: s.suspended)
        assert paused.step_mode is True
        assert h.calls == {"double": 2}
        # Then turn stepping off; the rest runs through.
        await psych_runtime.resume(h.store, run_id, payload={"step_mode": False})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert h.calls == {"double": 3}

    async def test_replay_starts_a_new_run_from_a_chosen_step(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="pipeline",
            tools=TOOLS,
            steps=(
                ToolStep(name="one", tool="double", arguments_from={"n": ref("input.n")}),
                ToolStep(name="two", tool="flaky", arguments={"label": "two", "succeed_on": 99}),
                ToolStep(
                    name="three",
                    tool="double",
                    arguments_from={"n": ref("steps.one.output.result")},
                ),
            ),
        )
        first = await h.start(spec, input={"n": 2})
        state = await h.drive(first)
        assert state.terminal_state is TerminalState.FAILED
        assert h.calls == {"double": 1, "two": 1}

        # The cause is fixed (here: the tool starts working) and the second
        # step is replayed with a different input for the rest of the run.
        h.calls["two"] = 98
        replayed = await psych_runtime.replay(
            h.store, first, from_step="two", input={"n": 10}, scope=SCOPE
        )
        assert replayed.created
        state = await h.drive(replayed.run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert h.calls["double"] == 2, "step one was copied, not re-run"
        assert state.output is not None
        assert state.output["one"] == {"result": 4}, "the copied step keeps the original result"
        assert state.output["three"] == {"result": 8}

        view = await psych_runtime.workflow_view(h.store, replayed.run_id)
        assert view.replays_run_id == first
        assert view.replay_from_step == "two"
        assert view.steps[0].status is StepViewStatus.REPLAYED
        assert view.steps[0].replayed_from == first
        assert [s.status for s in view.steps[1:]] == [
            StepViewStatus.COMPLETED,
            StepViewStatus.COMPLETED,
        ]
        assert view.input == {"n": 10}

        original = await psych_runtime.state(h.store, first)
        assert original.terminal_state is TerminalState.FAILED, "the source Run is untouched"

    async def test_replay_refuses_the_wrong_scope_step_or_kind(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="w", tools=TOOLS, steps=(ToolStep(name="a", tool="double", arguments={"n": 1}),)
        )
        run_id = await h.start(spec)
        await h.drive(run_id)
        with pytest.raises(AccessDenied):
            await psych_runtime.replay(h.store, run_id, from_step="a", scope=OTHER)
        with pytest.raises(WorkflowRequired):
            await psych_runtime.replay(h.store, run_id, from_step="nope")
        with pytest.raises(AccessDenied):
            await psych_runtime.workflow_view(h.store, run_id, scope=OTHER)

        agent = AgentSpec(name="a", instructions="x", model=ModelRef(model="fake-standard"))
        version = await psych_runtime.publish(h.store, agent)
        agent_run = await psych_runtime.dispatch(h.store, version, SCOPE)
        with pytest.raises(WorkflowRequired):
            await psych_runtime.workflow_view(h.store, agent_run.run_id)
        with pytest.raises(WorkflowRequired):
            await psych_runtime.replay(h.store, agent_run.run_id, from_step="a")


class TestRecovery:
    async def test_a_crash_inside_a_parallel_step_keeps_the_finished_branch(self) -> None:
        calls: dict[str, int] = {}
        h = _Harness(calls)
        spec = WorkflowSpec(
            name="fanout",
            tools=TOOLS,
            steps=(
                ParallelStep(
                    name="par",
                    branches=(
                        ToolStep(
                            name="quick", tool="slow", arguments={"label": "quick", "seconds": 0.01}
                        ),
                        ToolStep(
                            name="stuck", tool="slow", arguments={"label": "stuck", "seconds": 30}
                        ),
                    ),
                ),
                ToolStep(name="after", tool="double", arguments={"n": 1}),
            ),
        )
        run_id = await h.start(spec)

        # First attempt, driven directly so it can be killed mid-parallel.
        journal = await Journal.open(h.store, run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        attempt = asyncio.create_task(h.runtime.engine_for(journal).execute(spec))
        for _ in range(500):
            await asyncio.sleep(0.005)
            state = reduce(await h.store.read(run_id))
            if any(s.name == "quick" and s.completed for s in state.steps.values()):
                break
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        assert calls == {"quick": 1, "stuck": 1}

        # Second attempt through a Worker: the finished branch is memoised,
        # the unfinished one re-runs from its start.
        await h.store.release(run_id, WorkerId("wrk_1"), RunState.RUNNABLE)
        h.runtime = Runtime(store=h.store, model=FakeModel(), registry=_registry(calls, quick=True))
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert calls["quick"] == 1
        assert calls["stuck"] == 2
        report = await psych_runtime.report(h.store, run_id)
        stuck = [s for s in report.steps if s.name == "stuck"]
        assert [s.attempt_number for s in stuck] == [1, 2]
        assert stuck[0].completed is False, "the killed attempt dangles in the log, honestly"

    async def test_an_interrupt_while_parked_stops_the_run(self) -> None:
        h = _Harness()
        spec = WorkflowSpec(
            name="nap",
            tools=TOOLS,
            steps=(
                SleepStep(name="z", seconds=30),
                ToolStep(name="a", tool="double", arguments={"n": 2}),
            ),
        )
        run_id = await h.start(spec)
        await h.drive(run_id, until=lambda s: s.suspended)
        await psych_runtime.interrupt(h.store, run_id, scope=SCOPE)
        await psych_runtime.resume(h.store, run_id, payload={"woken_by": "operator"})
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.ABORTED
        assert h.calls == {}


class TestTracing:
    async def test_every_step_opens_a_span_and_the_agents_turn_nests_under_it(self) -> None:
        from psych_runtime.telemetry.conformance import RecordingTelemetry, check_schema_conformance
        from psych_runtime.telemetry.schema import PSYCH_SCHEMA

        telemetry = RecordingTelemetry()
        model = FakeModel().turn(text="hi")
        h = _Harness(model=model, telemetry=telemetry)
        agent = AgentSpec(name="a", instructions="x", model=ModelRef(model="fake-standard"))
        spec = WorkflowSpec(
            name="traced",
            tools=TOOLS,
            steps=(
                ParallelStep(
                    name="par",
                    branches=(
                        ToolStep(name="one", tool="double", arguments={"n": 1}),
                        ToolStep(name="two", tool="double", arguments={"n": 2}),
                    ),
                ),
                AgentStep(name="talk", spec=agent, input={"message": lit("hello")}),
            ),
        )
        run_id = await h.start(spec)
        state = await h.drive(run_id)
        assert state.terminal_state is TerminalState.COMPLETED

        assert check_schema_conformance(PSYCH_SCHEMA, telemetry.spans) == ()
        steps = [s for s in telemetry.spans if s.name == "psych.step"]
        by_name = {s.attributes["psych.step.name"]: s for s in steps}
        assert set(by_name) == {"par", "one", "two", "talk"}
        assert by_name["par"].parent == "psych.attempt"
        assert by_name["one"].parent == "psych.step"
        assert by_name["par"].attributes["psych.step.kind"] == "parallel"
        assert all(s.attributes["psych.step.outcome"] == "succeeded" for s in steps)
        turns = [s for s in telemetry.spans if s.name == "psych.turn"]
        assert turns
        assert all(t.parent == "psych.step" for t in turns)
