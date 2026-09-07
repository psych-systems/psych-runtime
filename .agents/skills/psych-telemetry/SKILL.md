---
name: psych-telemetry
description: >-
  Trace a Psych Run with OpenTelemetry: the `Telemetry` port, the `OTelTelemetry`
  adapter over `gen_ai.*` semantic conventions, the eight declared span names and
  their nesting, and the conformance tests that stop spans drifting from their
  schema. Use whenever someone adds tracing or observability to Psych, wires
  OTel, asks what spans and attributes Psych emits, wants to see why a Run was
  slow, or asks whether Psych ships a collector or dashboard (it does not). Read
  before wiring telemetry, because Psych takes only the OTel API and SDK and
  never chooses an exporter, and because the span schema is declared and checked
  rather than emitted ad hoc.
---

# Telemetry

A port with a no-op default. Psych emits spans; it owns no collector, no
exporter configuration and no dashboard.

## Wiring

```sh
pip install "psych-runtime[otel]"    # opentelemetry-api and opentelemetry-sdk only
```

The extra deliberately does **not** include an exporter. A library must not
choose your exporter. Install and configure the one you already run
(`opentelemetry-exporter-otlp-proto-http`, say) in your own application.

```python
from psych_runtime.telemetry.otel import OTelTelemetry

runtime = psych_runtime.Runtime(
    store=store, model=model, registry=registry, telemetry=OTelTelemetry()
)
```

`OTelTelemetry()` takes the global tracer by default, or `OTelTelemetry(tracer)`
with one you made, and `tracer_name="psych"` names it.

Without `telemetry=`, the default is a no-op. Nothing is emitted and nothing
fails.

## The eight spans

| Span | Parent |
|---|---|
| `psych_runtime.run` | root |
| `psych_runtime.attempt` | `psych_runtime.run` |
| `psych_runtime.turn` | `psych_runtime.attempt` or `psych_runtime.step` |
| `psych_runtime.model_call` | `psych_runtime.turn` |
| `psych_runtime.tool_call` | `psych_runtime.turn` |
| `psych_runtime.step` | workflow steps |
| `psych_runtime.subagent_delegation` | delegation |
| `psych_runtime.compaction` | a compaction |

An attempt under a run, a turn under an attempt, model and tool calls under a
turn: the shape of the trace is the shape of the execution, so a slow Run reads
directly off the waterfall.

## Attributes

`gen_ai.*` wherever an OpenTelemetry semantic convention exists, so your
existing LLM dashboards work without a mapping layer: `gen_ai.system`,
`gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`.

`psych.*` for what those conventions do not cover: `psych_runtime.run.id`,
`psych_runtime.scope.tenant`, `psych_runtime.version.hash`, `psych_runtime.run.terminal_state`,
`psych_runtime.worker.id`, `psych_runtime.attempt.number`,
`psych_runtime.attempt.reclaimed_expired_lease`, `psych_runtime.turn.number`, `psych_runtime.step.id`,
`psych_runtime.usage.cache_read_tokens`, `psych_runtime.usage.cache_write_tokens`,
`psych_runtime.cost.usd`.

`reclaimed_expired_lease` is the one to alert on. It means a Worker died and
another took over, which is the system working and also something you want to
know the rate of.

## The schema is declared and checked

Span names and attributes live in a schema, and conformance tests check the code
against it. Spans cannot drift from their contract as the code changes, which is
the failure mode that makes tracing quietly useless: a dashboard built on an
attribute that silently stopped being emitted.

If you add a span or an attribute in this repository, add it to the schema in
the same change or the conformance tests fail.

## Implementing the port yourself

`Telemetry` is a `Protocol` with `start_span(name, attributes=...)` returning an
async context manager. `TelemetrySpan` adds setting attributes and status on the
live span. Implement it structurally against whatever you already run; nothing
requires OpenTelemetry.

## Telemetry is not the report

They answer different questions and neither replaces the other.

- **Telemetry** is for an operator watching a fleet: latency distributions, error
  rates, which tenant is hot right now.
- **`psych_runtime.report()`** is for one Run: what it did, what it cost, in a typed
  object read from the log. It never depends on a span having been exported.

Everything a Run *did* is in its log and reaches you through `report()` and this
port, never through Psych's own logging. Psych logs sparingly and only from
`psych_runtime.runtime`: things a person operating a fleet needs that are not Records.

## Logging

Psych attaches a `NullHandler` to its root logger, so a consumer who configured
no logging gets no uninvited output. Configure `logging.getLogger("psych")` like
any other library.

## Gotchas

- **A slow Run with large `report.totals.latency.unaccounted_seconds`** usually sat
  suspended or waited for a lease. The trace shows the gap; the report names it.
- **Tenant is on the span,** so per-tenant latency needs no join.
- **`psych_runtime.cost.usd` is absent when the price is unknown,** the same discipline
  as `cost=None`. It is never zero.
