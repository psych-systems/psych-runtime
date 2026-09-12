"""``run_code``'s payload: what the model sees, and what is kept beside it.

DESIGN.md §18 and §10.8. Everything here drives ``make_run_code`` with a
stand-in sandbox and inspects the ``CodeExecutionOutcome`` it returns: the
compact payload, the deterministic previews, and the attachments the agent
loop stores. No process is spawned.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from psych_runtime.core.code_execution import (
    Enforcement,
    IsolationLevel,
    OutputPreservation,
)
from psych_runtime.sandbox.port import (
    SandboxArtifact,
    SandboxFailure,
    SandboxGuarantees,
    SandboxLimit,
    SandboxResult,
)
from psych_runtime.tools.code import (
    SLOT_PLACEHOLDER,
    CodeExecutionOutcome,
    attachment_handle,
    make_run_code,
    preview_bytes,
    run_code_definition,
)

pytestmark = pytest.mark.unit


class _Sandbox:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run(self, program: str, **options: Any) -> SandboxResult:
        self.calls.append({"program": program, **options})
        return self.result


def _result(**kwargs: Any) -> SandboxResult:
    base: dict[str, Any] = {
        "duration_seconds": 0.25,
        "isolation": IsolationLevel.ISOLATED,
        "network_denied": True,
        "guarantees": SandboxGuarantees(
            filesystem=Enforcement.ENFORCED,
            network=Enforcement.ENFORCED,
            process_tree=Enforcement.ENFORCED,
        ),
    }
    base.update(kwargs)
    if "stdout" in base and "stdout_data" not in base:
        base["stdout_data"] = base["stdout"].encode("utf-8")
        base["stdout_size"] = len(base["stdout_data"])
    if "stderr" in base and "stderr_data" not in base:
        base["stderr_data"] = base["stderr"].encode("utf-8")
        base["stderr_size"] = len(base["stderr_data"])
    return SandboxResult(**base)


async def _run(result: SandboxResult, **options: Any) -> CodeExecutionOutcome:
    executor = make_run_code(_Sandbox(result), **options)
    return await executor({"program": "return 1"})


class TestTheDefinition:
    def test_it_is_compact_and_names_the_bindings(self) -> None:
        definition = run_code_definition(["refund", "lookup"], wall_seconds=30.0)
        assert definition.name == "run_code"
        assert "`lookup(...)`, `refund(...)`" in definition.description
        assert "30s" in definition.description
        assert "No network" in definition.description
        assert len(definition.description) < 1_200
        assert definition.input_schema["required"] == ["program"]

    def test_it_says_when_network_is_available(self) -> None:
        assert "Network is available" in run_code_definition([], network=True).description


class TestThePayload:
    async def test_a_small_result_is_inline_and_carries_no_handles(self) -> None:
        outcome = await _run(_result(value={"total": 650}, stdout="hi\n"))
        payload = outcome.payload
        assert payload["ok"] is True
        assert payload["value"] == {"total": 650}
        assert payload["stdout"] == "hi\n"
        assert "stdout_handle" not in payload
        assert payload["isolation"] == "isolated"
        assert "network_denied" not in payload  # only stated when false
        assert "stderr" not in payload
        # Only the execution report is attached, and the model may not read it.
        assert [a.slot for a in outcome.attachments] == ["execution"]

    async def test_the_enforcement_report_is_recorded_not_shown(self) -> None:
        outcome = await _run(_result(value=1))
        report = next(a for a in outcome.attachments if a.slot == "execution")
        parsed = json.loads(report.data)
        assert parsed["isolation"] == "isolated"
        assert parsed["guarantees"]["filesystem"] == "enforced"
        assert "guarantees" not in outcome.payload

    async def test_a_large_stream_is_previewed_head_and_tail_and_attached(self) -> None:
        text = "".join(f"line {i}\n" for i in range(2000))
        outcome = await _run(_result(value=1, stdout=text), preview_bytes_budget=1_000)
        payload = outcome.payload
        assert payload["stdout"].startswith("line 0\n")
        assert payload["stdout"].endswith("line 1999\n")
        assert "bytes omitted" in payload["stdout"]
        assert len(payload["stdout"].encode("utf-8")) < 1_100
        assert payload["stdout_bytes"] == len(text.encode("utf-8"))
        assert payload["stdout_handle"] == f"{SLOT_PLACEHOLDER}stdout"
        stdout = next(a for a in outcome.attachments if a.slot == "stdout")
        assert stdout.data == text.encode("utf-8")
        assert stdout.content_type.startswith("text/plain")
        assert stdout.must_keep is False

    async def test_a_capture_truncation_is_stated_separately_from_the_preview(self) -> None:
        outcome = await _run(
            _result(value=1, stdout="x" * 100, stdout_size=5_000, stdout_truncated=True),
            preview_bytes_budget=4_000,
        )
        payload = outcome.payload
        assert payload["stdout_truncated"] is True
        assert payload["stdout"] == "x" * 100
        assert "stdout_handle" not in payload

    async def test_binary_output_is_described_not_rendered(self) -> None:
        data = b"\x00\x01\x02" * 100
        outcome = await _run(_result(value=None, stdout_data=data, stdout_size=len(data)))
        payload = outcome.payload
        assert payload["stdout"] == f"[binary output, {len(data)} bytes]"
        assert payload["stdout_handle"] == f"{SLOT_PLACEHOLDER}stdout"
        stdout = next(a for a in outcome.attachments if a.slot == "stdout")
        assert stdout.content_type == "application/octet-stream"

    async def test_a_large_value_is_previewed_and_attached_as_json(self) -> None:
        value = [{"i": i, "text": "x" * 50} for i in range(500)]
        outcome = await _run(_result(value=value), preview_bytes_budget=2_000)
        payload = outcome.payload
        assert "value" not in payload
        assert payload["value_handle"] == f"{SLOT_PLACEHOLDER}value"
        assert payload["value_bytes"] > 2_000
        assert payload["value_preview"].startswith('[{"i":0')
        attached = next(a for a in outcome.attachments if a.slot == "value")
        assert json.loads(attached.data) == value
        assert attached.content_type == "application/json"

    async def test_preservation_required_marks_every_attachment_must_keep(self) -> None:
        outcome = await _run(
            _result(value=1, stdout="y" * 5_000),
            preview_bytes_budget=1_000,
            preserve=OutputPreservation.REQUIRED,
        )
        stdout = next(a for a in outcome.attachments if a.slot == "stdout")
        assert stdout.must_keep is True
        assert outcome.preserve is OutputPreservation.REQUIRED

    async def test_artifacts_are_listed_by_relative_path_with_handles(self) -> None:
        artifacts = (
            SandboxArtifact(
                path="out/report.csv", size_bytes=5, data=b"a,b\n1", content_type="text/csv"
            ),
            SandboxArtifact(path="big.bin", size_bytes=10, data=b"12345", truncated=True),
        )
        outcome = await _run(_result(value=1, artifacts=artifacts, artifacts_omitted=2))
        listed = outcome.payload["artifacts"]
        assert listed[0] == {
            "path": "out/report.csv",
            "bytes": 5,
            "content_type": "text/csv",
            "handle": f"{SLOT_PLACEHOLDER}file1",
        }
        assert listed[1]["truncated"] is True
        assert outcome.payload["artifacts_omitted"] == 2
        files = [a for a in outcome.attachments if a.slot.startswith("file")]
        assert [a.name for a in files] == ["file:out/report.csv", "file:big.bin"]

    async def test_a_failure_carries_guidance_and_a_capped_traceback_once(self) -> None:
        failure = SandboxFailure(
            kind="exception",
            message="division by zero",
            traceback="Traceback...\nZeroDivisionError: division by zero\n",
        )
        outcome = await _run(_result(failure=failure, limit_hit=None))
        payload = outcome.payload
        assert payload["ok"] is False
        assert payload["error_type"] == "exception"
        assert "division by zero" in payload["error"]
        assert "ZeroDivisionError" not in payload["error"]
        assert "ZeroDivisionError" in payload["traceback"]
        assert "value" not in payload

    async def test_a_limit_hit_and_cancellation_are_named(self) -> None:
        outcome = await _run(
            _result(
                failure=SandboxFailure(kind="timeout", message="too slow"),
                limit_hit=SandboxLimit.WALL_SECONDS,
                cancelled=True,
            )
        )
        assert outcome.payload["limit_hit"] == "wall_seconds"
        assert outcome.payload["cancelled"] is True

    async def test_an_unverified_result_says_so(self) -> None:
        outcome = await _run(SandboxResult(duration_seconds=0.1, value=1))
        assert outcome.payload["isolation"] == "unverified"
        assert outcome.payload["network_denied"] is False


class TestRefusalsAndArguments:
    async def test_a_refusal_is_returned_without_touching_the_sandbox(self) -> None:
        executor = make_run_code(None, refusal=("isolation_unavailable", "not here"))
        outcome = await executor({"program": "return 1"})
        assert outcome.payload["ok"] is False
        assert outcome.payload["error_type"] == "isolation_unavailable"
        assert "not here" in outcome.payload["error"]
        assert outcome.attachments == ()

    async def test_a_missing_program_is_an_argument_error(self) -> None:
        sandbox = _Sandbox(_result(value=1))
        outcome = await make_run_code(sandbox)({"program": "   "})
        assert outcome.payload["error_type"] == "invalid_arguments"
        assert sandbox.calls == []

    async def test_the_plan_is_passed_through_to_the_sandbox(self) -> None:
        sandbox = _Sandbox(_result(value=1))

        async def host_call(name: str, arguments: dict[str, Any]) -> Any:
            return {"called": name, **arguments}

        executor = make_run_code(
            sandbox,
            host_call,
            binding_names=["lookup"],
            limits="LIMITS",
            network=True,
            isolation=IsolationLevel.PROCESS,
            capture="CAPTURE",
        )
        await executor({"program": "return 1"})
        call = sandbox.calls[0]
        assert call["limits"] == "LIMITS"
        assert call["network"] is True
        assert call["isolation"] is IsolationLevel.PROCESS
        assert call["capture"] == "CAPTURE"
        assert set(call["bindings"]) == {"lookup"}
        # A binding routes through host_call with its own name and arguments.
        assert await call["bindings"]["lookup"]({"sku": "A"}) == {"called": "lookup", "sku": "A"}

    async def test_without_a_host_call_no_bindings_are_built(self) -> None:
        sandbox = _Sandbox(_result(value=1))
        await make_run_code(sandbox, binding_names=["lookup"])({"program": "return 1"})
        assert sandbox.calls[0]["bindings"] == {}

    async def test_a_legacy_sandbox_gets_only_the_three_original_options(self) -> None:
        sandbox = _Sandbox(_result(value=1))
        executor = make_run_code(sandbox, isolation=IsolationLevel.PROCESS, legacy_signature=True)
        await executor({"program": "return 1"})
        assert set(sandbox.calls[0]) == {"program", "bindings", "limits", "network"}


class TestPreviews:
    def test_a_fit_is_returned_whole(self) -> None:
        text, fits, binary = preview_bytes(b"hello", 100)
        assert (text, fits, binary) == ("hello", True, False)

    def test_head_and_tail_are_deterministic_and_never_split_a_character(self) -> None:
        data = ("é" * 5_000).encode("utf-8")
        text, fits, binary = preview_bytes(data, 1_000)
        assert not fits
        assert not binary
        assert "�" not in text
        assert "bytes omitted" in text
        assert preview_bytes(data, 1_000) == (text, fits, binary)

    def test_binary_is_not_previewed(self) -> None:
        text, fits, binary = preview_bytes(b"\x00" * 50, 1_000)
        assert (text, fits, binary) == ("", False, True)

    def test_handles_fold_in_the_call_id_and_slot(self) -> None:
        assert attachment_handle("call_1", "stdout") == "out_call_1_stdout"
        assert attachment_handle("call_1", "stdout") != attachment_handle("call_2", "stdout")
