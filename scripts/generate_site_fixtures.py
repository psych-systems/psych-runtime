#!/usr/bin/env python
"""Generate the record log the website's approval demonstration steps through.

psychruntime.com shows a support agent being stopped before a refund, and a
visitor approving or denying it. The page must not draw that from memory: a
hand-typed log drifts from the runtime the moment a field is renamed, and a
demo that shows records the store never wrote is the opposite of the point.

So this executes the Run. Twice, against ``FakeModel`` with the same tools and
approval selectors the ``tour`` template uses, once approving and once denying,
and writes both logs plus the library's own ``report()`` totals to
``web/site/content/site/approval-run.json``. The site folds the records into
the state it displays and asserts that its fold agrees with the report, so the
arithmetic on the page is the library's arithmetic.

``scripts/check.sh`` runs this with ``--check`` and fails on a diff, the same
way it does for the generated reference: change a record type and the demo
goes red here rather than quietly showing last month's protocol.

Two things are normalised, and the file says so in its header. Identifiers
carry a timestamp and random bytes, so they are replaced with stable names in
order of first appearance. Wall-clock fields (``at``, ``deadline_at``,
``expires_at``, ``duration_seconds``, ``timings``) differ every run; they are
replaced with the offset the runtime was configured with where one exists and
removed otherwise. The prompt as sent is also dropped: it is several kilobytes
per model call and the page does not show it. Everything else is the record as
the store held it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psych_runtime
from psych_runtime.core.usage import Usage
from psych_runtime.memory.store_backed import StoreBackedMemory
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "web" / "site" / "content" / "site" / "approval-run.json"

INPUT = "refund order A1, it arrived broken"
MODEL = "gpt-4o-mini"
DEADLINE_SECONDS = 120
APPROVER = "manager-7"

REFUND_POLICY = """Refunds are allowed within 30 days of delivery.
Anything over 10000 cents needs a manager, whatever the customer says.
Always state the amount back to the customer before issuing."""


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "delivered", "total_cents": "4200"}


async def issue_refund(order_id: str, cents: int) -> str:
    """Refund an order. Destructive: this moves real money."""
    return f"refunded {cents} on {order_id}"


SPEC = psych_runtime.AgentSpec(
    name="support",
    instructions=(
        "Help the customer with their order. "
        "Read [[skill:refund-policy]] before issuing any refund."
    ),
    model=psych_runtime.ModelRef(model=MODEL),
    tools=(
        psych_runtime.CodeTool(name="lookup_order"),
        psych_runtime.CodeTool(name="issue_refund", interruptible=False),
    ),
    skills=(
        psych_runtime.Skill(
            name="refund-policy",
            description="When a refund is allowed, and the limit on one",
            body=REFUND_POLICY,
        ),
    ),
    limits=psych_runtime.Limits(max_turns=10, deadline_seconds=DEADLINE_SECONDS),
)


def script(branch: str) -> FakeModel:
    """The model's side of the conversation, with usage a real provider would report.

    The token counts are scripted, since the fake model reads no prompt, and
    they are the one thing in the fixture that is chosen rather than measured.
    The cost is not: the runtime prices each call from ``DEFAULT_PRICES`` as it
    is recorded.
    """
    model = (
        FakeModel()
        .turn(
            text="Checking the order.",
            tool_calls=[("lookup_order", {"order_id": "A1"})],
            usage=Usage(input=412, output=23),
        )
        .turn(
            tool_calls=[("load_skill", {"name": "refund-policy"})],
            usage=Usage(input=61, output=14, cache_read=430),
        )
        .turn(
            text="That is within policy.",
            tool_calls=[("issue_refund", {"order_id": "A1", "cents": 4200})],
            usage=Usage(input=96, output=31, cache_read=491),
        )
    )
    if branch == "approved":
        return model.turn(
            tool_calls=[("remember", {"content": "prefers email over SMS"})],
            usage=Usage(input=44, output=19, cache_read=618),
        ).turn(
            text="Refunded $42.00 on order A1. I will email you from now on.",
            usage=Usage(input=38, output=27, cache_read=681),
        )
    return model.turn(
        text=(
            "I was not able to issue that refund. A colleague needs to look at it. "
            "Shall I open a ticket?"
        ),
        usage=Usage(input=52, output=34, cache_read=618),
    )


async def run_branch(branch: str) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    """Execute one branch across two Workers and return its log and report.

    Two ``session()`` blocks over one store on purpose. The first Worker runs
    the agent up to the approval and exits; the decision is delivered with no
    Worker alive; the second Worker claims the suspended Run and finishes it.
    That is the shape a deployment has, where the process that asked is rarely
    the one that resumes, and it is why the log shows two ``attempt_started``
    records from two Workers.
    """
    scope = psych_runtime.Scope(tenant="acme", principal="user-42")
    store = InMemoryStore()
    memory = StoreBackedMemory(store)
    registry = psych_runtime.ToolRegistry()
    registry.register(annotations={"read-only"})(lookup_order)
    registry.register(interruptible=False, annotations={"destructive"})(issue_refund)
    model = script(branch)

    def open_session() -> Any:
        return psych_runtime.session(
            model,
            store=store,
            registry=registry,
            tenant="acme",
            principal="user-42",
            approval_selectors=("@destructive",),
            memory=memory,
            end_user_id="user-42",
            prices=psych_runtime.DEFAULT_PRICES,
        )

    async with open_session() as first:
        started = await first.start(SPEC, INPUT)
        run_id = started.run_id
        for _ in range(100):
            await asyncio.sleep(0.05)
            status = await psych_runtime.status(store, run_id, scope=scope)
            if status.pending_approval is not None:
                break
        else:
            raise RuntimeError(f"{branch}: the Run never suspended for approval")

    await psych_runtime.resume(store, run_id, approved=branch == "approved", by=APPROVER)

    async with open_session() as second:
        view = await second.wait(run_id)
        if not view.finished:
            raise RuntimeError(f"{branch}: the Run did not finish after the decision")

    log = await psych_runtime.records(store, run_id, scope=scope)
    report = await psych_runtime.report(store, run_id, scope=scope)
    if report.terminal_state != "completed":
        raise RuntimeError(f"{branch}: settled {report.terminal_state}, expected completed")

    records = [record.model_dump(mode="json") for record in log]
    totals = {
        "terminal_state": str(report.terminal_state),
        "usage": {
            "input": report.totals.usage.input,
            "output": report.totals.usage.output,
            "cache_read": report.totals.usage.cache_read,
        },
        "cost": None
        if report.totals.cost is None
        else {"amount": str(report.totals.cost.amount), "currency": report.totals.cost.currency},
        "tool_calls": [
            {"tool": call.tool, "outcome": None if call.outcome is None else str(call.outcome)}
            for call in report.tool_calls
        ],
        "answer": view.text,
    }
    return normalise(records), totals


class _Names:
    """Stable names for generated identifiers, in order of first appearance."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def __call__(self, value: str) -> str:
        if value in self._seen:
            return self._seen[value]
        prefix = value.split("_", 1)[0]
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        n = self._counts[prefix]
        name = {
            "run": "run_9b7c",
            "att": f"att_{n}",
            "wrk": f"w-0{n}",
            "call": f"call_{n}",
        }.get(prefix, f"{prefix}_{n}")
        self._seen[value] = name
        return name


ID_FIELDS = ("run_id", "attempt_id", "worker_id", "call_id", "pending_call_id")
DROPPED = ("at", "duration_seconds", "timings", "system_prompt")
RELATIVE = {"deadline_at": f"+{DEADLINE_SECONDS}s", "expires_at": "+24h"}


def normalise(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    names = _Names()
    out: list[Mapping[str, Any]] = []
    for record in records:
        clean: dict[str, Any] = {}
        for key, value in record.items():
            if key in DROPPED:
                continue
            if key in RELATIVE:
                clean[key] = RELATIVE[key]
            elif key in ID_FIELDS and isinstance(value, str):
                clean[key] = names(value)
            elif key == "tool_calls" and isinstance(value, list):
                clean[key] = [names(v) if isinstance(v, str) else v for v in value]
            else:
                clean[key] = value
        out.append(clean)
    return out


async def build() -> str:
    branches = {}
    for branch in ("approved", "denied"):
        records, totals = await run_branch(branch)
        branches[branch] = {"records": records, "report": totals}
    fixture = {
        "generated_by": "scripts/generate_site_fixtures.py",
        "psych_runtime": psych_runtime.__version__,
        "input": INPUT,
        "model": MODEL,
        "approval_selectors": ["@destructive"],
        "normalised": {
            "identifiers": "replaced with stable names in order of first appearance",
            "dropped": list(DROPPED),
            "relative": RELATIVE,
        },
        "branches": branches,
    }
    return json.dumps(fixture, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed file differs")
    args = parser.parse_args()

    text = asyncio.run(build())
    if args.check:
        committed = TARGET.read_text() if TARGET.exists() else ""
        if committed != text:
            print(
                f"site fixtures: {TARGET.relative_to(REPO)} is out of date with the runtime.\n"
                "Run `uv run python scripts/generate_site_fixtures.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("site fixtures: the approval demo matches the runtime")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text)
    print(f"site fixtures: wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
