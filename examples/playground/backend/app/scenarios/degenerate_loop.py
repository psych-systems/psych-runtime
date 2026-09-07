"""A known gap, shown rather than hidden: a model repeating a *successful* tool
call until the turn budget is gone, and nothing in Psych stops it.

Found against a real model (Cloudflare Workers AI,
``llama-3.3-70b-instruct-fp8-fast``): asked "Where is order A1?" against an
agent with one working ``lookup_order`` tool, the model called it 32 times
with byte-identical arguments, every call returned ``ok``, and the Run ended
``failed``/``budget_exhausted`` after burning 31,120 input tokens to answer a
question one call answers. The failure-streak
guard names the reason plainly: that guard counts *consecutive failures* of
a tool (DESIGN.md §10.6), and every one of these calls succeeded, so the
streak never advances and the guard never has anything to trip on. This is
the guard behaving exactly as specified -- and exactly why it cannot help
here.

The gap is open and no fix is scheduled. Do not read a passing result below
as "Psych caught it" -- the assertions are written the other way around: they
prove the guard's blindness structurally, from a real Run's own state,
whether or not the live model in front of this particular run happens to
loop today. Whether it actually loops is left to the model in front of it,
exactly as it was the day this was found, and is reported either way rather
than asserted on -- a well-behaved model on a given day does not close this
gap, and a badly-behaved one does not need this scenario's help to prove it.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import SCOPE_A, records_dict, report_dict
from psych_runtime.core.messages import UserMessage
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import ModelRequest, StreamDone
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="degenerate-loop",
    title="The loop that nothing stops",
    proves=(
        "A model repeating one already-successful tool call is not caught by "
        "the failure-streak guard, because that guard counts consecutive "
        "failures and every one of these calls succeeds. Psych does not "
        "currently stop this; this scenario shows the gap rather "
        "than hiding it, and reports what the live model in front of it "
        "actually did rather than asserting it must loop."
    ),
    design_ref="§23",
    requires=("model-provider",),
)

_NAMES_A_REPETITION_GUARD_MIGHT_USE = (
    "repetition_threshold",
    "duplicate_call_threshold",
    "call_repetition_threshold",
    "identical_call_threshold",
)
"""If DESIGN.md ever grows a real fix for this, ``Limits`` gains a field
named something like one of these. Their absence today is the structural
half of what this scenario proves; when one of them shows up, this check
starts failing on purpose, which is the signal that this module needs
updating rather than deleting."""


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def lookup_order(order_id: str) -> str:
        """Look up an order's shipping status."""
        return f"{order_id}: shipped"

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

    await emit("structural", "checking Limits for a repetition guard that does not exist")
    present = set(_NAMES_A_REPETITION_GUARD_MIGHT_USE) & set(psych_runtime.Limits.model_fields)
    checks.require(
        "Limits carries a turn budget but no field bounding identical repeated calls",
        not present,
        "no repetition guard field found (the gap is still open)"
        if not present
        else f"found {sorted(present)} -- the gap may already be fixed; update this scenario",
    )

    store = InMemoryStore()
    client = OpenAICompatibleClient(
        base_url=provider.base_url, transport=ctx.transport, scope=SCOPE_A, api_key=provider.api_key
    )
    registry = _registry()
    spec = psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer with their order using the tools available.",
        model=psych_runtime.ModelRef(model=provider.model),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
        limits=psych_runtime.Limits(max_turns=10, deadline_seconds=120),
    )

    await emit("dispatch", f"asking a real model ({provider.label!r}) where order A1 is")
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(
        store, version, SCOPE_A, input={"message": "Where is order A1?"}
    )
    journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=client, registry=registry)
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())

    report = await psych_runtime.report(store, dispatched.run_id)
    state = await psych_runtime.state(store, dispatched.run_id)
    terminal = report.terminal_state.value if report.terminal_state else None

    call_keys = [(c.tool, json.dumps(c.arguments, sort_keys=True)) for c in report.tool_calls]
    repeats = Counter(call_keys)
    most_repeated = repeats.most_common(1)
    repeat_count = most_repeated[0][1] if most_repeated else 0

    checks.require(
        "the failure-streak guard never tripped on this Run -- every call succeeded",
        report.failure_streak_trips == (),
        f"failure_streak_trips={report.failure_streak_trips!r}",
    )
    checks.require(
        "the per-tool failure streak stayed at zero throughout, no matter how often "
        "lookup_order was called, because a success always clears it",
        state.failure_streaks.get("lookup_order", 0) == 0,
        f"failure_streaks={state.failure_streaks!r}",
    )
    checks.require(
        "if the model did repeat one call, nothing in the log challenged it for repeating "
        "-- there is no advisory to withdraw a tool for succeeding too many times",
        repeat_count < 2 or report.failure_streak_trips == (),
        f"most-repeated (tool, arguments) pair called {repeat_count} time(s); "
        f"failure_streak_trips={report.failure_streak_trips!r}",
    )

    summary = (
        f"{len(report.tool_calls)} tool call(s), most-repeated pair called {repeat_count} "
        f"time(s), {report.totals.model_calls} model call(s), terminal_state={terminal!r}, "
        f"input_tokens={report.totals.usage.input}"
    )
    if repeat_count >= 3:
        summary = "reproduced the known gap: " + summary
    else:
        summary = (
            "this run's live model did not reproduce the loop (that is expected to vary "
            "run to run) -- structural checks still confirm the gap exists: " + summary
        )

    log = await psych_runtime.records(store, dispatched.run_id)
    return checks.result(
        summary,
        run_ids=[str(dispatched.run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
