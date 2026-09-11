"""Pricing arithmetic and the unknown-model rule.

DESIGN.md §13.2. The arithmetic is one of the named unit-test targets in §22,
and the ``cost=None`` rule is an invariant that gets a change rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.pricing import (
    DEFAULT_PRICE_CATALOG_VERSION,
    DEFAULT_PRICES,
    ModelPrice,
    StaticPriceTable,
    compute_cost,
    resolve_cost,
)

pytestmark = pytest.mark.unit


def price(**kwargs: object) -> ModelPrice:
    base: dict[str, object] = {
        "input": Decimal("10"),
        "output": Decimal("30"),
        "cache_read": Decimal("1"),
        "cache_write": Decimal("12.50"),
    }
    base.update(kwargs)
    return ModelPrice.model_validate(base)


class TestUnknownModel:
    def test_unknown_model_costs_none_not_zero(self) -> None:
        """The invariant. A silent zero makes metering look correct and be wrong."""
        table = StaticPriceTable({})
        assert compute_cost("some-model-nobody-priced", Usage(input=1000), table) is None

    def test_unknown_model_costs_none_even_with_zero_usage(self) -> None:
        """Zero tokens on an unpriced model is still 'I do not know', not 'free'."""
        table = StaticPriceTable({})
        assert compute_cost("unpriced", Usage(), table) is None

    def test_known_model_with_zero_usage_costs_zero_not_none(self) -> None:
        """The distinction has to run both ways or it carries no information."""
        table = StaticPriceTable({"m": price()})
        result = compute_cost("m", Usage(), table)
        assert result is not None
        assert result.amount == Decimal(0)

    def test_missing_provider_usage_is_not_priced_as_zero(self) -> None:
        """An omitted measurement must not turn into a confident free call."""
        result = resolve_cost(
            "gpt-4o",
            Usage(),
            DEFAULT_PRICES,
            None,
            usage_reported=False,
        )
        assert result is None

    def test_provider_cost_remains_valid_when_usage_is_omitted(self) -> None:
        reported = Cost(amount=Decimal("0.12"), model="metered", source="provider")
        result = resolve_cost(
            "metered",
            Usage(),
            DEFAULT_PRICES,
            reported,
            usage_reported=False,
        )
        assert result == reported


class TestBundledCatalog:
    def test_catalog_is_versioned_and_has_broad_coverage(self) -> None:
        assert DEFAULT_PRICE_CATALOG_VERSION == "2026-09-10"
        for model in ("gpt-4o", "gpt-5.6-luna", "azure/gpt-5.6-luna"):
            assert DEFAULT_PRICES.price_for(model) is not None


class TestArithmetic:
    def test_each_counter_bills_at_its_own_rate(self) -> None:
        table = StaticPriceTable({"m": price()})
        usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
        result = compute_cost("m", usage, table)
        assert result is not None
        # 10 + 30 + 1
        assert result.amount == Decimal("41.00000000")
        assert result.input_amount == Decimal("10.00000000")
        assert result.output_amount == Decimal("30.00000000")
        assert result.cache_read_amount == Decimal("1.00000000")
        assert result.cache_write_amount == Decimal("0E-8")
        assert result.has_breakdown

    def test_cache_read_is_disjoint_from_input(self) -> None:
        """cache_read tokens are not also counted as input, or every cached
        conversation would be billed twice."""
        table = StaticPriceTable({"m": price()})
        cached = compute_cost("m", Usage(cache_read=1_000_000), table)
        fresh = compute_cost("m", Usage(input=1_000_000), table)
        assert cached is not None
        assert fresh is not None
        assert cached.amount == Decimal("1.00000000")
        assert fresh.amount == Decimal("10.00000000")

    def test_one_hour_writes_bill_at_the_long_rate_and_the_rest_at_the_short(self) -> None:
        table = StaticPriceTable({"m": price(cache_write_1h=Decimal("20"))})
        usage = Usage(cache_write=1_000_000, cache_write_1h=400_000)
        result = compute_cost("m", usage, table)
        assert result is not None
        # 600k at 12.50/M + 400k at 20/M = 7.50 + 8.00
        assert result.amount == Decimal("15.50000000")

    def test_without_a_long_rate_every_write_bills_at_the_standard_rate(self) -> None:
        table = StaticPriceTable({"m": price(cache_write_1h=None)})
        usage = Usage(cache_write=1_000_000, cache_write_1h=1_000_000)
        result = compute_cost("m", usage, table)
        assert result is not None
        assert result.amount == Decimal("12.50000000")

    def test_reasoning_tokens_are_not_billed_separately(self) -> None:
        """Providers that charge for reasoning report it inside output. Billing
        it again would double-count."""
        table = StaticPriceTable({"m": price()})
        without = compute_cost("m", Usage(output=1000), table)
        with_reasoning = compute_cost("m", Usage(output=1000, reasoning=900), table)
        assert without is not None
        assert with_reasoning is not None
        assert without.amount == with_reasoning.amount

    def test_cost_records_the_model_it_priced(self) -> None:
        table = StaticPriceTable({"m": price()})
        result = compute_cost("m", Usage(input=1), table)
        assert result is not None
        assert result.model == "m"


class TestResolution:
    def test_exact_match_wins(self) -> None:
        table = StaticPriceTable(
            {"gpt-4o": price(input=Decimal("1")), "gpt-4o-mini": price(input=Decimal("2"))}
        )
        result = compute_cost("gpt-4o-mini", Usage(input=1_000_000), table)
        assert result is not None
        assert result.amount == Decimal("2.00000000")

    def test_longest_prefix_wins_for_a_dated_release(self) -> None:
        """Providers version ids by suffix; an exact-only table goes stale silently."""
        table = StaticPriceTable(
            {"gpt-4o": price(input=Decimal("1")), "gpt-4o-mini": price(input=Decimal("2"))}
        )
        result = compute_cost("gpt-4o-mini-2024-07-18", Usage(input=1_000_000), table)
        assert result is not None
        assert result.amount == Decimal("2.00000000")

    def test_a_prefix_that_does_not_match_still_returns_none(self) -> None:
        table = StaticPriceTable({"gpt-4o": price()})
        assert compute_cost("unlisted-model", Usage(input=1), table) is None

    def test_overrides_replace_shipped_rates(self) -> None:
        table = DEFAULT_PRICES.with_overrides({"gpt-4o": price(input=Decimal("999"))})
        result = compute_cost("gpt-4o-2024-11-20", Usage(input=1_000_000), table)
        assert result is not None
        assert result.amount == Decimal("999.00000000")


class TestUsageModel:
    def test_one_hour_writes_cannot_exceed_total_writes(self) -> None:
        with pytest.raises(ValueError, match="subset"):
            Usage(cache_write=100, cache_write_1h=101)

    def test_negative_counters_are_refused(self) -> None:
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            Usage(input=-1)

    def test_total_billable_counts_each_token_once(self) -> None:
        usage = Usage(
            input=1, output=2, cache_read=4, cache_write=8, cache_write_1h=8, reasoning=16
        )
        assert usage.total_billable == 15

    def test_usage_is_frozen(self) -> None:
        usage = Usage(input=1)
        with pytest.raises(ValueError, match="frozen"):
            usage.input = 2  # type: ignore[misc]

    def test_addition_sums_every_counter(self) -> None:
        total = Usage(input=1, cache_write=4, cache_write_1h=2) + Usage(output=3, cache_write=6)
        assert total == Usage(input=1, output=3, cache_write=10, cache_write_1h=2)


class TestCostAddition:
    def test_mixing_currencies_raises_rather_than_guessing_a_rate(self) -> None:
        usd = Cost(amount=Decimal(1), currency="USD", model="m")
        eur = Cost(amount=Decimal(1), currency="EUR", model="m")
        with pytest.raises(ValueError, match="does not convert currencies"):
            _ = usd + eur

    def test_summing_two_models_marks_the_result_mixed(self) -> None:
        total = Cost(amount=Decimal(1), model="a") + Cost(amount=Decimal(2), model="b")
        assert total.model == "<mixed>"
        assert total.amount == Decimal(3)

    def test_a_total_only_provider_cost_drops_category_precision(self) -> None:
        detailed = Cost(
            amount=Decimal("1"),
            model="m",
            input_amount=Decimal("0.4"),
            output_amount=Decimal("0.6"),
            cache_read_amount=Decimal("0"),
            cache_write_amount=Decimal("0"),
        )
        total_only = Cost(amount=Decimal("2"), model="m", source="provider")
        total = detailed + total_only
        assert total.amount == Decimal("3")
        assert not total.has_breakdown


class TestProperties:
    @given(
        st.integers(min_value=0, max_value=10**9),
        st.integers(min_value=0, max_value=10**9),
        st.integers(min_value=0, max_value=10**9),
    )
    def test_cost_is_monotone_in_every_counter(self, inp: int, out: int, read: int) -> None:
        """More tokens never costs less. Guards a sign error in the arithmetic."""
        table = StaticPriceTable({"m": price()})
        smaller = compute_cost("m", Usage(input=inp, output=out, cache_read=read), table)
        larger = compute_cost("m", Usage(input=inp + 1, output=out + 1, cache_read=read + 1), table)
        assert smaller is not None
        assert larger is not None
        assert larger.amount >= smaller.amount

    @given(st.integers(min_value=0, max_value=10**7), st.integers(min_value=0, max_value=10**7))
    def test_summing_two_calls_equals_pricing_their_summed_usage(
        self, first: int, second: int
    ) -> None:
        """Report totals sum per-call costs. That has to agree with pricing the
        combined usage, or the report and the log disagree."""
        table = StaticPriceTable({"m": price()})
        a = compute_cost("m", Usage(input=first), table)
        b = compute_cost("m", Usage(input=second), table)
        combined = compute_cost("m", Usage(input=first + second), table)
        assert a is not None
        assert b is not None
        assert combined is not None
        assert (a + b).amount == combined.amount
