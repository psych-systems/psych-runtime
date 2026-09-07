"""End to end: a real Run through the agent loop, reported through
``psych_runtime.report.build.build_report``.

DESIGN.md §23 item 7: ``psych_runtime.report()`` gives
correct token totals split by cache state, correct cost, and a latency
breakdown that accounts for the Run's wall-clock time. This drives a real
``AgentLoop`` against ``FakeModel`` and ``InMemoryStore`` exactly the way
``tests/e2e/test_agent_run.py`` does, then asserts everything through the
report rather than through internal state, because the report is what a
consumer actually reads.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.ids import RunId, new_run_id
from psych_runtime.core.messages import SystemMessage
from psych_runtime.core.records import RECORD_ADAPTER, ModelCallStarted, TerminalState, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.core.version import publish
from psych_runtime.model.pricing import ModelPrice, StaticPriceTable
from psych_runtime.report.build import build_report
from psych_runtime.runtime.agent import AgentLoop, ToolExecutor
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

_PRICE = ModelPrice(
    input=Decimal("1"),
    output=Decimal("2"),
    cache_read=Decimal("0.1"),
    cache_write=Decimal("1.25"),
    cache_write_1h=Decimal("2"),
)


def _spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer with their order.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> dict[str, str]:
        """Look up an order by id."""
        return {"order_id": order_id, "status": "shipped"}

    return registry


async def _start_run(store: InMemoryStore, spec: AgentSpec) -> RunId:
    version = publish(spec)
    await store.put_version(version)
    run_id = new_run_id()
    now = datetime.now(UTC)
    deadline_at = now + timedelta(seconds=spec.limits.deadline_seconds)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=SCOPE,
            version_hash=version.hash,
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=deadline_at,
        )
    )
    admitted = RECORD_ADAPTER.validate_python(
        {
            "type": "run_admitted",
            "run_id": run_id,
            "seq": 1,
            "at": now,
            "scope": SCOPE,
            "version_hash": version.hash,
            "input": {"message": "where is order A1?"},
            "deadline_at": deadline_at,
        }
    )
    await store.append(run_id, 1, admitted)
    return run_id


async def _run_to_completion(
    store: InMemoryStore, run_id: RunId, spec: AgentSpec, model: FakeModel
) -> None:
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    loop = AgentLoop(
        journal,
        spec,
        model,
        ToolResolver(_registry()),
        ToolExecutor(_registry()),
        prices=StaticPriceTable({"fake-standard": _PRICE}),
    )
    outcome = await loop.run()
    # AgentLoop does not settle its own Run (DESIGN.md: a workflow step that is
    # an agent finishes without the Run finishing), so the caller writes the
    # terminal record, the same as the Worker wiring does.
    await journal.append(
        type="run_settled",
        state=outcome.state,
        output=outcome.output,
        failure=outcome.failure,
    )


class TestReportOverARealRun:
    async def test_token_totals_split_by_cache_state_are_correct(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        first_usage = Usage(
            input=1000, output=200, cache_read=500, cache_write=300, cache_write_1h=100
        )
        second_usage = Usage(input=1200, output=150, cache_read=800)
        model = (
            FakeModel()
            .turn(
                text="Let me check that order.",
                tool_calls=[("lookup", {"order_id": "A1"})],
                usage=first_usage,
                chunk_delay_seconds=0.01,
            )
            .turn(text="Your order A1 has shipped.", usage=second_usage, chunk_delay_seconds=0.01)
        )
        run_id = await _start_run(store, spec)
        await _run_to_completion(store, run_id, spec, model)

        report = await build_report(store, run_id)

        assert report.terminal_state is TerminalState.COMPLETED
        assert report.totals.usage == Usage(
            input=2200, output=350, cache_read=1300, cache_write=300, cache_write_1h=100
        )
        # cache_write_1h (100) is a subset of cache_write (300), so total_billable
        # excludes it rather than adding it on top.
        assert report.totals.usage.total_billable == 2200 + 350 + 1300 + 300

    async def test_the_cost_is_correct_and_complete(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        first_usage = Usage(
            input=1000, output=200, cache_read=500, cache_write=300, cache_write_1h=100
        )
        second_usage = Usage(input=1200, output=150, cache_read=800)
        model = (
            FakeModel()
            .turn(tool_calls=[("lookup", {"order_id": "A1"})], usage=first_usage)
            .turn(text="Your order A1 has shipped.", usage=second_usage)
        )
        run_id = await _start_run(store, spec)
        await _run_to_completion(store, run_id, spec, model)

        report = await build_report(store, run_id)

        # Priced by hand, independently of psych_runtime.model.pricing.compute_cost, so
        # this actually checks the pipeline rather than restating it:
        #   call 1: 1000*1 + 200*2 + 500*0.1 + (300-100)*1.25 + 100*2 = 1900 -> /1e6
        #   call 2: 1200*1 + 150*2 + 800*0.1                          = 1580 -> /1e6
        expected = Decimal("0.00190000") + Decimal("0.00158000")
        assert report.totals.cost == Cost(amount=expected, currency="USD", model="fake-standard")
        assert report.totals.unpriced_model_calls == 0
        assert not report.totals.cost_is_incomplete

        first_call, second_call = report.model_calls
        assert first_call.model == "fake-standard"
        assert first_call.usage == first_usage
        assert first_call.cost == Cost(amount=Decimal("0.00190000"), model="fake-standard")
        assert second_call.cost == Cost(amount=Decimal("0.00158000"), model="fake-standard")

    async def test_an_unpriced_model_leaves_the_report_honest_about_it(self) -> None:
        store = InMemoryStore()
        spec = _spec(model=ModelRef(model="model-nobody-priced"))
        model = FakeModel().turn(text="done")
        run_id = await _start_run(store, spec)
        await _run_to_completion(store, run_id, spec, model)

        report = await build_report(store, run_id)

        assert report.totals.cost is None
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete
        assert report.model_calls[0].cost is None
        assert report.model_calls[0].usage is not None  # usage is still known

    async def test_the_latency_breakdown_accounts_for_the_runs_wall_clock(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        model = (
            FakeModel()
            .turn(
                text="Let me check that order.",
                tool_calls=[("lookup", {"order_id": "A1"})],
                chunk_delay_seconds=0.01,
            )
            .turn(text="Your order A1 has shipped.", chunk_delay_seconds=0.01)
        )
        run_id = await _start_run(store, spec)
        await _run_to_completion(store, run_id, spec, model)

        report = await build_report(store, run_id)
        latency = report.totals.latency

        assert latency.wall_clock_seconds > 0
        # Two turns, each streamed with a real (if small) delay per chunk: the
        # measured model time must be a real positive number, not a stub.
        assert latency.model_seconds > 0
        assert latency.tool_seconds >= 0
        # The gap is reported exactly as computed, not silently dropped.
        assert latency.unaccounted_seconds == pytest.approx(
            latency.wall_clock_seconds - latency.model_seconds - latency.tool_seconds
        )

    async def test_tool_calls_model_calls_and_the_terminal_state_are_all_reported(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        model = (
            FakeModel()
            .turn(text="Let me check.", tool_calls=[("lookup", {"order_id": "A1"})])
            .turn(text="Your order A1 has shipped.")
        )
        run_id = await _start_run(store, spec)
        await _run_to_completion(store, run_id, spec, model)

        report = await build_report(store, run_id)

        assert report.version_hash.startswith("sha256:")
        assert report.spec_name == "support"
        assert report.system_prompt.startswith("Help the customer with their order.")

        assert len(report.model_calls) == 2
        assert [call.turn for call in report.model_calls] == [1, 2]
        assert not any(call.dangling for call in report.model_calls)

        assert len(report.tool_calls) == 1
        tool_call = report.tool_calls[0]
        assert tool_call.tool == "lookup"
        assert tool_call.arguments == {"order_id": "A1"}
        assert tool_call.outcome is ToolOutcome.OK
        assert tool_call.result == {"order_id": "A1", "status": "shipped"}

        assert report.terminal_state is TerminalState.COMPLETED
        assert report.output == {"text": "Your order A1 has shipped."}
        assert report.totals.model_calls == 2
        assert report.totals.tool_calls == 1


class TestTheRecordedSystemPrompt:
    """What the model was told, answerable from the log alone.

    Every other question a person asks of a trace was already answerable: what
    the user said, what the model decided, which tools it was offered, what
    came back. "What was it told" was not, because the report rebuilt the
    prompt from the Spec and could not know what the runtime added at assembly
    time.
    """

    async def test_the_recorded_prompt_is_what_the_client_received(self) -> None:
        """Byte-identical, not merely similar. A prompt that is nearly what was
        sent is worse than none: it invites a reader to debug the difference
        between two things they think are the same."""
        store = InMemoryStore()
        spec = psych_runtime.AgentSpec(
            name="support",
            instructions="Help the customer. Be brief.",
            model=psych_runtime.ModelRef(model="fake-standard"),
            skills=(
                psych_runtime.Skill(
                    name="refunds",
                    description="How refunds work here.",
                    body="Refunds take three days.",
                ),
            ),
        )
        version = await psych_runtime.publish(store, spec)
        model = FakeModel().turn(text="It shipped.")
        runtime = Runtime(store=store, model=model, registry=ToolRegistry())
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            run = await psych_runtime.dispatch(
                store, version, SCOPE, input={"message": "where is A1?"}
            )
            async for _ in psych_runtime.stream(store, run.run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)

        sent = model.requests[0].messages[0]
        assert isinstance(sent, SystemMessage)

        records = await store.read(run.run_id)
        started = [r for r in records if isinstance(r, ModelCallStarted)]
        assert len(started) == 1
        assert started[0].system_prompt == sent.content

        # And the report reads it back rather than rebuilding one.
        report = await psych_runtime.report(store, run.run_id)
        assert report.system_prompt == sent.content
        assert report.model_calls[0].system_prompt == sent.content
        assert "How refunds work here." in report.system_prompt
        # The skills *index* is in the prompt; a skill body is not, or the
        # whole load-on-demand mechanism would be pointless.
        assert "Refunds take three days." not in report.system_prompt

    async def test_a_turn_whose_advisories_changed_shows_its_own_prompt(self) -> None:
        """Per-turn, not once per Run. A tool withheld on turn three means the
        model was told something different from turn one, and a report that
        showed one prompt for the whole Run would hide exactly that."""
        store = InMemoryStore()
        registry = ToolRegistry()

        @registry.register
        async def flaky(value: str) -> str:
            """Always fails, so the streak guard withdraws it."""
            raise RuntimeError(f"down: {value}")

        spec = psych_runtime.AgentSpec(
            name="support",
            instructions="Try the tool.",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="flaky"),),
            limits=psych_runtime.Limits(
                max_turns=8, failure_streak_threshold=2, deadline_seconds=60
            ),
        )
        version = await psych_runtime.publish(store, spec)
        model = FakeModel()
        for _ in range(3):
            model = model.turn(tool_calls=[("flaky", {"value": "x"})])
        model = model.turn(text="I could not.")

        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "go"})
            async for _ in psych_runtime.stream(store, run.run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)

        report = await psych_runtime.report(store, run.run_id)
        prompts = [call.system_prompt for call in report.model_calls]
        assert len(prompts) >= 3
        # The first turn had nothing to advise; a later one names the withheld
        # tool. Same Spec, same Version, genuinely different prompts.
        assert "flaky" not in prompts[0]
        assert any("flaky" in prompt for prompt in prompts[1:])

    async def test_the_recorded_prompt_costs_what_the_docstring_claims(self) -> None:
        """The size question, measured rather than assumed.

        The ticket for this said a system prompt is typically one to three KB
        and that storing it per turn is affordable. Asserting it keeps that
        claim honest, and fails loudly if prompt assembly ever grows something
        large enough to reconsider.
        """
        store = InMemoryStore()
        spec = psych_runtime.AgentSpec(
            name="support",
            instructions="Help the customer with their order. Be brief and accurate.",
            model=psych_runtime.ModelRef(model="fake-standard"),
        )
        version = await psych_runtime.publish(store, spec)
        model = FakeModel().turn(text="done")
        runtime = Runtime(store=store, model=model, registry=ToolRegistry())
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})
            async for _ in psych_runtime.stream(store, run.run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)

        records = await store.read(run.run_id)
        started = next(r for r in records if isinstance(r, ModelCallStarted))
        # One record, well inside DynamoDB's 400 KB item limit (DESIGN.md §7),
        # which is the constraint that would make this design wrong.
        assert len(started.system_prompt) < 4_000
        assert len(started.model_dump_json()) < 100_000
