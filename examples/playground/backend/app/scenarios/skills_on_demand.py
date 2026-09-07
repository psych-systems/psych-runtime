"""DESIGN.md §16: a skill's description is always in the prompt, its body is not.

The claim worth proving is an economic one, and it is the reason skills exist
at all rather than being a folder of extra instructions. An agent can carry a
procedure of any length, pay for one line of it on every turn, and pay for the
rest only on the turns that actually need it.

That is easy to assert loosely and easy to get wrong quietly: an
implementation that put every body in the prompt would still pass "the model
answered correctly", and would have thrown away the whole point. So this
scenario checks the negative as hard as the positive -- the body must be
absent from turn one -- and then checks that the model could reach it anyway.

Also covers the two behaviours DESIGN.md §16 asks for that a naive
implementation would get backwards: loading the same skill twice still returns
the body, and an unknown name is a tool result rather than a failed Run.
"""

from __future__ import annotations

import psych_runtime
from app.scenarios.base import Checks, ProgressFn, ScenarioContext, ScenarioInfo, ScenarioResult
from app.scenarios.support import drive, records_dict, report_dict
from psych_runtime.core.records import TerminalState
from psych_runtime.core.spec import AgentSpec, ModelRef, Skill
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

INFO = ScenarioInfo(
    id="skills-on-demand",
    title="Skills load on demand",
    proves=(
        "A skill's one-line description is in the system prompt on every "
        "turn, while its instructions are not: the model calls load_skill "
        "to fetch a body it can see it needs, so a long procedure costs "
        "nothing on the conversations that never use it."
    ),
    design_ref="§16",
    requires=(),
)

REFUND_BODY = (
    "1. Confirm the order shipped less than 30 days ago.\n"
    "2. Refund to the original payment method. Never store credit.\n"
    "3. Anything above $500 needs a manager: see [[skill:escalation]].\n"
    "4. Record the reason code on the order before closing it."
)

ESCALATION_BODY = "Page the on-call manager in #escalations and wait for an acknowledgement."


async def check_availability(ctx: ScenarioContext) -> str | None:
    _ = ctx
    return None


async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult:
    _ = ctx
    checks = Checks()
    store = InMemoryStore()
    spec = AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=ModelRef(model="fake-standard"),
        skills=(
            Skill(
                name="escalation",
                description="When to involve a manager",
                body=ESCALATION_BODY,
            ),
            Skill(
                name="refund-policy",
                description="When a refund is allowed, and how to issue one",
                body=REFUND_BODY,
            ),
        ),
    )

    await emit("script", "scripting a model that reads the index, then asks for one body")
    model = (
        FakeModel()
        .turn(tool_calls=[("load_skill", {"name": "refund-policy"})])
        # Asked for again, because a model that lost the body out of its
        # context needs it back rather than a reminder that it once had it.
        .turn(tool_calls=[("load_skill", {"name": "refund-policy"})])
        # And a name that does not exist, which must not fail the Run.
        .turn(tool_calls=[("load_skill", {"name": "no-such-skill"})])
        .turn(text="Refunds go back to the original card, within 30 days.")
    )

    run_id = await drive(store, spec, model, ToolRegistry())
    report = await psych_runtime.report(store, run_id)

    checks.require(
        "the Run completed",
        report.terminal_state is TerminalState.COMPLETED,
        f"terminal_state={report.terminal_state.value if report.terminal_state else None!r}",
    )

    await emit("prompt", "reading the system prompt the first turn was actually sent")
    first_prompt = model.requests[0].messages[0].content
    checks.require(
        "both skills' descriptions were in the first prompt, so the model knew they existed",
        "When a refund is allowed" in first_prompt and "When to involve a manager" in first_prompt,
        f"prompt was {len(first_prompt)} characters",
    )
    checks.require(
        "neither body was in the prompt -- this is the saving, and the whole point",
        "original payment method" not in first_prompt and "#escalations" not in first_prompt,
        (
            f"the two bodies total {len(REFUND_BODY) + len(ESCALATION_BODY)} characters "
            f"and the whole prompt is {len(first_prompt)}"
        ),
    )
    checks.require(
        "load_skill was offered, so the model had a way to fetch what it could see it needed",
        "load_skill" in {tool.name for tool in model.requests[0].tools},
        f"turn 1 offered: {sorted(tool.name for tool in model.requests[0].tools)}",
    )

    await emit("load", "checking what came back from each load_skill call")
    calls = [call for call in report.tool_calls if call.tool == "load_skill"]
    checks.require(
        "all three calls were answered, the unknown name included",
        len(calls) == 3,
        f"load_skill calls: {len(calls)}",
    )

    records = records_dict(await store.read(run_id))
    results = [
        str(record.get("result", ""))
        for record in records
        if record.get("type") == "tool_call_finished"
    ]
    checks.require(
        "the first call returned the body",
        any("original payment method" in result for result in results),
        "no tool result carried the skill's instructions",
    )
    checks.require(
        "asking twice returned the body again rather than only a reminder",
        sum("original payment method" in result for result in results) == 2,
        (
            "a model that asks again has lost the body out of its context, so a bare "
            "'you already loaded this' would answer the wrong question"
        ),
    )
    checks.require(
        "an unknown skill name came back as a tool result, not a failed Run",
        report.terminal_state is TerminalState.COMPLETED
        and any("no-such-skill" in result for result in results),
        "the unknown name should be answerable, not fatal",
    )

    log = await psych_runtime.records(store, run_id)
    return checks.result(
        f"{len(REFUND_BODY) + len(ESCALATION_BODY)} characters of instructions stayed out "
        f"of a {len(first_prompt)}-character prompt until the model asked for them",
        run_ids=[str(run_id)],
        report={"report": report_dict(report), "records": records_dict(log)},
    )
