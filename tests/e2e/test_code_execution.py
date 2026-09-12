"""End to end: an agent with ``code_execution`` runs programs through the runtime.

DESIGN.md §23 item 9, in full: *a model program executes in the sandbox,
calls a host tool, and its traceback on failure reaches the model as data.*
Every case here goes from a published Spec through ``Runtime`` to the log and
is asserted through the projections, on whichever local backend this host
has (``local_sandbox()``), so the same file proves the same behaviour on
Linux, macOS and Windows with a real child process each time.

``run_code`` is offered only to a Spec whose ``code_execution`` is enabled,
and the terms of every execution are the intersection of the deployment's
profile, the tenant's policy and the Spec's request. The bindings a program
may call are reached through the same executor a direct tool call uses:
"no privileged back door" is a property of that routing, not a promise in
a comment.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.code_execution import IsolationLevel, OutputPreservation
from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId, WorkerId
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import (
    AgentSpec,
    CodeExecution,
    CodeExecutionLimits,
    CodeTool,
    Limits,
    ModelRef,
    OutputPolicy,
)
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.port import Sandbox, SandboxLimits
from psych_runtime.sandbox.profiles import DEFAULT_HARD_LIMITS, SandboxProfile
from psych_runtime.store.blob_memory import InMemoryBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.sandbox_service import ScriptedSandbox, scripted_result
from psych_runtime.tools.policy import Decision
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

_LIMITS = SandboxLimits(
    cpu_seconds=5.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _python_bin() -> str | None:
    """A system interpreter where the worker is root and drops privileges."""
    if sys.platform != "win32" and os.geteuid() == 0:
        for candidate in (
            f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
            "/usr/bin/python3",
        ):
            if Path(candidate).is_file():
                return candidate
    return None


@pytest.fixture(scope="module")
def sandbox() -> Sandbox:
    return local_sandbox(python_bin=_python_bin(), default_limits=_LIMITS)


def build_registry(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup_price(sku: str) -> int:
        """Look up a price in pence."""
        calls.append(sku)
        return {"A": 250, "B": 400}.get(sku, 0)

    @registry.register(annotations={"destructive"})
    def issue_refund(sku: str) -> str:
        """Refund an order."""
        calls.append(f"refund:{sku}")
        return "refunded"

    return registry


def build_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "analyst",
        "instructions": "Work things out with code.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup_price"), CodeTool(name="issue_refund")),
        # PROCESS, because that is what every host's local backend can do with
        # nothing installed; a host whose backend is ISOLATED satisfies it too.
        "code_execution": CodeExecution(isolation=IsolationLevel.PROCESS),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def drive(
    store: InMemoryStore,
    model: FakeModel,
    registry: ToolRegistry,
    sandbox: Sandbox,
    *,
    spec: AgentSpec | None = None,
    **runtime_options: Any,
) -> RunId:
    version = await psych_runtime.publish(store, spec or build_spec())
    dispatched = await psych_runtime.dispatch(
        store, version, SCOPE, input={"message": "total A and B"}
    )
    journal = await Journal.open(store, dispatched.run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(
        store=store, model=model, registry=registry, sandbox=sandbox, **runtime_options
    )
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id


def _run_code_result(state: Any) -> Any:
    return next(r for r in state.tool_results if r.tool == "run_code")


def _lf(text: str) -> str:
    """Text as the program printed it, whatever newline the platform wrote."""
    return text.replace("\r\n", "\n")


class TestCodeExecution:
    async def test_a_program_runs_in_a_real_child_and_returns_its_value(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "print('working')\nreturn 6 * 7"})])
            .turn(text="The answer is 42.")
        )
        run_id = await drive(store, model, build_registry([]), sandbox)

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        state = await psych_runtime.state(store, run_id)
        result = _run_code_result(state)
        assert result.outcome is ToolOutcome.OK
        assert result.result["ok"] is True
        assert result.result["value"] == 42
        assert "working" in result.result["stdout"]
        assert result.result["isolation"] in ("process", "isolated")
        # The enforcement report is recorded beside the result, for a person,
        # and is not something the model is offered a handle to.
        execution = next(a for a in result.attachments if a.name == "execution")
        assert execution.data is not None
        parsed = json.loads(execution.data)
        assert parsed["guarantees"]["process_tree"] == "enforced"
        # The effective caps are the profile's ceiling (the deployment default
        # when `Runtime(sandbox=...)` registered it), not the adapter's own.
        assert parsed["limits"]["wall_seconds"] == DEFAULT_HARD_LIMITS.wall_seconds
        assert not execution.readable
        assert "run_code" in {t.name for t in model.requests[0].tools}
        assert "read_tool_output" not in {t.name for t in model.requests[1].tools}

    async def test_a_program_calls_a_host_tool_through_the_normal_path(
        self, sandbox: Sandbox
    ) -> None:
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
        run_id = await drive(store, model, build_registry(calls), sandbox)

        state = await psych_runtime.state(store, run_id)
        by_tool = [(r.tool, r.outcome) for r in state.tool_results]
        assert by_tool == [
            ("lookup_price", ToolOutcome.OK),
            ("lookup_price", ToolOutcome.OK),
            ("run_code", ToolOutcome.OK),
        ]
        assert _run_code_result(state).result["value"] == {"total": 650}
        assert calls == ["A", "B"]

    async def test_a_traceback_reaches_the_model_as_data(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "return 1 / 0"})])
            .turn(text="I divided by zero. Let me fix that.")
        )
        run_id = await drive(store, model, build_registry([]), sandbox)

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED, (
            "a failing program must not fail the turn"
        )
        result = _run_code_result(await psych_runtime.state(store, run_id))
        assert result.outcome is ToolOutcome.OK
        assert result.result["ok"] is False
        assert "ZeroDivisionError" in result.result["traceback"]
        assert "division by zero" in result.result["error"]
        assert result.result["error"].count("ZeroDivisionError") == 0
        tool_message = next(m for m in model.requests[1].messages if m.role == "tool")
        assert tool_message.content.count("ZeroDivisionError") == 1

    async def test_no_state_carries_between_two_programs(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "kept = 99\nreturn kept"})])
            .turn(tool_calls=[("run_code", {"program": "return 'kept' in dir()"})])
            .turn(text="Nothing carried over.")
        )
        run_id = await drive(store, model, build_registry([]), sandbox)
        state = await psych_runtime.state(store, run_id)
        assert state.tool_results[0].result["value"] == 99
        assert state.tool_results[1].result["value"] is False

    async def test_the_model_is_told_which_bindings_it_may_call(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        model = FakeModel().turn(text="nothing to do")
        await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=build_spec(
                code_execution=CodeExecution(
                    isolation=IsolationLevel.PROCESS, bindings=("lookup_price",)
                )
            ),
        )
        run_code = next(t for t in model.requests[0].tools if t.name == "run_code")
        assert "lookup_price" in run_code.description
        assert "issue_refund" not in run_code.description


class TestGranting:
    async def test_an_agent_without_code_execution_is_not_offered_run_code(
        self, sandbox: Sandbox
    ) -> None:
        """Wiring a sandbox makes execution possible; the Spec grants it."""
        store = InMemoryStore()
        model = FakeModel().turn(text="done")
        await drive(store, model, build_registry([]), sandbox, spec=build_spec(code_execution=None))
        assert "run_code" not in {t.name for t in model.requests[0].tools}

    async def test_a_disabled_configuration_is_off_too(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        model = FakeModel().turn(text="done")
        await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=build_spec(code_execution=CodeExecution(enabled=False)),
        )
        assert "run_code" not in {t.name for t in model.requests[0].tools}

    async def test_a_run_without_any_sandbox_is_not_offered_run_code(self) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(store, build_spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="w", attempt_number=1)
        model = FakeModel().turn(text="done")
        runtime = Runtime(store=store, model=model, registry=build_registry([]))
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        # The Spec asks for a profile this Runtime does not have: a
        # configuration error, and it fails the Attempt loudly.
        with pytest.raises(psych_runtime.PsychError, match="default"):
            await runtime(journal, header, AbortSignal())

    async def test_an_unknown_profile_is_refused_at_publish_with_a_context(self) -> None:
        store = InMemoryStore()
        spec = build_spec(code_execution=CodeExecution(profile="gpu"))
        with pytest.raises(psych_runtime.SpecValidationError, match="gpu"):
            await psych_runtime.publish(
                store, spec, context=psych_runtime.ValidationContext(sandbox_profiles={"default"})
            )

    async def test_a_request_the_backend_cannot_meet_is_refused_as_data(
        self, sandbox: Sandbox
    ) -> None:
        """The default request is ISOLATED. A host whose only backend is
        PROCESS-level refuses to run the program and says why, and the Run
        carries on with its other tools rather than failing."""
        description = await sandbox.describe()
        if description.isolation is IsolationLevel.ISOLATED:
            pytest.skip("this host's local backend is isolated, so nothing is refused")
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "return 'secret'"})])
            .turn(text="Code execution is not available here.")
        )
        run_id = await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=build_spec(code_execution=CodeExecution()),
        )
        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        result = _run_code_result(await psych_runtime.state(store, run_id))
        assert result.result["ok"] is False
        assert result.result["error_type"] == "isolation_unavailable"
        assert "secret" not in json.dumps(result.result)

    async def test_different_agents_use_different_profiles(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        scripted = ScriptedSandbox(
            script={"return 'scripted'": scripted_result(value="from-the-other-profile")}
        )
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "return 'scripted'"})])
            .turn(text="done")
        )
        run_id = await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=build_spec(code_execution=CodeExecution(profile="strict")),
            sandboxes=[SandboxProfile("strict", scripted)],
        )
        result = _run_code_result(await psych_runtime.state(store, run_id))
        assert result.result["value"] == "from-the-other-profile"
        assert scripted.calls[0]["isolation"] is IsolationLevel.ISOLATED

    async def test_deployment_hard_limits_narrow_the_agents_request(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        model = (
            FakeModel().turn(tool_calls=[("run_code", {"program": "return 1"})]).turn(text="done")
        )
        spec = build_spec(
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS,
                profile="capped",
                limits=CodeExecutionLimits(wall_seconds=600.0, cpu_seconds=1.0),
            )
        )
        run_id = await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=spec,
            sandboxes=[
                SandboxProfile(
                    "capped",
                    sandbox,
                    hard_limits=_LIMITS.model_copy(update={"wall_seconds": 8.0}),
                )
            ],
        )
        result = _run_code_result(await psych_runtime.state(store, run_id))
        execution = json.loads(next(a for a in result.attachments if a.name == "execution").data)
        assert execution["limits"]["wall_seconds"] == 8.0  # the ceiling won
        assert execution["limits"]["cpu_seconds"] == 1.0  # the request narrowed
        run_code = next(t for t in model.requests[0].tools if t.name == "run_code")
        assert "8s" in run_code.description


class TestSpill:
    """Large output leaves the prompt and stays retrievable."""

    async def test_large_stdout_is_previewed_kept_in_the_blob_store_and_readable(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        blob = InMemoryBlobStore()
        program = "for i in range(5000):\n    print(f'row {i} of the report')\nreturn 'ok'"
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": program})])
            .turn(
                tool_calls=[
                    ("read_tool_output", {"handle": "__HANDLE__", "offset": 4990, "limit": 5})
                ]
            )
            .turn(tool_calls=[("read_tool_output", {"handle": "__HANDLE__", "pattern": "row 42 "})])
            .turn(text="done")
        )
        # The handle is only known once the call id is minted, so the fake
        # model substitutes it from the first result before its second turn.
        first_result: dict[str, Any] = {}
        spec = build_spec(
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS, output=OutputPolicy(preview_bytes=512)
            )
        )
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
        journal = await Journal.open(store, dispatched.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        runtime = Runtime(
            store=store,
            model=_resolving(model, first_result),
            registry=build_registry([]),
            sandbox=sandbox,
            blob=blob,
        )
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())

        state = await psych_runtime.state(store, dispatched.run_id)
        run_code = _run_code_result(state)
        payload = run_code.result
        assert payload["stdout"].startswith("row 0 of the report")
        assert "bytes omitted" in payload["stdout"]
        assert _lf(payload["stdout"]).endswith("row 4999 of the report\n")
        assert payload["stdout_bytes"] > 90_000
        handle = payload["stdout_handle"]
        assert handle.startswith(f"out_{run_code.call_id}_")
        stdout = next(a for a in run_code.attachments if a.name == "stdout")
        assert stdout.stored == "blob"
        assert stdout.size_bytes == payload["stdout_bytes"]
        assert stdout.sha256 is not None
        assert stdout.readable
        # The model read a window and searched, through the same handle.
        reads = [r for r in state.tool_results if r.tool == "read_tool_output"]
        assert len(reads) == 2
        window = reads[0].result
        assert window["total_lines"] == 5001
        assert window["content"].splitlines()[0] == "row 4990 of the report"
        search = reads[1].result
        assert search["total_matches"] == 1
        assert search["matches"][0]["text"].rstrip("\r") == "row 42 of the report"
        # The whole capture is in the blob, byte for byte.
        report = await psych_runtime.report(store, dispatched.run_id)
        call = next(c for c in report.tool_calls if c.tool == "run_code")
        assert call.attachments[0].handle == handle
        assert "read_tool_output" in {t.name for t in model.requests[1].tools}

    async def test_required_preservation_without_a_blob_store_fails_the_call(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        program = "print('x' * 20000)\nreturn 1"
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="done")
        spec = build_spec(
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS,
                output=OutputPolicy(preview_bytes=512, preserve=OutputPreservation.REQUIRED),
            )
        )
        run_id = await drive(store, model, build_registry([]), sandbox, spec=spec)
        result = _run_code_result(await psych_runtime.state(store, run_id))
        assert result.outcome is ToolOutcome.ERROR
        assert result.failure is not None
        assert result.failure.kind == "blob_store_required"

    async def test_without_a_blob_store_the_rest_is_dropped_and_the_payload_says_so(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        program = "print('x' * 20000)\nreturn 1"
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="done")
        spec = build_spec(
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS, output=OutputPolicy(preview_bytes=512)
            )
        )
        run_id = await drive(store, model, build_registry([]), sandbox, spec=spec)
        result = _run_code_result(await psych_runtime.state(store, run_id))
        assert result.outcome is ToolOutcome.OK
        payload = result.result
        assert "stdout_handle" not in payload
        assert payload["stdout_omitted_bytes"] >= 20_001  # a newline, of either width
        stdout = next(a for a in result.attachments if a.name == "stdout")
        assert stdout.stored == "preview_only"
        assert not stdout.readable
        # Nothing to read, so the model is not offered the reader.
        assert "read_tool_output" not in {t.name for t in model.requests[1].tools}

    async def test_a_forged_or_foreign_handle_cannot_be_read(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        blob = InMemoryBlobStore()
        program = "print('x' * 20000)\nreturn 1"
        spec = build_spec(
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS, output=OutputPolicy(preview_bytes=512)
            )
        )
        first_model = (
            FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="a")
        )
        first = await drive(store, first_model, build_registry([]), sandbox, spec=spec, blob=blob)
        stolen = _run_code_result(await psych_runtime.state(store, first)).result["stdout_handle"]

        from psych_runtime.tools.large_results import read_tool_output_async

        other = await psych_runtime.state(store, first)
        with pytest.raises(AccessDenied):
            await read_tool_output_async(other, {"handle": "out_call_forged_stdout"}, blob)
        # A second Run cannot resolve the first Run's handle either.
        second_model = FakeModel().turn(text="b")
        second = await drive(store, second_model, build_registry([]), sandbox, spec=spec, blob=blob)
        with pytest.raises(AccessDenied):
            await read_tool_output_async(
                await psych_runtime.state(store, second), {"handle": stolen}, blob
            )

    async def test_artifacts_come_back_by_relative_path(self, sandbox: Sandbox) -> None:
        store = InMemoryStore()
        program = "with open('report.csv', 'w') as f:\n    f.write('a,b\\n1,2\\n')\nreturn 'ok'"
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="done")
        run_id = await drive(store, model, build_registry([]), sandbox)
        result = _run_code_result(await psych_runtime.state(store, run_id))
        listed = result.result["artifacts"]
        assert listed[0]["path"] == "report.csv"
        assert listed[0]["content_type"] == "text/csv"
        assert listed[0]["handle"].startswith("out_")
        attached = next(a for a in result.attachments if a.name == "file:report.csv")
        assert attached.data.replace(b"\r\n", b"\n") == b"a,b\n1,2\n"
        assert attached.stored == "inline"
        assert "\\" not in listed[0]["path"]
        assert not listed[0]["path"].startswith("/")


def _resolving(model: FakeModel, seen: dict[str, Any]) -> FakeModel:
    """Wrap a scripted model so ``__HANDLE__`` becomes the real handle.

    The handle is minted with the call id at record time, so a script written
    ahead of the Run cannot know it. This substitutes it from the tool result
    the model was shown, which is exactly what a real model would read.
    """
    original = model.stream

    def stream(request: Any) -> Any:
        for message in request.messages:
            content = getattr(message, "content", "")
            if getattr(message, "role", None) == "tool" and "stdout_handle" in content:
                seen.update(json.loads(content))
        handle = seen.get("stdout_handle")
        if handle is not None:
            for turn in model._script:
                for call in getattr(turn, "tool_calls", ()):
                    arguments = call.arguments
                    if isinstance(arguments, dict) and arguments.get("handle") == "__HANDLE__":
                        arguments["handle"] = handle
        return original(request)

    model.stream = stream  # type: ignore[method-assign]
    return model


class TestPolicyAndApprovals:
    async def test_a_binding_the_policy_denies_fails_inside_the_program(
        self, sandbox: Sandbox
    ) -> None:
        class NoRefunds:
            async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
                if tool == "issue_refund":
                    return Decision.deny("refunds are off")
                return Decision.allow()

            async def allow_run(self, scope: Scope, version: Any) -> Decision:
                return Decision.allow()

        store = InMemoryStore()
        calls: list[str] = []
        program = (
            "try:\n"
            "    await issue_refund(sku='A')\n"
            "except Exception as err:\n"
            "    return f'refused: {err}'\n"
            "return 'refunded'"
        )
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="done")
        run_id = await drive(store, model, build_registry(calls), sandbox, policy=NoRefunds())
        state = await psych_runtime.state(store, run_id)
        assert _run_code_result(state).result["value"].startswith("refused:")
        refund = next(r for r in state.tool_results if r.tool == "issue_refund")
        assert refund.outcome is ToolOutcome.ERROR
        assert refund.failure is not None
        assert refund.failure.kind == "access_denied"
        assert calls == []

    async def test_a_binding_that_needs_approval_is_refused_inside_the_program(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        calls: list[str] = []
        program = (
            "try:\n"
            "    await issue_refund(sku='A')\n"
            "except Exception as err:\n"
            "    return f'refused: {err}'\n"
            "return 'refunded'"
        )
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})]).turn(text="done")
        run_id = await drive(
            store,
            model,
            build_registry(calls),
            sandbox,
            approval_selectors=("destructive",),
        )
        state = await psych_runtime.state(store, run_id)
        assert "approval" in _run_code_result(state).result["value"]
        refund = next(r for r in state.tool_results if r.tool == "issue_refund")
        assert refund.failure is not None
        assert refund.failure.kind == "approval_required_in_program"
        assert calls == []
        assert state.terminal_state is TerminalState.COMPLETED


class TestDurability:
    async def test_a_worker_that_dies_mid_program_does_not_replay_it(self) -> None:
        """``run_code`` is never safe to retry: a crash mid-program settles the
        call unknown and the next Worker does not run it again."""
        store = InMemoryStore()
        spec = build_spec()
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
        first = await Journal.open(store, run.run_id, SCOPE)
        await first.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
        await first.append(type="turn_started", turn=1)
        await first.append(type="model_call_started", turn=1, model="fake-standard")
        await first.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 10},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await first.append(
            type="tool_call_started",
            call_id="call-1",
            tool="run_code",
            arguments={"program": "await issue_refund(sku='A')"},
            turn=1,
        )
        await store.release(run.run_id, WorkerId("wrk_dead"), RunState.RUNNABLE)

        calls: list[str] = []
        scripted = ScriptedSandbox()
        model = FakeModel().turn(text="I could not confirm the program ran.")
        runtime = Runtime(
            store=store, model=model, registry=build_registry(calls), sandbox=scripted
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _wait_for_settled(store, run.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = await psych_runtime.state(store, run.run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.tool_results[0].outcome is ToolOutcome.UNKNOWN
        assert scripted.calls == []
        assert calls == []

    async def test_an_interrupt_during_a_program_kills_it_and_aborts_the_run(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        started = asyncio.Event()
        registry = ToolRegistry()

        @registry.register
        async def tick() -> str:
            """Signal that the program is running."""
            started.set()
            return "tick"

        # The loop observes an interrupt at the tool-call boundary, so a
        # program still running when one arrives ends at its own wall clock
        # at the latest; the abort then lands and the Run settles. A short
        # wall clock here proves that without a long wait.
        spec = build_spec(
            tools=(CodeTool(name="tick"),),
            code_execution=CodeExecution(
                isolation=IsolationLevel.PROCESS, limits=CodeExecutionLimits(wall_seconds=3.0)
            ),
        )
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
        program = "await tick()\nimport time\ntime.sleep(60)\nreturn 'never'"
        model = FakeModel().turn(tool_calls=[("run_code", {"program": program})])
        runtime = Runtime(store=store, model=model, registry=registry, sandbox=sandbox)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        began = datetime.now(UTC)
        try:
            await asyncio.wait_for(started.wait(), timeout=20)
            await psych_runtime.interrupt(store, run.run_id, reason="stop")
            await _wait_for_settled(store, run.run_id, timeout=20)
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)
        state = await psych_runtime.state(store, run.run_id)
        assert state.terminal_state is TerminalState.ABORTED
        assert datetime.now(UTC) - began < timedelta(seconds=30)
        assert not state.has_dangling_tool_calls
        assert _run_code_result(state).result["limit_hit"] == "wall_seconds"


class TestProjections:
    async def test_report_status_answer_and_thread_all_see_the_execution(
        self, sandbox: Sandbox
    ) -> None:
        store = InMemoryStore()
        model = (
            FakeModel()
            .turn(tool_calls=[("run_code", {"program": "print('hi')\nreturn 42"})])
            .turn(text="It is 42.")
        )
        run_id = await drive(
            store,
            model,
            build_registry([]),
            sandbox,
            spec=build_spec(limits=Limits(large_result_bytes=32_768)),
        )
        report = await psych_runtime.report(store, run_id, scope=SCOPE)
        call = next(c for c in report.tool_calls if c.tool == "run_code")
        assert call.outcome is ToolOutcome.OK
        assert [a.name for a in call.attachments] == ["execution"]
        status = await psych_runtime.status(store, run_id, scope=SCOPE)
        assert status.tool_calls == 1
        answer = await psych_runtime.answer(store, run_id, scope=SCOPE)
        assert answer.text == "It is 42."
        assert answer.work[0].tool_calls[0].tool == "run_code"
        thread = await psych_runtime.thread(store, run_id, scope=SCOPE)
        assert any(m.tool_name == "run_code" for m in thread.messages)


async def _wait_for_settled(store: InMemoryStore, run_id: RunId, timeout: float = 5.0) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        records = await store.read(run_id)
        if records and reduce(records).settled:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the Run did not settle in time")
