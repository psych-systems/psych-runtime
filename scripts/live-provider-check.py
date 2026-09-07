#!/usr/bin/env python
"""One real Run against one real provider. Run it by hand; it is not a test.

The project forbids a test making a network call, and that rule is worth more
than this check: a suite that reaches the internet is a suite that fails for
reasons nobody can reproduce. So the provider dialects this found are pinned
in ``tests/functional/test_openai_compat.py`` against a local socket, and this
script exists for the thing those tests cannot do, which is meet a provider
nobody has met yet.

Run it when adding a provider, or when a provider changes something. Three
dialect bugs came out of running it the first time, none of which the suite
could see, because the suite reproduced OpenAI's chunk layout exactly and the
adapter was therefore only ever checked against its own assumptions.

Credentials come from the environment and are never written down:

    export PSYCH_LIVE_BASE_URL=https://api.cloudflare.com/client/v4/accounts/<id>/ai/v1
    export PSYCH_LIVE_API_KEY=<token>
    export PSYCH_LIVE_MODEL='@cf/meta/llama-3.3-70b-instruct-fp8-fast'
    uv run python scripts/live-provider-check.py

It exercises the path a consumer actually cares about: publish, dispatch, a
turn that calls a tool, a turn that reads the tool's result, and a report with
token counts on it.
"""

from __future__ import annotations

import asyncio
import os
import sys

import psych_runtime
from psych_runtime.core.validation import ValidationContext
from psych_runtime.model.egress import HttpTransport
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.tools.registry import ToolRegistry

SCOPE = psych_runtime.Scope(tenant="live-check", principal="operator")

registry = ToolRegistry()


@registry.register(annotations={"read-only"})
async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "shipped", "carrier": "DHL"}


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} is not set. See this script's docstring for the three it needs.")
    return value


async def main() -> int:
    base_url, api_key, model_name = (
        _env("PSYCH_LIVE_BASE_URL"),
        _env("PSYCH_LIVE_API_KEY"),
        _env("PSYCH_LIVE_MODEL"),
    )

    store = InMemoryStore()
    spec = psych_runtime.AgentSpec(
        name="live-check",
        instructions="Help with the order. Use lookup_order when asked about one. Be brief.",
        model=psych_runtime.ModelRef(model=model_name, temperature=0.0),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
        limits=psych_runtime.Limits(max_turns=6, deadline_seconds=120),
    )
    version = await psych_runtime.publish(
        store, spec, context=ValidationContext(registered_tools=registry.names)
    )

    transport = HttpTransport()
    runtime = Runtime(
        store=store,
        model=OpenAICompatibleClient(
            base_url=base_url, transport=transport, scope=SCOPE, api_key=api_key
        ),
        registry=registry,
    )
    worker = Worker(store, runtime, poll_interval=0.05, supervisor_interval=0.5)
    worker_task = asyncio.create_task(worker.run())

    run = await psych_runtime.dispatch(
        store, version, SCOPE, input={"message": "Where is order A1?"}
    )
    said = [
        record.text
        async for record in psych_runtime.stream(store, run.run_id)
        if record.type == "model_call_finished" and record.text
    ]
    report = await psych_runtime.report(store, run.run_id)

    worker.stop()
    await asyncio.wait_for(worker_task, timeout=20)
    await transport.aclose()

    usage = report.totals.usage
    # A Run still in flight has no terminal state and a call still running has
    # no outcome. Neither should be reachable after the stream ends, but this
    # script exists to catch the provider behaving unexpectedly, so it reports
    # what it found rather than raising an AttributeError over it.
    terminal = report.terminal_state.value if report.terminal_state else "still running"
    calls = [
        (call.tool, call.outcome.value if call.outcome else "unsettled")
        for call in report.tool_calls
    ]
    print(f"  model      : {model_name}")
    print(f"  terminal   : {terminal}")
    print(f"  said       : {' | '.join(text.strip() for text in said)}")
    print(f"  tool calls : {calls}")
    print(f"  usage      : input={usage.input} output={usage.output} cache_read={usage.cache_read}")
    print(f"  cost       : {report.totals.cost}")

    problems: list[str] = []
    if terminal != "completed":
        problems.append(f"the Run did not complete: {terminal}")
    if not any(call.tool == "lookup_order" for call in report.tool_calls):
        problems.append("the model never called the tool, so the tool path is unproven")
    if usage.input == 0 or usage.output == 0:
        # The bug this script was written to catch. A zero here is not a small
        # inaccuracy: a known price times zero tokens is a confident zero cost,
        # so metering looks correct and is wrong.
        problems.append(f"token usage came back zero (input={usage.input}, output={usage.output})")

    if problems:
        print("\n  FAILED:")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    print("\n  a real Run completed against a real provider, with real token counts")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
