"""Psych's own built-in tools, and the one place the offer is assembled.

``load_skill`` (§16), ``remember`` and ``forget`` (§15) are named in the design
and their names live in ``psych_runtime.core.spec.RESERVED_TOOL_NAMES`` so no Spec can
shadow them. ``ask_question``, ``update_tasks`` and ``show_component`` join
them here, each gated on a Spec field and off by default; only the runtime
offers any of them.

## Two separate jobs, because execution and advertisement are separate paths

``ToolExecutor.call`` (``psych_runtime.runtime.agent``) reaches a code tool through
``if name in self._registry: return await self._registry.call(name, arguments)``.
So a built-in is *callable* the moment it is registered into the same
``ToolRegistry`` the executor was built with, which ``register_builtins`` does.

But being callable is not being *offered*: the model only sees tools in
``ToolResolver.resolve``'s returned definitions, and a built-in is not a
``CodeTool`` in the Spec (Specs are forbidden from naming a reserved name), so
the resolver's own ``_local_tools`` never picks it up from ``spec.tools``. It has
to arrive through ``resolve``'s ``extra`` parameter, which exists for exactly
this (``psych_runtime.tools.resolver`` docstring). ``BuiltinToolResolver`` is that
wiring: given the real resolver and the definitions ``register_builtins``
already computed, it adds them to whatever ``extra`` a caller (or, inside
``AgentLoop``, the loop itself for things like a subagent's delegation tool)
passes on every call, so nothing about the merge has to be remembered at each
call site.

## Registration is per Run, not per process

A code tool is registered once at boot because its function never changes. A
built-in closes over one Spec's skills and one Scope's memories, both of which
differ Run to Run, so ``register_builtins`` is called once per Run (or once per
Attempt) against a registry the caller owns for that Run, never against the
process-wide registry code tools live in. Passing the same shared registry
here would let one Run's ``load_skill`` see another Run's skills.

## Why memory is a local Protocol, not an import of ``psych_runtime.memory``

``pyproject.toml``'s import-linter layering puts ``psych_runtime.tools`` and
``psych_runtime.memory`` in the same layer (both sit above ``psych_runtime.store``, neither
above the other), so this module may not import
``psych_runtime.memory.port.MemoryStore`` without breaking that contract. ``MemoryPort``
below is the same move ``psych_runtime.tools.resolver`` already makes for MCP
(``McpCatalog``, ``TenantToolPolicy``): a structural Protocol naming exactly the
methods this module calls, so any object with that shape works, including
``psych_runtime.memory.store_backed.StoreBackedMemory`` or a consumer's own
adapter, without this module needing to know which package defines it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import TypeAdapter, ValidationError

from psych_runtime.core.components import (
    MAX_BADGES,
    MAX_CARDS,
    MAX_FIELDS,
    MAX_POINTS,
    MAX_SERIES,
    MAX_STEPS,
    Component,
    component_summary,
)
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.questions import MAX_OPTIONS, MAX_QUESTIONS, AskedQuestion, QuestionOption
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, WorkflowSpec
from psych_runtime.core.tasks import MAX_TASKS, Task, task_summary
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ResolvedTools, ToolResolver
from psych_runtime.tools.skills import LOAD_SKILL_DESCRIPTION, make_load_skill

__all__ = [
    "BuiltinToolResolver",
    "MemoryPort",
    "RememberedFact",
    "register_builtins",
]

_COMPONENT_ADAPTER: TypeAdapter[Component] = TypeAdapter(Component)
"""Built once: a discriminated union rebuilds its whole validator per call."""


@runtime_checkable
class RememberedFact(Protocol):
    """The shape of one durable fact, as ``remember``/``forget`` need it.

    Matches ``psych_runtime.memory.port.Memory`` structurally (``id``, ``content``)
    without importing it; see the module docstring for why.

    Read-only properties rather than plain attribute annotations on purpose:
    ``Memory`` is a frozen Pydantic model, and mypy's Pydantic plugin only
    recognises a frozen model's fields as satisfying a Protocol when the
    Protocol asks for a read-only property, not a plain (implicitly
    read-write) attribute. A plain ``id: str`` here type-checks in isolation
    but fails the moment a concrete frozen model is passed where this Protocol
    is expected, with an error that does not name the actual cause.
    """

    @property
    def id(self) -> str: ...

    @property
    def content(self) -> str: ...


@runtime_checkable
class JournalPort(Protocol):
    """What ``update_tasks`` needs from a ``Journal``, structurally.

    A Protocol rather than an import for the same reason ``MemoryPort`` above
    is one: ``psych_runtime.tools`` sits below ``psych_runtime.runtime`` in the import graph
    and importing the concrete ``Journal`` would invert that. Only the two
    members the tool actually touches.
    """

    @property
    def state(self) -> Any: ...

    async def append(self, **fields: Any) -> Any: ...


class MemoryPort(Protocol):
    """What ``register_builtins`` needs from a ``MemoryStore``, structurally.

    Matches ``psych_runtime.memory.port.MemoryStore`` method for method. Only the three
    methods a ``remember``/``forget`` tool actually calls; ``erase`` is a
    consumer-facing operation (DESIGN.md §15's erasure requirement) with no
    tool wrapping it, so it is not part of what this module needs.
    """

    async def remember(self, scope: Scope, end_user_id: str, content: str) -> RememberedFact: ...

    async def forget(self, scope: Scope, end_user_id: str, memory_id: str) -> bool: ...

    async def recall(
        self, scope: Scope, end_user_id: str, *, limit: int | None = None
    ) -> Sequence[RememberedFact]: ...


_REMEMBER_DESCRIPTION = (
    "Remember one durable fact about this end user, for use in future runs as "
    "well as this one. Call this for a fact worth recalling later (a "
    "preference, a constraint, something they told you to remember), not for "
    "information that only matters to the current conversation."
)

_FORGET_DESCRIPTION = (
    "Forget one previously remembered fact about this end user. Pass the exact "
    'text of the fact as it appears under "What you remember about this '
    'user" above.'
)


UPDATE_TASKS = "update_tasks"
"""The task-list tool's name. A constant because two modules agree on it."""

ASK_QUESTION = "ask_question"
"""The one built-in whose body never runs.

Every other tool is a callable the executor dispatches. This one is a *request
to stop*: the agent loop intercepts it at the same gate an approval passes
through, writes a suspension, and returns. What comes back is not a return
value but a person's answer, delivered through `psych_runtime.resume(payload=...)` on
whatever Worker picks the Run up next, which may not be the one that asked.

Named as a constant because two modules have to agree on the string and a typo
in either would silently turn the interception off, leaving a tool that parks
nothing and answers nothing.
"""

_ASK_QUESTION_DESCRIPTION = (
    "Ask the person you are helping, and wait for their answer. The "
    "conversation stops until they reply, so use this only when you genuinely "
    "cannot proceed without knowing: a missing order number, a choice only "
    "they can make. Not to check in, not to confirm something you already "
    "have, and not to ask permission for a tool call, which is handled "
    "separately.\n\n"
    "Pass `questions`: a list of up to four objects, each with `question` (the "
    "whole question), an optional short `header` for a compact label, and "
    "optional `options` -- up to four `{label, description}` choices. Offer "
    "options whenever you are choosing between branches you can name: it is "
    "faster for them and unambiguous for you. Set `multi_select` when several "
    "answers can be true at once. They can always reply in their own words "
    "instead, so a wrong guess at the options costs nothing. Their answers "
    "come back as this call's result."
)


_UPDATE_TASKS_DESCRIPTION = (
    "Write down your plan and keep it current as you work, so the person "
    "watching can see what you are doing and what is left.\n\n"
    "Pass `tasks`: the whole list every time, including items that have not "
    "changed. This replaces the plan rather than merging into it. Each item "
    "is an object with:\n"
    '- `title`: the step, imperative and short. "Fix the failing login test".\n'
    "- `active_form`: the same step in the present continuous, shown while it "
    'is running. "Fixing the failing login test".\n'
    "- `description`: what it involves, only when the title is not enough.\n"
    "- `status`: `pending`, `in_progress` or `completed`.\n\n"
    "How to use it well:\n"
    "- Mark a task `in_progress` BEFORE you start it, not after.\n"
    "- Exactly one task `in_progress` at a time.\n"
    "- Mark it `completed` as soon as it is genuinely done, then start the "
    "next one.\n"
    "- Do NOT mark something `completed` if it is half-finished, if it "
    "failed, or if you hit an error you have not resolved. Leave it "
    "`in_progress` and say what is blocking you.\n\n"
    "Worth doing when the work has several steps you can name up front. Skip "
    "it for a single lookup: a one-item plan tells the person watching nothing "
    "they could not already see."
)


def register_builtins(
    registry: ToolRegistry,
    spec: AgentSpec,
    scope: Scope,
    *,
    memory: MemoryPort | None = None,
    end_user_id: str | None = None,
    journal: JournalPort | None = None,
) -> tuple[ToolDefinition, ...]:
    """Register Psych's built-ins for one Run into ``registry``.

    Args:
        registry: a Run-scoped (or Attempt-scoped) registry, distinct from any
            process-wide registry holding the consumer's code tools. Passed to
            ``ToolExecutor`` so a call the model makes actually dispatches.
        spec: the pinned Spec. ``load_skill`` is registered only when it has
            skills to serve; a Spec with none gets no ``load_skill`` at all,
            since offering a tool that can only ever say "no such skill" is
            worse than not offering it.
        scope: whose Run this is. Bound into every ``remember``/``forget``
            call so a fact always lands under the right tenant.
        memory: the configured ``MemoryStore``. ``remember`` and ``forget`` are
            registered only when this is given, because memory is optional:
            a consumer who has not wired a ``MemoryStore`` gets no memory
            tools rather than tools that fail every call.
        journal: this Run's journal, needed by ``update_tasks`` and
            ``show_component``, both of which write into the log rather than
            into a store of their own. ``None`` skips both, which is also what
            a Spec that did not ask for them does.
        end_user_id: whose durable facts these are. Required together with
            ``memory``, see the raised error below, because DESIGN.md §15
            keys memory by Scope *plus* an end-user id, and a call that forgot
            to supply one is a configuration bug, not a case to paper over
            with a default that would (quietly) point every end user at the
            same bucket of facts.

    Returns:
        The ``ToolDefinition`` for each built-in actually registered, ready to
        hand to ``ToolResolver.resolve(extra=...)`` (directly, or through
        ``BuiltinToolResolver`` below).

    Raises:
        ValueError: ``memory`` was given without ``end_user_id``, or the
            reverse. Both or neither, never one alone.
    """
    if (memory is None) != (end_user_id is None):
        raise ValueError(
            "register_builtins needs both `memory` and `end_user_id` to offer "
            "remember/forget, or neither to skip memory entirely; got one "
            "without the other, which would key memory ambiguously"
        )

    definitions: list[ToolDefinition] = []

    if spec.skills:
        registry.register(
            name="load_skill",
            description=LOAD_SKILL_DESCRIPTION,
            interruptible=True,
            safe_to_retry=True,
            annotations=frozenset({"read-only"}),
        )(make_load_skill(spec))
        definitions.append(registry.require("load_skill").definition())

    if spec.suspension.may_ask_questions:
        # Registered so it appears in the model's tool list and so the executor
        # can find a definition for it; the body is never reached, because
        # `psych_runtime.runtime.agent` intercepts the call before dispatch. It raises
        # rather than returning a placeholder: if interception ever breaks, a
        # loud failure beats a tool that silently answers its own question.
        registry.register(
            name=ASK_QUESTION,
            description=_ASK_QUESTION_DESCRIPTION,
            interruptible=True,
            safe_to_retry=False,
            annotations=frozenset({"read-only"}),
        )(_unreachable_ask_question)
        definitions.append(registry.require(ASK_QUESTION).definition())

    if spec.components_enabled and journal is not None:
        registry.register(
            name=SHOW_COMPONENT,
            description=_SHOW_COMPONENT_DESCRIPTION,
            interruptible=True,
            safe_to_retry=True,
            annotations=frozenset({"read-only"}),
        )(_make_show_component(journal))
        definitions.append(registry.require(SHOW_COMPONENT).definition())

    if spec.tasks_enabled and journal is not None:
        registry.register(
            name=UPDATE_TASKS,
            description=_UPDATE_TASKS_DESCRIPTION,
            interruptible=True,
            safe_to_retry=True,
            annotations=frozenset({"read-only"}),
        )(_make_update_tasks(journal))
        definitions.append(registry.require(UPDATE_TASKS).definition())

    if memory is not None and end_user_id is not None:
        registry.register(
            name="remember",
            description=_REMEMBER_DESCRIPTION,
            interruptible=True,
            safe_to_retry=False,
            annotations=frozenset({"write"}),
        )(_make_remember(memory, scope, end_user_id))
        definitions.append(registry.require("remember").definition())

        registry.register(
            name="forget",
            description=_FORGET_DESCRIPTION,
            interruptible=True,
            safe_to_retry=False,
            annotations=frozenset({"write"}),
        )(_make_forget(memory, scope, end_user_id))
        definitions.append(registry.require("forget").definition())

    return tuple(definitions)


def _make_remember(
    memory: MemoryPort, scope: Scope, end_user_id: str
) -> Callable[[str], Awaitable[dict[str, Any]]]:
    """Build the ``remember`` closure, bound to one Scope and end user.

    A closure rather than a callable class: ``ToolRegistry`` derives a schema
    by calling ``get_type_hints`` on whatever is registered, and that function
    only accepts a module, class, method or function (``typing`` docs), not an
    arbitrary object with ``__call__``. A nested function is a real function
    object, so it qualifies; an instance of a callable class would raise
    ``TypeError`` the moment registration tried to derive its schema.
    """

    async def remember(content: str) -> dict[str, Any]:
        fact = await memory.remember(scope, end_user_id, content)
        return {"remembered": True, "content": fact.content}

    return remember


def _make_forget(
    memory: MemoryPort, scope: Scope, end_user_id: str
) -> Callable[[str], Awaitable[dict[str, Any]]]:
    """Build the ``forget`` closure, bound to one Scope and end user.

    Takes the fact's *content*, not an opaque id: ``psych_runtime.model.prompt.
    memories_block`` renders remembered facts as plain content strings with no
    id attached (by design, see that module for the cache-ordering
    rationale this must not disturb), so an id is not something the model can
    actually reference. ``MemoryPort.forget`` itself is id-based, which stays
    the right shape for the port; this is the translation from what the model
    can see to what the port needs.
    """

    async def forget(content: str) -> dict[str, Any]:
        facts = await memory.recall(scope, end_user_id)
        match = next((fact for fact in facts if fact.content == content), None)
        if match is None:
            return {
                "forgotten": False,
                "error": (
                    f"no remembered fact matches {content!r} exactly. Use the exact "
                    'text as it appears under "What you remember about this user".'
                ),
            }
        await memory.forget(scope, end_user_id, match.id)
        return {"forgotten": True, "content": content}

    return forget


class BuiltinToolResolver(ToolResolver):
    """Wraps a ``ToolResolver`` to also offer a fixed set of built-in definitions.

    ``AgentLoop`` (or any caller) resolves the tool set by calling
    ``resolver.resolve(spec, scope, extra=...)`` itself, computing ``extra``
    fresh each turn for things that genuinely change turn to turn, such as a
    subagent's delegation tool. This class does not replace that: it wraps the
    resolver actually doing the work and adds a fixed tail of definitions to
    whatever ``extra`` arrives on each call, so built-ins show up without every
    call site needing to remember to append them.

    Deliberately does not call ``ToolResolver.__init__``: every method it
    inherits is overridden below, so there is no base-class state to
    initialise, and the type still satisfies anything that requires a
    ``ToolResolver`` (``AgentLoop``'s constructor, notably).
    """

    def __init__(self, inner: ToolResolver, definitions: Sequence[ToolDefinition]) -> None:
        self._inner = inner
        self._definitions = tuple(definitions)

    async def resolve(
        self,
        spec: AgentSpec | WorkflowSpec,
        scope: Scope,
        *,
        failure_streaks: Mapping[str, int] | None = None,
        extra: Sequence[ToolDefinition] = (),
    ) -> ResolvedTools:
        return await self._inner.resolve(
            spec,
            scope,
            failure_streaks=failure_streaks,
            extra=(*extra, *self._definitions),
        )


async def _unreachable_ask_question(questions: list[dict[str, Any]]) -> str:  # noqa: ARG001
    """Never called. See `ASK_QUESTION`.

    Args:
        questions: up to four questions, each `{question, header?, options?,
            multi_select?}` where an option is `{label, description?}`.
            Declared so the registry derives the schema the model is shown; the
            value is read off the recorded call by the agent loop, not here.
    """
    raise RuntimeError(
        "ask_question was dispatched instead of intercepted, which means the "
        "agent loop's suspension path did not run. The Run would otherwise "
        "have answered its own question."
    )


def parse_questions(arguments: dict[str, Any]) -> tuple[AskedQuestion, ...]:
    """Read the model's `questions` argument into validated questions.

    Lenient on the way in, deliberately. A model that sends one question as a
    bare string, or a plain list of strings for options, has said something
    unambiguous, and refusing it would park nothing and teach nothing. What is
    *not* accepted is an empty ask: nothing to answer means nobody should be
    interrupted, and the caller turns an empty result into a tool error rather
    than a suspension.

    Over-long lists are truncated rather than rejected for the same reason: a
    model that asked six questions still asked four good ones first.
    """
    raw = arguments.get("questions")
    if isinstance(raw, str):
        raw = [{"question": raw}]
    if not isinstance(raw, list):
        return ()

    parsed: list[AskedQuestion] = []
    for raw_item in raw[:MAX_QUESTIONS]:
        item = {"question": raw_item} if isinstance(raw_item, str) else raw_item
        if not isinstance(item, dict):
            continue
        text = item.get("question")
        if not isinstance(text, str) or not text.strip():
            continue
        parsed.append(
            AskedQuestion(
                question=text.strip()[:2000],
                header=str(item.get("header") or "")[:24],
                options=_parse_options(item.get("options")),
                multi_select=bool(item.get("multi_select")),
            )
        )
    return tuple(parsed)


def _parse_options(raw: Any) -> tuple[QuestionOption, ...]:
    if not isinstance(raw, list):
        return ()
    options: list[QuestionOption] = []
    for raw_item in raw[:MAX_OPTIONS]:
        item = {"label": raw_item} if isinstance(raw_item, str) else raw_item
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        options.append(
            QuestionOption(
                label=label.strip()[:80],
                description=str(item.get("description") or "")[:400],
            )
        )
    return tuple(options)


def _make_update_tasks(journal: JournalPort) -> Callable[..., Awaitable[str]]:
    """Build the `update_tasks` body, bound to this Run's journal.

    Unlike `ask_question`, this tool really runs: writing the plan *is* the
    work, and there is nothing to wait for. It appends a `task_list_updated`
    record and returns a confirmation, so the plan lives in the log where every
    other fact about a Run lives, and a reader gets it from the same projection
    rather than from a second place.
    """

    async def update_tasks(tasks: list[dict[str, Any]]) -> str:
        """Replace the plan with `tasks`.

        Args:
            tasks: the whole list, as `{title, active_form?, description?,
                status?}` objects.
        """
        parsed = parse_tasks(tasks)
        if not parsed:
            return "No tasks were recorded: every entry needs a `title`. The plan is unchanged."
        await journal.append(type="task_list_updated", tasks=parsed)
        return f"Plan updated: {task_summary(parsed)}."

    return update_tasks


def parse_tasks(raw: Any) -> tuple[Task, ...]:
    """Read the model's `tasks` argument into validated tasks.

    Lenient the same way `parse_questions` is: a bare string becomes a pending
    task, an unknown status becomes `pending` rather than failing the call, and
    an over-long list is truncated. A model that wrote a good plan and got one
    status word wrong should not lose the plan.

    An entry with no title is dropped rather than defaulted, because a task
    with no title tells a reader nothing and there is no sensible guess.
    """
    if not isinstance(raw, list):
        return ()
    parsed: list[Task] = []
    for raw_item in raw[:MAX_TASKS]:
        item = {"title": raw_item} if isinstance(raw_item, str) else raw_item
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        status = item.get("status")
        parsed.append(
            Task(
                title=title.strip()[:200],
                # `activeForm` accepted as well as `active_form`: the camelCase
                # spelling is what several harnesses use, and a model that
                # reaches for it has still said the right thing.
                active_form=str(item.get("active_form") or item.get("activeForm") or "")[:200],
                description=str(item.get("description") or "")[:2000],
                status=status if status in ("pending", "in_progress", "completed") else "pending",
            )
        )
    return tuple(parsed)


SHOW_COMPONENT = "show_component"
"""The structured-answer tool's name. A constant because two modules agree on it."""


_SHOW_COMPONENT_DESCRIPTION = (
    "Show something structured alongside your answer, when a paragraph is the "
    "wrong shape for what you have: a product, a set of products to choose "
    "between, an order, an itinerary, a number, a trend.\n\n"
    "Do NOT use it for a plain table -- your answer already renders markdown "
    "tables, and a table of two columns is not worth a component. Use it when "
    "the data has a shape: one entity, several to compare, a record with a "
    "status, a sequence with a position in it, numbers over time, or a single "
    "figure.\n\n"
    "Pass `component`: one object with a `kind` and that kind's fields. Call "
    "the tool again to show another; each call shows exactly one, and they "
    "appear in the order you sent them. Keep your written answer as well: the "
    "component supports it, it does not replace it.\n\n"
    "The kinds:\n"
    "- `card` -- one entity. `title`, `subtitle`, `image_url`, `badges` (short "
    'standing facts like "In stock"), `fields` ([{label, value}]), `href`.\n'
    "- `carousel` -- several to browse or compare. `title`, `cards` (a list of "
    "card objects, same fields as above).\n"
    "- `detail` -- a record read on its own: an order, a booking, a "
    "confirmation. `title`, `subtitle`, `status`, `fields`, `total` "
    "({label, value}), `href`.\n"
    "- `timeline` -- an ordered sequence with a position in it: a trip, a "
    "delivery, a process. `title`, `steps` ([{label, at, description, state}] "
    "where `state` is `done`, `current` or `upcoming`).\n"
    "- `chart` -- numbers with a shape. `mark` (`line`, `bar`, `area` or "
    "`pie`), `series` ([{name, points: [{x, y}]}]), `x_label`, `y_label`, "
    "`title`. `x` may be a number or a label; `y` is always a number.\n"
    "- `metric` -- one number that is the answer. `label`, `value`, `unit`, "
    '`delta` ("+12%"), `direction` (`up`, `down` or `flat`).\n\n'
    "Links and images must be http or https URLs; anything else is refused. "
    "Send data only: there is nowhere to put a colour, a size or any styling, "
    "because whoever displays this owns how it looks."
)


def _make_show_component(journal: JournalPort) -> Callable[..., Awaitable[str]]:
    """Build the `show_component` body, bound to this Run's journal.

    Writes a `component_shown` record and returns a confirmation, so what was
    shown lives in the log beside everything else that happened and a reader
    gets it from the same projection. Nothing is rendered here and nothing is
    rendered anywhere in `psych` -- see `psych_runtime.core.components`.
    """

    async def show_component(component: dict[str, Any]) -> str:
        """Show one component beside your answer.

        Args:
            component: one object with a `kind` (`card`, `carousel`, `detail`,
                `timeline`, `chart` or `metric`) and that kind's fields.
        """
        try:
            parsed = parse_component(component)
        except ValidationError as err:
            # A tool result rather than a tool failure, deliberately. The
            # component was the garnish; the answer is the answer. Failing the
            # call would spend the model's failure streak on a decoration and
            # could take down a Run that had already found the right answer,
            # so this hands back what was wrong and lets it say the thing in
            # prose instead.
            return (
                f"Nothing was shown: {_first_problem(err)}. Say it in your answer "
                "instead, or fix the component and call again."
            )
        await journal.append(type="component_shown", component=parsed)
        return f"Shown: {component_summary(parsed)}."

    return show_component


def _first_problem(err: ValidationError) -> str:
    """The one thing to tell the model about a component it got wrong.

    One rather than all of them: a discriminated union reports an error per
    member it tried, and handing a model twelve failures for one typo teaches
    it nothing about which field to fix.
    """
    problems = err.errors()
    if not problems:
        return "the component could not be read"
    first = problems[0]
    where = ".".join(str(part) for part in first["loc"]) or "component"
    return f"{where}: {first['msg']}"


def parse_component(raw: Any) -> Component:
    """Read the model's `component` argument into a validated component.

    Lenient in the same places `parse_tasks` is, and strict in the one place it
    must not be. Aliases are accepted (`type` for `kind`, `imageUrl` for
    `image_url`), numbers are read where strings are wanted, a bare list of
    points becomes a one-series chart, and over-long lists are truncated rather
    than refused -- a model that sent thirty cards still picked twelve good
    ones first.

    What is not smoothed over is a URL Psych will not vouch for. That raises,
    the caller turns it into a tool result rather than a component, and the
    Run answers in prose. Quietly dropping the bad URL and showing the card
    anyway would teach the model that `javascript:` links are fine and merely
    ineffective.

    Raises:
        ValidationError: the payload is not a component this vocabulary has,
            or one of its fields is not something a renderer may be handed.
    """
    return _COMPONENT_ADAPTER.validate_python(_normalise(raw))


def _normalise(raw: Any) -> Any:
    """Rewrite what the model sent into the shape each component model expects.

    Returns the input untouched when it is not a mapping or names no kind this
    vocabulary has, so the adapter produces the error rather than this function
    inventing one.
    """
    if not isinstance(raw, dict):
        return raw
    kind = raw.get("kind") or raw.get("type")
    kind = kind.strip().lower() if isinstance(kind, str) else kind
    normalise = _NORMALISERS.get(kind) if isinstance(kind, str) else None
    return raw if normalise is None else normalise(raw)


def _normalise_carousel(raw: dict[str, Any]) -> Any:
    cards = _as_list(raw.get("cards") or raw.get("items"))
    return {
        "kind": "carousel",
        "title": _text(raw.get("title")),
        "cards": [_normalise_card(card) for card in cards[:MAX_CARDS]],
    }


def _normalise_detail(raw: dict[str, Any]) -> Any:
    return {
        "kind": "detail",
        "title": _text(raw.get("title")),
        "subtitle": _text(raw.get("subtitle")),
        "status": _text(raw.get("status")),
        "fields": _normalise_fields(raw.get("fields")),
        "total": _normalise_total(raw.get("total")),
        "href": _text(raw.get("href") or raw.get("url")),
    }


def _normalise_timeline(raw: dict[str, Any]) -> Any:
    steps = _as_list(raw.get("steps") or raw.get("items"))
    return {
        "kind": "timeline",
        "title": _text(raw.get("title")),
        "steps": [_normalise_step(step) for step in steps[:MAX_STEPS]],
    }


def _normalise_metric(raw: dict[str, Any]) -> Any:
    value = raw.get("value")
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    return {
        "kind": "metric",
        "label": _text(raw.get("label") or raw.get("title")),
        "value": value if numeric else _text(value),
        "unit": _text(raw.get("unit")),
        "delta": _text(raw.get("delta") or raw.get("change")),
        "direction": raw.get("direction") or raw.get("trend") or None,
    }


def _normalise_card(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    badges = [_text(badge) for badge in _as_list(raw.get("badges"))[:MAX_BADGES]]
    return {
        "kind": "card",
        "title": _text(raw.get("title") or raw.get("name")),
        "subtitle": _text(raw.get("subtitle") or raw.get("description")),
        "image_url": _text(raw.get("image_url") or raw.get("imageUrl") or raw.get("image")),
        "badges": [badge for badge in badges if badge],
        "fields": _normalise_fields(raw.get("fields")),
        "href": _text(raw.get("href") or raw.get("url") or raw.get("link")),
    }


def _normalise_fields(raw: Any) -> list[Any]:
    """Read `fields` as either a list of pairs or the mapping a model reaches for.

    `{"Price": "$40"}` is what a model writes when it is thinking about the
    data rather than about this schema, and it is unambiguous, so it is
    accepted. Order is the mapping's own insertion order, which is the order
    the model wrote it in.
    """
    items: list[Any]
    if isinstance(raw, dict):
        items = [{"label": key, "value": value} for key, value in raw.items()]
    else:
        items = _as_list(raw)
    fields: list[Any] = []
    for item in items[:MAX_FIELDS]:
        if not isinstance(item, dict):
            continue
        fields.append(
            {
                "label": _text(item.get("label") or item.get("name")),
                "value": _text(item.get("value")),
            }
        )
    return fields


def _normalise_total(raw: Any) -> Any:
    """A total sent as a bare value still means "the total", so it is labelled."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return {"label": _text(raw.get("label") or "Total"), "value": _text(raw.get("value"))}
    return {"label": "Total", "value": _text(raw)}


def _normalise_step(raw: Any) -> Any:
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict):
        return raw
    state = raw.get("state") or raw.get("status")
    return {
        "label": _text(raw.get("label") or raw.get("title")),
        "at": _text(raw.get("at") or raw.get("time") or raw.get("when")),
        "description": _text(raw.get("description")),
        "state": state if state in ("done", "current", "upcoming") else "upcoming",
    }


def _normalise_chart(raw: dict[str, Any]) -> Any:
    series = raw.get("series") or raw.get("data")
    # A model that has one line to draw very often sends the points straight
    # through without wrapping them in a series. It has said something
    # unambiguous, so it gets an unnamed series rather than an error.
    if isinstance(series, list) and any(_looks_like_point(item) for item in series):
        series = [{"name": _text(raw.get("title")), "points": series}]
    mark = raw.get("mark") or raw.get("chart_type") or raw.get("chartType")
    return {
        "kind": "chart",
        "title": _text(raw.get("title")),
        "mark": mark.strip().lower() if isinstance(mark, str) else mark,
        "series": [_normalise_series(item) for item in _as_list(series)[:MAX_SERIES]],
        "x_label": _text(raw.get("x_label") or raw.get("xLabel")),
        "y_label": _text(raw.get("y_label") or raw.get("yLabel")),
    }


def _looks_like_point(item: Any) -> bool:
    if isinstance(item, dict):
        return "y" in item or "value" in item
    return isinstance(item, (list, tuple)) and len(item) == 2


def _normalise_series(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    points = _as_list(raw.get("points") or raw.get("data") or raw.get("values"))
    return {
        "name": _text(raw.get("name") or raw.get("label")),
        "points": [_normalise_point(point) for point in points[:MAX_POINTS]],
    }


def _normalise_point(raw: Any) -> Any:
    """Accept `{x, y}`, `{label, value}` and the bare `[x, y]` pair alike."""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return {"x": raw[0], "y": raw[1]}
    if not isinstance(raw, dict):
        return raw
    x = raw.get("x", raw.get("label"))
    return {
        "x": x if isinstance(x, (int, float)) and not isinstance(x, bool) else _text(x),
        "y": raw.get("y", raw.get("value")),
    }


def _as_list(raw: Any) -> list[Any]:
    return list(raw) if isinstance(raw, list) else []


def _text(raw: Any) -> str:
    """A string for a field that wants one, without inventing a `None`.

    Numbers are stringified because a model sending `value: 40` for a price has
    not made a mistake; anything else non-scalar becomes empty rather than
    `"{'a': 1}"`, which would put a Python repr in front of a person.
    """
    if raw is None or isinstance(raw, (dict, list, tuple)):
        return ""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    return str(raw).strip()


_NORMALISERS: Final[dict[str, Callable[[dict[str, Any]], Any]]] = {
    "card": _normalise_card,
    "carousel": _normalise_carousel,
    "detail": _normalise_detail,
    "timeline": _normalise_timeline,
    "chart": _normalise_chart,
    "metric": _normalise_metric,
}
"""One normaliser per kind, keyed by the discriminator the model sends.

A table rather than a match statement so that adding a seventh kind is one
line in one place, and so that a kind the vocabulary does not have falls
through to the adapter's own "no such kind" error rather than to a branch that
guessed.
"""
