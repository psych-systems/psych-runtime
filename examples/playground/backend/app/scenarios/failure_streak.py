"""DESIGN.md §23 item 10: three consecutive failures of one tool stop the
model repeating it.

DESIGN.md §10.6. The counterpart scenario ``degenerate-loop`` exists to make
the boundary concrete: this guard counts *failures*, and a call that keeps
succeeding never trips it however many times the model repeats it.
Here the tool is genuinely broken, so the guard is squarely in its own
jurisdiction: at the threshold the tool is withdrawn from what the model is
offered and the model is told why, in its own words, rather than the Run
being left to burn its whole turn budget retrying something that cannot
work.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import drive, records_dict, report_dict
from psych_runtime.core.records import TerminalState
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="failure-streak",
    title="Failure-streak guard",
    proves=(
        "Three consecutive failures of one tool stop the model repeating "
        "it: the tool is withdrawn from what the next turn is offered and "
        "the model is told why, before the turn budget is exhausted "
        "retrying it."
    ),
    design_ref="§23.10",
    requires=(),
)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def flaky(value: str) -> str:
        """Always fails."""
        raise RuntimeError(f"it never works, not even for {value!r}")

    return registry


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    registry = _registry()
    spec = AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="flaky"),),
    )

    await emit("script", "scripting a model that keeps retrying a tool that always fails")
    model = FakeModel()
    for _ in range(4):
        model.turn(tool_calls=[("flaky", {"value": "x"})])
    model.turn(text="I will stop trying that and let the customer know.")

    run_id = await drive(store, spec, model, registry)

    report = await psych_runtime.report(store, run_id)
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the Run still completed -- the guard redirects the model, it does not fail the Run",
        report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={terminal!r}",
    )

    fourth_request_tools = {tool.name for tool in model.requests[3].tools}
    checks.require(
        "by the fourth turn, the broken tool was gone from what the model was offered",
        "flaky" not in fourth_request_tools,
        f"turn 4 offered tools: {sorted(fourth_request_tools)}",
    )
    checks.require(
        "the guard's own advisory recorded exactly one trip, at the threshold",
        len(report.failure_streak_trips) == 1 and report.failure_streak_trips[0].tool == "flaky",
        f"failure_streak_trips={report.failure_streak_trips!r}",
    )
    trip = report.failure_streak_trips[0]
    checks.require(
        "the trip fired at the configured threshold (3), not before and not after",
        trip.streak == trip.threshold == 3,
        f"streak={trip.streak}, threshold={trip.threshold}",
    )

    # advisory_message() lands in the system prompt's own "## Notices" section
    # on the turn the tool is actually withheld (turn 4) -- not attached to
    # the third call's own ToolFailure, which carries only that one call's
    # generic per-failure guidance. See psych_runtime.model.prompt.advisories_block.
    fourth_system = model.requests[3].messages[0]
    checks.require(
        "the model was told, in its next turn's own system prompt, why the tool disappeared",
        fourth_system.role == "system"
        and "removed from the tools available to you" in fourth_system.content,
        f"turn 4 system prompt tail={fourth_system.content[-400:]!r}",
    )

    log = await psych_runtime.records(store, run_id)
    return checks.result(
        "the broken tool was withdrawn and explained at the third failure, well before "
        "the model's turn budget ran out",
        run_ids=[str(run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
