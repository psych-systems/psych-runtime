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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import psych_runtime
from app.schemas import AgentStepIn, NestedWorkflowStepIn, ToolStepIn, WorkflowStepIn
from app.store_index import PlaygroundIndex, WorkflowEntry, WorkflowStepEntry, new_workflow_id
from app.tools import WorkflowPublisher
from psych_runtime.core.errors import SpecValidationError
from psych_runtime.core.ids import VersionHash
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

    async def publish(  # noqa: PLR0912 - one branch per step kind, each refusal named
        self, owner: str, definition: WorkflowDefinition, *, workflow_id: str | None = None
    ) -> tuple[WorkflowEntry, bool]:
        """Publish ``definition`` as ``owner``'s workflow.

        Raises:
            WorkflowRefused: a step names something this account does not
                have, a nested workflow would contain itself, or the Spec
                failed validation at publish.
        """
        if workflow_id is None:
            workflow_id = new_workflow_id()
        elif await self._index.get_workflow(owner, workflow_id) is None:
            raise WorkflowRefused(404, f"no workflow {workflow_id!r}")

        steps: list[
            psych_runtime.AgentStep | psych_runtime.ToolStep | psych_runtime.WorkflowStepRef
        ] = []
        entries: list[WorkflowStepEntry] = []
        tools: dict[str, psych_runtime.CodeTool] = {}
        for step in definition.steps:
            match step:
                case ToolStepIn():
                    if step.tool not in self._registry:
                        raise WorkflowRefused(
                            400,
                            f"step {step.name!r} names no registered tool {step.tool!r}; "
                            f"the registered tools are {sorted(self._registry.names)}",
                        )
                    tools.setdefault(step.tool, psych_runtime.CodeTool(name=step.tool))
                    steps.append(
                        psych_runtime.ToolStep(
                            name=step.name, tool=step.tool, arguments=step.arguments
                        )
                    )
                    entries.append(
                        WorkflowStepEntry(
                            kind="tool", name=step.name, tool=step.tool, arguments=step.arguments
                        )
                    )
                case AgentStepIn():
                    found = await self._index.get_agent(owner, step.agent_id)
                    if found is None:
                        raise WorkflowRefused(
                            404, f"step {step.name!r} names no agent {step.agent_id!r}"
                        )
                    version = await self._store.get_version(found[0].version_hash)
                    if version is None or not isinstance(version.spec, psych_runtime.AgentSpec):
                        raise WorkflowRefused(
                            409, f"agent {step.agent_id!r} has no published agent Version"
                        )
                    steps.append(psych_runtime.AgentStep(name=step.name, spec=version.spec))
                    entries.append(
                        WorkflowStepEntry(
                            kind="agent",
                            name=step.name,
                            agent_id=step.agent_id,
                            version_hash=str(version.hash),
                        )
                    )
                case NestedWorkflowStepIn():
                    if step.workflow_id == workflow_id:
                        raise WorkflowRefused(
                            400, f"step {step.name!r} would nest this workflow inside itself"
                        )
                    nested = await self._index.get_workflow(owner, step.workflow_id)
                    if nested is None:
                        raise WorkflowRefused(
                            404, f"step {step.name!r} names no workflow {step.workflow_id!r}"
                        )
                    version = await self._store.get_version(nested.version_hash)
                    if version is None or not isinstance(version.spec, psych_runtime.WorkflowSpec):
                        raise WorkflowRefused(
                            409, f"workflow {step.workflow_id!r} has no published Version"
                        )
                    steps.append(psych_runtime.WorkflowStepRef(name=step.name, spec=version.spec))
                    entries.append(
                        WorkflowStepEntry(
                            kind="workflow",
                            name=step.name,
                            workflow_id=step.workflow_id,
                            version_hash=str(version.hash),
                        )
                    )

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
            description=spec.description,
            steps=tuple(entries),
            tools=tuple(tools),
            limits=spec.limits.model_dump(),
            published_at=version.published_at,
            now=datetime.now(UTC),
        )


def summary_of(entry: WorkflowEntry) -> dict[str, Any]:
    """The entry as ``WorkflowSummary``'s fields, shared by the route and the tool."""
    return {
        "workflow_id": entry.workflow_id,
        "version_hash": str(entry.version_hash),
        "name": entry.name,
        "description": entry.description,
        "steps": [step.model_dump() for step in entry.steps],
        "tools": list(entry.tools),
        "limits": dict(entry.limits),
        "published_at": entry.published_at.isoformat(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
        "version_count": len(entry.history),
    }


class ToolWorkflowPublisher(WorkflowPublisher):
    """``create_workflow``'s door into the service. A tool call composes tool
    steps only: an agent or nested workflow step is a person's decision made
    on the Workflows page, where the ids are visible."""

    def __init__(self, service: WorkflowService) -> None:
        self._service = service

    async def publish_for(
        self, tenant: str, *, name: str, description: str, steps: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            definition = WorkflowDefinition(
                name=name,
                description=description,
                steps=[ToolStepIn(**step) for step in steps],
            )
            entry, created = await self._service.publish(tenant, definition)
        except (WorkflowRefused, ValidationError) as err:
            # Handed back as the tool's failure, so the model reads why and
            # fixes the definition rather than the Run failing.
            raise ValueError(str(err)) from err
        return {**summary_of(entry), "created": created}

    async def list_for(self, tenant: str) -> list[dict[str, Any]]:
        return [summary_of(entry) for entry in await self._service.index.list_workflows(tenant)]
