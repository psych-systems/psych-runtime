"""Publish-time validation.

DESIGN.md §4: a Spec referencing an unregistered tool, an unreachable MCP
connection, a missing skill or a malformed schema is refused at publish with a
readable error naming what is missing. Validation happens at publish, never at
run.

## Why this is a pure function over sets of names

``psych_runtime.core`` imports nothing from ``psych_runtime.tools``, so this cannot reach into a
live registry. That is a feature rather than an inconvenience: the caller passes
the names it knows, so the same function validates against a real registry at
publish time and against a fixture in a unit test, with no divergence between
the two paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from psych_runtime.core.errors import SpecValidationError, ValidationIssue
from psych_runtime.core.spec import (
    AgentSpec,
    AgentStep,
    Skill,
    Spec,
    ToolStep,
    WorkflowSpec,
    WorkflowStepRef,
)

__all__ = ["ValidationContext", "collect_issues", "validate_spec"]

SKILL_LINK: Final = re.compile(r"\[\[skill:([a-zA-Z_][a-zA-Z0-9_.-]{0,127})\]\]")
"""DESIGN.md §16: skills reference each other with ``[[skill:name]]`` links,
forming a graph validated at publish. A dangling link fails publication."""

_JSON_SCHEMA_TYPES: Final = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


class ValidationContext:
    """What the world looks like at publish time.

    Attributes:
        registered_tools: names of code tools the consumer registered at boot.
            An HTTP tool carries its own definition and needs no registration;
            a code tool is a reference to a function that must already exist.
        known_models: model ids the ModelClient will accept. Empty means "do not
            check", which is the right default for a consumer pointing at a
            proxy that can reach models Psych has never heard of.
        reachable_mcp_servers: server names that answered at publish. Empty means
            "do not check", because reachability is a network fact and a
            consumer validating a Spec offline should still get every other
            error.
        sandbox_profiles: the logical sandbox names the deployment resolves
            (``psych_runtime.Runtime``'s profile registry). Empty means "do not
            check". Given, an agent whose ``code_execution.profile`` names
            nothing here is refused at publish rather than at its first
            ``run_code`` call.
    """

    __slots__ = ("known_models", "reachable_mcp_servers", "registered_tools", "sandbox_profiles")

    def __init__(
        self,
        registered_tools: Iterable[str] = (),
        known_models: Iterable[str] = (),
        reachable_mcp_servers: Iterable[str] = (),
        sandbox_profiles: Iterable[str] = (),
    ) -> None:
        self.registered_tools = frozenset(registered_tools)
        self.known_models = frozenset(known_models)
        self.reachable_mcp_servers = frozenset(reachable_mcp_servers)
        self.sandbox_profiles = frozenset(sandbox_profiles)


def validate_spec(spec: Spec, context: ValidationContext | None = None) -> None:
    """Refuse a Spec that cannot run, naming every problem at once.

    Args:
        spec: the Spec being published.
        context: what exists at publish time. Omitted means structural checks
            only, which is what an offline ``psych validate`` should do.

    Raises:
        SpecValidationError: carrying every issue found, not just the first.
            Fixing one typo per publish attempt is a bad way to spend an evening.
    """
    issues = collect_issues(spec, context)
    if issues:
        raise SpecValidationError(issues)


def collect_issues(spec: Spec, context: ValidationContext | None = None) -> list[ValidationIssue]:
    """Every problem with ``spec``, in a stable order. Empty means publishable."""
    ctx = context if context is not None else ValidationContext()
    issues: list[ValidationIssue] = []
    _visit(spec, ctx, path=spec.name, issues=issues, depth=0, seen_specs=set())
    return issues


_MAX_NESTING: Final = 16
"""Guards against a Spec that nests itself into a stack overflow at validation.
Genuine delegation trees are shallow; the runtime cap in ``Limits`` is 16 too."""


def _visit(
    spec: Spec,
    ctx: ValidationContext,
    *,
    path: str,
    issues: list[ValidationIssue],
    depth: int,
    seen_specs: set[int],
) -> None:
    if depth > _MAX_NESTING:
        issues.append(
            ValidationIssue(
                path,
                f"nesting exceeds {_MAX_NESTING} levels. Genuine delegation trees are "
                "shallow, so this is almost always a Spec that refers to itself.",
            )
        )
        return
    if id(spec) in seen_specs:
        issues.append(ValidationIssue(path, "this Spec contains itself, so it cannot terminate"))
        return
    seen_specs = seen_specs | {id(spec)}

    if isinstance(spec, AgentSpec):
        _visit_agent(spec, ctx, path=path, issues=issues, depth=depth, seen_specs=seen_specs)
    else:
        _visit_workflow(spec, ctx, path=path, issues=issues, depth=depth, seen_specs=seen_specs)


def _visit_agent(
    spec: AgentSpec,
    ctx: ValidationContext,
    *,
    path: str,
    issues: list[ValidationIssue],
    depth: int,
    seen_specs: set[int],
) -> None:
    _check_model(spec, ctx, path, issues)
    _check_tools(spec, ctx, path, issues)
    _check_mcp(spec, ctx, path, issues)
    _check_skill_graph(spec.skills, spec.instructions, path, issues)
    _check_delegation_depth(spec, path, issues)
    _check_code_execution(spec, ctx, path, issues)

    for subagent in spec.subagents:
        _visit(
            subagent.spec,
            ctx,
            path=f"{path}.subagents.{subagent.name}",
            issues=issues,
            depth=depth + 1,
            seen_specs=seen_specs,
        )


def _visit_workflow(
    spec: WorkflowSpec,
    ctx: ValidationContext,
    *,
    path: str,
    issues: list[ValidationIssue],
    depth: int,
    seen_specs: set[int],
) -> None:
    _check_tools(spec, ctx, path, issues)
    _check_mcp(spec, ctx, path, issues)

    workflow_tool_names = {tool.name for tool in spec.tools}
    for step in spec.steps:
        step_path = f"{path}.steps.{step.name}"
        if isinstance(step, ToolStep):
            if step.tool not in workflow_tool_names and step.tool not in ctx.registered_tools:
                issues.append(
                    ValidationIssue(
                        step_path,
                        f"step calls tool {step.tool!r}, which the workflow does not "
                        "grant and which is not registered",
                    )
                )
        elif isinstance(step, AgentStep | WorkflowStepRef):
            _visit(
                step.spec,
                ctx,
                path=step_path,
                issues=issues,
                depth=depth + 1,
                seen_specs=seen_specs,
            )


def _check_code_execution(
    spec: AgentSpec, ctx: ValidationContext, path: str, out: list[ValidationIssue]
) -> None:
    """An enabled ``code_execution`` must name a profile the deployment has.

    The subset check on ``bindings`` already ran when the Spec was built; this
    is the one check that needs the world outside the Spec, and it is skipped
    when the context does not describe that world.
    """
    config = spec.code_execution
    if config is None or not config.enabled or not ctx.sandbox_profiles:
        return
    if config.profile not in ctx.sandbox_profiles:
        known = ", ".join(sorted(ctx.sandbox_profiles))
        out.append(
            ValidationIssue(
                f"{path}.code_execution.profile",
                f"sandbox profile {config.profile!r} is not one this deployment resolves "
                f"(known: {known}). Name a configured profile, or register one on the "
                "Runtime.",
            )
        )


def _check_model(
    spec: AgentSpec, ctx: ValidationContext, path: str, out: list[ValidationIssue]
) -> None:
    if not ctx.known_models:
        return
    # The summariser is checked here with the agent's own model and its
    # fallbacks, because it is the same kind of mistake: a typo in a model id
    # belongs at publish, not on the turn a long conversation finally has to be
    # compacted (DESIGN.md §4).
    compaction_model = spec.compaction.model if spec.compaction is not None else None
    for label, model in [
        ("model", spec.model.model),
        *[(f"model.fallbacks[{i}]", name) for i, name in enumerate(spec.model.fallbacks)],
        *([("compaction.model", compaction_model)] if compaction_model is not None else []),
    ]:
        if model not in ctx.known_models:
            out.append(
                ValidationIssue(
                    f"{path}.{label}",
                    f"model {model!r} is not one the configured ModelClient accepts",
                )
            )


def _check_tools(
    spec: AgentSpec | WorkflowSpec, ctx: ValidationContext, path: str, out: list[ValidationIssue]
) -> None:
    for tool in spec.tools:
        tool_path = f"{path}.tools.{tool.name}"
        if tool.kind == "code":
            if ctx.registered_tools and tool.name not in ctx.registered_tools:
                out.append(
                    ValidationIssue(
                        tool_path,
                        f"code tool {tool.name!r} is not registered. Register the "
                        "function at boot, or use an http tool if it is remote.",
                    )
                )
        else:
            _check_json_schema(tool.input_schema, f"{tool_path}.input_schema", out)


def _check_mcp(
    spec: AgentSpec | WorkflowSpec, ctx: ValidationContext, path: str, out: list[ValidationIssue]
) -> None:
    if not ctx.reachable_mcp_servers:
        return
    for server in spec.mcp_servers:
        if server.name in ctx.reachable_mcp_servers:
            continue
        if server.optional:
            continue  # §10.7: an optional server may be absent by design.
        out.append(
            ValidationIssue(
                f"{path}.mcp_servers.{server.name}",
                f"MCP server {server.name!r} at {server.url} did not answer at publish. "
                "Mark it optional=True if the Run should proceed without its tools.",
            )
        )


def _check_skill_graph(
    skills: tuple[Skill, ...], instructions: str, path: str, out: list[ValidationIssue]
) -> None:
    """DESIGN.md §16: a dangling ``[[skill:name]]`` link fails publication."""
    available = {skill.name for skill in skills}

    for source_path, text in [
        (f"{path}.instructions", instructions),
        *[(f"{path}.skills.{skill.name}.body", skill.body) for skill in skills],
    ]:
        for match in SKILL_LINK.finditer(text):
            target = match.group(1)
            if target not in available:
                out.append(
                    ValidationIssue(
                        source_path,
                        f"links to [[skill:{target}]], which this Spec does not define. "
                        f"Available skills: {sorted(available) or 'none'}",
                    )
                )


def _check_delegation_depth(spec: AgentSpec, path: str, out: list[ValidationIssue]) -> None:
    """A Spec whose subagent tree is deeper than its own cap can never run it.

    Better to say so at publish than to have the runtime refuse a delegation
    halfway through a customer's request.
    """
    actual = _subagent_depth(spec)
    if actual > spec.limits.max_delegation_depth:
        out.append(
            ValidationIssue(
                f"{path}.limits.max_delegation_depth",
                f"the subagent tree is {actual} deep but max_delegation_depth is "
                f"{spec.limits.max_delegation_depth}, so the deepest subagents can never run",
            )
        )


def _subagent_depth(spec: AgentSpec, _guard: int = 0) -> int:
    if not spec.subagents or _guard > _MAX_NESTING:
        return 0
    return 1 + max(_subagent_depth(sub.spec, _guard + 1) for sub in spec.subagents)


def _check_json_schema(schema: dict[str, object], path: str, out: list[ValidationIssue]) -> None:
    """A shallow structural check, not a full JSON Schema validator.

    Psych deliberately does not vendor a schema validator: the model provider
    will reject a genuinely malformed schema, and the failure modes worth
    catching at publish are the ones a provider accepts and then behaves oddly
    about. Those are the ones checked here.
    """
    if not schema:
        return  # A tool taking no arguments is legitimate.

    declared = schema.get("type")
    if declared is None:
        out.append(ValidationIssue(path, "has no 'type'; providers require one at the root"))
    elif isinstance(declared, str) and declared not in _JSON_SCHEMA_TYPES:
        out.append(
            ValidationIssue(path, f"declares type {declared!r}, which is not a JSON Schema type")
        )

    if declared == "object":
        properties = schema.get("properties")
        if properties is not None and not isinstance(properties, dict):
            out.append(ValidationIssue(f"{path}.properties", "must be an object"))
        required = schema.get("required")
        if required is not None:
            if not isinstance(required, list):
                out.append(ValidationIssue(f"{path}.required", "must be an array of names"))
            elif isinstance(properties, dict):
                missing = sorted(set(required) - set(properties))
                if missing:
                    out.append(
                        ValidationIssue(
                            f"{path}.required",
                            f"requires {missing}, which 'properties' does not define",
                        )
                    )
