"""DESIGN.md §23 item 9: a model program runs in the subprocess sandbox,
calls a host tool through the normal path, and its traceback on failure
reaches the model as data rather than killing the turn.

Uses ``SubprocessSandbox`` -- a real child process, not the container
backend. DESIGN.md §22 names sandbox execution against a real subprocess as
a functional requirement in its own right, and the container backend is a
separate, heavier adapter this repository's own environment cannot run at
all (no container runtime here; its tests skip locally and run in CI). This
scenario proves the sandbox port's contract with the cheaper of its two real
adapters, not a fake one.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.sandbox.port import SandboxLimits
from psych_runtime.sandbox.subprocess import SubprocessSandbox
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="sandboxed-code",
    title="Sandboxed code execution",
    proves=(
        "A model program runs in a real subprocess, calls a host tool "
        "through the same path a direct tool call uses, and a program that "
        "raises has its traceback handed back to the model as data instead "
        "of failing the turn."
    ),
    design_ref="§23.9",
    requires=(),
)

_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=128 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _sandbox_python_bin() -> str | None:
    """A system interpreter the sandboxed child can actually execute.

    The adapter drops the child to an unprivileged account, and a virtualenv
    under a root-owned home is not traversable from there -- a system
    interpreter is. ``None`` when nothing usable is found, which
    ``check_availability`` turns into an honest unavailable rather than a
    confusing failure mid-run.
    """
    for candidate in (
        f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
        shutil.which("python3"),
        sys.executable,
    ):
        if candidate and Path(candidate).is_file():
            return candidate
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
    )


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    if _sandbox_python_bin() is None:
        return "no usable Python interpreter found for the sandboxed child process"
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    python_bin = _sandbox_python_bin()
    assert python_bin is not None  # guaranteed by check_availability
    store = InMemoryStore()
    sandbox = SubprocessSandbox(python_bin=python_bin, default_limits=_LIMITS)

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
    result = state.tool_results[0]
    checks.require(
        "the program ran and returned its computed value",
        result.outcome is ToolOutcome.OK and result.result.get("value") == {"total": 650},
        f"outcome={result.outcome}, result={result.result!r}",
    )
    checks.require(
        "the host tool actually ran, in this process, through the ordinary registry path",
        result.result.get("ok") is True and "650" not in program,  # the sandbox did the math
        f"program never contained the answer; value={result.result.get('value')!r}",
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
    failing_result = failing_state.tool_results[0]
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
    sandbox: SubprocessSandbox,
    *,
    message: str,
) -> RunId:
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": message})
    journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=registry, sandbox=sandbox)
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id
