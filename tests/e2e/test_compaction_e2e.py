"""A long conversation compacting itself, from dispatch through to the report.

Every assertion goes through ``psych_runtime.report()``, which is what a
consumer reads, and the point of it is a pair of facts that have to hold at
once: the model stopped being sent the earlier conversation, and the report
still contains every record of it. Compaction changes what the model sees
next, never what happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import CompactionApplied, TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.usage import Usage
from psych_runtime.model.pricing import ModelPrice, StaticPriceTable
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

TABLE = StaticPriceTable(
    {
        "fake-standard": ModelPrice(
            input=Decimal(1), output=Decimal(2), cache_read=Decimal(0), cache_write=Decimal(0)
        )
    }
)
"""Only the agent's own model is priced. The summariser below runs on
`fake-reasoning`, which is deliberately absent: a model with no known price
records `cost=None`, never zero (DESIGN.md §13.2)."""

OVER_LONG = (
    "This model's maximum context length is 8192 tokens, however your messages "
    "resulted in 9001 tokens."
)


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> dict[str, str]:
        """Look up an order by id."""
        return {"order_id": order_id, "status": "shipped"}

    yield registry


def _spec(**policy: object) -> psych_runtime.AgentSpec:
    settings: dict[str, object] = {"trigger_tokens": 500, "keep_recent_turns": 1}
    settings.update(policy)
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup"),),
        compaction=psych_runtime.CompactionPolicy.model_validate(settings),
        limits=psych_runtime.Limits(max_turns=8, deadline_seconds=60),
    )


async def _run(
    model: FakeModel, registry: ToolRegistry, spec: psych_runtime.AgentSpec
) -> tuple[InMemoryStore, RunId, psych_runtime.RunReport]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    runtime = Runtime(store=store, model=model, registry=registry, prices=TABLE)
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(
            store, version.hash, SCOPE, input={"message": "where is AAA?"}
        )
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return store, run.run_id, await psych_runtime.report(store, run.run_id)


def _two_long_turns() -> FakeModel:
    return (
        FakeModel()
        .turn(
            text="looking up order AAA",
            tool_calls=[("lookup", {"order_id": "AAA"})],
            usage=Usage(input=1_000, output=20),
        )
        .turn(
            text="looking up order BBB",
            tool_calls=[("lookup", {"order_id": "BBB"})],
            usage=Usage(input=1_000, output=20),
        )
    )


class TestALongConversationCarriesOn:
    async def test_the_run_completes_and_the_report_holds_both_halves(
        self, registry: ToolRegistry
    ) -> None:
        model = (
            _two_long_turns()
            .turn(text="Earlier: the customer asked after order AAA, which shipped.")
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )

        store, run_id, report = await _run(model, registry, _spec(model="fake-reasoning"))

        assert report.terminal_state is TerminalState.COMPLETED

        assert len(report.compactions) == 1
        compaction = report.compactions[0]
        assert compaction.reason == "threshold"
        assert compaction.model == "fake-reasoning"
        assert "order AAA" in compaction.summary

        # The replaced records are still here, in the report a consumer reads.
        assert [call.tool for call in report.tool_calls] == ["lookup", "lookup"]
        assert report.tool_calls[0].arguments == {"order_id": "AAA"}
        assert [call.turn for call in report.model_calls] == [1, 2, 3]

        # And in the log, unedited: nothing was rewritten to make room.
        records = await store.read(run_id)
        replaced = [r for r in records if r.seq <= compaction.replaced_to_seq]
        assert [r.seq for r in replaced] == list(range(1, compaction.replaced_to_seq + 1))

        # What the model was sent last is the summary plus the tail.
        last = "\n".join(
            message.content
            for message in model.requests[-1].messages
            if isinstance(message.content, str)
        )
        assert "Earlier: the customer asked after order AAA, which shipped." in last
        assert "looking up order AAA" not in last
        assert "looking up order BBB" in last

    async def test_the_summarising_call_is_metered_and_attributable(
        self, registry: ToolRegistry
    ) -> None:
        """Its tokens are in the totals, its own row says what it spent, and it
        is not counted as a turn. `fake-reasoning` has no price in the table, so
        the total is honest about being incomplete rather than quietly short."""
        model = (
            _two_long_turns()
            .turn(text="Earlier: two orders.", usage=Usage(input=900, output=40))
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )

        _, _, report = await _run(model, registry, _spec(model="fake-reasoning"))

        assert report.totals.model_calls == 3
        assert report.totals.compaction_calls == 1
        assert report.compactions[0].usage == Usage(input=900, output=40)
        assert report.compactions[0].cost is None
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete is True

        turns = sum(call.usage.input for call in report.model_calls if call.usage is not None)
        assert report.totals.usage.input == turns + 900

    async def test_the_time_the_summary_took_is_accounted_for(self, registry: ToolRegistry) -> None:
        """The other half of metering, and the half that was missing.

        Compaction was counted in tokens and cost from the start and was not
        timed at all, so a summarising call fell into `unaccounted_seconds`
        beside "time between turns" and read as a gap nobody could explain.
        DESIGN.md §23's seventh item asks for a breakdown that accounts for the
        Run's wall clock; a call the Run really made has to be in it.

        Counted apart from `model_seconds` for the same reason the call count
        is: this is time spent fitting the conversation into the window rather
        than doing the work, and a breakdown that blended the two could not say
        whether compacting was worth it.
        """
        model = (
            _two_long_turns()
            .turn(text="Earlier: two orders.", usage=Usage(input=900, output=40))
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )

        _, _, report = await _run(model, registry, _spec())

        assert report.totals.compaction_calls == 1
        latency = report.totals.latency
        assert latency.compaction_seconds > 0.0, "the summarising call took measurable time"
        # And it is its own line rather than being folded into the turns.
        assert latency.compaction_seconds < latency.wall_clock_seconds
        assert latency.unaccounted_seconds == pytest.approx(
            latency.wall_clock_seconds
            - latency.model_seconds
            - latency.tool_seconds
            - latency.compaction_seconds
        )

    async def test_a_summariser_with_a_known_price_is_costed_like_any_other_call(
        self, registry: ToolRegistry
    ) -> None:
        model = (
            _two_long_turns()
            .turn(text="Earlier: two orders.", usage=Usage(input=1_000, output=500))
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )

        _, _, report = await _run(model, registry, _spec())

        # $1/M in and $2/M out over the summariser's own usage.
        assert report.compactions[0].cost is not None
        assert report.compactions[0].cost.amount == Decimal("0.00200000")
        assert report.totals.cost_is_incomplete is False


class TestWhenTheProviderRefusesThePromptOutright:
    async def test_the_run_compacts_and_carries_on_instead_of_failing(
        self, registry: ToolRegistry
    ) -> None:
        """The reactive half. Before this the Run settled FAILED on a 400 and a
        person saw the conversation stop rather than continue."""
        model = (
            _two_long_turns()
            .raises_permanent(400, message=OVER_LONG)
            .turn(text="Earlier: two orders.")
            .turn(text="Both shipped.", usage=Usage(input=120, output=10))
        )

        _, _, report = await _run(model, registry, _spec(trigger_tokens=10_000_000))

        assert report.terminal_state is TerminalState.COMPLETED
        assert [c.reason for c in report.compactions] == ["overflow"]
        assert [call.will_retry for call in report.model_calls if call.failure is not None] == [
            True
        ]


class TestAnAgentThatDidNotAskForIt:
    async def test_nothing_is_compacted_and_nothing_is_summarised(
        self, registry: ToolRegistry
    ) -> None:
        """Off by default, and the default costs nothing: no extra model call,
        no extra record, no line in the report."""
        spec = psych_runtime.AgentSpec(
            name="support",
            instructions="Help the customer.",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="lookup"),),
            limits=psych_runtime.Limits(max_turns=8, deadline_seconds=60),
        )
        model = _two_long_turns().turn(text="Both shipped.", usage=Usage(input=2_000, output=10))

        store, run_id, report = await _run(model, registry, spec)

        assert report.terminal_state is TerminalState.COMPLETED
        assert report.compactions == ()
        assert report.totals.compaction_calls == 0
        assert not any(isinstance(r, CompactionApplied) for r in await store.read(run_id))
        assert len(model.requests) == 3
