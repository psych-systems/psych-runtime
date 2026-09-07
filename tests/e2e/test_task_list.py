"""A Run that writes down its plan and works through it.

The decision to build this at all is argued in `psych_runtime/core/tasks.py`:
a task list is closer to product opinion than to runtime mechanism, so it is
opt-in per Spec and off by default. These assert both halves, that an agent
which asked for it gets a plan in its log and its report, and that one which
did not sees no extra tool and no extra prompt.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import TaskListUpdated, TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.tasks import task_summary
from psych_runtime.core.version import publish as make_version
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

PLAN = [
    {"title": "Look up the order", "active_form": "Looking up the order", "status": "in_progress"},
    {"title": "Check the refund window", "active_form": "Checking the refund window"},
    {"title": "Issue the refund", "active_form": "Issuing the refund"},
]


def _spec(*, tasks: bool = True) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tasks_enabled=tasks,
        limits=psych_runtime.Limits(max_turns=8, deadline_seconds=60),
    )


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    yield ToolRegistry()


async def _run(
    model: FakeModel, registry: ToolRegistry, spec: psych_runtime.AgentSpec
) -> tuple[InMemoryStore, RunId]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    runtime = Runtime(store=store, model=model, registry=registry)
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(
            store, version.hash, SCOPE, input={"message": "refund A1"}
        )
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return store, run.run_id


class TestKeepingAPlan:
    async def test_the_report_carries_the_plan_as_the_model_left_it(
        self, registry: ToolRegistry
    ) -> None:
        finished = [dict(item, status="completed") for item in PLAN]
        model = (
            FakeModel()
            .turn(tool_calls=[("update_tasks", {"tasks": PLAN})])
            .turn(tool_calls=[("update_tasks", {"tasks": finished})])
            .turn(text="Refunded A1.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        # The final list, not every revision: the intermediate ones are in the
        # log for anyone who needs them.
        assert [task.title for task in report.tasks] == [item["title"] for item in PLAN]
        assert all(task.status == "completed" for task in report.tasks)

    async def test_the_whole_list_is_replaced_rather_than_merged(
        self, registry: ToolRegistry
    ) -> None:
        """A shorter second list means a shorter plan. Merging would leave
        items nobody wrote, and the model has no way to say "drop that one"
        other than by omitting it."""
        model = (
            FakeModel()
            .turn(tool_calls=[("update_tasks", {"tasks": PLAN})])
            .turn(tool_calls=[("update_tasks", {"tasks": [{"title": "Just refund it"}]})])
            .turn(text="Done.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert [task.title for task in report.tasks] == ["Just refund it"]

    async def test_the_summary_leads_with_what_is_happening_now(
        self, registry: ToolRegistry
    ) -> None:
        """`active_form` earns its place here: the current line of a plan
        should read as an activity rather than as an instruction."""
        model = (
            FakeModel()
            .turn(tool_calls=[("update_tasks", {"tasks": PLAN})])
            .turn(text="Working on it.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert task_summary(report.tasks) == "Looking up the order · 0 of 3 done"

    async def test_a_task_with_no_active_form_falls_back_to_its_title(
        self, registry: ToolRegistry
    ) -> None:
        """A model that does not bother still produces something readable."""
        model = (
            FakeModel()
            .turn(
                tool_calls=[
                    ("update_tasks", {"tasks": [{"title": "Refund it", "status": "in_progress"}]})
                ]
            )
            .turn(text="Working on it.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert task_summary(report.tasks) == "Refund it · 0 of 1 done"

    async def test_the_plan_lands_in_the_log_beside_the_turn_that_wrote_it(
        self, registry: ToolRegistry
    ) -> None:
        """No store, no second source of truth: the plan is Records, folded by
        the reducer like everything else."""
        model = FakeModel().turn(tool_calls=[("update_tasks", {"tasks": PLAN})]).turn(text="Done.")
        store, run_id = await _run(model, registry, _spec())

        log = await psych_runtime.records(store, run_id)
        written = [record for record in log if isinstance(record, TaskListUpdated)]
        assert len(written) == 1
        assert len(written[0].tasks) == 3
        # Appended by the tool body, so it sits between its own start and
        # finish records rather than at the end of the turn.
        types = [type(record).__name__ for record in log]
        start = types.index("ToolCallStarted")
        assert types[start + 1] == "TaskListUpdated"


class TestLenience:
    async def test_a_bad_status_becomes_pending_rather_than_losing_the_plan(
        self, registry: ToolRegistry
    ) -> None:
        model = (
            FakeModel()
            .turn(
                tool_calls=[
                    ("update_tasks", {"tasks": [{"title": "Refund it", "status": "blocked"}]})
                ]
            )
            .turn(text="Done.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert [(task.title, task.status) for task in report.tasks] == [("Refund it", "pending")]

    async def test_camel_case_active_form_is_accepted(self, registry: ToolRegistry) -> None:
        """`activeForm` is what several harnesses call it, and a model reaching
        for that spelling has still said the right thing."""
        model = (
            FakeModel()
            .turn(
                tool_calls=[
                    (
                        "update_tasks",
                        {
                            "tasks": [
                                {
                                    "title": "Refund it",
                                    "activeForm": "Refunding it",
                                    "status": "in_progress",
                                }
                            ]
                        },
                    )
                ]
            )
            .turn(text="Done.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.tasks[0].active_form == "Refunding it"

    async def test_an_empty_plan_leaves_the_previous_one_alone(
        self, registry: ToolRegistry
    ) -> None:
        """Nothing usable in means nothing written: a model that sent a
        malformed list should not silently lose the plan it already had."""
        model = (
            FakeModel()
            .turn(tool_calls=[("update_tasks", {"tasks": PLAN})])
            .turn(tool_calls=[("update_tasks", {"tasks": [{"nope": 1}]})])
            .turn(text="Done.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert len(report.tasks) == 3


class TestWhoIsOfferedIt:
    async def test_an_agent_that_did_not_ask_for_it_is_not_offered_it(
        self, registry: ToolRegistry
    ) -> None:
        """Off by default, and the cost of being wrong about that is prompt
        spent on every turn of every Run for a tool nobody wanted."""
        model = FakeModel().turn(text="Done.")
        store, run_id = await _run(model, registry, _spec(tasks=False))
        _ = store, run_id

        offered = {tool.name for tool in model.requests[0].tools}
        assert "update_tasks" not in offered

    async def test_enabling_tasks_moves_the_version_hash(self) -> None:
        """An agent that keeps a plan is a different agent, and its prompt
        differs on every turn once it writes one."""
        assert make_version(_spec(tasks=False)).hash != make_version(_spec(tasks=True)).hash
