"""A workflow step's failure keeps the nested agent's own failure kind.

``StepFailed`` exists so a nested agent's real failure kind and traceback
survive into the step's record. The only catcher used to be a generic
``except Exception``, which recorded ``kind="StepFailed"`` and this frame's
traceback: the bug the class was written to fix, reproduced one layer up.
"""

from __future__ import annotations

from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.records import StepCompleted
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, AgentStep, CodeTool, Limits, ModelRef, WorkflowSpec
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> str:
        """Look up an order."""
        return f"{order_id} is shipped"

    return registry


def _agent(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "looper",
        "instructions": "Keep looking things up.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(kwargs)
    return AgentSpec(**base)


class TestAStepCarriesTheAgentsOwnFailure:
    async def test_the_step_records_the_nested_failure_kind_not_step_failed(self) -> None:
        store = InMemoryStore()
        workflow = WorkflowSpec(
            name="nightly",
            tools=(CodeTool(name="lookup"),),
            steps=(
                AgentStep(
                    name="collect",
                    spec=_agent(limits=Limits(max_turns=1, deadline_seconds=60)),
                ),
            ),
        )
        version = await psych_runtime.publish(store, workflow)
        run = await psych_runtime.dispatch(store, version, SCOPE)

        # One turn allowed; the model wants more, so the agent ends on its
        # turn budget, which is a failure with its own kind.
        model = FakeModel()
        for _ in range(3):
            model = model.turn(tool_calls=[("lookup", {"order_id": "A1"})])
        runtime = Runtime(store=store, model=model, registry=_registry())
        journal = await Journal.open(store, run.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await runtime.engine_for(journal).run(workflow)

        records = await store.read(run.run_id)
        completed = [r for r in records if isinstance(r, StepCompleted) and r.failure is not None]
        assert len(completed) == 1
        failure = completed[0].failure
        assert failure is not None
        assert failure.kind == "budget_exhausted", failure
        assert failure.kind != "StepFailed"
