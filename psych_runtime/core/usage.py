"""Token usage and the cost computed from it.

DESIGN.md §13.1. Input tokens are split by cache state, and the split is not
negotiable: a single ``input_tokens`` field makes correct cost impossible,
because cached input is billed at a different rate from fresh input, and cache
writes at a different rate again.

## Why this lives in core rather than model

DESIGN.md §21 files "usage, pricing" under ``psych_runtime.model``. The arithmetic and
the price table do live there. The ``Usage`` type itself cannot: a model-call
Record carries usage, Records live in ``psych_runtime.core``, and ``psych_runtime.core`` imports
nothing from its siblings. So the data shape is here and the pricing that reads
it is in ``psych_runtime.model.pricing``. The alternative would invert the dependency
direction that import-linter enforces.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["Cost", "Usage"]


class Usage(BaseModel):
    """Token counters for one model call.

    Recorded per call and never aggregated at write time. Aggregation is a
    projection over the log (DESIGN.md §13.4), so a pricing correction can be
    recomputed while the recorded cost of each call stays as it was.

    Attributes:
        input: uncached input tokens, billed at the input rate.
        output: generated tokens, billed at the output rate.
        cache_read: tokens read from the prompt cache, billed at the read rate.
            These are not counted in ``input``; the two are disjoint.
        cache_write: tokens written to the prompt cache, billed at the write
            rate.
        cache_write_1h: the subset of ``cache_write`` held at one-hour retention
            rather than the default five minutes. This is a
            subset and not an addition: it is never larger than ``cache_write``,
            and the two are not summed.
        reasoning: reasoning tokens where the provider reports them separately.
            Providers that bill these at the output rate already include them in
            ``output``; this field is for visibility, not for a second charge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)
    cache_write_1h: int = Field(default=0, ge=0)
    reasoning: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _one_hour_writes_are_a_subset(self) -> Usage:
        if self.cache_write_1h > self.cache_write:
            raise ValueError(
                f"cache_write_1h ({self.cache_write_1h}) exceeds cache_write "
                f"({self.cache_write}); the 1h counter is a subset of the write "
                "counter, not a separate bucket added to it"
            )
        return self

    @property
    def total_billable(self) -> int:
        """Every token the provider charges for, counted once.

        ``cache_write_1h`` is excluded because it is a subset of ``cache_write``.
        ``reasoning`` is excluded because providers that charge for it report it
        inside ``output``.
        """
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: Usage) -> Usage:
        """Sum two usages. Used by report projections, never at write time."""
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
            reasoning=self.reasoning + other.reasoning,
        )


class Cost(BaseModel):
    """What one model call cost, computed at the time of the call.

    Written into the Record so it never changes retroactively when the price
    table is updated (DESIGN.md §13.2). A report six months from now reports
    what was actually billed, not what the current table would say.

    ``Decimal`` rather than ``float``: money summed across thousands of calls
    accumulates binary rounding error, and a metering system that is off by a
    cent per million calls is a metering system nobody trusts.

    Attributes:
        amount: the cost in ``currency``.
        currency: ISO 4217, uppercase.
        model: the model id the rates were resolved for, recorded so a later
            reader can see which table entry applied.
        source: where the number came from. ``"computed"`` means Psych derived
            it from a ``PriceResolver``; ``"provider"`` means the provider or
            gateway reported it with the response; ``"mixed"`` appears only on
            a sum of costs that did not agree.

            Recorded because the two are not interchangeable when somebody is
            reconciling against an invoice. A provider-reported figure comes
            from the party doing the billing and is authoritative; a computed
            one is as good as whatever table was in force, which Psych's own
            docstrings admit goes stale. A total that blended them without
            saying so would look like one number and be two.

            Defaulted to ``"computed"`` so every Record written before this
            field existed still loads, and still means what it said.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal = Field(ge=Decimal(0))
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    model: str = Field(min_length=1)
    source: Literal["computed", "provider", "mixed"] = "computed"

    def __add__(self, other: Cost) -> Cost:
        if self.currency != other.currency:
            raise ValueError(
                f"cannot add {self.currency} to {other.currency}; Psych does not "
                "convert currencies and guessing a rate would be worse than failing"
            )
        model = self.model if self.model == other.model else "<mixed>"
        # A sum of a provider-reported cost and a computed one is neither, and
        # calling it either would be a claim about where the money figure came
        # from that is false for half of it. `"mixed"` exists for exactly this
        # and for nothing else: it is never written at call time.
        source: Literal["computed", "provider", "mixed"] = (
            self.source if self.source == other.source else "mixed"
        )
        return Cost(
            amount=self.amount + other.amount,
            currency=self.currency,
            model=model,
            source=source,
        )
