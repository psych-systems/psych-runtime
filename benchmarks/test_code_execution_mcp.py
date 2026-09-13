"""What a program costs the prompt when the tools come from an MCP server.

The sibling benchmark (``test_code_execution.py``) measures the same claim for
registered Python tools. This one exists because the MCP case is where the
claim is both strongest and easiest to break, and the two failure modes pull in
opposite directions:

- **Strongest**, because an MCP catalogue is somebody else's and is usually
  large. Forty calls to a server tool cost forty round trips and forty results
  in the conversation, and the results are whatever that server chose to
  return rather than something an author trimmed.
- **Easiest to break**, because making MCP tools bindable means ``run_code``'s
  description has to say what a program may call -- and a description that
  named three hundred tools would put the catalogue in every prompt of every
  turn and spend the saving before the model wrote a line. So the ratio is
  measured *with a large catalogue connected*, not with the three tools the
  program happens to use.

## What is measured, and what it is not

Both paths run a real Run against a real store, the same fake model, and the
same MCP catalogue. The difference is only how the work is expressed: forty
model turns, or one program that loops.

- **prompt bytes**: every message of every request the Run sent, serialised.
  Not the final request -- a path with fewer turns would look cheaper for the
  wrong reason. Bytes, and deliberately never called tokens: a token count
  needs a tokenizer, tokenizers differ per provider, and the same figure would
  mean different things depending on which model an agent named.
- **model calls**: round trips, which is latency and per-call overhead.
- **underlying MCP calls**: counted on both paths, because the point is that
  the *same* work happens either way. A program that made fewer calls would be
  measuring a different task.
- **result equivalence**: both paths must reach the same answer.

Every number here is **deterministic and simulated**. The model is
``FakeModel``, the sandbox is ``ScriptedSandbox`` (no child process, no timing,
no platform), and the MCP catalogue is a local fake. Nothing here is billed by
a provider and nothing here is a latency measurement; the only quantities with
meaning are the byte counts, the call counts and their ratios.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import McpServer
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.testing.sandbox_service import ScriptedSandbox, scripted_result

pytestmark = pytest.mark.benchmark

SCOPE = Scope(tenant="acme", principal="user-1")

ORDERS: tuple[str, ...] = tuple(f"A-{index:04d}" for index in range(40))

CATALOGUE_SIZE = 300
"""Tools the connected server offers.

Large on purpose. A three-tool server would make the program path look good for
a reason that has nothing to do with code execution, and would not notice a
``run_code`` description that had started dumping a catalogue into the prompt.
"""

PROGRAM = (
    "total = 0\n"
    "for order_id in ORDERS:\n"
    "    row = json.loads(await call_tool('orders__get_order', {'order_id': order_id}))\n"
    "    total += row['qty']\n"
    "return {'orders': len(ORDERS), 'total_qty': total}\n"
)

TOTAL_QTY = sum(index % 7 for index in range(len(ORDERS)))


def _row(order_id: str) -> dict[str, Any]:
    """One order, the way a real MCP tool returns one: more than the task needs."""
    index = int(order_id.split("-")[1])
    return {
        "order_id": order_id,
        "qty": index % 7,
        "sku": f"SKU-{index:05d}",
        "status": "shipped" if index % 3 else "pending",
        "warehouse": "east-2",
        "updated_at": "2026-01-01T00:00:00Z",
        "notes": "handled by the automated flow; no exceptions raised",
    }


class _Catalogue:
    """A connected MCP server, faked at the catalogue seam.

    Not a socket: this benchmark measures prompt bytes and call counts, and a
    real server would add timing without changing either. It satisfies the two
    methods ``ToolResolver`` needs and the one ``ToolExecutor`` needs, which is
    the whole surface a Run touches.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.calls: list[str] = []

    def _definitions(self) -> list[ToolDefinition]:
        tools = [
            ToolDefinition(
                name="get_order",
                description="Fetch one order by id, with its line items and status.",
                input_schema={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
                annotations=frozenset({"read-only"}),
            )
        ]
        tools += [
            ToolDefinition(
                name=f"report_{index:03d}",
                description=(
                    f"Run saved report {index}. Returns rows matching the saved filter, "
                    "with totals per warehouse and a breakdown by status."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "since": {"type": "string"},
                        "until": {"type": "string"},
                        "warehouse": {"type": "string"},
                    },
                },
                annotations=frozenset({"read-only"}),
            )
            for index in range(self.size - 1)
        ]
        return tools

    async def tools_for(self, _scope: Scope, _server: McpServer) -> Sequence[ToolDefinition]:
        return self._definitions()

    async def describe(self, _scope: Scope, _server: McpServer) -> str | None:
        return "The order management system."

    async def list_for(self, _spec: Any, _scope: Scope, _server_name: str) -> list[ToolDefinition]:
        return self._definitions()

    async def call(self, _spec: Any, _scope: Scope, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        # The text shape a real MCP call returns (`call_tool_text`), so both
        # paths carry the same bytes.
        return json.dumps(_row(str(arguments.get("order_id", ""))))

    def caller(self, spec: Any, scope: Scope) -> Any:
        """``call`` bound to one Run, the shape ``ToolExecutor`` expects."""

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            return await self.call(spec, scope, name, arguments)

        return call


@pytest.fixture
def store() -> Store:
    return InMemoryStore()


def prompt_bytes(model: FakeModel) -> int:
    """Every message of every request this Run sent, serialised.

    Messages only. Tool *definitions* travel in their own field and are
    measured separately below, because conflating them would hide the exact
    regression this file exists to catch.
    """
    total = 0
    for request in model.requests:
        for message in request.messages:
            total += len(json.dumps(message.model_dump(mode="json")).encode())
    return total


def definition_bytes(model: FakeModel) -> int:
    """Every tool definition of every request, serialised.

    The number that breaks if ``run_code`` ever starts naming a whole
    catalogue: a deferred server contributes three discovery tools to this,
    and a program's bindings must contribute nothing beyond a bounded summary.
    """
    total = 0
    for request in model.requests:
        for definition in request.tools:
            total += len(json.dumps(definition.model_dump(mode="json")).encode())
    return total


def _spec(*, code: bool) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="totals-by-program" if code else "totals-by-tool",
        model=psych_runtime.ModelRef(model="fake-standard"),
        mcp_servers=(McpServer(name="orders", url="https://example.invalid/mcp"),),
        code_execution=(
            psych_runtime.CodeExecution(isolation=psych_runtime.IsolationLevel.ISOLATED)
            if code
            else None
        ),
        limits=psych_runtime.Limits(max_turns=len(ORDERS) + 4),
    )


async def measure_tool_path(catalogue: _Catalogue) -> tuple[FakeModel, Any]:
    model = FakeModel()
    for order_id in ORDERS:
        # `call_tool` rather than the qualified name, because a catalogue this
        # size is deferred out of the prompt: this is what the model actually
        # has to do against a large server, and comparing against a fantasy
        # where 300 schemas were free would flatter the program path.
        model.turn(
            tool_calls=[
                (
                    "call_tool",
                    {
                        "server": "orders",
                        "tool": "get_order",
                        "arguments": {"order_id": order_id},
                    },
                )
            ]
        )
    model.turn(text=f"The forty orders total {TOTAL_QTY} units.")

    async with psych_runtime.session(
        model, store=InMemoryStore(), tenant="acme", mcp=catalogue
    ) as session:
        answer = await session.ask(
            _spec(code=False), "Total the quantities across every order.", scope=SCOPE
        )
    return model, answer


async def measure_program_path(catalogue: _Catalogue) -> tuple[FakeModel, Any]:
    model = (
        FakeModel()
        .turn(tool_calls=[("run_code", {"program": PROGRAM})])
        .turn(text=f"The forty orders total {TOTAL_QTY} units.")
    )
    sandbox = ScriptedSandbox(
        default=scripted_result(value={"orders": len(ORDERS), "total_qty": TOTAL_QTY}),
        bindings_to_call=tuple(
            ("orders__get_order", {"order_id": order_id}) for order_id in ORDERS
        ),
    )
    async with psych_runtime.session(
        model,
        store=InMemoryStore(),
        tenant="acme",
        mcp=catalogue,
        sandboxes=[psych_runtime.SandboxProfile("default", sandbox)],
    ) as session:
        answer = await session.ask(
            _spec(code=True), "Total the quantities across every order.", scope=SCOPE
        )
    return model, answer


class TestMcpFanOut:
    async def test_the_same_forty_mcp_calls_cost_one_turn_instead_of_forty(
        self, benchmark: Callable[..., None]
    ) -> None:
        by_tool = _Catalogue(CATALOGUE_SIZE)
        by_program = _Catalogue(CATALOGUE_SIZE)
        tool_model, tool_answer = await measure_tool_path(by_tool)
        program_model, program_answer = await measure_program_path(by_program)

        tool_prompt = prompt_bytes(tool_model)
        program_prompt = prompt_bytes(program_model)

        # The same underlying work on both paths. A program that made fewer
        # MCP calls would be answering a different question, and the ratio
        # below would be measuring that instead.
        assert by_tool.calls == ["orders__get_order"] * len(ORDERS)
        assert by_program.calls == ["orders__get_order"] * len(ORDERS)
        # And the same answer.
        assert str(TOTAL_QTY) in str(tool_answer)
        assert str(TOTAL_QTY) in str(program_answer)

        print(
            f"\n  MCP fan-out over {len(ORDERS)} orders, {CATALOGUE_SIZE} tools connected"
            f"\n  tool path:    {len(tool_model.requests):>3} model calls, "
            f"{tool_prompt:>7} prompt bytes, {len(by_tool.calls)} MCP calls"
            f"\n  program path: {len(program_model.requests):>3} model calls, "
            f"{program_prompt:>7} prompt bytes, {len(by_program.calls)} MCP calls"
            f"\n  ratio:        {program_prompt / tool_prompt:.4f}"
            "\n  (deterministic: fake model, scripted sandbox, faked catalogue;"
            " bytes are serialised messages, never tokens, and nothing here is"
            " billed by a provider)"
        )
        benchmark(
            "mcp_code_execution_prompt_ratio",
            program_prompt / tool_prompt,
            unit="program bytes / tool bytes",
            higher_is_better=False,
            tolerance=0.25,
        )

    async def test_model_calls_for_forty_mcp_rows(self, benchmark: Callable[..., None]) -> None:
        tool_model, _ = await measure_tool_path(_Catalogue(CATALOGUE_SIZE))
        program_model, _ = await measure_program_path(_Catalogue(CATALOGUE_SIZE))
        assert len(program_model.requests) < len(tool_model.requests)
        benchmark(
            "mcp_code_execution_model_calls",
            float(len(program_model.requests)),
            unit="model calls",
            higher_is_better=False,
            tolerance=0.0,
        )

    async def test_binding_a_large_catalogue_does_not_move_it_into_the_prompt(
        self, benchmark: Callable[..., None]
    ) -> None:
        """The regression this whole file exists to catch.

        Making MCP tools bindable means ``run_code``'s description has to say
        what a program may call. If that description ever names a catalogue
        instead of summarising it, the saving above is spent on definitions
        before the model writes a line -- and the prompt-bytes ratio would not
        notice, because definitions travel in their own field.

        So the definitions a program-capable agent sends are measured against
        the ones the tool-calling agent sends over the same catalogue. The
        program path should be *smaller*: its server is deferred, so it carries
        three discovery tools and one bounded ``run_code`` description instead
        of three hundred schemas.
        """
        tool_model, _ = await measure_tool_path(_Catalogue(CATALOGUE_SIZE))
        program_model, _ = await measure_program_path(_Catalogue(CATALOGUE_SIZE))

        tool_definitions = definition_bytes(tool_model)
        program_definitions = definition_bytes(program_model)
        per_request = program_definitions / max(len(program_model.requests), 1)

        print(
            f"\n  tool definitions sent: {tool_definitions} bytes over "
            f"{len(tool_model.requests)} request(s)"
            f"\n  program definitions sent: {program_definitions} bytes over "
            f"{len(program_model.requests)} request(s) "
            f"({per_request:.0f} bytes/request)"
        )
        # The guard: `run_code`'s description plus the three discovery tools
        # must stay a summary. Naming the catalogue would put roughly 60 KB
        # here instead of roughly 4 KB.
        assert per_request < 6_000, (
            "run_code's description is carrying a catalogue rather than summarising it"
        )
        # The metric: what is actually paid across the Run. The program path
        # sends slightly more per request -- `run_code`'s own description is
        # not free -- and an order of magnitude less in total, because it sends
        # two requests rather than forty-one.
        benchmark(
            "mcp_code_execution_definition_ratio",
            program_definitions / tool_definitions,
            unit="program definition bytes / tool definition bytes",
            higher_is_better=False,
            tolerance=0.25,
        )
