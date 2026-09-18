"""The typed builder for a Workflow Spec.

DESIGN.md §5: a Workflow is a Step that sequences other Steps deterministically,
by step memoisation rather than replay, so ``.tool_step()``, ``.agent_step()``
and ``.workflow_step()`` append to an ordered list that is never sorted: order
is exactly what "sequences deterministically" means, and the Spec model
(``psych_runtime.core.spec.WorkflowSpec``) preserves it for the same reason.
"""

from __future__ import annotations

from typing import Any, Self

from psych_runtime.builder.agent import AgentBuilder
from psych_runtime.builder.errors import BuilderError
from psych_runtime.builder.shared import SharedBuilder
from psych_runtime.core.questions import AskedQuestion
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    BranchCase,
    BranchStep,
    Condition,
    ConditionOp,
    ForEachStep,
    HumanStep,
    Limits,
    LiteralValue,
    LoopStep,
    MapStep,
    ParallelStep,
    RetryPolicy,
    SetStateStep,
    SleepStep,
    SuspensionPolicy,
    ToolStep,
    ValuePath,
    ValueRef,
    WaitStep,
    WorkflowSpec,
    WorkflowStep,
    WorkflowStepRef,
)
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["WorkflowBuilder", "all_of", "any_of", "lit", "ref", "when", "workflow"]


def ref(path: str) -> ValuePath:
    """A reference to a value the workflow already has: ``ref("steps.fetch.output.rows")``."""
    return ValuePath(path=path)


def lit(value: Any) -> LiteralValue:
    """A constant, for a mapping that mixes constants with references."""
    return LiteralValue(value=value)


def when(path: str, op: ConditionOp = "truthy", value: Any = None) -> Condition:
    """A leaf condition: ``when("steps.check.output.result", "eq", "ok")``."""
    return Condition(path=path, op=op, value=value)


def all_of(*conditions: Condition) -> Condition:
    return Condition(all_of=tuple(conditions))


def any_of(*conditions: Condition) -> Condition:
    return Condition(any_of=tuple(conditions))


def _mapping(values: dict[str, ValueRef | Any]) -> dict[str, ValueRef]:
    """Accept bare Python values as literals, so a mapping reads naturally."""
    return {
        name: value if isinstance(value, ValuePath | LiteralValue) else LiteralValue(value=value)
        for name, value in values.items()
    }


def workflow(name: str, *, registry: ToolRegistry | None = None) -> WorkflowBuilder:
    """Start building a Workflow Spec named ``name``."""
    return WorkflowBuilder(name, registry=registry)


class WorkflowBuilder(SharedBuilder):
    """Fluent construction of a ``WorkflowSpec``.

    ```python
    spec = (
        WorkflowBuilder("onboard-customer")
        .tool(create_account)
        .tool_step("create", "create_account", arguments_from={"email": ref("input.email")})
        .branch(
            "tier",
            ("paid", when("input.plan", "ne", "free"), ToolStep(name="bill", tool="charge")),
            otherwise=MapStep(name="free", output={"charged": lit(False)}),
        )
        .agent_step("welcome", AgentBuilder("greeter").model("gpt-4o-mini"))
        .output(account=ref("steps.create.output.result"))
        .build()
    )
    ```

    Composite steps take their children as Spec models (``ToolStep``,
    ``MapStep`` and the rest, all on ``psych_runtime``), so nesting reads the
    way the tree runs; ``ref``, ``lit``, ``when``, ``all_of`` and ``any_of``
    build the references and conditions.

    A step's ``name`` is what ``psych_runtime.core.validation`` and a report address
    it by, so it must be unique within one workflow; the Spec model enforces
    that at validation, not this builder.
    """

    def __init__(self, name: str, *, registry: ToolRegistry | None = None) -> None:
        super().__init__(registry=registry)
        self._name = name
        self._description = ""
        self._steps: list[WorkflowStep] = []
        self._input_schema: dict[str, Any] | None = None
        self._initial_state: dict[str, Any] = {}
        self._output: dict[str, ValueRef] | None = None
        self._retry: RetryPolicy | None = None

    def description(self, text: str) -> Self:
        self._description = text
        return self

    def input_schema(self, schema: dict[str, Any]) -> Self:
        """A JSON Schema the Run's input must satisfy before any step runs."""
        self._input_schema = schema
        return self

    def initial_state(self, **values: Any) -> Self:
        """What ``state.<name>`` reads as before any ``set_state`` step."""
        self._initial_state = dict(values)
        return self

    def output(self, **fields: ValueRef | Any) -> Self:
        """What the Run's output is, as a mapping. Bare values are literals."""
        self._output = _mapping(fields)
        return self

    def retry(
        self, max_attempts: int, *, backoff_seconds: float = 0.0, multiplier: float = 2.0
    ) -> Self:
        """The default retry policy for every step that sets none of its own."""
        self._retry = RetryPolicy(
            max_attempts=max_attempts, backoff_seconds=backoff_seconds, multiplier=multiplier
        )
        return self

    def step(self, step: WorkflowStep) -> Self:
        """Append any step, built directly from the Spec models. The methods
        below are conveniences over this one."""
        self._steps.append(step)
        return self

    def tool_step(
        self,
        name: str,
        tool: str,
        arguments: dict[str, object] | None = None,
        *,
        arguments_from: dict[str, ValueRef] | None = None,
        **options: Any,
    ) -> Self:
        """A step that calls one tool. ``arguments`` are written down;
        ``arguments_from`` fills argument names from values the workflow
        already has (``ref(...)``). ``tool`` must be granted by this
        workflow's ``.tool()``/``.tool_by_name()`` or registered in the
        process the Run executes in; publish-time validation checks the
        former and names the latter as a possibility when it cannot.
        ``options`` are the per-step fields: ``when``, ``retry``,
        ``timeout_seconds``, ``on_failure``, ``output_schema``, ``description``."""
        self._steps.append(
            ToolStep(
                name=name,
                tool=tool,
                arguments=arguments or {},
                arguments_from=arguments_from or {},
                **options,
            )
        )
        return self

    def agent_step(
        self,
        name: str,
        spec: AgentSpec | AgentBuilder,
        *,
        input: dict[str, ValueRef | Any] | None = None,  # noqa: A002 - the field's name
        **options: Any,
    ) -> Self:
        """A step that runs a nested agent to completion. Accepts a built
        ``AgentSpec`` or an ``AgentBuilder``, calling ``.build()`` on the
        latter so a subagent can be composed inline without a separate
        variable. ``input`` maps the agent's Run input; its ``message`` is
        the first user message the agent sees."""
        resolved = spec.build() if isinstance(spec, AgentBuilder) else spec
        self._steps.append(
            AgentStep(name=name, spec=resolved, input=_mapping(input or {}), **options)
        )
        return self

    def workflow_step(
        self,
        name: str,
        spec: WorkflowSpec | WorkflowBuilder,
        *,
        input: dict[str, ValueRef | Any] | None = None,  # noqa: A002 - the field's name
        **options: Any,
    ) -> Self:
        """A step that runs a nested workflow. Recursion falls out of
        DESIGN.md §5's one engine: a workflow step may be a workflow."""
        resolved = spec.build() if isinstance(spec, WorkflowBuilder) else spec
        self._steps.append(
            WorkflowStepRef(name=name, spec=resolved, input=_mapping(input or {}), **options)
        )
        return self

    def parallel(self, name: str, *branches: WorkflowStep, **options: Any) -> Self:
        """Run ``branches`` at once; the output is each branch's output by name."""
        self._steps.append(ParallelStep(name=name, branches=branches, **options))
        return self

    def branch(
        self,
        name: str,
        *cases: tuple[str, Condition, WorkflowStep],
        otherwise: WorkflowStep | None = None,
        **options: Any,
    ) -> Self:
        """Choose a step by condition: ``(case_name, when(...), step)`` per arm."""
        self._steps.append(
            BranchStep(
                name=name,
                cases=tuple(
                    BranchCase(name=case_name, when=condition, step=step)
                    for case_name, condition, step in cases
                ),
                otherwise=otherwise,
                **options,
            )
        )
        return self

    def foreach(
        self, name: str, items: ValuePath | str, body: WorkflowStep, **options: Any
    ) -> Self:
        """Run ``body`` once per element of the list at ``items``."""
        resolved = items if isinstance(items, ValuePath) else ValuePath(path=items)
        self._steps.append(ForEachStep(name=name, items=resolved, body=body, **options))
        return self

    def loop(
        self,
        name: str,
        body: WorkflowStep,
        *,
        until: Condition | None = None,
        while_: Condition | None = None,
        **options: Any,
    ) -> Self:
        """Run ``body`` at least once, then until ``until`` holds or while
        ``while_`` holds."""
        self._steps.append(LoopStep(name=name, body=body, until=until, while_=while_, **options))
        return self

    def map(self, name: str, **fields: ValueRef | Any) -> Self:
        """Shape data between steps; bare values are literals."""
        self._steps.append(MapStep(name=name, output=_mapping(fields)))
        return self

    def set_state(self, name: str, **values: ValueRef | Any) -> Self:
        """Write values into the workflow state."""
        self._steps.append(SetStateStep(name=name, values=_mapping(values)))
        return self

    def sleep(
        self,
        name: str,
        *,
        seconds: float | None = None,
        until: ValuePath | str | None = None,
        **options: Any,
    ) -> Self:
        """Wait without holding a Worker."""
        resolved = ValuePath(path=until) if isinstance(until, str) else until
        self._steps.append(SleepStep(name=name, seconds=seconds, until=resolved, **options))
        return self

    def wait(
        self,
        name: str,
        event: str,
        *,
        payload_schema: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        **options: Any,
    ) -> Self:
        """Suspend until ``psych_runtime.resume(payload=...)`` delivers ``event``."""
        self._steps.append(
            WaitStep(
                name=name,
                event=event,
                payload_schema=payload_schema or {},
                timeout_seconds=timeout_seconds,
                **options,
            )
        )
        return self

    def human(
        self,
        name: str,
        prompt: str,
        *,
        questions: tuple[AskedQuestion, ...] = (),
        expires_seconds: float | None = None,
        **options: Any,
    ) -> Self:
        """Stop and ask a person; the answer is the step's output."""
        self._steps.append(
            HumanStep(
                name=name,
                prompt=prompt,
                questions=questions,
                expires_seconds=expires_seconds,
                **options,
            )
        )
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
            input_schema=self._input_schema,
            initial_state=self._initial_state,
            output=self._output,
            retry=self._retry if self._retry is not None else RetryPolicy(),
        )
