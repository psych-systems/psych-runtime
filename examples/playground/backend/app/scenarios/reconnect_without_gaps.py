"""DESIGN.md §23 item 4: a reconnecting client misses nothing.

A dashboard tailing a Run's record stream drops its connection constantly --
a laptop sleeps, a proxy times out, a tab reloads -- and ``GET
/api/runs/{id}/stream?after=N`` (``psych_runtime.stream``) is what lets it pick back
up without asking the caller to re-derive what it already saw. The property
this checks is narrower than "streaming works": it is that *every* cut point
across a whole Run's log, not just a convenient one, reconnects to exactly
the records after it, with no duplicate and no gap. DESIGN.md §23 item 3
already puts one interrupt in the log this scenario's Run does not
exercise; this scenario instead sweeps every seq boundary a real Run
produces, which is the check ``tests/e2e/test_regression_gate.py``'s own
``test_5_a_reconnecting_client_misses_nothing_at_any_cut_point`` performs.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import drive, lookup_registry, records_dict, support_agent_spec
from psych_runtime.core.records import RunSettled
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

INFO = ScenarioInfo(
    id="reconnect-without-gaps",
    title="Reconnect without gaps",
    proves=(
        "A client reconnecting to the record stream with after=N receives "
        "every later record and misses none, checked at every possible cut "
        "point across a whole Run."
    ),
    design_ref="§23.4",
    requires=(),
)


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()

    await emit("drive", "running a whole Run to completion, tool call included")
    model = FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 shipped.")
    run_id = await drive(store, support_agent_spec(), model, lookup_registry())

    await emit("stream", "reading the whole record stream once, from the start")
    whole = [record async for record in psych_runtime.stream(store, run_id)]
    checks.require(
        "sequence numbers are contiguous from 1",
        [r.seq for r in whole] == list(range(1, len(whole) + 1)),
        f"seqs={[r.seq for r in whole]}",
    )
    checks.require(
        "the stream ends on the Run's terminal record",
        len(whole) > 0 and isinstance(whole[-1], RunSettled),
        f"last record type={whole[-1].type if whole else None!r}",
    )

    await emit("cut", f"reconnecting after every one of the {len(whole)} cut points")
    mismatches: list[int] = []
    for cut in range(len(whole)):
        resumed = [r async for r in psych_runtime.stream(store, run_id, after=cut)]
        if [r.seq for r in resumed] != [r.seq for r in whole[cut:]]:
            mismatches.append(cut)
    checks.require(
        "every after=N reconnection returns exactly the records after N, in order",
        mismatches == [],
        "all cut points matched" if not mismatches else f"mismatched at cuts: {mismatches}",
    )

    return checks.result(
        f"reconnecting at all {len(whole)} cut points reproduced the tail of the log exactly",
        run_ids=[str(run_id)],
        report={"records": records_dict(whole)},
    )
