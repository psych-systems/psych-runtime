"""Small pieces shared by more than one scenario.

Nothing here is a Psych concept of its own -- it is the same handful of
fixtures ``tests/e2e/test_regression_gate.py`` and its neighbours already
established as the right way to drive the real runtime by hand: two tenant
Scopes, a registry of a few small tools, a ``drive()`` that publishes,
dispatches and runs an Attempt to completion the way a consumer's own Worker
would, and a JSON-safe rendering of a report or a log for the scenario result.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import psych_runtime
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import RECORD_ADAPTER, Record
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef, Spec
from psych_runtime.report.model import RunReport
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

__all__ = [
    "SCOPE_A",
    "SCOPE_B",
    "await_settled",
    "drive",
    "lookup_registry",
    "record_dict",
    "records_dict",
    "report_dict",
    "support_agent_spec",
]

SCOPE_A = psych_runtime.Scope(tenant="acme", principal="user-1")
SCOPE_B = psych_runtime.Scope(tenant="beta", principal="user-2")


def lookup_registry(effects: list[str] | None = None) -> ToolRegistry:
    """A minimal read-only tool most scenarios only need one of."""
    registry = ToolRegistry()
    sink = effects if effects is not None else []

    @registry.register(annotations={"read-only"})
    def lookup(order_id: str) -> str:
        """Look up an order's shipping status."""
        sink.append(f"lookup:{order_id}")
        return f"{order_id} shipped"

    return registry


def support_agent_spec(**overrides: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer with their order.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(overrides)
    return AgentSpec(**base)


async def drive(
    store: Store,
    spec: Spec,
    model: FakeModel,
    registry: ToolRegistry,
    *,
    scope: psych_runtime.Scope = SCOPE_A,
    message: str = "where is my order?",
    worker_id: str = "wrk_1",
    **runtime_kwargs: Any,
) -> RunId:
    """Publish, dispatch and run one Attempt to a terminal record, exactly the
    shape a consumer's own Worker takes -- see ``drive()`` in
    ``tests/e2e/test_regression_gate.py``, which this mirrors so a scenario
    reads the same way the test suite that proves the same claims does.
    """
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(store, version, scope, input={"message": message})
    journal = await Journal.open(store, dispatched.run_id, scope)
    await journal.append(type="attempt_started", worker_id=worker_id, attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=registry, **runtime_kwargs)
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id


async def await_settled(store: Store, run_id: RunId, timeout: float = 15.0) -> bool:
    """Poll ``psych_runtime.state()`` until the Run settles, or give up.

    Returns whether it settled in time rather than raising, so a scenario can
    turn a timeout into a held-or-not assertion instead of an unhandled
    exception that would look like a bug in the demo rather than a fact about
    the Run.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        state = await psych_runtime.state(store, run_id)
        if state.settled:
            return True
        await asyncio.sleep(0.02)
    return False


def report_dict(report: RunReport) -> dict[str, Any]:
    """``RunReport`` as plain JSON -- Decimal costs and datetimes included --
    by going through pydantic's own JSON encoder rather than hand-rolling one
    that will drift from whatever ``psych_runtime.report.model`` adds next."""
    parsed: dict[str, Any] = json.loads(report.model_dump_json())
    return parsed


def record_dict(record: Record) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(RECORD_ADAPTER.dump_json(record))
    return parsed


def records_dict(records: Sequence[Record]) -> list[dict[str, Any]]:
    return [record_dict(r) for r in records]
