"""The code tools every playground agent can be given, registered the way any
consumer registers theirs: a Python function, a docstring, type hints, and
``ToolRegistry.register`` reading a schema off them (DESIGN.md §10.1).

``lookup_order`` and ``issue_refund`` keep the read-only/destructive pair and
shapes DESIGN.md's own worked example (``docs/api.md``, and
``scripts/live-provider-check.py``) already uses, so behaviour here matches
what a reader of either already expects. ``check_inventory`` always raises: it
stands in for a downstream service that is down, so driving the playground by
hand can actually watch DESIGN.md §10.6's failure-streak guard trip -- and the
``failure_guidance`` text the model is shown -- rather than needing a flaky
real backend to reproduce that path.

``create_workflow`` and ``list_workflows`` are the ones that show what a tool
is. A tool is a function. This one publishes a ``WorkflowSpec`` -- DESIGN.md
§5's other authoring surface, a fixed pipeline of tool calls with each
completed step memoised -- out of the tools registered here, so an agent asked
to "set up the refund check for A1" can build the pipeline itself and a person
can then run it from the Workflows page, or edit it there first.

## How a tool learns whose Run it is in

Psych hands a tool its arguments and nothing else, on purpose: a Spec holds
names, a tool is a function, and a function that took a Scope would have one
more thing to get wrong in every consumer. ``create_workflow`` needs to know
which account it is publishing for, so ``app.runtime_router`` sets
``current_scope`` for the duration of each Attempt and the tool reads it. A
context variable rather than a global, because two accounts' Attempts run
concurrently in this one process.
"""

from __future__ import annotations

import contextvars
from typing import Any

from pydantic import BaseModel, Field

from psych_runtime.core.scope import Scope
from psych_runtime.tools.registry import ToolRegistry

registry = ToolRegistry()

current_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "psych_playground_scope", default=None
)
"""The Scope of the Attempt executing on this task, set by ``app.runtime_router``."""

_ORDERS: dict[str, str] = {
    "A1": "shipped",
    "A2": "processing",
    "A3": "delivered",
}


@registry.register(annotations={"read-only"})
async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order's shipping status by its id."""
    status = _ORDERS.get(order_id)
    if status is None:
        return {"order_id": order_id, "status": "unknown"}
    return {"order_id": order_id, "status": status}


@registry.register(interruptible=False, annotations={"destructive"})
async def issue_refund(order_id: str, cents: int) -> str:
    """Refund an order.

    Not interruptible: a half-issued refund is worse than a slow stop. This is
    the tool worth pointing an agent's ``approval_selectors`` at
    (``@destructive``) to see a Run suspend for a human decision before it
    runs -- ``POST /api/runs/{run_id}/resume`` is what lets it through.
    """
    _ORDERS.setdefault(order_id, "refunded")
    return f"refunded {cents} cents on order {order_id}"


@registry.register(annotations={"read-only"})
async def check_inventory(sku: str) -> dict[str, int]:
    """Check warehouse inventory for a SKU.

    Always raises. This is the tool worth pointing an agent at to see
    DESIGN.md §10.6's guidance and failure-streak guard: three consecutive
    failures of one tool within a Run stop the model repeating it.
    """
    raise RuntimeError(f"inventory service unreachable for sku {sku!r}")


# ---------------------------------------------------------------------------
# A tool that builds a workflow
# ---------------------------------------------------------------------------


class WorkflowStepArg(BaseModel):
    """One step of a workflow the model is composing."""

    name: str = Field(description="A short identifier for this step, unique in the workflow.")
    tool: str = Field(description="One of the registered tools, by name.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="The exact arguments the tool is called with."
    )


class WorkflowPublisher:
    """What ``create_workflow`` needs from the application: a way to publish
    for the account whose Run is calling it. Set once at boot by ``app.main``;
    a base class rather than an import so this module does not depend on the
    service that implements it."""

    async def publish_for(
        self, tenant: str, *, name: str, description: str, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def list_for(self, tenant: str) -> list[dict[str, Any]]:
        raise NotImplementedError


_publisher: WorkflowPublisher | None = None


def install_workflow_publisher(publisher: WorkflowPublisher) -> None:
    global _publisher  # noqa: PLW0603 - one process, one application, set at boot
    _publisher = publisher


def _require_publisher() -> tuple[WorkflowPublisher, Scope]:
    scope = current_scope.get()
    if _publisher is None or scope is None:
        raise RuntimeError("create_workflow can only be called from inside a playground Run")
    return _publisher, scope


@registry.register(annotations={"write"})
async def create_workflow(
    name: str, steps: list[WorkflowStepArg], description: str = ""
) -> dict[str, Any]:
    """Publish a fixed pipeline of tool calls that a person can run again later,
    from the Workflows page or by dispatching its id.

    Steps run in order, each with the arguments given here, and each completed
    step is remembered so a crash resumes where it stopped. Use this when the
    user asks for something repeatable, such as "check order A1 and refund it
    if it never shipped". The name must be one word: letters, digits, dots,
    dashes or underscores. Use list_workflows first if the user may mean one
    that already exists.
    """
    publisher, scope = _require_publisher()
    return await publisher.publish_for(
        scope.tenant,
        name=name,
        description=description,
        steps=[step.model_dump() for step in steps],
    )


@registry.register(annotations={"read-only"})
async def list_workflows() -> list[dict[str, Any]]:
    """The workflows already published in this workspace, with their ids and steps."""
    publisher, scope = _require_publisher()
    return await publisher.list_for(scope.tenant)
