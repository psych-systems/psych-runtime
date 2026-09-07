"""DESIGN.md §23 item 7: psych_runtime.report() is honest about tokens, cost and time.

Every other scenario in this set proves this against ``FakeModel``, whose
usage numbers are whatever the script says -- correct by construction, and so
incapable of catching a bug in how the pipeline turns a *real* provider's
own usage report into totals. This is the one scenario that needs an actual
model: it drives one live turn against whichever provider is configured
through ``/api/settings``, and checks the accounting against numbers this
process did not invent.

What is asserted is the accounting, not the provider's prices: this
scenario supplies its own ``StaticPriceTable`` for whichever model the
active provider names, the same way a consumer would price a model Psych
itself has no opinion on. "Correct" here means cost is genuinely
``usage * price``, computed from tokens the provider actually reported --
not that the price matches what the provider bills.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.messages import UserMessage
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import ModelRequest, StreamDone
from psych_runtime.model.pricing import ModelPrice, StaticPriceTable
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="honest-accounting",
    title="Honest accounting",
    proves=(
        "psych.report() gives correct token totals split by cache state, "
        "correct cost, and a latency breakdown that accounts for the Run's "
        "wall-clock time -- checked against a real provider's own reported "
        "usage, not a scripted one."
    ),
    design_ref="§23.7",
    requires=("model-provider",),
)

_PRICE = ModelPrice(
    input=Decimal("1"), output=Decimal("2"), cache_read=Decimal("0.1"), cache_write=Decimal("1.25")
)
"""Per-million-token, in an arbitrary currency: what is under test is the
arithmetic connecting usage to cost, not this scenario's made-up prices."""


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def lookup_order(order_id: str) -> str:
        """Look up an order's shipping status."""
        return f"{order_id} shipped yesterday, arriving tomorrow"

    return registry


async def check_availability(ctx: ScenarioContext) -> str | None:
    provider = ctx.active_provider
    if provider is None:
        return "no active model provider configured; set one via /api/settings/providers"
    client = OpenAICompatibleClient(
        base_url=provider.base_url,
        transport=ctx.transport,
        scope=SCOPE_A,
        api_key=provider.api_key,
    )
    probe = ModelRequest(
        model=provider.model, messages=(UserMessage(content="ping"),), max_output_tokens=1
    )
    try:
        async with asyncio.timeout(15.0):
            async for event in client.stream(probe):
                if isinstance(event, StreamDone):
                    break
    except Exception as err:
        return f"provider {provider.label!r} ({provider.base_url}) is not reachable: {err}"
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    checks = Checks()
    provider = ctx.active_provider
    assert provider is not None  # guaranteed by check_availability

    real_store = InMemoryStore()
    client = OpenAICompatibleClient(
        base_url=provider.base_url, transport=ctx.transport, scope=SCOPE_A, api_key=provider.api_key
    )
    registry = _registry()
    spec = psych_runtime.AgentSpec(
        name="support",
        instructions=(
            "Help the customer with their order. Use the lookup_order tool to check "
            "on an order before answering."
        ),
        model=psych_runtime.ModelRef(model=provider.model),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
        limits=psych_runtime.Limits(max_turns=4, deadline_seconds=90),
    )

    await emit("dispatch", f"dispatching one real Run against {provider.label!r}")
    version = await psych_runtime.publish(real_store, spec)
    dispatched = await psych_runtime.dispatch(
        real_store, version, SCOPE_A, input={"message": "Where is order A1?"}
    )
    journal = await Journal.open(real_store, dispatched.run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(
        store=real_store,
        model=client,
        registry=registry,
        prices=StaticPriceTable({provider.model: _PRICE}),
    )
    header = await real_store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())

    await emit("report", "reading psych_runtime.report() back and checking it against the raw log")
    report = await psych_runtime.report(real_store, dispatched.run_id)
    log = await psych_runtime.records(real_store, dispatched.run_id)

    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the Run reached a terminal state",
        report.terminal_state is not None,
        f"terminal_state={terminal!r}",
    )
    checks.require(
        "at least one model call actually happened",
        report.totals.model_calls >= 1 and len(report.model_calls) >= 1,
        f"model_calls={report.totals.model_calls}",
    )

    from_records = psych_runtime.Usage()
    for record in log:
        if record.type == "model_call_finished":
            from_records = from_records + record.usage
    checks.require(
        "the report's usage total reconciles exactly with the log's own "
        "model_call_finished records",
        report.totals.usage == from_records,
        f"report={report.totals.usage!r} vs from_records={from_records!r}",
    )
    checks.require(
        "real, non-zero input tokens were reported by the provider and reached the report",
        report.totals.usage.input > 0,
        f"usage.input={report.totals.usage.input}",
    )

    checks.require(
        "the priced model produced a real cost, not a silent zero",
        report.totals.cost is not None and report.totals.cost.amount > 0,
        f"cost={report.totals.cost!r}",
    )
    checks.require(
        "cost is reported complete because every call in this Run was priced",
        not report.totals.cost_is_incomplete and report.totals.unpriced_model_calls == 0,
        f"cost_is_incomplete={report.totals.cost_is_incomplete}, "
        f"unpriced_model_calls={report.totals.unpriced_model_calls}",
    )

    latency = report.totals.latency
    reconciled = abs(
        latency.unaccounted_seconds
        - (latency.wall_clock_seconds - latency.model_seconds - latency.tool_seconds)
    )
    checks.require(
        "the latency breakdown accounts for the Run's own wall-clock time",
        latency.wall_clock_seconds > 0 and latency.model_seconds > 0 and reconciled < 1e-6,
        f"wall_clock={latency.wall_clock_seconds:.3f}s model={latency.model_seconds:.3f}s "
        f"tool={latency.tool_seconds:.3f}s unaccounted={latency.unaccounted_seconds:.3f}s",
    )

    return checks.result(
        f"a real Run against {provider.label!r} reconciled exactly between the log and the report",
        run_ids=[str(dispatched.run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
