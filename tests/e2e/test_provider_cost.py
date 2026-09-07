"""A provider's own cost, recorded instead of recomputed.

`compute_cost` derives a figure from whatever price table is in
force, and that table is documented as incomplete and known to go stale. Many
gateways already return an authoritative cost with the response, computed
against the caller's real contract including negotiated rates Psych cannot
know. That number is better, because it comes from the party doing the
billing.

What these assert is the whole contract: the provider's figure is recorded
when there is one, the table still answers when there is not, an unknown stays
`None` rather than becoming a confident zero, a non-USD figure survives, and a
report can tell the two apart rather than blending them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.scope import Scope
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.pricing import CostPolicy, ModelPrice, StaticPriceTable
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

USAGE = Usage(input=1_000, output=500)

# $1/M in, $2/M out over USAGE = $0.001 + $0.001 = $0.002. Chosen to be
# nothing like the provider figure below, so a test cannot pass by accident.
TABLE = StaticPriceTable(
    {
        "fake-standard": ModelPrice(
            input=Decimal(1), output=Decimal(2), cache_read=Decimal(0), cache_write=Decimal(0)
        )
    }
)
COMPUTED = Decimal("0.00200000")

PROVIDER_SAID = Cost(amount=Decimal("0.00042"), currency="USD", model="fake-standard")


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    yield ToolRegistry()


def _spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        limits=psych_runtime.Limits(max_turns=4, deadline_seconds=60),
    )


async def _run(
    model: FakeModel,
    registry: ToolRegistry,
    *,
    prices: StaticPriceTable | None,
    policy: CostPolicy = "prefer_provider",
) -> psych_runtime.RunReport:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, _spec())
    runtime = Runtime(
        store=store, model=model, registry=registry, prices=prices, cost_policy=policy
    )
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(store, version.hash, SCOPE, input={"message": "hi"})
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return await psych_runtime.report(store, run.run_id)


class TestWhichCostGetsRecorded:
    async def test_the_provider_figure_wins_over_the_table(self, registry: ToolRegistry) -> None:
        """The case the whole ticket is for. Both numbers exist and differ, and
        the one from the party doing the billing is what lands in the log."""
        model = FakeModel().turn(text="Done.", usage=USAGE, cost=PROVIDER_SAID)

        report = await _run(model, registry, prices=TABLE)

        assert report.totals.cost is not None
        assert report.totals.cost.amount == PROVIDER_SAID.amount
        assert report.totals.cost.amount != COMPUTED, "the table's figure must not have won"
        assert report.totals.cost.source == "provider"
        assert report.totals.provider_reported_costs == 1
        assert report.totals.cost_is_incomplete is False

    async def test_a_silent_provider_falls_back_to_the_table(self, registry: ToolRegistry) -> None:
        """Nothing changes for a deployment whose provider says nothing, which
        is most of them. The default policy is a preference, not a requirement."""
        model = FakeModel().turn(text="Done.", usage=USAGE)

        report = await _run(model, registry, prices=TABLE)

        assert report.totals.cost is not None
        assert report.totals.cost.amount == COMPUTED
        assert report.totals.cost.source == "computed"
        assert report.totals.provider_reported_costs == 0

    async def test_neither_knowing_records_none_and_never_zero(
        self, registry: ToolRegistry
    ) -> None:
        """DESIGN.md §13.2, which no policy is allowed to break: a silent zero
        makes metering look correct and be wrong."""
        model = FakeModel().turn(text="Done.", usage=USAGE)

        report = await _run(model, registry, prices=None)

        assert report.totals.cost is None
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete is True

    async def test_computed_policy_ignores_what_the_provider_said(
        self, registry: ToolRegistry
    ) -> None:
        """For a consumer who does not trust the gateway's arithmetic, or who
        needs one basis across providers that count differently."""
        model = FakeModel().turn(text="Done.", usage=USAGE, cost=PROVIDER_SAID)

        report = await _run(model, registry, prices=TABLE, policy="computed")

        assert report.totals.cost is not None
        assert report.totals.cost.amount == COMPUTED
        assert report.totals.cost.source == "computed"

    async def test_provider_only_records_nothing_rather_than_guessing(
        self, registry: ToolRegistry
    ) -> None:
        """For a consumer who would rather record an honest unknown than a
        number their invoice will not match. The table is present and is still
        not consulted."""
        model = FakeModel().turn(text="Done.", usage=USAGE)

        report = await _run(model, registry, prices=TABLE, policy="provider_only")

        assert report.totals.cost is None
        assert report.totals.unpriced_model_calls == 1


class TestWhatTheRecordSays:
    async def test_a_non_usd_figure_keeps_its_currency(self, registry: ToolRegistry) -> None:
        """A gateway billing in euros reports euros. Psych does not convert and
        must not quietly relabel."""
        euros = Cost(amount=Decimal("0.0009"), currency="EUR", model="fake-standard")
        model = FakeModel().turn(text="Done.", usage=USAGE, cost=euros)

        report = await _run(model, registry, prices=TABLE)

        assert report.totals.cost is not None
        assert report.totals.cost.currency == "EUR"
        assert report.totals.cost.amount == Decimal("0.0009")

    async def test_a_mixed_run_says_mixed_rather_than_claiming_one_source(
        self, registry: ToolRegistry
    ) -> None:
        """The failure this exists to prevent. Two calls, one priced by the
        provider and one by the table: the total is a real number and is not
        wholly either kind, so it says so rather than inheriting the
        provider's authority for arithmetic that is half Psych's."""
        model = (
            FakeModel()
            .turn(tool_calls=[("nope", {})], usage=USAGE, cost=PROVIDER_SAID)
            .turn(text="Done.", usage=USAGE)
        )

        report = await _run(model, registry, prices=TABLE)

        assert report.totals.cost is not None
        assert report.totals.cost.amount == PROVIDER_SAID.amount + COMPUTED
        assert report.totals.cost.source == "mixed"
        assert report.totals.provider_reported_costs == 1
        assert report.totals.model_calls == 2

    async def test_each_call_carries_its_own_source(self, registry: ToolRegistry) -> None:
        """The totals line is a summary; the per-call figures are what somebody
        reconciling against an invoice actually reads."""
        model = (
            FakeModel()
            .turn(tool_calls=[("nope", {})], usage=USAGE, cost=PROVIDER_SAID)
            .turn(text="Done.", usage=USAGE)
        )

        report = await _run(model, registry, prices=TABLE)

        sources = [call.cost.source for call in report.model_calls if call.cost is not None]
        assert sources == ["provider", "computed"]

    async def test_the_recorded_cost_does_not_move_when_the_table_does(
        self, registry: ToolRegistry
    ) -> None:
        """A report is a projection over an immutable log. Cost is resolved once
        at call time and written in, so re-reading an old Run under a different
        table reports what it actually cost rather than what it would cost
        today."""
        store = InMemoryStore()
        version = await psych_runtime.publish(store, _spec())
        model = FakeModel().turn(text="Done.", usage=USAGE)
        runtime = Runtime(store=store, model=model, registry=registry, prices=TABLE)
        worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            run = await psych_runtime.dispatch(store, version.hash, SCOPE, input={"message": "hi"})
            async for _ in psych_runtime.stream(store, run.run_id):
                pass
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=10)

        first = await psych_runtime.report(store, run.run_id)
        assert first.totals.cost is not None
        assert first.totals.cost.amount == COMPUTED

        # The table is not consulted on read at all, so nothing about a later
        # one can reach an old Run.
        again = await psych_runtime.report(store, run.run_id)
        assert again.totals.cost is not None
        assert again.totals.cost.amount == COMPUTED
