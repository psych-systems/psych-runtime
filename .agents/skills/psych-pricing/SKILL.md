---
name: psych-pricing
description: >-
  Meter and price a Psych Run: `Usage` split by cache state, `Cost` with its
  currency and source, `DEFAULT_PRICES` and `StaticPriceTable.with_overrides`,
  the `PriceResolver` port, and the `cost_policy` choice between a provider's
  reported figure and Psych's own arithmetic. Use whenever someone needs token
  counts or spend per Run, tenant or agent, asks why a cost is None, wires
  custom rates or a gateway's numbers, mentions cache-read or cache-write
  billing, or asks about budgets and quotas in Psych (metering yes, enforcement
  no). Read before touching cost, because an unknown model records `cost=None`
  and never `0`, and a Run priced in two currencies raises `inconsistent_cost`
  rather than summing them.
---

# Metering, usage and cost

## Usage splits by cache state, per call

```python
psych_runtime.Usage(
    input=0,  # uncached input tokens
    output=0,  # generated
    cache_read=0,  # read from the prompt cache
    cache_write=0,  # written to it
    cache_write_1h=0,  # written at a longer TTL, where the provider bills it separately
    reasoning=0,  # where the provider reports it separately
)
```

Usage is recorded **per model call and never aggregated at write time**. The
totals in a report are a fold over those records, so a report can always be
recomputed and never disagrees with the log.

The OpenAI-compatible adapter asks for streaming usage and copies the
provider's counters. It does not tokenize the prompt locally or estimate a
count. Read provenance before treating a total as complete:

```python
call.usage_reported
report.totals.usage_source  # provider | partial | unknown
report.totals.unreported_usage_calls
```

A provider that omits usage no longer looks like a measured zero. Psych keeps
the zero-valued shape for compatibility, marks the call unreported, makes the
aggregate partial or unknown, and will not estimate a cost from those zeros.

The split matters because the four rates differ by an order of magnitude.
Collapsing to one "tokens" number makes a cache-heavy agent look identical to
one that recomputes its prompt every turn.

## Cost

```python
psych_runtime.Cost(
    amount=Decimal("0.0123"),
    currency="USD",
    model="gpt-4o",
    source="computed",
    input_amount=Decimal("0.0040"),
    output_amount=Decimal("0.0075"),
    cache_read_amount=Decimal("0.0008"),
    cache_write_amount=Decimal("0"),
)
```

`source` is `"computed"` when Psych derived it from a price table, or the
provider when a gateway reported one. A report can tell the two apart rather
than blending them.

Computed costs also retain the four category amounts used to make the total:
uncached input, output, cache read and cache write. They are optional because
many providers report only one final amount. `cost.has_breakdown` is true only
when all four are present; missing categories mean "not reported", never zero.
Adding costs preserves the breakdown only when both operands have one, so a
conversation that mixes detailed and total-only billing cannot look more
precise than its source data.

## An unknown price records `None`, never `0`

This is the rule to remember. A silent zero makes metering look correct and be
wrong, and nobody notices until an invoice does not reconcile.

`report.totals.unpriced_model_calls` counts the calls that produced no cost, so
"cost unknown" is visible rather than absorbed.

## Prices

`DEFAULT_PRICES` is a bundled, validated snapshot covering thousands of exact
provider and model ids. `DEFAULT_PRICE_CATALOG_VERSION` identifies the date of
that snapshot. Rates change, negotiated rates differ, and local models have no
universal token price, so override the models you actually pay for:

```python
from psych_runtime import DEFAULT_PRICE_CATALOG_VERSION, DEFAULT_PRICES, ModelPrice

prices = DEFAULT_PRICES.with_overrides(
    {
        "gpt-4o": ModelPrice(
            input=Decimal("2.50"),  # per MILLION tokens
            output=Decimal("10.00"),
            cache_read=Decimal("1.25"),
            cache_write=Decimal("3.125"),
            currency="USD",
        ),
    }
)
runtime = psych_runtime.Runtime(..., prices=prices)
```

Overrides affect only the models they name; configuring one price does not
silently make another model look free.

Matching is longest-prefix, so `provider-model` still prices
`provider-model-20250514` on a day the shipped table has not caught up. An
override wins over a longer shipped prefix, so your `provider-model` entry is
not out-matched by a shipped `provider-model-v2` entry.

`PriceResolver` is a port. Implement it against your own reconciled rates when a
static table is not enough.

## Provider cost versus estimated cost

Streaming OpenAI-compatible endpoints may include the monetary amount in
either `usage.cost` or `usage.response_cost` on the final usage chunk. Psych
accepts both. It does not treat response headers as the final streaming cost
because a server can write those before it has consumed the stream.

These are separate facts in every report:

- `cost.source == "provider"` means the response carried the monetary amount.
- `cost.source == "computed"` means Psych estimated it from provider-reported
  token counters and the active rate table.
- `cost is None` means neither the provider nor the rate table could price it.

Most model APIs report tokens and omit money. That is normal: the invoice can
depend on account-specific rates that are not part of the completion response.
Psych therefore prefers a provider amount when one exists and otherwise uses
the configured catalog without presenting the estimate as provider billing.

## `cost_policy`: whose number wins

```python
runtime = psych_runtime.Runtime(..., cost_policy="prefer_provider")
```

| Value | Behaviour |
|---|---|
| `"prefer_provider"` (default) | The provider's figure when it reported one, else Psych's arithmetic. |
| `"computed"` | Ignore the provider entirely. |
| `"provider_only"` | Only the provider's figure; nothing when it is silent. |

The default is `prefer_provider` because a gateway's number comes from the party
doing the billing and is computed against your real contract, negotiated rates
included, which Psych has no way to know. Recording Psych's own arithmetic over
the top of it would record the worse of two numbers. A silent provider falls
back to the table exactly as before.

## Reading it

```python
report = await psych_runtime.report(store, run_id, child_depth=2)

report.totals.usage  # this Run alone
report.totals.cost
report.totals.cost.has_breakdown if report.totals.cost else False
report.totals.cost.input_amount if report.totals.cost else None
report.totals.unpriced_model_calls
report.subtree  # this Run plus every descendant

for call in report.model_calls:
    call.model, call.usage, call.cost, call.timings
```

`totals` and `subtree` both exist because "what did this agent cost" and "what
did this request cost" stop being the same question the moment a subagent tree
exists.

## Two currencies do not sum

A Run whose model calls were priced in two currencies raises the
`inconsistent_cost` corruption reason rather than adding them. There is no
exchange rate Psych could apply that would not be wrong by the time anyone read
it.

## Metering yes, budgets no

Psych records what was spent. It does not enforce a limit and will not grow one.
Budget enforcement is a product decision with its own policy, its own appeals
process and its own failure mode when it is wrong. Read the report, decide in
your own code, and refuse the next `dispatch()`.

## Gotchas

- **Rates are per million tokens.** Getting this wrong by 10^6 is the classic
  mistake.
- **Cached input still occupies the context window** even though it bills less.
  That is why compaction's trigger counts `input + cache_read + cache_write`.
- **`report.totals.latency`** splits wall clock into `model_seconds`, `tool_seconds`,
  `compaction_seconds` and `unaccounted_seconds`. Large unaccounted time usually
  means a Run sat suspended or waited for a lease.
- **Prompt assembly order is fixed** so a mid-conversation change invalidates the
  shortest prefix. Changing that order is a performance change and gets reviewed
  as one.
