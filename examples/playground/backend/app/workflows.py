"""Workflows in the playground: the request shape becomes a ``WorkflowSpec``,
and one service publishes it for both the HTTP route and the ``create_workflow``
tool.

A workflow is DESIGN.md §5's other authoring surface: ordered steps, each a
registered tool with fixed arguments, an agent, or another workflow, run by
the same Worker under the same log with each completed step memoised. Nothing
here executes one. ``POST /api/runs`` dispatches a workflow's Version exactly
as it dispatches an agent's, and the pinned Version decides which engine the
Worker drives.

## Embedded, never referenced

An agent step and a nested workflow step name an existing agent or workflow
of this account, and what is published is a *copy* of that thing's current
Spec. That is the library's rule (``AgentStep.spec`` and
``WorkflowStepRef.spec`` are Specs, not hashes) and the reason is crash
recovery: a resuming Worker replays the one hash the Run pinned, and a hash
that pointed at something else's pointer could find it moved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

import psych_runtime
from app.schemas import (
    AgentStepIn,
    AskedQuestionIn,
    BranchStepIn,
    ConditionIn,
    ForEachStepIn,
    HumanStepIn,
    LoopStepIn,
    MappingIn,
    MapStepIn,
    NestedWorkflowStepIn,
    ParallelStepIn,
    RetryPolicyIn,
    SetStateStepIn,
    SleepStepIn,
    ToolStepIn,
    ValuePathIn,
    WaitStepIn,
    WorkflowStepIn,
)
from app.store_index import PlaygroundIndex, WorkflowEntry, WorkflowStepEntry, new_workflow_id
from app.tools import WorkflowPublisher
from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.ids import VersionHash
from psych_runtime.core.questions import AskedQuestion, QuestionOption
from psych_runtime.core.validation import ValidationContext
from psych_runtime.store.port import Store
from psych_runtime.tools.registry import ToolRegistry


class WorkflowDefinition(BaseModel):
    """What both doors -- the route and the tool -- hand the service."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(default="", max_length=4096)
    steps: list[WorkflowStepIn] = Field(min_length=1)
    limits: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    initial_state: dict[str, Any] = Field(default_factory=dict)
    output: MappingIn | None = None
    retry: RetryPolicyIn | None = None


class WorkflowRefused(Exception):
    """A definition this account cannot publish, with an HTTP status the
    route maps straight through and a message the tool hands the model."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class WorkflowService:
    """Publish a workflow for one account and remember it."""

    def __init__(self, *, store: Store, index: PlaygroundIndex, registry: ToolRegistry) -> None:
        self._store = store
        self._index = index
        self._registry = registry

    @property
    def index(self) -> PlaygroundIndex:
        return self._index

    async def publish(
        self,
        owner: str,
        definition: WorkflowDefinition,
        *,
        workflow_id: str | None = None,
        catalogue_id: str | None = None,
    ) -> tuple[WorkflowEntry, bool]:
        """Publish ``definition`` as ``owner``'s workflow.

        ``catalogue_id`` marks a workflow ``app.catalogue_seed`` shipped, so a
        re-seed skips it and a console can label it.

        Raises:
            WorkflowRefused: a step names something this account does not
                have, a nested workflow would contain itself, or the Spec
                failed validation at publish.
        """
        if workflow_id is None:
            workflow_id = new_workflow_id()
        else:
            editing = await self._index.get_workflow(owner, workflow_id)
            if editing is None:
                raise WorkflowRefused(404, f"no workflow {workflow_id!r}")
            # Inherited on an edit, for the reason `app.agent_publish` spells
            # out: an entry is rewritten per Version, and dropping this would
            # make a re-seed publish a second copy of a catalogue workflow
            # somebody had merely edited.
            if catalogue_id is None:
                catalogue_id = editing.catalogue_id

        # One accumulator for the whole tree. A tool named inside a branch arm
        # or a loop body has to reach `WorkflowSpec.tools` exactly as a
        # top-level one does, or the Spec fails validation at publish naming a
        # tool it never declared. An embedded agent or nested workflow carries
        # its own tools in its own Spec and contributes nothing here.
        tools: dict[str, psych_runtime.CodeTool] = {}
        steps: list[Any] = []
        entries: list[WorkflowStepEntry] = []
        for step in definition.steps:
            built = await self._build_step(owner, step, workflow_id=workflow_id, tools=tools)
            steps.append(built)
            entries.append(_entry_for(step))

        try:
            spec = psych_runtime.WorkflowSpec(
                name=definition.name,
                description=definition.description,
                steps=tuple(steps),
                tools=tuple(tools.values()),
                limits=(
                    psych_runtime.Limits(**definition.limits)
                    if definition.limits
                    else psych_runtime.Limits()
                ),
                input_schema=definition.input_schema,
                initial_state=dict(definition.initial_state),
                output=_mapping(definition.output) if definition.output else None,
                retry=(
                    psych_runtime.RetryPolicy(**definition.retry.model_dump())
                    if definition.retry is not None
                    else psych_runtime.RetryPolicy()
                ),
            )
            version = await psych_runtime.publish(
                self._store,
                spec,
                context=ValidationContext(registered_tools=self._registry.names),
            )
        except SpecValidationError as err:
            raise WorkflowRefused(
                400, "; ".join(f"{i.path}: {i.message}" for i in err.issues) or str(err)
            ) from err
        except (ValidationError, ValueError) as err:
            raise WorkflowRefused(400, str(err)) from err

        return await self._index.record_workflow(
            owner=owner,
            workflow_id=workflow_id,
            version_hash=VersionHash(version.hash),
            name=spec.name,
            catalogue_id=catalogue_id,
            description=spec.description,
            steps=tuple(entries),
            tools=tuple(tools),
            limits=spec.limits.model_dump(),
            input_schema=definition.input_schema,
            initial_state=dict(definition.initial_state),
            output=(
                {name: ref.model_dump(mode="json") for name, ref in definition.output.items()}
                if definition.output
                else None
            ),
            # The resolved policy rather than the request's, exactly as
            # `limits` above is: a workflow that set no `retry` still has the
            # default one, and reporting `null` would leave a console to guess
            # what every step actually inherits.
            retry=spec.retry.model_dump(mode="json"),
            published_at=version.published_at,
            now=datetime.now(UTC),
        )

    async def _build_step(  # noqa: PLR0911, PLR0912 - one arm per step kind, each named
        self,
        owner: str,
        step: WorkflowStepIn,
        *,
        workflow_id: str,
        tools: dict[str, psych_runtime.CodeTool],
    ) -> Any:
        """One request step as the library's own step, children and all.

        Recursive because the request is: a branch arm is a step, a loop body
        is a step, and each of them may be another composite. The three kinds
        that name something of this account -- a tool, an agent, a nested
        workflow -- are the only ones that can be refused, and each refusal
        keeps the status code it had when only those three kinds existed, so a
        console that already reads a 404 as "you deleted that agent" keeps
        reading it that way however deeply the step is nested.
        """
        common = _common(step)
        match step:
            case ToolStepIn():
                if step.tool not in self._registry:
                    raise WorkflowRefused(
                        400,
                        f"step {step.name!r} names no registered tool {step.tool!r}; "
                        f"the registered tools are {sorted(self._registry.names)}",
                    )
                tools.setdefault(step.tool, psych_runtime.CodeTool(name=step.tool))
                return psych_runtime.ToolStep(
                    **common,
                    tool=step.tool,
                    arguments=step.arguments,
                    arguments_from=_mapping(step.arguments_from),
                )
            case AgentStepIn():
                agent_spec = await self._agent_spec(owner, step)
                return psych_runtime.AgentStep(
                    **common, spec=agent_spec, input=_mapping(step.input)
                )
            case NestedWorkflowStepIn():
                nested_spec = await self._nested_spec(owner, step, workflow_id=workflow_id)
                return psych_runtime.WorkflowStepRef(
                    **common, spec=nested_spec, input=_mapping(step.input)
                )
            case ParallelStepIn():
                branches = [
                    await self._build_step(owner, branch, workflow_id=workflow_id, tools=tools)
                    for branch in step.branches
                ]
                return psych_runtime.ParallelStep(
                    **common,
                    branches=tuple(branches),
                    on_branch_failure=step.on_branch_failure,
                )
            case BranchStepIn():
                cases = [
                    psych_runtime.BranchCase(
                        name=case.name,
                        when=_condition(case.when),
                        step=await self._build_step(
                            owner, case.step, workflow_id=workflow_id, tools=tools
                        ),
                    )
                    for case in step.cases
                ]
                otherwise = (
                    await self._build_step(
                        owner, step.otherwise, workflow_id=workflow_id, tools=tools
                    )
                    if step.otherwise is not None
                    else None
                )
                return psych_runtime.BranchStep(
                    **common, cases=tuple(cases), otherwise=otherwise, mode=step.mode
                )
            case ForEachStepIn():
                return psych_runtime.ForEachStep(
                    **common,
                    items=psych_runtime.ValuePath(path=step.items.path),
                    body=await self._build_step(
                        owner, step.body, workflow_id=workflow_id, tools=tools
                    ),
                    concurrency=step.concurrency,
                    on_item_failure=step.on_item_failure,
                )
            case LoopStepIn():
                return psych_runtime.LoopStep(
                    **common,
                    body=await self._build_step(
                        owner, step.body, workflow_id=workflow_id, tools=tools
                    ),
                    until=_condition(step.until) if step.until is not None else None,
                    while_=_condition(step.while_) if step.while_ is not None else None,
                    max_iterations=step.max_iterations,
                )
            case MapStepIn():
                return psych_runtime.MapStep(**common, output=_mapping(step.output))
            case SetStateStepIn():
                return psych_runtime.SetStateStep(**common, values=_mapping(step.values))
            case SleepStepIn():
                return psych_runtime.SleepStep(
                    **common,
                    seconds=step.seconds,
                    until=(
                        psych_runtime.ValuePath(path=step.until.path)
                        if step.until is not None
                        else None
                    ),
                )
            case WaitStepIn():
                # `WaitStep` redeclares `timeout_seconds` -- there it bounds
                # the suspension rather than an attempt -- so the shared value
                # is dropped and the step's own is the one that is passed.
                return psych_runtime.WaitStep(
                    **{key: value for key, value in common.items() if key != "timeout_seconds"},
                    event=step.event,
                    payload_schema=step.payload_schema,
                    timeout_seconds=step.timeout_seconds,
                )
            case HumanStepIn():
                return psych_runtime.HumanStep(
                    **common,
                    prompt=step.prompt,
                    questions=tuple(_question(q) for q in step.questions),
                    expires_seconds=step.expires_seconds,
                )

    async def _agent_spec(self, owner: str, step: AgentStepIn) -> psych_runtime.AgentSpec:
        found = await self._index.get_agent(owner, step.agent_id)
        if found is None:
            raise WorkflowRefused(404, f"step {step.name!r} names no agent {step.agent_id!r}")
        version = await self._store.get_version(found[0].version_hash)
        if version is None or not isinstance(version.spec, psych_runtime.AgentSpec):
            raise WorkflowRefused(409, f"agent {step.agent_id!r} has no published agent Version")
        # Written back onto the step so the stored definition -- and therefore
        # `GET /api/workflows/{id}` -- reports the child hash this Version
        # actually pinned. Never read from the request: a caller's
        # `version_hash` is ignored, because the copy is taken here and a hash
        # that disagreed with it would be a lie about what runs.
        step.version_hash = str(version.hash)
        return version.spec

    async def _nested_spec(
        self, owner: str, step: NestedWorkflowStepIn, *, workflow_id: str
    ) -> psych_runtime.WorkflowSpec:
        if step.workflow_id == workflow_id:
            raise WorkflowRefused(400, f"step {step.name!r} would nest this workflow inside itself")
        nested = await self._index.get_workflow(owner, step.workflow_id)
        if nested is None:
            raise WorkflowRefused(404, f"step {step.name!r} names no workflow {step.workflow_id!r}")
        version = await self._store.get_version(nested.version_hash)
        if version is None or not isinstance(version.spec, psych_runtime.WorkflowSpec):
            raise WorkflowRefused(409, f"workflow {step.workflow_id!r} has no published Version")
        step.version_hash = str(version.hash)
        return version.spec


def _common(step: WorkflowStepIn) -> dict[str, Any]:
    """The fields every step kind shares, in the library's own spelling."""
    return {
        "name": step.name,
        "description": step.description,
        "when": _condition(step.when) if step.when is not None else None,
        "retry": (
            psych_runtime.RetryPolicy(**step.retry.model_dump()) if step.retry is not None else None
        ),
        "timeout_seconds": step.timeout_seconds,
        "on_failure": step.on_failure,
        "output_schema": step.output_schema,
    }


def _condition(condition: ConditionIn) -> psych_runtime.Condition:
    """The request's mirror validated as the library's own model.

    Through ``model_dump`` rather than field by field: the two shapes are the
    same by construction, and a recursive hand-written copy is one more place
    for them to drift apart silently.
    """
    return psych_runtime.Condition.model_validate(condition.model_dump(mode="json"))


def _mapping(mapping: MappingIn) -> dict[str, Any]:
    """A request Mapping as the library validates one: name to path or literal."""
    return {
        name: psych_runtime.ValuePath(path=ref.path)
        if isinstance(ref, ValuePathIn)
        else psych_runtime.LiteralValue(value=ref.value)
        for name, ref in mapping.items()
    }


def _question(question: AskedQuestionIn) -> AskedQuestion:
    return AskedQuestion(
        question=question.question,
        header=question.header,
        options=tuple(
            QuestionOption(label=option.label, description=option.description)
            for option in question.options
        ),
        multi_select=question.multi_select,
    )


def _entry_for(step: WorkflowStepIn) -> WorkflowStepEntry:
    """One top-level step as the index stores it.

    ``definition`` is the whole step, serialised the way the wire carries it
    (``by_alias``, so a loop's exit condition is ``while`` here as it is in the
    request), which is what makes ``GET /api/workflows/{id}`` round-trip a
    definition the console can put straight back into the editor. The flat
    fields beside it are the pre-composite shape, still filled for the three
    kinds that have them.
    """
    definition = step.model_dump(mode="json", by_alias=True)
    entry: dict[str, Any] = {"kind": step.kind, "name": step.name, "definition": definition}
    match step:
        case ToolStepIn():
            entry["tool"] = step.tool
            entry["arguments"] = dict(step.arguments)
        case AgentStepIn():
            entry["agent_id"] = step.agent_id
            entry["version_hash"] = step.version_hash
        case NestedWorkflowStepIn():
            entry["workflow_id"] = step.workflow_id
            entry["version_hash"] = step.version_hash
        case _:
            pass
    return WorkflowStepEntry(**entry)


def summary_of(entry: WorkflowEntry) -> dict[str, Any]:
    """The entry as ``WorkflowSummary``'s fields, shared by the route and the tool."""
    return {
        "workflow_id": entry.workflow_id,
        "version_hash": str(entry.version_hash),
        "name": entry.name,
        "description": entry.description,
        "steps": [step.definition for step in entry.steps],
        "tools": list(entry.tools),
        "limits": dict(entry.limits),
        "input_schema": entry.input_schema,
        "initial_state": dict(entry.initial_state),
        "output": entry.output,
        "retry": entry.retry,
        "published_at": entry.published_at.isoformat(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
        "version_count": len(entry.history),
    }


class ToolWorkflowPublisher(WorkflowPublisher):
    """``create_workflow``'s door into the service.

    A tool call composes any of the twelve step kinds, agent and nested
    workflow steps included. It did not always: when the only catalogue was
    three demo tools there was nothing worth delegating to, and restricting the
    tool to ``tool`` steps saved validating a recursive union. With a specialist
    agent per connector there is, and a model asked to "set up the weekly
    report" that could not put an agent in a step would build something worse
    out of the tools it had.

    The steps arrive as plain objects -- ``list[dict]`` on the tool's schema --
    and are validated here against ``WorkflowStepIn``, the same discriminated
    union ``POST /api/workflows`` validates a request body against. One
    ``TypeAdapter`` over the alias rather than a match on ``kind``: the union is
    recursive, and a hand-written dispatch would have to recurse too, and would
    be the copy that drifts.

    Dispatching is here as well (``dispatch_for``) rather than on a second port,
    because the application object a tool needs for either is the same one.
    """

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

    async def publish_for(
        self, tenant: str, *, name: str, description: str, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            parsed = _STEPS.validate_python(steps)
            definition = WorkflowDefinition(name=name, description=description, steps=parsed)
            entry, created = await self._service.publish(tenant, definition)
        except (WorkflowRefused, ValidationError) as err:
            # Handed back as the tool's failure, so the model reads why and
            # fixes the definition rather than the Run failing.
            raise ValueError(str(err)) from err
        return {**summary_of(entry), "created": created}

    async def list_for(self, tenant: str) -> list[dict[str, Any]]:
        return [summary_of(entry) for entry in await self._service.index.list_workflows(tenant)]

    async def dispatch_for(
        self, tenant: str, *, workflow_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "run_workflow needs the application's dispatch path; see app.main"
        )


_STEPS: TypeAdapter[list[WorkflowStepIn]] = TypeAdapter(list[WorkflowStepIn])
"""``WorkflowStepIn`` is an ``Annotated`` union alias, not a model, so it is
validated through an adapter. Built once: constructing one per call rebuilds a
recursive schema on every tool call."""
