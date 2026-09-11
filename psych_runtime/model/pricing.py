"""Pricing: rates in, cost out, and an honest ``None`` when the rate is unknown.

DESIGN.md §13.2. A pricing table maps a model id to four per-million rates.
Cost is computed per model call from the recorded usage and the rate in force at
the time of the call, and the computed cost is written into the Record so it
never changes retroactively when the table is updated.

The rule that matters more than the table: **a model with no known rate records
``cost=None``, never ``0``.** A silent zero makes the metering look correct and
be wrong, and nobody discovers it until they reconcile against a provider bill.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.usage import Cost, Usage

__all__ = [
    "DEFAULT_PRICES",
    "DEFAULT_PRICE_CATALOG_VERSION",
    "CostPolicy",
    "ModelPrice",
    "PriceResolver",
    "StaticPriceTable",
    "compute_cost",
    "resolve_cost",
]

_PER_MILLION: Final = Decimal(1_000_000)

_CENT_PRECISION: Final = Decimal("0.00000001")
"""Eight decimal places. A single cheap call can cost a small fraction of a
cent, and rounding to two would round most individual calls to zero."""


class ModelPrice(BaseModel):
    """Four per-million rates for one model.

    Attributes:
        input: cost per million uncached input tokens.
        output: cost per million generated tokens.
        cache_read: cost per million tokens read from the prompt cache.
        cache_write: cost per million tokens written to the prompt cache at the
            provider's default retention.
        cache_write_1h: cost per million tokens written at one-hour retention.
            Some providers charge more for the longer hold. When ``None`` the
            ``cache_write`` rate applies to those tokens too.
        currency: ISO 4217, uppercase.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: Decimal = Field(ge=Decimal(0))
    output: Decimal = Field(ge=Decimal(0))
    cache_read: Decimal = Field(ge=Decimal(0))
    cache_write: Decimal = Field(ge=Decimal(0))
    cache_write_1h: Decimal | None = Field(default=None, ge=Decimal(0))
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


@runtime_checkable
class PriceResolver(Protocol):
    """Price resolution is a port, not a hardcoded table.

    DESIGN.md §13.2: the shipped table is a convenience with a known staleness
    problem. A consumer reconciling against their real provider bill supplies
    their own, and theirs is authoritative.

    An implementation returns ``None`` for a model it does not know. It must not
    invent a rate, and it must not return zeros: ``compute_cost`` turns ``None``
    into a recorded ``cost=None``, which is the honest answer.
    """

    def price_for(self, model: str) -> ModelPrice | None:
        """Return the rates for ``model``, or ``None`` when they are unknown."""
        ...


class StaticPriceTable:
    """A ``PriceResolver`` over a fixed dict.

    Exact match first, then the longest matching prefix. The prefix fallback
    exists because providers version model ids by suffix
    (``gpt-4o-2024-08-06``), and a table that only matched exactly would go
    stale silently every time a provider dated a release. A prefix match is
    still a real match against a rate someone entered, never a guess.
    """

    def __init__(
        self,
        prices: dict[str, ModelPrice],
        *,
        overrides: dict[str, ModelPrice] | None = None,
    ) -> None:
        self._prices = dict(prices)
        # Longest first so "model-v2" wins over "model" for an id
        # that both would match.
        self._prefixes = sorted(self._prices, key=len, reverse=True)
        self._overrides = dict(overrides or {})
        self._override_prefixes = sorted(self._overrides, key=len, reverse=True)

    @staticmethod
    def _match(model: str, prices: dict[str, ModelPrice], prefixes: list[str]) -> ModelPrice | None:
        exact = prices.get(model)
        if exact is not None:
            return exact
        for prefix in prefixes:
            if model.startswith(prefix):
                return prices[prefix]
        return None

    def price_for(self, model: str) -> ModelPrice | None:
        override = self._match(model, self._overrides, self._override_prefixes)
        if override is not None:
            return override
        return self._match(model, self._prices, self._prefixes)

    def with_overrides(self, overrides: dict[str, ModelPrice]) -> StaticPriceTable:
        """A copy with ``overrides`` applied, for a consumer amending the shipped table.

        Overrides are a separate layer consulted before the shipped rates, not
        merged into them, and that distinction became load-bearing when the
        shipped table grew to thousands of generated models. Merged into one
        pool, longest-prefix matching would let a shipped ``model-v2``
        out-match a consumer's override on ``model`` -- so the more
        specific *shipped* row would silently beat the rate somebody entered
        against their own bill. DESIGN.md §13.2 says the consumer's table is
        authoritative; layering is what keeps that true regardless of how
        specific the shipped id happens to be.

        Within each layer, longest-prefix still applies: a consumer with both
        ``gpt-4o`` and ``gpt-4o-mini`` overrides gets the expected one.
        """
        return StaticPriceTable(self._prices, overrides={**self._overrides, **overrides})


CostPolicy = Literal["prefer_provider", "computed", "provider_only"]
"""Which cost gets written into the Record, when two are available.

- ``"prefer_provider"``: the provider's figure when it reported one, otherwise
  a locally computed one. **The default**, because a gateway's number comes
  from the party doing the billing and is computed against the caller's real
  contract, including negotiated rates Psych cannot know. Falling back keeps a
  provider that says nothing working exactly as before.
- ``"computed"``: always Psych's own table, ignoring what the provider said.
  For a consumer who does not trust the gateway's arithmetic, or who prices in
  a currency the gateway does not report in, or who needs one consistent basis
  across several providers that disagree about how to count.
- ``"provider_only"``: the provider's figure or ``None``. For a consumer who
  would rather record an honest unknown than a number their invoice will not
  match.

None of the three can produce a zero out of ignorance. That invariant holds
across every branch: an unknown cost is ``None`` (DESIGN.md §13.2).
"""


def resolve_cost(
    model: str,
    usage: Usage,
    resolver: PriceResolver | None,
    reported: Cost | None,
    policy: CostPolicy = "prefer_provider",
    *,
    usage_reported: bool = True,
) -> Cost | None:
    """The cost to record for one model call, under ``policy``.

    Called once, at the time of the call, and the result is written into
    ``ModelCallFinished``. It is never recomputed on read: a report is a
    projection over an immutable log, so changing the policy or the table later
    does not retroactively change what a past Run cost. That is the property
    that makes an old report still mean what it meant.

    Args:
        model: the model id the call was made against.
        usage: the recorded counters for that call.
        resolver: the local price source, or ``None`` when the consumer wired
            no table at all.
        reported: what the provider said, or ``None`` when it said nothing.
        policy: which of the two wins. See ``CostPolicy``.

    Returns:
        The cost to record, or ``None`` when nothing available knows. Never a
        zero standing in for an unknown.
    """
    if policy == "computed":
        if not usage_reported:
            return None
        return compute_cost(model, usage, resolver) if resolver is not None else None
    if reported is not None:
        # Stamped rather than trusted to be stamped. A client is free to
        # construct a `Cost` however it likes, and a provider-reported figure
        # that arrived labelled "computed" would be indistinguishable from
        # Psych's own in a report, which is the one thing this field exists to
        # prevent.
        return reported.model_copy(update={"source": "provider"})
    if policy == "provider_only":
        return None
    if not usage_reported:
        return None
    return compute_cost(model, usage, resolver) if resolver is not None else None


def compute_cost(model: str, usage: Usage, resolver: PriceResolver) -> Cost | None:
    """Cost for one model call, or ``None`` when the model has no known price.

    Returning ``None`` is the whole point of this function. DESIGN.md §13.2:
    a silent zero makes metering look correct and be wrong. A caller that wants
    to treat unknown as free has to write that down explicitly at their own call
    site, where a reviewer can see it.

    ``cache_write_1h`` is billed at its own rate when the table gives one, and
    the remaining ``cache_write - cache_write_1h`` tokens at the standard write
    rate. When the table gives no 1h rate, every write token bills at the
    standard rate.

    ``reasoning`` is not billed here. Providers that charge for reasoning tokens
    report them inside ``output`` already; billing them again would double-count.

    Args:
        model: the model id the call was made against.
        usage: the recorded counters for that call.
        resolver: the price source. The consumer's, if they supplied one.

    Returns:
        The computed cost, or ``None`` when ``resolver`` does not know ``model``.
    """
    price = resolver.price_for(model)
    if price is None:
        return None

    standard_write = usage.cache_write - usage.cache_write_1h
    long_write_rate = (
        price.cache_write_1h if price.cache_write_1h is not None else price.cache_write
    )

    input_amount = Decimal(usage.input) * price.input / _PER_MILLION
    output_amount = Decimal(usage.output) * price.output / _PER_MILLION
    cache_read_amount = Decimal(usage.cache_read) * price.cache_read / _PER_MILLION
    cache_write_amount = (
        Decimal(standard_write) * price.cache_write
        + Decimal(usage.cache_write_1h) * long_write_rate
    ) / _PER_MILLION
    total = input_amount + output_amount + cache_read_amount + cache_write_amount

    return Cost(
        amount=total.quantize(_CENT_PRECISION, rounding=ROUND_HALF_EVEN),
        currency=price.currency,
        model=model,
        input_amount=input_amount.quantize(_CENT_PRECISION, rounding=ROUND_HALF_EVEN),
        output_amount=output_amount.quantize(_CENT_PRECISION, rounding=ROUND_HALF_EVEN),
        cache_read_amount=cache_read_amount.quantize(_CENT_PRECISION, rounding=ROUND_HALF_EVEN),
        cache_write_amount=cache_write_amount.quantize(_CENT_PRECISION, rounding=ROUND_HALF_EVEN),
    )


DEFAULT_PRICE_CATALOG_VERSION: Final = "2026-09-10"
"""Date of the bundled rate snapshot, exposed so reports can identify its age."""


def _load_default_prices() -> dict[str, ModelPrice]:
    """Load and validate the bundled per-million-token rate snapshot.

    The data is package content rather than executable Python so it can be
    audited and refreshed independently. Validation happens once at import;
    a malformed bundled rate must fail loudly instead of producing a plausible
    but incorrect bill.
    """
    path = Path(__file__).with_name("default_prices.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("default_prices.json must contain an object keyed by model id")
    return {str(model): ModelPrice.model_validate(price) for model, price in raw.items()}


_CURATED: Final = _load_default_prices()

DEFAULT_PRICES: Final = StaticPriceTable(_CURATED)
"""The shipped rate snapshot. Broad, versioned, and still able to go stale.

The bundled snapshot covers thousands of exact provider and model ids. Anything
absent still resolves to ``None``, which records ``cost=None`` rather than a
zero. ``DEFAULT_PRICE_CATALOG_VERSION`` tells a consumer when the snapshot was
made; an application reconciling against an invoice should still override it
with the rates it actually pays.

None of this moves the design position in DESIGN.md §13.2: a consumer
reconciling against a real provider bill supplies their own ``PriceResolver``,
and theirs is authoritative. A bigger shipped table is a better convenience,
not a substitute for that.
"""
