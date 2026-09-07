"""The typed builder for a Workflow Spec.

DESIGN.md §5: a Workflow is a Step that sequences other Steps deterministically,
by step memoisation rather than replay, so ``.tool_step()``, ``.agent_step()``
and ``.workflow_step()`` append to an ordered list that is never sorted: order
is exactly what "sequences deterministically" means, and the Spec model
(``psych_runtime.core.spec.WorkflowSpec``) preserves it for the same reason.
"""

from __future__ import annotations

from typing import Self

from psych_runtime.builder.agent import AgentBuilder
from psych_runtime.builder.errors import BuilderError
from psych_runtime.builder.shared import SharedBuilder
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    Limits,
    SuspensionPolicy,
    ToolStep,
    WorkflowSpec,
    WorkflowStep,
    WorkflowStepRef,
)
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["WorkflowBuilder", "workflow"]


def workflow(name: str, *, registry: ToolRegistry | None = None) -> WorkflowBuilder:
    """Start building a Workflow Spec named ``name``."""
    return WorkflowBuilder(name, registry=registry)


class WorkflowBuilder(SharedBuilder):
    """Fluent construction of a ``WorkflowSpec``.

    ```python
    spec = (
        WorkflowBuilder("onboard-customer")
        .tool(create_account)
        .tool_step("create", "create_account", {"plan": "starter"})
        .agent_step("welcome", AgentBuilder("greeter").model("gpt-4o-mini"))
        .build()
    )
    ```

    A step's ``name`` is what ``psych_runtime.core.validation`` and a report address
    it by, so it must be unique within one workflow; the Spec model enforces
    that at validation, not this builder.
    """

    def __init__(self, name: str, *, registry: ToolRegistry | None = None) -> None:
        super().__init__(registry=registry)
        self._name = name
        self._description = ""
        self._steps: list[WorkflowStep] = []

    def description(self, text: str) -> Self:
        self._description = text
        return self

    def tool_step(self, name: str, tool: str, arguments: dict[str, object] | None = None) -> Self:
        """A step that calls one tool with fixed arguments. ``tool`` must be
        granted by this workflow's ``.tool()``/``.tool_by_name()`` or
        registered in the process the Run executes in; publish-time
        validation checks the former and names the latter as a possibility
        when it cannot."""
        self._steps.append(ToolStep(name=name, tool=tool, arguments=arguments or {}))
        return self

    def agent_step(self, name: str, spec: AgentSpec | AgentBuilder) -> Self:
        """A step that runs a nested agent to completion. Accepts a built
        ``AgentSpec`` or an ``AgentBuilder``, calling ``.build()`` on the
        latter so a subagent can be composed inline without a separate
        variable."""
        resolved = spec.build() if isinstance(spec, AgentBuilder) else spec
        self._steps.append(AgentStep(name=name, spec=resolved))
        return self

    def workflow_step(self, name: str, spec: WorkflowSpec | WorkflowBuilder) -> Self:
        """A step that runs a nested workflow. Recursion falls out of
        DESIGN.md §5's one engine: a workflow step may be a workflow."""
        resolved = spec.build() if isinstance(spec, WorkflowBuilder) else spec
        self._steps.append(WorkflowStepRef(name=name, spec=resolved))
        return self

    def build(self) -> WorkflowSpec:
        """Validate and produce the ``WorkflowSpec``.

        Raises:
            BuilderError: no step was added. ``WorkflowSpec.steps`` requires
                at least one; a workflow with none could never run.
        """
        if not self._steps:
            raise BuilderError(
                f"workflow {self._name!r} has no steps; call .tool_step(), "
                ".agent_step() or .workflow_step() before .build()"
            )
        return WorkflowSpec(
            name=self._name,
            description=self._description,
            steps=tuple(self._steps),
            tools=tuple(self._tools),
            mcp_servers=tuple(self._mcp_servers),
            limits=self._limits if self._limits is not None else Limits(),
            suspension=self._suspension if self._suspension is not None else SuspensionPolicy(),
        )
