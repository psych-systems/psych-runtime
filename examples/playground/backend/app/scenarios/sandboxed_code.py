"""DESIGN.md §23 item 9: a model program runs in a real sandbox, calls a host
tool through the normal path, and its traceback on failure reaches the model
as data rather than killing the turn.

Uses this host's own local backend (``psych_runtime.sandbox.local_sandbox``):
a real child process on Linux, macOS and Windows alike, at whatever isolation
level the host reaches with nothing installed. The agent asks for
``process``-level isolation, which every host can provide; the result's own
report says what was actually achieved, and the scenario records it rather
than asserting a level this host may not have.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.port import Sandbox, SandboxLimits, SandboxSetupError
from psych_runtime.sandbox.profiles import SandboxProfile
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="sandboxed-code",
    title="Sandboxed code execution",
    proves=(
        "A model program runs in a real child process on this host, calls a host tool "
        "through the same path a direct tool call uses, and a program that raises has "
        "its traceback handed back to the model as data instead of failing the turn."
    ),
    design_ref="§23.9",
    requires=(),
)

_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _sandbox() -> Sandbox | None:
    """This host's local backend, or ``None`` when it cannot be built here."""
    try:
        return local_sandbox(default_limits=_LIMITS, allow_same_uid=True)
    except SandboxSetupError:
        return None


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def lookup_price(sku: str) -> int:
        """Look up a price in pence."""
        return {"A": 250, "B": 400}.get(sku, 0)

    return registry


def _spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="analyst",
        instructions="Work things out with code.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup_price"),),
        code_execution=psych_runtime.CodeExecution(isolation=psych_runtime.IsolationLevel.PROCESS),
    )


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    if _sandbox() is None:
        return "no local sandbox backend can be built on this host"
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    sandbox = _sandbox()
    assert sandbox is not None  # guaranteed by check_availability
    store = InMemoryStore()
    description = await sandbox.describe()
    await emit(
        "describe",
        f"{description.backend} on {description.platform}: "
        f"{description.isolation.value if description.isolation else 'no'} isolation",
    )

    await emit("run", "a program that calls a host tool through the sandbox's normal path")
    program = (
        "a = await lookup_price(sku='A')\nb = await lookup_price(sku='B')\nreturn {'total': a + b}"
    )
    model = (
        FakeModel()
        .turn(tool_calls=[("run_code", {"program": program})])
        .turn(text="That comes to 650 pence.")
    )
    run_id = await _drive(store, _spec(), model, _registry(), sandbox, message="add up A and B")

    state = await psych_runtime.state(store, run_id)
    result = next(r for r in state.tool_results if r.tool == "run_code")
    checks.require(
        "the program ran and returned its computed value",
        result.outcome is ToolOutcome.OK and result.result.get("value") == {"total": 650},
        f"outcome={result.outcome}, result={result.result!r}",
    )
    checks.require(
        "the host tool ran in this process, through the ordinary registry path, twice",
        [r.tool for r in state.tool_results[:2]] == ["lookup_price", "lookup_price"],
        f"tool calls in order: {[r.tool for r in state.tool_results]}",
    )
    checks.require(
        "the execution reported the isolation it achieved rather than assuming one",
        result.result.get("isolation") in ("process", "isolated"),
        f"isolation={result.result.get('isolation')!r}",
    )

    await emit("traceback", "a program that fails, and the traceback reaching the model as data")
    failing_model = (
        FakeModel()
        .turn(tool_calls=[("run_code", {"program": "return 1 / 0"})])
        .turn(text="I divided by zero. Let me fix that.")
    )
    failing_run_id = await _drive(
        store, _spec(), failing_model, _registry(), sandbox, message="divide"
    )

    failing_report = await psych_runtime.report(store, failing_run_id)
    terminal = failing_report.terminal_state.value if failing_report.terminal_state else None
    checks.require(
        "a program that raises does not fail the turn",
        failing_report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={terminal!r}",
    )
    failing_state = await psych_runtime.state(store, failing_run_id)
    failing_result = next(r for r in failing_state.tool_results if r.tool == "run_code")
    checks.require(
        "the sandbox reports the program's own failure rather than raising one of its own",
        failing_result.outcome is ToolOutcome.OK and failing_result.result.get("ok") is False,
        f"outcome={failing_result.outcome}, result.ok={failing_result.result.get('ok')!r}",
    )
    checks.require(
        "the actual traceback -- not just 'it failed' -- reached the model on its next turn",
        "ZeroDivisionError" in failing_result.result.get("traceback", ""),
        f"traceback={failing_result.result.get('traceback', '')!r}",
    )
    tool_message = next(m for m in failing_model.requests[1].messages if m.role == "tool")
    checks.require(
        "the model's own next request carried that traceback, not a paraphrase",
        "ZeroDivisionError" in tool_message.content,
        f"tool message content={tool_message.content!r}",
    )

    log = await psych_runtime.records(store, run_id)
    failing_log = await psych_runtime.records(store, failing_run_id)
    return checks.result(
        "a sandboxed program called a host tool and returned its value; a failing one "
        "handed its traceback back as data instead of failing the turn",
        run_ids=[str(run_id), str(failing_run_id)],
        report={
            "backend": description.model_dump(mode="json"),
            "arithmetic_run": {"records": records_dict(log)},
            "traceback_run": {
                "report": report_dict(failing_report),
                "records": records_dict(failing_log),
            },
        },
    )


async def _drive(
    store: InMemoryStore,
    spec: psych_runtime.AgentSpec,
    model: FakeModel,
    registry: ToolRegistry,
    sandbox: Sandbox,
    *,
    message: str,
) -> RunId:
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": message})
    journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(
        store=store,
        model=model,
        registry=registry,
        sandboxes=[SandboxProfile("default", sandbox, hard_limits=_LIMITS)],
    )
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id
