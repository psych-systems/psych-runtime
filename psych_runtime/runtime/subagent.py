"""Subagents: a nested Run with its own log, its own budget, and narrower tools.

DESIGN.md §17.

## Depth is monotone, and this is the bug worth naming

Runtime options may deepen a Run's delegation depth but never lower it. A resumed
child arriving with fresh options must not be counted from zero, or it delegates
as though it were top-level and the recursion budget is defeated.

That is a real failure with a real shape: a child that resumes at depth 0 can
spawn its own children at depth 1, each of which resumes at 0, and the tree never
terminates while every individual check passes. ``resolve_child_depth`` below is
the one place depth is computed, and it takes the maximum rather than the
argument.

Monotone depth alone still lets one parent spawn a hundred children at depth 1,
so DESIGN.md §17 also asks for a shipped default maximum depth, a fan-out cap,
and a minimum length on a subagent description. All three are enforced here and
in ``psych_runtime.core.spec``. A single turn quietly spawning a tree is a cost
incident, and the first anyone hears of it is the bill.

## Tool narrowing across the boundary

A subagent's tools are the intersection of what it asks for and what its parent
holds, computed by the same narrowing function as §10.5. A subagent can never
reach a tool its parent was not granted, so a child cannot be used to launder
access the parent did not have.

## Routing quality is a publish-time concern

The parent's delegation tool describes each subagent by name, purpose and the
tools it holds. Vague descriptions are the root cause of bad routing, so the Spec
model refuses a description under 20 characters and this module puts the full
description in front of the model rather than summarising it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId, ToolCallId
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.reducer import ChildRun, RunStateView

# `_NAME_PATTERN` is private and imported anyway. A composed subagent's name is
# written by a model and lands in an `AgentSpec`, so it has to satisfy exactly
# the pattern that model's own `name` field does. A second copy of the regex
# here would be a second definition of what a name is, and the day the two
# disagree is the day a name this accepts fails at publish.
from psych_runtime.core.spec import (
    _NAME_PATTERN,
    MIN_SPAWN_DELIVERABLE,
    MIN_SPAWN_PURPOSE,
    MIN_SPAWN_TASK,
    AgentSpec,
    ModelRef,
    SpawnEnvelope,
    SubagentRef,
    SuspensionPolicy,
)
from psych_runtime.tools.narrowing import narrow

DelegateFn = Callable[[SubagentRef, str, int], Awaitable[Any]]
"""Runs a child agent: the subagent to run, the task, and the child's depth.

Supplied by the runtime rather than built here, so this module stays pure and the
agent loop does not have to know how a child Run is created or stored.
"""

__all__ = [
    "CHECK_TOOL",
    "DELEGATE_TOOL",
    "MESSAGE_TOOL",
    "SPAWN_TOOL",
    "ChildOps",
    "ComposedChild",
    "DelegateFn",
    "SpawnRequest",
    "check_alive",
    "check_definition",
    "child_tool_names",
    "compose_child_spec",
    "composed_tool_names",
    "delegation_definition",
    "envelope_models",
    "envelope_tools",
    "find_child",
    "message_definition",
    "resolve_child_depth",
    "spawn_definition",
]

DELEGATE_TOOL = "delegate"
"""The name the parent's delegation tool takes. Reserved in
``psych_runtime.core.spec.RESERVED_TOOL_NAMES`` so a Spec cannot claim it."""


def resolve_child_depth(parent_depth: int, requested: int | None = None) -> int:
    """The depth a child Run runs at.

    Monotone by construction: the result is never below ``parent_depth + 1``,
    whatever ``requested`` says. Runtime options may deepen the tree and never
    flatten it.

    Args:
        parent_depth: the parent Run's depth, from its admission record.
        requested: a depth the caller would like. Honoured only when it is
            deeper.

    Returns:
        The child's depth.
    """
    natural = parent_depth + 1
    if requested is None:
        return natural
    return max(natural, requested)


def check_depth(child_depth: int, max_depth: int) -> None:
    """Refuse a delegation that would exceed the cap.

    Raises:
        AccessDenied: the tree is too deep. An access error rather than a
            budget error because from the model's side this is "you may not",
            and the message says what to do instead.
    """
    if child_depth > max_depth:
        raise AccessDenied(
            "delegation",
            f"this would be delegation depth {child_depth} and the limit is "
            f"{max_depth}. Do the work yourself rather than delegating further, or "
            "tell the user the task is nested more deeply than this agent allows.",
        )


def check_fanout(already_spawned: int, max_fanout: int) -> None:
    """Refuse a turn that has already spawned enough children.

    DESIGN.md §17: one turn spawning a tree is a cost incident. The cap is per
    turn rather than per Run so a long conversation can delegate repeatedly
    without one turn being able to fan out without bound.
    """
    if already_spawned >= max_fanout:
        raise AccessDenied(
            "delegation",
            f"this turn has already delegated {already_spawned} times and the limit "
            f"is {max_fanout}. Wait for the results you already asked for before "
            "delegating again.",
        )


def child_tool_names(parent_tools: Sequence[str], child_spec: AgentSpec) -> list[str]:
    """What a child may call: what it asks for, narrowed by what the parent holds.

    Uses the same ``narrow`` as the validator and the per-turn resolver, so a
    subagent's access is computed by one function rather than by a second
    implementation that could disagree (DESIGN.md §10.5).
    """
    asked = [tool.name for tool in child_spec.tools]
    return narrow(parent_tools, spec_grants=asked)


@dataclass(frozen=True, slots=True)
class Delegation:
    """One child Run a parent asked for."""

    subagent: str
    task: str
    child_run_id: RunId | None = None
    depth: int = 0


def delegation_definition(
    subagents: Sequence[SubagentRef], parent_tools: Sequence[str]
) -> ToolDefinition | None:
    """The parent's delegation tool, describing every subagent it may route to.

    Returns ``None`` when there are no subagents, so an agent that cannot
    delegate is not offered a tool that would always fail.

    Each subagent is described by its own full description and by the tools it
    would actually hold after narrowing, not the tools its Spec asks for. A model
    routing on a tool list the child will not get routes badly and the report
    then shows a delegation that could never have worked.
    """
    if not subagents:
        return None

    lines: list[str] = [
        "Delegate a task to a specialist agent. Choose the one whose description "
        "matches the work. Give it everything it needs in `task`: it does not see "
        "this conversation.",
        "",
        "Available agents:",
    ]
    for ref in subagents:
        holds = child_tool_names(parent_tools, ref.spec)
        tools = ", ".join(holds) if holds else "no tools"
        lines.append(f"- `{ref.name}`: {ref.description} (has: {tools})")

    return ToolDefinition(
        name=DELEGATE_TOOL,
        description="\n".join(lines),
        input_schema={
            "type": "object",
            "properties": {
                "subagent": {
                    "type": "string",
                    "description": "Which agent to delegate to.",
                    "enum": [ref.name for ref in subagents],
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The complete task, self-contained. The agent does not see "
                        "this conversation, so include every detail it needs."
                    ),
                },
            },
            "required": ["subagent", "task"],
            "additionalProperties": False,
        },
        annotations=frozenset({"write"}),
    )


def find_subagent(spec: AgentSpec, name: str) -> SubagentRef:
    """Look up a subagent by name, or say clearly which ones exist.

    Raises:
        AccessDenied: no subagent of that name. The message lists the real ones
            so the model can correct itself rather than guessing again.
    """
    for ref in spec.subagents:
        if ref.name == name:
            return ref
    available = ", ".join(r.name for r in spec.subagents) or "none"
    raise AccessDenied(
        f"subagent {name!r}",
        f"this agent has no subagent by that name. Available: {available}.",
    )


def child_input(task: str, parent_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a child Run receives.

    The task and nothing else by default. A child that inherited the parent's
    whole conversation would defeat the point of delegation, which is to hand a
    narrow job to a narrow context, and would make the routing description a lie.
    """
    payload: dict[str, Any] = {"message": task}
    if parent_input:
        payload["parent_input"] = parent_input
    return payload


# ---------------------------------------------------------------------------
# Composed subagents: written by the model, pinned by their own Run
# ---------------------------------------------------------------------------


SPAWN_TOOL = "spawn_subagent"
CHECK_TOOL = "check_subagent"
MESSAGE_TOOL = "message_subagent"
"""The three tools a parent inside a ``SpawnEnvelope`` is offered. Reserved in
``psych_runtime.core.spec.RESERVED_TOOL_NAMES`` so a Spec cannot claim any of them."""


class SpawnRequest(BaseModel):
    """What the model asked for, validated at the boundary.

    A Pydantic model rather than a dict of strings pulled out with ``.get``,
    because this is a boundary in the sense DESIGN.md §22 means: the values come
    from a model's JSON and everything downstream -- a published Version, a Run,
    a set of tool grants -- is built out of them.

    ## Why the brief is three fields and not one

    Modelled on what actually determines whether delegation works. ``purpose``
    is what the child is for, and is what makes a bad routing decision legible
    afterwards. ``deliverable`` is what it should hand back, and is what stops a
    child returning an essay where the parent needed a list. ``task`` is the
    work itself, and is the only one the child sees as a message rather than as
    an instruction, because the child does not see the parent's conversation.

    One free-text field would let a model write "research this" and leave the
    other two implied, which is precisely the failure ``MIN_SUBAGENT_DESCRIPTION``
    was added to catch on the authored path. The minimum lengths here are the
    same idea applied to the composed one: refuse a one-line brief at the
    boundary rather than debugging bad routing later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    """How the parent addresses this child in ``check_subagent`` and
    ``message_subagent``. Unique among the parent's live children, which the
    reducer enforces on the record."""
    purpose: str = Field(min_length=MIN_SPAWN_PURPOSE, max_length=2048)
    task: str = Field(min_length=MIN_SPAWN_TASK, max_length=65_536)
    deliverable: str = Field(min_length=MIN_SPAWN_DELIVERABLE, max_length=4096)
    tools: tuple[str, ...] = ()
    """What the child should be able to call. Narrowed, never granted: the
    result is the intersection of this, the envelope's ceiling and what the
    parent itself holds. Empty asks for everything the envelope allows."""
    model: str | None = None
    """Which model, out of the envelope's list. ``None`` means the parent's."""


@dataclass(frozen=True, slots=True)
class ComposedChild:
    """A child Spec composed inside an envelope, with what it ended up holding.

    The Spec is what gets published; the two values beside it are what the spawn
    record says, because a report showing what was *asked for* would describe a
    child that never existed.
    """

    spec: AgentSpec
    tools: tuple[str, ...]
    model: str


def spawn_definition(spec: AgentSpec, parent_tools: Sequence[str]) -> ToolDefinition | None:
    """The tool a parent uses to write itself a subagent.

    Returns ``None`` for an agent with no envelope, so an agent that may not
    compose children is not offered a tool that would always refuse.

    The description states the shape of the brief rather than only naming the
    fields, and lists the tools and models the envelope actually permits, for
    the same reason ``delegation_definition`` lists what a child will really
    hold: a model composing against a menu it does not have composes badly, and
    the report then shows a spawn that could never have worked.
    """
    envelope = spec.spawn
    if envelope is None:
        return None

    allowed = envelope_tools(envelope, parent_tools)
    models = envelope_models(envelope, spec.model.model)
    tools_line = ", ".join(allowed) if allowed else "no tools"

    description = "\n".join(
        [
            "Start a subagent you describe yourself, running in the background.",
            "",
            "It gets its own conversation and does not see this one, so `task` has to "
            "carry everything it needs. You keep working while it runs; when it "
            "finishes, its result arrives as a message here. Use `check_subagent` to "
            "look at one mid-flight and `message_subagent` to send it something.",
            "",
            "Say three separate things:",
            "- `purpose`: what this agent is for, in a sentence someone else could route on.",
            "- `task`: the work itself, self-contained.",
            "- `deliverable`: what it should hand back, and in what shape.",
            "",
            f"It may be given any of: {tools_line}.",
            f"It may run on: {', '.join(models)}.",
            f"At most {envelope.max_alive} of your subagents may be running at once.",
        ]
    )

    return ToolDefinition(
        name=SPAWN_TOOL,
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "A short name you will use to refer to this subagent later, "
                        "such as `pricing_research`."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        f"What this agent is for, at least {MIN_SPAWN_PURPOSE} characters."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The complete task. The agent does not see this conversation, "
                        f"so include every detail it needs. At least {MIN_SPAWN_TASK} "
                        "characters."
                    ),
                },
                "deliverable": {
                    "type": "string",
                    "description": (
                        "What it should return, and in what shape. At least "
                        f"{MIN_SPAWN_DELIVERABLE} characters."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(allowed)},
                    "description": (
                        "Which tools it may call. Leave empty to give it everything "
                        "listed above. Anything else you name is ignored."
                    ),
                },
                "model": {
                    "type": "string",
                    "enum": list(models),
                    "description": "Which model it runs on. Omit for the default.",
                },
            },
            "required": ["name", "purpose", "task", "deliverable"],
            "additionalProperties": False,
        },
        annotations=frozenset({"write"}),
    )


def check_definition(spec: AgentSpec) -> ToolDefinition | None:
    """The tool that reads a running child without waiting for it."""
    if spec.spawn is None:
        return None
    return ToolDefinition(
        name=CHECK_TOOL,
        description=(
            "Look at a subagent you started: whether it is still running, what it has "
            "done so far, and what it last said. Returns immediately and does not wait "
            "for it to finish."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name you gave it when you started it.",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        annotations=frozenset({"read-only"}),
    )


def message_definition(spec: AgentSpec) -> ToolDefinition | None:
    """The tool that sends a message into a running child."""
    if spec.spawn is None or not spec.spawn.may_message:
        return None
    return ToolDefinition(
        name=MESSAGE_TOOL,
        description=(
            "Send a message to a subagent that is still running -- a correction, a "
            "constraint you forgot, something you have since learned. It is delivered "
            "at the start of its next turn, not in the middle of whatever it is doing "
            "right now. To stop one instead of steering it, say so in the message and "
            "let it finish."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name you gave it when you started it.",
                },
                "message": {"type": "string", "description": "What to tell it."},
            },
            "required": ["name", "message"],
            "additionalProperties": False,
        },
        annotations=frozenset({"write"}),
    )


def envelope_tools(envelope: SpawnEnvelope, parent_tools: Sequence[str]) -> list[str]:
    """The ceiling: what any composed child of this parent could ever hold.

    ``narrow`` with the parent's own tools as the offer and the envelope as the
    grant, so the empty-means-everything rule and the trailing-``*`` patterns
    behave here exactly as they do at every other plane (DESIGN.md §10.5). This
    is the value the spawn tool's schema advertises, so the model chooses from
    what it can actually be given.
    """
    return narrow(parent_tools, spec_grants=list(envelope.tools))


def envelope_models(envelope: SpawnEnvelope, parent_model: str) -> list[str]:
    """Which models a composed child may name.

    An empty list means the parent's own model and nothing else. That is the
    conservative reading and the deliberate one: a model the author never wrote
    down is a model they never priced, and inheriting "any model the client
    accepts" from a field left blank is how a cheap agent spawns an expensive
    tree.
    """
    if not envelope.models:
        return [parent_model]
    return list(envelope.models)


def composed_tool_names(
    envelope: SpawnEnvelope, parent_tools: Sequence[str], asked: Sequence[str]
) -> list[str]:
    """What a composed child actually holds.

    The one line in this module that matters most. Three planes, composed in one
    direction only: what the parent holds, narrowed by the envelope's ceiling,
    narrowed by what the model asked for. Each step's output is a subset of its
    input by construction, because every one of them runs through ``narrow``,
    which selects *from* the plane above rather than unioning with it.

    So a parent cannot write itself a child with more access than it has. Not
    because this function checks for that case, but because there is no
    expressible request that reaches a name the parent's own tool list did not
    contain -- an unknown name simply selects nothing, exactly as a stale
    pattern does at every other plane.
    """
    ceiling = envelope_tools(envelope, parent_tools)
    if not asked:
        return ceiling
    return narrow(ceiling, spec_grants=list(asked))


def check_alive(alive: int, max_alive: int) -> None:
    """Refuse a spawn while enough children are already running.

    Counts children still alive rather than children spawned this turn. A
    background child outlives its turn, so the per-turn cap that bounds a
    blocking ``delegate`` would let a parent hold twenty open by spawning four a
    turn for five turns, and the number that costs money is the number running.
    """
    if alive >= max_alive:
        raise AccessDenied(
            "spawning a subagent",
            f"{alive} of your subagents are still running and the limit is "
            f"{max_alive}. Wait for one to finish, or use `check_subagent` to see "
            "where they are, before starting another.",
        )


def compose_child_spec(
    parent: AgentSpec, request: SpawnRequest, parent_tools: Sequence[str]
) -> ComposedChild:
    """Build the child Spec a spawn request asks for, inside the envelope.

    Raises:
        AccessDenied: the parent has no envelope, or asked for a model the
            envelope does not list.

    ## What the child inherits, and what it does not

    **Tools**, narrowed by ``composed_tool_names``, carried across as the
    parent's own tool objects rather than as names: the child Spec has to be
    self-contained enough to publish and to validate on its own.

    **Limits**, inherited whole. A composed child that could set its own budgets
    would make the parent's budgets advisory.

    **MCP servers are not inherited.** The envelope's ceiling is a list of names
    an author can read; a server's catalogue is resolved per turn and can grow
    tomorrow, so inheriting one would let a child be granted, next week, a tool
    nobody approved this week. A parent whose capabilities are all MCP composes
    children with no tools, which is visible in the spawn tool's own description
    rather than silent.

    **Skills, subagents and the envelope itself are not inherited.** The last of
    those is the one worth arguing: depth is capped, so a composed child that
    could compose its own children would still terminate. But the envelope is a
    permission an author granted to an agent they wrote, and an envelope that
    propagates into agents nobody wrote is a permission that grants itself. The
    authored path keeps its depth budget for trees an author can read.

    **Questions are off.** ``ask_question`` parks a Run waiting for a person, and
    nobody is watching a background child. A child that needs an answer says so
    in its output and the parent, which does have a person, asks.
    """
    envelope = parent.spawn
    if envelope is None:
        raise AccessDenied(
            "spawning a subagent",
            "this agent may not compose subagents. It can only delegate to the "
            "ones it was published with, if it has any.",
        )

    model = request.model or parent.model.model
    permitted_models = envelope_models(envelope, parent.model.model)
    if model not in permitted_models:
        raise AccessDenied(
            f"model {model!r}",
            f"a subagent of this agent may run on: {', '.join(permitted_models)}.",
        )

    granted = composed_tool_names(envelope, parent_tools, request.tools)
    by_name = {tool.name: tool for tool in parent.tools}
    tools = tuple(by_name[name] for name in granted if name in by_name)

    spec = AgentSpec(
        name=request.name,
        description=request.purpose,
        instructions=child_instructions(request),
        model=ModelRef(
            model=model,
            temperature=parent.model.temperature,
            top_p=parent.model.top_p,
            max_output_tokens=parent.model.max_output_tokens,
            reasoning_effort=parent.model.reasoning_effort,
        ),
        tools=tools,
        limits=parent.limits,
        suspension=SuspensionPolicy(
            approval_expires_seconds=parent.suspension.approval_expires_seconds,
            question_expires_seconds=parent.suspension.question_expires_seconds,
            external_expires_seconds=parent.suspension.external_expires_seconds,
            may_ask_questions=False,
        ),
        answer_style=parent.answer_style,
    )
    return ComposedChild(spec=spec, tools=tuple(tool.name for tool in tools), model=model)


def child_instructions(request: SpawnRequest) -> str:
    """The composed child's system instructions.

    The purpose and the deliverable, plus the one fact the child cannot work out
    for itself: that it is a subagent, that the conversation it came from is not
    visible to it, and that what it returns is read by another agent rather than
    by a person. Without that last line a child writes a chatty answer for a
    reader who is a program.

    The ``task`` is deliberately absent: it goes into the child's Run input as
    its first message (``child_input``). Two spawns that share a purpose, a
    deliverable, a tool set and a model then publish one Version rather than two,
    which is what makes a Version hash worth having.
    """
    return "\n\n".join(
        [
            f"You are a subagent. Your job: {request.purpose}",
            "You were started by another agent to do one piece of work. You cannot "
            "see the conversation you came from, and you cannot ask the person who "
            "started it anything: everything you need is in the message you were "
            "given. If something essential is missing, say exactly what is missing "
            "and stop rather than guessing.",
            f"What to return: {request.deliverable}",
            "Your answer is read by the agent that started you, not by a person. "
            "Give it the result and what it needs to trust the result. Skip the "
            "pleasantries.",
        ]
    )


@runtime_checkable
class ChildOps(Protocol):
    """What the agent loop needs to run a composed subagent.

    Supplied by ``psych_runtime.runtime.execute`` rather than built here, for the same
    reason ``DelegateFn`` is: this module stays a pure statement of the rules --
    what may be composed, out of what, addressed how -- and the loop does not
    have to know how a Run is admitted, stored or woken.

    A Protocol rather than four callables passed side by side, because the four
    are one thing: they share a parent journal, a Scope and an Attempt, and
    passing them separately is how three of them end up bound to one Run and the
    fourth to another.
    """

    async def spawn(self, call_id: ToolCallId, request: SpawnRequest, depth: int) -> dict[str, Any]:
        """Compose, publish, admit and record one child. Returns its id.

        ``call_id`` is the model's own tool call, carried through so the spawn
        record and the call that asked for it are one thing in a report rather
        than two a reader has to join by timestamp."""
        ...

    async def check(self, child: ChildRun) -> dict[str, Any]:
        """A snapshot of a child's own log. Never waits."""
        ...

    async def message(self, call_id: ToolCallId, child: ChildRun, message: str) -> dict[str, Any]:
        """Queue a message for the child's next turn boundary."""
        ...

    async def reconcile(self) -> None:
        """Record any child that has already finished but never said so.

        The parent's own defence against a lost notification. A child's Worker
        appends ``subagent_finished`` when it settles, but that append can be
        lost -- the process dies, the store blinks -- and a parent that suspended
        on a notification which is never coming would wait out its whole expiry
        for a child that finished a second later. So before it suspends, a parent
        reads its live children's own logs and writes the notification itself for
        any that have already settled.
        """
        ...


def find_child(state: RunStateView, name: str) -> ChildRun:
    """The child a parent means by ``name``.

    Prefers a live child, then the most recently spawned one with that name. The
    reducer already refuses two live children sharing a name, so the first branch
    is unambiguous; the second exists because a name is reusable once its holder
    has finished, and "check the one I just ran" has to keep working.

    Raises:
        AccessDenied: no child by that name. The message lists the ones there
            are, so the model can correct itself rather than guessing again --
            the same shape ``find_subagent`` uses for the authored path.
    """
    live = [child for child in state.children.values() if child.name == name and child.alive]
    if live:
        return live[-1]
    matching = [child for child in state.children.values() if child.name == name]
    if matching:
        return matching[-1]
    available = ", ".join(child.name for child in state.children.values()) or "none"
    raise AccessDenied(
        f"subagent {name!r}",
        f"you have not started a subagent by that name. Started so far: {available}.",
    )
