"""Large results: a multi-megabyte tool result offloads to the log with a
preview and a handle, and the model reads it back in slices.

DESIGN.md §10.8. The log holds the whole result -- nothing is dropped -- but
the model is never handed the whole thing directly: past ``Limits
.large_result_bytes`` it gets a preview and a handle, and ``read_tool_output``
(offered only on the turn after such a result exists, never before, since a
tool that can only ever say "nothing to read yet" is worse than no tool) lets
it page through the rest by offset and limit.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import drive, records_dict, report_dict
from psych_runtime.core.ids import ToolCallId
from psych_runtime.core.records import TerminalState
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel, ToolCallScript
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="large-result-offload",
    title="Large-result offload",
    proves=(
        "A tool result over the configured size limit is kept whole in the "
        "log but handed to the model as a preview plus a handle; the model "
        "then reads the rest back in slices through read_tool_output, "
        "offered only once there is something to read."
    ),
    design_ref="§10.8",
    requires=(),
)

_ROW_COUNT = 500
_LIMIT_BYTES = 1024


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def big_report(rows: int) -> str:
        """Produce a large report."""
        return "\n".join(f"line {index}: something happened" for index in range(rows))

    return registry


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    spec = AgentSpec(
        name="support",
        instructions="Produce and inspect reports.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="big_report"),),
        limits=Limits(large_result_bytes=_LIMIT_BYTES),
    )

    await emit("produce", f"a tool producing a {_ROW_COUNT}-line report, over the size limit")
    # The handle is derived from the call id, so it is pinned here rather
    # than guessed -- a handle the Run never issued is refused as corruption.
    report_call = ToolCallScript(
        name="big_report", arguments={"rows": _ROW_COUNT}, call_id=ToolCallId("call-report")
    )
    model = (
        FakeModel()
        .turn(tool_calls=[report_call])
        .turn(
            tool_calls=[
                ("read_tool_output", {"handle": "res_call-report", "offset": 10, "limit": 2})
            ]
        )
        .turn(text="Lines 10 and 11 say something happened.")
    )
    run_id = await drive(store, spec, model, _registry(), message="run the big report")

    report = await psych_runtime.report(store, run_id)
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the Run completed after reading the offloaded result back by handle",
        report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={terminal!r}",
    )

    log = await psych_runtime.records(store, run_id)
    stored = next(r for r in log if r.type == "tool_call_finished" and r.result_handle is not None)
    checks.require(
        "the log kept the whole result, over the configured limit, nothing dropped",
        stored.result_bytes > _LIMIT_BYTES
        and stored.result is not None
        and len(stored.result) == stored.result_bytes,
        f"result_bytes={stored.result_bytes}, limit={_LIMIT_BYTES}",
    )
    checks.require(
        "the model was handed a preview strictly smaller than the whole result",
        stored.preview is not None and len(stored.preview) < stored.result_bytes,
        f"preview_len={len(stored.preview) if stored.preview else None}, "
        f"result_bytes={stored.result_bytes}",
    )

    first_turn_tools = {tool.name for tool in model.requests[0].tools}
    second_turn_tools = {tool.name for tool in model.requests[1].tools}
    checks.require(
        "read_tool_output was not offered before there was anything to read",
        "read_tool_output" not in first_turn_tools,
        f"turn 1 tools: {sorted(first_turn_tools)}",
    )
    checks.require(
        "read_tool_output was offered on the very next turn, once the large result existed",
        "read_tool_output" in second_turn_tools,
        f"turn 2 tools: {sorted(second_turn_tools)}",
    )

    return checks.result(
        "a large result stayed whole in the log while the model worked from a preview "
        "and a handle, then read the rest back in slices",
        run_ids=[str(run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
