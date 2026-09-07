"""End to end: a model program runs in a real subprocess and calls a host tool.

DESIGN.md §23 item 9, in full: *a model program executes in the subprocess
sandbox, calls a host tool, and its traceback on failure reaches the model as
data.* All three clauses are asserted here, and the third is the one that gets
skipped in practice. A sandbox that raises on a failed program looks fine right
up until a model writes a typo and loses a customer's turn to it.

``run_code`` is a Psych built-in rather than something a consumer wires
themselves. It is in ``RESERVED_TOOL_NAMES`` and the runtime offers it only when
a ``Sandbox`` is configured, so a Run without one is not shown a tool that could
only fail. The bindings a program may call are the Spec's own tools, reached
through the same executor a direct tool call uses: DESIGN.md §18's "no
privileged back door" is a property of that routing, not a promise in a comment.

These run against a real child process. §22 names sandbox execution against a
real subprocess as a functional requirement, and a sandbox tested against a fake
proves nothing about isolation.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.sandbox.port import SandboxLimits
from psych_runtime.sandbox.subprocess import SubprocessSandbox
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=128 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _sandbox_python_bin() -> str:
    """An interpreter the sandboxed child can actually execute.

    Where the worker is root the adapter drops the child to an unprivileged
    account, and a virtualenv under a root-owned home is not traversable from
    there. A system interpreter is. This is a property of where the repository
    is checked out rather than of the sandbox, so it is resolved here rather
    than papered over by turning the privilege drop off, which is the part
    worth keeping.
    """
    for candidate in (
        f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
        shutil.which("python3"),
        sys.executable,
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("no usable interpreter for the sandboxed child")


def build_registry(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup_price(sku: str) -> int:
        """Look up a price in pence."""
        calls.append(sku)
        return {"A": 250, "B": 400}.get(sku, 0)

    return registry


def build_spec() -> AgentSpec:
    return AgentSpec(
        name="analyst",
        instructions="Work things out with code.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="lookup_price"),),
    )


async def drive(store: InMemoryStore, model: FakeModel, registry: ToolRegistry) -> Any:
    version = await psych_runtime.publish(store, build_spec())
    dispatched = await psych_runtime.dispatch(
        store, version, SCOPE, input={"message": "total A and B"}
    )
    journal = await Journal.open(store, dispatched.run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(
        store=store,
        model=model,
        registry=registry,
        # allow_same_uid because this runs on unprivileged CI runners as well as
        # a root development container. It takes effect only where there is no
        # privilege drop to make: as root, run_as defaults to "nobody" and the
        # child is a different uid regardless. What this case proves is that a
        # model's program executes, calls a host tool and returns its traceback
        # as data; the uid boundary itself is tests/functional's to prove.
        sandbox=SubprocessSandbox(
            python_bin=_sandbox_python_bin(),
            default_limits=_LIMITS,
            allow_same_uid=True,
        ),
    )
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id


class TestCodeExecution:
    async def test_a_program_runs_in_a_real_subprocess_and_returns_its_value(self) -> None:
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "print('working')\nreturn 6 * 7"})])
            .turn(text="The answer is 42.")
        )
        run_id = await drive(store, model, build_registry([]))

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        state = await psych_runtime.state(store, run_id)
        result = state.tool_results[0]
        assert result.outcome is ToolOutcome.OK
        assert result.result["ok"] is True
        assert result.result["value"] == 42
        assert "working" in result.result["stdout"]

    async def test_a_program_calls_a_host_tool_through_the_normal_path(self) -> None:
        """The binding is the same tool the model could have called directly,
        reached a different way. There is no privileged back door."""
        store = InMemoryStore()
        calls: list[str] = []
        program = (
            "a = await lookup_price(sku='A')\n"
            "b = await lookup_price(sku='B')\n"
            "return {'total': a + b}"
        )
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": program})])
            .turn(text="That comes to 650 pence.")
        )
        run_id = await drive(store, model, build_registry(calls))

        state = await psych_runtime.state(store, run_id)
        # Every binding call is a recorded tool call of its own, gated and
        # logged exactly like one the model made directly (DESIGN.md §18: no
        # privileged back door). So the log holds the two lookups *and* the
        # run_code call that made them, in that order.
        by_tool = [(r.tool, r.outcome) for r in state.tool_results]
        assert by_tool == [
            ("lookup_price", ToolOutcome.OK),
            ("lookup_price", ToolOutcome.OK),
            ("run_code", ToolOutcome.OK),
        ]
        program_result = state.tool_results[-1].result
        assert isinstance(program_result, dict)
        assert program_result["ok"] is True, program_result
        assert program_result["value"] == {"total": 650}
        # The host tool really ran, in this process, through the registry.
        assert calls == ["A", "B"]

    async def test_a_traceback_reaches_the_model_as_data(self) -> None:
        """The clause most likely to be skipped. A model shown only "it failed"
        rewrites from scratch and reproduces the mistake; one shown the traceback
        fixes the line."""
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "return 1 / 0"})])
            .turn(text="I divided by zero. Let me fix that.")
        )
        run_id = await drive(store, model, build_registry([]))

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED, (
            "a failing program must not fail the turn"
        )

        state = await psych_runtime.state(store, run_id)
        result = state.tool_results[0]
        # Recorded as a successful tool call whose result says the program
        # failed. The distinction matters: the sandbox did its job.
        assert result.outcome is ToolOutcome.OK
        assert result.result["ok"] is False
        assert "ZeroDivisionError" in result.result["traceback"]

        # "error" is now built by
        # psych_runtime.tools.guidance.sandbox_failure_guidance rather than the bare
        # SandboxFailure.message, so it carries the kind's own advice too.
        assert "division by zero" in result.result["error"]
        assert "read the traceback" in result.result["error"].lower()
        # The traceback is not folded into "error" as well: it stays its own
        # field, so the full traceback text must appear exactly once across
        # the whole payload rather than doubled between "error" and
        # "traceback".
        assert result.result["error"].count("ZeroDivisionError") == 0

        # And the model actually saw it on its next turn, with the traceback
        # appearing exactly once (only via "traceback", not doubled into
        # "error" too).
        tool_message = next(m for m in model.requests[1].messages if m.role == "tool")
        assert "ZeroDivisionError" in tool_message.content
        assert tool_message.content.count("ZeroDivisionError") == 1

    async def test_no_state_carries_between_two_programs(self) -> None:
        """One program per execution (DESIGN.md §18). A model relying on a
        variable surviving would work in a test with one call and fail in
        production with two."""
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "kept = 99\nreturn kept"})])
            .turn(tool_calls=[("run_code", {"program": "return 'kept' in dir()"})])
            .turn(text="Nothing carried over.")
        )
        run_id = await drive(store, model, build_registry([]))

        state = await psych_runtime.state(store, run_id)
        assert state.tool_results[0].result["value"] == 99
        assert state.tool_results[1].result["value"] is False

    async def test_the_model_is_told_which_bindings_it_may_call(self) -> None:
        """A model not told what it may call either invents a name and gets a
        NameError, or writes a program that cannot do anything."""
        store = InMemoryStore()
        model = FakeModel().turn(text="nothing to do")
        await drive(store, model, build_registry([]))

        run_code = next(t for t in model.requests[0].tools if t.name == "run_code")
        assert "lookup_price" in run_code.description

    async def test_a_run_without_a_sandbox_is_not_offered_run_code(self) -> None:
        """Offering a tool that can only fail is worse than not offering it."""
        store = InMemoryStore()
        version = await psych_runtime.publish(store, build_spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="w", attempt_number=1)

        model = FakeModel().turn(text="done")
        runtime = Runtime(store=store, model=model, registry=build_registry([]))
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())

        assert "run_code" not in {tool.name for tool in model.requests[0].tools}
