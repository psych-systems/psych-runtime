"""The code tools every playground agent can be given, registered the way any
consumer registers theirs: a Python function, a docstring, type hints, and
``ToolRegistry.register`` reading a schema off them (DESIGN.md §10.1).

## Why these tools and not a shop

This registry used to hold ``lookup_order``, ``issue_refund`` and
``check_inventory`` -- a pretend storefront, useful for demonstrating what a
tool *is* and useless to anybody who had just signed up. The playground now
ships a real catalogue (``app.catalogue``): vendor-run MCP servers, a
specialist agent per connector, and an orchestrator over all of them. A person
who connects GitHub does not need a fake refund button; what they need is the
handful of things every agent wants and no MCP server provides.

So the registry is now:

``current_time`` and ``calculate``
    The two an agent reaches for constantly and cannot do itself. A model has
    no clock -- "since last Tuesday" is unanswerable without one -- and
    arithmetic done in a token stream is arithmetic done wrong. Both read-only,
    so neither ever asks for an approval.

``list_agents``, ``create_agent``, ``update_agent``
    The console's own agent-building surface, offered to the agent. This is
    what makes the orchestrator's roster extensible from a conversation: "make
    me a Stripe agent that only reads" publishes through the very same path
    ``POST /api/agents`` uses, so an agent built this way is indistinguishable
    from one built on the Agents page -- same validation, same Version, same
    entry in the same index.

``create_workflow``, ``list_workflows``, ``run_workflow``
    The Workflows page, likewise. ``create_workflow`` publishes a
    ``WorkflowSpec`` -- DESIGN.md §5's other authoring surface, a fixed
    pipeline with each completed step memoised -- and ``run_workflow``
    dispatches one.

``calculate`` is also where the failure path lives that ``check_inventory``
used to demonstrate: a malformed expression raises, and three such failures in
a Run trip DESIGN.md §10.6's failure-streak guard with the ``failure_guidance``
text the model is shown. A tool that fails when it is *misused* is a better
illustration of that path than one built to fail always, because the model can
also recover from it.

## The one destructive tool

``update_agent`` is annotated ``destructive`` and the rest are ``read-only`` or
``write``. That is not decoration: publishing a new Version and moving an
agent's pointer changes what every future Run of that agent does, and there is
no undo in the console. It is the tool worth pointing an agent's
``approval_selectors`` at (``@destructive``) to watch a Run suspend for a human
decision -- ``POST /api/runs/{run_id}/resume`` is what lets it through.

## How a tool learns whose Run it is in

Psych hands a tool its arguments and nothing else, on purpose: a Spec holds
names, a tool is a function, and a function that took a Scope would have one
more thing to get wrong in every consumer. The tools that publish or dispatch
need to know which account they are acting for, so ``app.runtime_router`` sets
``current_scope`` for the duration of each Attempt and they read it. A context
variable rather than a global, because two accounts' Attempts run concurrently
in this one process.

The application side of each is a base class here and an implementation
elsewhere (``app.workflows``, ``app.agent_tools``), installed once at boot.
A base class rather than an import, so this module does not depend on the
services that implement it -- and so a test can install a fake.
"""

from __future__ import annotations

import ast
import contextvars
import operator
from datetime import UTC, datetime
from typing import Any

from psych_runtime.core.scope import Scope
from psych_runtime.tools.registry import ToolRegistry

registry = ToolRegistry()

current_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar(
    "psych_playground_scope", default=None
)
"""The Scope of the Attempt executing on this task, set by ``app.runtime_router``."""


# ---------------------------------------------------------------------------
# The two an agent cannot do for itself
# ---------------------------------------------------------------------------


@registry.register(annotations={"read-only"})
async def current_time() -> dict[str, str]:
    """The current date and time, in UTC.

    Use this before working with anything relative -- "today", "this week",
    "since Tuesday", "how long ago" -- rather than guessing at the date. The
    answer is ISO 8601 with an explicit offset, so it can be handed straight to
    another tool that wants a timestamp.
    """
    now = datetime.now(UTC)
    return {
        "iso": now.isoformat(),
        "date": now.date().isoformat(),
        "weekday": now.strftime("%A"),
        "timezone": "UTC",
    }


_BINARY_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_EXPONENT = 64
"""``2 ** 10_000_000`` is a valid expression and a way to hang the process.
A tool that can be asked to burn a core is a denial of service with a friendly
schema, so the exponent is bounded and a larger one is refused by name."""


def _evaluate(node: ast.expr) -> float:
    """One node of a parsed expression, or a ``ValueError`` naming what was wrong.

    An allow-list over the AST rather than ``eval`` with a stripped
    ``__builtins__``: the blocklist approach has been escaped often enough that
    "no attribute access, no calls, no names, these seven operators" is the only
    version worth shipping. Everything outside the list is refused with a
    message the model can act on, which is the difference between a tool that
    teaches it the syntax and one it retries identically three times.
    """
    match node:
        case ast.Constant(value=bool()):
            raise ValueError("booleans are not numbers; write 1 or 0")
        case ast.Constant(value=int() | float() as value):
            return float(value)
        case ast.BinOp(op=op, left=left, right=right) if type(op) in _BINARY_OPS:
            if isinstance(op, ast.Pow):
                exponent = _evaluate(right)
                if abs(exponent) > _MAX_EXPONENT:
                    raise ValueError(f"exponent {exponent} is larger than {_MAX_EXPONENT}")
                return float(_BINARY_OPS[type(op)](_evaluate(left), exponent))
            return float(_BINARY_OPS[type(op)](_evaluate(left), _evaluate(right)))
        case ast.UnaryOp(op=op, operand=operand) if type(op) in _UNARY_OPS:
            return float(_UNARY_OPS[type(op)](_evaluate(operand)))
        case _:
            raise ValueError(
                f"{type(node).__name__} is not allowed here; this tool evaluates numbers "
                "with + - * / // % ** and parentheses, and nothing else"
            )


@registry.register(annotations={"read-only"})
async def calculate(expression: str) -> dict[str, Any]:
    """Evaluate an arithmetic expression exactly, rather than in your head.

    Supports numbers, parentheses and ``+ - * / // % **``. Nothing else: no
    names, no function calls, no attribute access. Use it for any figure you
    are going to report -- a total, a percentage, a rate, a difference between
    two counts -- because arithmetic you do while writing is arithmetic you get
    wrong, and a number in an answer is the part somebody checks.

    Raises:
        ValueError: the expression did not parse, or used something outside the
            list above. The message says which, so fix the expression rather
            than repeating it.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as err:
        raise ValueError(f"{expression!r} is not an expression: {err.msg}") from err
    try:
        result = _evaluate(tree.body)
    except ZeroDivisionError as err:
        raise ValueError(f"{expression!r} divides by zero") from err
    except OverflowError as err:
        raise ValueError(f"{expression!r} overflowed: {err}") from err
    return {"expression": expression, "result": result}


# ---------------------------------------------------------------------------
# The application's own surfaces, as tools
# ---------------------------------------------------------------------------


class AgentPublisher:
    """What the agent tools need from the application: a way to list, publish
    and edit agents for the account whose Run is calling. Set once at boot by
    ``app.main``; a base class rather than an import so this module does not
    depend on the service that implements it."""

    async def list_for(self, tenant: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def create_for(
        self,
        tenant: str,
        *,
        name: str,
        instructions: str,
        description: str,
        connectors: list[str],
        tools: list[str],
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def update_for(
        self,
        tenant: str,
        *,
        agent_id: str,
        instructions: str | None,
        description: str | None,
        connectors: list[str] | None,
    ) -> dict[str, Any]:
        raise NotImplementedError


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

    async def dispatch_for(
        self, tenant: str, *, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


_publisher: WorkflowPublisher | None = None
_agents: AgentPublisher | None = None


def install_workflow_publisher(publisher: WorkflowPublisher) -> None:
    global _publisher  # noqa: PLW0603 - one process, one application, set at boot
    _publisher = publisher


def install_agent_publisher(publisher: AgentPublisher) -> None:
    global _agents  # noqa: PLW0603 - one process, one application, set at boot
    _agents = publisher


def _require_scope(tool: str) -> Scope:
    scope = current_scope.get()
    if scope is None:
        raise RuntimeError(f"{tool} can only be called from inside a playground Run")
    return scope


def _require_publisher(tool: str) -> tuple[WorkflowPublisher, Scope]:
    scope = _require_scope(tool)
    if _publisher is None:
        raise RuntimeError(f"{tool} can only be called from inside a playground Run")
    return _publisher, scope


def _require_agents(tool: str) -> tuple[AgentPublisher, Scope]:
    scope = _require_scope(tool)
    if _agents is None:
        raise RuntimeError(f"{tool} can only be called from inside a playground Run")
    return _agents, scope


@registry.register(annotations={"read-only"})
async def list_agents() -> list[dict[str, Any]]:
    """The agents published in this workspace: their ids, names and what each is for.

    Call this before creating one, in case the agent being asked for already
    exists, and before editing one, to find its id.
    """
    publisher, scope = _require_agents("list_agents")
    return await publisher.list_for(scope.tenant)


@registry.register(annotations={"write"})
async def create_agent(
    name: str,
    instructions: str,
    description: str = "",
    connectors: list[str] | None = None,
    tools: list[str] | None = None,
) -> dict[str, Any]:
    """Publish a new agent in this workspace, which a person can then run or
    delegate to.

    Args:
        name: one word -- letters, digits, dots, dashes or underscores. It is
            how the agent is referred to everywhere.
        instructions: the agent's system prompt: what it is for, how it should
            work, and what it must confirm before doing. Write it as you would
            brief a colleague, in a paragraph or several.
        description: one or two sentences for somebody browsing a list of
            agents. Not part of the prompt.
        connectors: connected systems this agent may reach, by name -- a
            catalogue connector such as ``github`` or ``slack``, or one of this
            workspace's own configured servers. Each is attached optionally, so
            the agent still publishes and runs before anybody has connected it.
        tools: registered tool names this agent may call, such as
            ``current_time``. Use ``list_agents`` on an existing agent to see
            what is conventional.

    Use this when somebody asks for a repeatable specialist -- "an agent that
    watches our Sentry issues" -- rather than doing the work yourself once.
    """
    publisher, scope = _require_agents("create_agent")
    return await publisher.create_for(
        scope.tenant,
        name=name,
        instructions=instructions,
        description=description,
        connectors=list(connectors or []),
        tools=list(tools or []),
    )


@registry.register(interruptible=False, annotations={"destructive"})
async def update_agent(
    agent_id: str,
    instructions: str | None = None,
    description: str | None = None,
    connectors: list[str] | None = None,
) -> dict[str, Any]:
    """Republish an existing agent with changed instructions, description or
    connectors.

    Destructive, and not interruptible: this publishes a new Version and moves
    the agent's pointer to it, so every future Run of that agent behaves
    differently and the console offers no undo. Say exactly what you are
    changing and get agreement before calling it. The three fields below are
    the only ones it can change; everything else the agent was published with
    is carried forward unchanged.

    It refuses an agent built out of things this tool cannot describe -- a
    delegation roster, A2A peers, HTTP tools, skills, code execution. Those
    are edited on the Agents page, where they are visible. Say so and stop,
    rather than publishing a version of the agent with them missing.

    Args:
        agent_id: from ``list_agents``. Must be an agent of this workspace.
        instructions: the replacement system prompt, or ``None`` to keep it.
        description: the replacement description, or ``None`` to keep it.
        connectors: the replacement connector list, or ``None`` to keep it. An
            empty list removes every connector, which is a real change and not
            a way of saying "leave it alone".
    """
    publisher, scope = _require_agents("update_agent")
    return await publisher.update_for(
        scope.tenant,
        agent_id=agent_id,
        instructions=instructions,
        description=description,
        connectors=None if connectors is None else list(connectors),
    )


@registry.register(annotations={"write"})
async def create_workflow(
    name: str, steps: list[dict[str, Any]], description: str = ""
) -> dict[str, Any]:
    """Publish a fixed pipeline that a person can run again later, from the
    Workflows page or by dispatching its id.

    Steps run in order with the arguments given here, and each completed step is
    remembered so a crash resumes where it stopped. Use this when the user asks
    for something repeatable. The name must be one word: letters, digits, dots,
    dashes or underscores. Call ``list_workflows`` first if they may mean one
    that already exists.

    Args:
        name: the workflow's name.
        steps: one object per step. Every step has a ``name`` unique within the
            workflow and a ``kind``:

            * ``{"kind": "tool", "name": ..., "tool": ..., "arguments": {...}}``
              calls a registered tool with written-down arguments;
              ``"arguments_from"`` fills them from values the Run already has,
              as ``{"arg": {"kind": "path", "path": "input.since"}}``.
            * ``{"kind": "agent", "name": ..., "agent_id": ..., "input": {...}}``
              runs one of this workspace's agents (see ``list_agents``). The
              ``message`` field of ``input`` is what the agent is told; a
              literal is ``{"kind": "literal", "value": "..."}``.
            * ``{"kind": "human", "name": ..., "prompt": "..."}`` pauses for a
              person to approve. Put one of these before anything that changes
              somebody else's system.
            * ``parallel``, ``branch``, ``foreach``, ``loop``, ``map``,
              ``set_state``, ``sleep``, ``wait`` and ``workflow`` compose the
              rest, and are described in the console's Workflows help.

    Raises:
        ValueError: a step was malformed or named something this workspace does
            not have. The message says which step and which field, so fix that
            step rather than resubmitting the same definition.
    """
    publisher, scope = _require_publisher("create_workflow")
    return await publisher.publish_for(
        scope.tenant, name=name, description=description, steps=steps
    )


@registry.register(annotations={"read-only"})
async def list_workflows() -> list[dict[str, Any]]:
    """The workflows already published in this workspace, with their ids and steps."""
    publisher, scope = _require_publisher("list_workflows")
    return await publisher.list_for(scope.tenant)


@registry.register(annotations={"write"})
async def run_workflow(workflow_id: str, input: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: A002 - the field is `input` everywhere else in this API
    """Dispatch a published workflow and return the id of the Run it started.

    The Run proceeds on its own; this does not wait for it. Report the run id
    back so the person can watch it, and say which workflow it is. Check the
    workflow's ``input_schema`` with ``list_workflows`` first -- a Run whose
    input does not satisfy it fails before its first step.

    Args:
        workflow_id: from ``list_workflows``.
        input: the workflow's input object, matching its ``input_schema``.
    """
    publisher, scope = _require_publisher("run_workflow")
    return await publisher.dispatch_for(
        scope.tenant, workflow_id=workflow_id, payload=dict(input or {})
    )
