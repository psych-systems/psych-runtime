"""The workflow tree a console renders, derived from Spec plus fold."""

from __future__ import annotations

import pytest

from psych_runtime.core.ids import RunId, StepId, derive_step_id
from psych_runtime.core.records import SuspendReason, ToolFailure
from psych_runtime.core.reducer import reduce
from psych_runtime.core.spec import (
    BranchCase,
    BranchStep,
    Condition,
    ForEachStep,
    ToolStep,
    ValuePath,
    WorkflowSpec,
)
from psych_runtime.core.workflow_view import StepViewStatus, workflow_view
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit

SPEC = WorkflowSpec(
    name="w",
    initial_state={"count": 0},
    steps=(
        ToolStep(name="a", tool="t"),
        BranchStep(
            name="pick",
            cases=(
                BranchCase(
                    name="left", when=Condition(path="input.x"), step=ToolStep(name="l", tool="t")
                ),
                BranchCase(
                    name="right", when=Condition(path="input.y"), step=ToolStep(name="r", tool="t")
                ),
            ),
        ),
        ForEachStep(
            name="each", items=ValuePath(path="input.items"), body=ToolStep(name="body", tool="t")
        ),
        ToolStep(name="last", tool="t"),
    ),
)


def _id(builder: LogBuilder, *path: str | int) -> StepId:
    return derive_step_id(RunId(builder.run_id), path)


class TestTree:
    def test_pending_steps_come_from_the_spec_and_children_from_the_log(self) -> None:
        b = LogBuilder().admitted(input={"x": 1, "items": [1, 2]}).attempt()
        a = _id(b, "w", 0, "a")
        pick = _id(b, "w", 1, "pick")
        left = _id(b, "w", 1, "pick", 0, "l")
        right = _id(b, "w", 1, "pick", 1, "r")
        each = _id(b, "w", 2, "each")
        body0 = _id(b, "w", 2, "each", 0, "body")
        body1 = _id(b, "w", 2, "each", 1, "body")
        b.step_started(a, name="a").step_completed(a, output={"result": 1})
        b.step_started(pick, name="pick", kind="branch")
        b.step_started(right, name="r").step_completed(right, skipped=True)
        b.step_started(left, name="l").step_completed(left, output={"result": 2})
        b.step_completed(pick, output={"chosen": ["l"], "l": {"result": 2}})
        b.step_started(each, name="each", kind="foreach")
        b.step_started(body0, name="body", iteration=0).step_completed(body0, output={"result": 3})
        b.step_started(
            body1, name="body", iteration=1, parent_step_id=each, path=("w", 2, "each", 1, "body")
        )
        view = workflow_view(SPEC, reduce(b.records))

        assert [s.name for s in view.steps] == ["a", "pick", "each", "last"]
        assert view.steps[0].status is StepViewStatus.COMPLETED
        assert view.steps[1].status is StepViewStatus.COMPLETED
        assert view.steps[1].cases == ("left", "right")
        assert [c.status for c in view.steps[1].children] == [
            StepViewStatus.COMPLETED,
            StepViewStatus.SKIPPED,
        ]
        assert view.steps[2].status is StepViewStatus.RUNNING
        # Only the iteration that named its parent is a child; the log is the
        # authority on how many there are.
        assert [c.iteration for c in view.steps[2].children] == [1]
        assert view.steps[2].children[0].status is StepViewStatus.RUNNING
        assert view.steps[3].status is StepViewStatus.PENDING
        assert view.steps[3].attempts == 0
        assert view.completed == 4, "a, the chosen arm, the branch itself and one item"
        assert view.state == {"count": 0}
        assert view.input == {"x": 1, "items": [1, 2]}

    def test_waiting_retrying_and_replayed(self) -> None:
        b = LogBuilder().admitted().attempt()
        a = _id(b, "w", 0, "a")
        pick = _id(b, "w", 1, "pick")
        b.step_started(a, name="a", replayed_from="run_src").step_completed(a, output={})
        b.step_started(pick, name="pick", kind="branch").step_completed(
            pick, failure=ToolFailure(kind="x", message="m"), will_retry=True
        )
        b.step_started(pick, name="pick", kind="branch", attempt_number=2)
        b.suspended(SuspendReason.BREAKPOINT, step_id=pick)
        view = workflow_view(SPEC, reduce(b.records))
        assert view.steps[0].status is StepViewStatus.REPLAYED
        assert view.steps[0].replayed_from == "run_src"
        assert view.steps[1].status is StepViewStatus.WAITING
        assert view.steps[1].attempts == 2
        assert view.waiting is not None
        assert view.waiting.name == "pick"
        assert view.waiting.reason is SuspendReason.BREAKPOINT

    def test_a_retrying_step_reads_as_retrying(self) -> None:
        b = LogBuilder().admitted().attempt()
        a = _id(b, "w", 0, "a")
        b.step_started(a, name="a").step_completed(
            a, failure=ToolFailure(kind="x", message="m"), will_retry=True
        )
        view = workflow_view(SPEC, reduce(b.records))
        assert view.steps[0].status is StepViewStatus.RETRYING
        assert view.failed == 0
