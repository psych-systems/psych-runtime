"""DESIGN.md §23 item 1: one Spec, two authors.

The same agent, built once through ``psych_runtime.builder`` and once from a plain
dict a JSON editor could have produced, must publish to the same Version hash
and then run identically. This is the whole point of DESIGN.md §4's four
authoring forms converging on one model: an agent a person clicked together in
a UI and the same agent a file in source control describes are not two
different things Psych treats differently, they are the same Spec, and the
hash is how a caller checks that without reading either one by eye.

``psych_runtime.builder``'s own README states the rule this proves: "The same agent
built in Python and built from a dict must produce the same Version hash."
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import lookup_registry, records_dict, report_dict
from psych_runtime.builder import agent
from psych_runtime.core.spec import AgentSpec
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

INFO = ScenarioInfo(
    id="same-spec-two-authors",
    title="One Spec, two authors",
    proves=(
        "The same agent built through psych_runtime.builder and the same agent built "
        "from a plain dict publish to the same Version hash and run "
        "identically."
    ),
    design_ref="§23.1",
    requires=(),
)


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()

    await emit("build", "building the agent through psych_runtime.builder.agent(...)")
    registry = lookup_registry()
    built = (
        agent("support", registry=registry)
        .instructions("Help the customer with their order.")
        .model("fake-standard")
        .tool_by_name("lookup")
        .build()
    )

    await emit("build", "validating the same agent from a hand-written dict")
    from_dict = AgentSpec.model_validate(
        {
            "kind": "agent",
            "name": "support",
            "instructions": "Help the customer with their order.",
            "model": {"model": "fake-standard"},
            "tools": [{"kind": "code", "name": "lookup"}],
        }
    )

    await emit("publish", "publishing both Specs")
    version_from_builder = await psych_runtime.publish(store, built)
    version_from_dict = await psych_runtime.publish(store, from_dict)

    checks.require(
        "the builder form and the dict form publish to the same Version hash",
        version_from_builder.hash == version_from_dict.hash,
        f"builder hash={version_from_builder.hash!r}, dict hash={version_from_dict.hash!r}",
    )
    checks.require(
        "republishing the dict form again returns the existing Version, not a duplicate",
        (await psych_runtime.publish(store, from_dict)).hash == version_from_dict.hash,
        "publish() is idempotent on an identical Spec",
    )

    await emit("run", "dispatching the dict-authored Version against a scripted model")
    model = (
        FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 has shipped.")
    )
    scope = psych_runtime.Scope(tenant="acme")
    dispatched = await psych_runtime.dispatch(
        store, version_from_dict.hash, scope, input={"message": "where is A1?"}
    )
    journal = await Journal.open(store, dispatched.run_id, scope)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=registry)
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())

    await emit("verify", "reading the report back for the Run just driven")
    report = await psych_runtime.report(store, dispatched.run_id)
    checks.require(
        "the Run pinned the shared Version hash",
        report.version_hash == version_from_dict.hash,
        f"report.version_hash={report.version_hash!r}",
    )
    # A Run still in flight has no terminal state. It should be settled by now,
    # but a scenario that raises AttributeError instead of reporting a failed
    # assertion tells the reader nothing about what went wrong.
    terminal = report.terminal_state.value if report.terminal_state else None
    checks.require(
        "the Run completed by calling the tool named in either authoring form",
        terminal == "completed" and len(report.tool_calls) == 1,
        f"terminal_state={terminal!r}, tool_calls={len(report.tool_calls)}",
    )

    log = await psych_runtime.records(store, dispatched.run_id)
    return checks.result(
        "the builder and the dict agree on a Version hash, and that Version runs",
        run_ids=[str(dispatched.run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
