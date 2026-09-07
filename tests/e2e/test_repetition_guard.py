"""A model repeating a call that works, and the three cases that must not trip.

Found against a real provider: one question, one working tool, and
thirty-two calls to it with byte-identical arguments, every one returning ``ok``.
The Run ended ``failed`` with ``budget_exhausted`` after 31,120 input tokens.
The failure-streak guard never advanced, correctly, because nothing failed.

The hard part is not catching that. It is catching it without breaking the
three shapes of repetition that are ordinary and useful, which is why they get
as much room here as the loop does: a guard that stopped a model polling a job
would be worse than the loop it fixed.
"""

from __future__ import annotations

import pytest

import psych_runtime
from psych_runtime.core.records import TerminalState
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = psych_runtime.Scope(tenant="acme")


async def _run(
    spec: psych_runtime.AgentSpec, model: FakeModel, registry: ToolRegistry
) -> psych_runtime.RunReport:
    import asyncio

    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    worker = Worker(
        store,
        Runtime(store=store, model=model, registry=registry),
        poll_interval=0.01,
        supervisor_interval=0.05,
    )
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
        return await psych_runtime.report(store, run.run_id)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)


def _spec(**limits: object) -> psych_runtime.AgentSpec:
    settings: dict[str, object] = {"max_turns": 20, "deadline_seconds": 60.0}
    settings.update(limits)
    return psych_runtime.AgentSpec(
        name="support",
        instructions="use the tools",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup"),),
        limits=psych_runtime.Limits(**settings),  # type: ignore[arg-type]
    )


class TestTheLoopIsStopped:
    async def test_an_identical_call_is_refused_once_it_has_taught_nothing(self) -> None:
        registry = ToolRegistry()
        calls: list[str] = []

        @registry.register
        async def lookup(order_id: str) -> dict[str, str]:
            """Look up an order."""
            calls.append(order_id)
            return {"order_id": order_id, "status": "shipped"}

        model = FakeModel()
        for _ in range(8):
            model = model.turn(tool_calls=[("lookup", {"order_id": "A1"})])
        model = model.turn(text="It shipped.")

        report = await _run(_spec(), model, registry)

        assert len(calls) == 2, (
            "the tool should stop actually running once the third identical call "
            f"would return the same answer again; it ran {len(calls)} times"
        )
        refusals = [
            call
            for call in report.tool_calls
            if call.failure is not None and call.failure.kind == "repeated_call"
        ]
        assert refusals, "the repeated calls should be recorded as refused, not silently dropped"
        assert "already have this answer" in (
            refusals[0].failure.message if refusals[0].failure else ""
        )

    async def test_the_run_does_not_burn_its_whole_turn_budget(self) -> None:
        """The symptom that made this worth fixing was the bill, not the tidiness."""
        registry = ToolRegistry()

        @registry.register
        async def lookup(order_id: str) -> str:
            """Look up an order."""
            return "shipped"

        model = FakeModel()
        for _ in range(30):
            model = model.turn(tool_calls=[("lookup", {"order_id": "A1"})])
        model = model.turn(text="done")

        report = await _run(_spec(max_turns=20, repeat_call_hard_stop=6), model, registry)

        assert len(report.model_calls) < 20, (
            "the Run should stop looping well before max_turns; it used "
            f"{len(report.model_calls)} turns"
        )


class TestLegitimateRepetitionIsNotTouched:
    """Each of these is the same tool called repeatedly and each is fine."""

    async def test_polling_until_the_answer_changes(self) -> None:
        registry = ToolRegistry()
        answers = iter(["pending", "pending", "pending", "pending", "ready"])

        @registry.register
        async def lookup(job_id: str) -> str:
            """Poll a job."""
            return next(answers, "ready")

        model = FakeModel()
        for _ in range(5):
            model = model.turn(tool_calls=[("lookup", {"job_id": "J1"})])
        model = model.turn(text="ready")

        report = await _run(
            _spec(repeat_call_threshold=6, repeat_call_hard_stop=12), model, registry
        )

        assert report.terminal_state is TerminalState.COMPLETED
        refused = [
            c
            for c in report.tool_calls
            if c.failure is not None and c.failure.kind == "repeated_call"
        ]
        assert not refused, f"polling was refused: {[c.failure for c in refused]}"

    async def test_paginating_with_a_changing_cursor(self) -> None:
        registry = ToolRegistry()

        @registry.register
        async def lookup(cursor: str) -> str:
            """Page through a list."""
            return f"page after {cursor}"

        model = FakeModel()
        for page in range(6):
            model = model.turn(tool_calls=[("lookup", {"cursor": f"p{page}"})])
        model = model.turn(text="done")

        report = await _run(_spec(), model, registry)

        refused = [
            c
            for c in report.tool_calls
            if c.failure is not None and c.failure.kind == "repeated_call"
        ]
        assert not refused, "changing arguments is not repetition"
        assert report.terminal_state is TerminalState.COMPLETED

    async def test_the_same_tool_for_many_different_entities(self) -> None:
        registry = ToolRegistry()

        @registry.register
        async def lookup(order_id: str) -> str:
            """Look up an order."""
            return f"status of {order_id}"

        model = FakeModel()
        for order in ("A1", "B2", "C3", "D4", "E5", "F6"):
            model = model.turn(tool_calls=[("lookup", {"order_id": order})])
        model = model.turn(text="done")

        report = await _run(_spec(), model, registry)

        refused = [
            c
            for c in report.tool_calls
            if c.failure is not None and c.failure.kind == "repeated_call"
        ]
        assert not refused, "six different orders is six different questions"
        assert report.terminal_state is TerminalState.COMPLETED
