"""What a program costs the prompt, against the same work as tool calls.

The claim code execution makes is that a model which can loop pays for one
round trip instead of one per row. That is a claim about bytes and about model
calls, so it is measured rather than asserted, on the same task twice: read
forty orders and total their quantities.

Both paths run a real Run against a real store and the same fake model. The
difference is only in how the work is expressed:

- **tool path**: forty `lookup_order` calls, each a model turn, each result
  appended to the conversation the next turn is sent.
- **program path**: one `run_code` call whose program loops the same binding
  forty times. The bindings still travel the ordinary tool path and are still
  recorded; they are simply not in the prompt, because the program consumed
  them.

The measurement is the total prompt sent across a Run -- every message of every
request, serialised -- because that is what is actually paid for, and a
measurement of the final request alone would flatter whichever path has fewer
turns. The sandbox is `ScriptedSandbox`, so the number is deterministic: no
child process, no timing, no platform.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

import psych_runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.sandbox_service import ScriptedSandbox, scripted_result

pytestmark = pytest.mark.benchmark

SCOPE = psych_runtime.Scope(tenant="acme", principal="user-1")
ORDERS: tuple[str, ...] = tuple(f"A-{index:04d}" for index in range(40))

PROGRAM = (
    "total = 0\n"
    "for order_id in ORDERS:\n"
    "    total += (await lookup_order(order_id=order_id))['qty']\n"
    "return {'orders': len(ORDERS), 'total_qty': total}\n"
)


@pytest.fixture
def store() -> Store:
    return InMemoryStore()


async def lookup_order(order_id: str) -> dict[str, Any]:
    """One order, the way a real one arrives: more fields than the task needs."""
    index = int(order_id.split("-")[1])
    return {
        "order_id": order_id,
        "qty": index % 7,
        "sku": f"SKU-{index:05d}",
        "status": "shipped" if index % 3 else "pending",
        "warehouse": "east-2",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def prompt_bytes(model: FakeModel) -> int:
    """Every message of every request this Run sent, serialised.

    Not the final request: a path with fewer turns would look cheaper for the
    wrong reason. This is the sum of what was actually put on the wire.
    """
    total = 0
    for request in model.requests:
        for message in request.messages:
            total += len(json.dumps(message.model_dump(mode="json")).encode())
    return total


async def measure_tool_path(store: Store) -> tuple[int, int]:
    model = FakeModel()
    for order_id in ORDERS:
        model.turn(tool_calls=[("lookup_order", {"order_id": order_id})])
    model.turn(text="The forty orders total 117 units.")

    async with psych_runtime.session(
        model, store=store, tools=[lookup_order], tenant="acme"
    ) as session:
        agent = psych_runtime.AgentSpec(
            name="totals-by-tool",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="lookup_order"),),
            limits=psych_runtime.Limits(max_turns=len(ORDERS) + 4),
        )
        await session.ask(agent, "Total the quantities across every order.", scope=SCOPE)
    return prompt_bytes(model), len(model.requests)


async def measure_program_path(store: Store) -> tuple[int, int]:
    model = (
        FakeModel()
        .turn(tool_calls=[("run_code", {"program": PROGRAM})])
        .turn(text="The forty orders total 117 units.")
    )
    sandbox = ScriptedSandbox(
        default=scripted_result(value={"orders": len(ORDERS), "total_qty": 117}),
        bindings_to_call=tuple(("lookup_order", {"order_id": o}) for o in ORDERS),
    )

    async with psych_runtime.session(
        model,
        store=store,
        tools=[lookup_order],
        tenant="acme",
        sandboxes=[psych_runtime.SandboxProfile("default", sandbox)],
    ) as session:
        agent = psych_runtime.AgentSpec(
            name="totals-by-program",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="lookup_order"),),
            code_execution=psych_runtime.CodeExecution(
                isolation=psych_runtime.IsolationLevel.ISOLATED,
                bindings=("lookup_order",),
            ),
        )
        await session.ask(agent, "Total the quantities across every order.", scope=SCOPE)
    return prompt_bytes(model), len(model.requests)


class TestModelContext:
    async def test_prompt_bytes_for_forty_rows(
        self, store: Store, benchmark: Callable[..., None]
    ) -> None:
        """The prompt a program costs, as a fraction of the same work in tools.

        Lower is better and the ratio is the number worth quoting, because it
        survives a change to the fixture's row shape in a way an absolute byte
        count does not.
        """
        by_tool, _ = await measure_tool_path(store)
        by_program, _ = await measure_program_path(InMemoryStore())

        print(f"\n  tool path: {by_tool} prompt bytes; program path: {by_program}")
        benchmark(
            "code_execution_prompt_ratio",
            by_program / by_tool,
            unit="program bytes / tool bytes",
            higher_is_better=False,
            tolerance=0.25,
        )

    async def test_model_calls_for_forty_rows(
        self, store: Store, benchmark: Callable[..., None]
    ) -> None:
        """Round trips, which is latency and per-call overhead rather than bytes."""
        _, tool_calls = await measure_tool_path(store)
        _, program_calls = await measure_program_path(InMemoryStore())

        print(f"\n  tool path: {tool_calls} model calls; program path: {program_calls}")
        assert program_calls < tool_calls
        benchmark(
            "code_execution_model_calls",
            float(program_calls),
            unit="model calls",
            higher_is_better=False,
            tolerance=0.0,
        )
