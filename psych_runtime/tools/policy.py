"""Authorization is a port, and approvals are selectors over annotations.

DESIGN.md §14 and §10.9.

Psych does not know what a tenant is, whether a principal may act, or how anyone
authenticated. It threads a Scope through every call, stamps it on every Record,
and asks the consumer. That is how Psych gets tenant-correct data and per-tenant
metering without owning orgs, teams or roles.

## Unannotated means write

DESIGN.md §10.9 says an unannotated tool is treated as ``write``. The tempting
alternative is to read the selectors literally: no annotations, so the tool
matches none of ``@read-only``, ``@write`` or ``@destructive``, so no approval
is needed. That hands an exemption to exactly the servers least likely to have
earned it. An MCP server that forgets to annotate a destructive tool should not
buy that tool a pass. Defaulting to ``write`` means a missing annotation costs
an extra approval prompt rather than an unreviewed destructive call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import Version

__all__ = [
    "AllowAll",
    "Decision",
    "Policy",
    "ToolClass",
    "approval_required",
    "classify",
]


class ToolClass(StrEnum):
    """How dangerous a tool is, from its MCP annotations."""

    READ_ONLY = "read-only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


_DEFAULT_CLASS: Final = ToolClass.WRITE
"""What an unannotated tool is treated as. The module docstring says why this is
``WRITE`` rather than an exemption."""


def classify(definition: ToolDefinition) -> ToolClass:
    """The class of one tool.

    ``destructive`` wins over ``write`` wins over ``read-only`` when a tool
    carries more than one annotation. A tool claiming to be both read-only and
    destructive is either mis-annotated or lying, and in both cases the strict
    reading is the right one.
    """
    annotations = {a.lower() for a in definition.annotations}
    if ToolClass.DESTRUCTIVE in annotations:
        return ToolClass.DESTRUCTIVE
    if ToolClass.WRITE in annotations:
        return ToolClass.WRITE
    if ToolClass.READ_ONLY in annotations:
        return ToolClass.READ_ONLY
    return _DEFAULT_CLASS


def approval_required(
    definition: ToolDefinition,
    *,
    selectors: Sequence[str] = ("@write", "@destructive"),
    always: Sequence[str] = (),
    never: Sequence[str] = (),
) -> bool:
    """Whether calling ``definition`` needs a human decision first.

    Args:
        definition: the tool as offered to the model.
        selectors: annotation selectors requiring approval. ``@write`` and
            ``@destructive`` by default, so a read is free and anything that
            changes something is reviewed.
        always: tool names that always need approval, whatever their annotations.
        never: tool names that never do. Checked last, so an explicit exemption
            beats a selector. Use it sparingly and knowingly.

    Returns:
        True when the call must suspend for a decision.
    """
    if definition.name in never:
        return False
    if definition.name in always:
        return True
    tool_class = classify(definition)
    return any(selector.lstrip("@").lower() == tool_class.value for selector in selectors)


@dataclass(frozen=True, slots=True)
class Decision:
    """The consumer's answer.

    Attributes:
        allowed: whether the call may proceed.
        reason: why not, when it may not. Reaches the model as the tool's result
            so it can explain to the user or try something else, which is why it
            should read as an explanation rather than an error code.
        requires_approval: the consumer wants a human decision. The Run suspends
            rather than failing, and resumes on the decision.
    """

    allowed: bool
    reason: str = ""
    requires_approval: bool = False

    @classmethod
    def allow(cls) -> Decision:
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> Decision:
        return cls(allowed=False, reason=reason)

    @classmethod
    def ask(cls, reason: str = "") -> Decision:
        return cls(allowed=False, reason=reason, requires_approval=True)


@runtime_checkable
class Policy(Protocol):
    """Authorization, implemented by the consumer against their own identity
    system.

    Psych calls this; it never decides. An implementation that raises is treated
    as a denial rather than as a crash, because an authorization system being
    down should stop work rather than let it through, and should not take the
    Run's whole log with it.
    """

    async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
        """May this Scope call this tool with these arguments right now?

        Arguments are included because "may refund" and "may refund £4000" are
        different questions and only the consumer knows which one they are
        asking.
        """
        ...

    async def allow_run(self, scope: Scope, version: Version) -> Decision:
        """May this Scope run this Version at all?"""
        ...


class AllowAll:
    """The default Policy: yes to everything.

    Psych ships this because a consumer evaluating the library should not have to
    write an authorization system first, and because Psych genuinely does not
    have an opinion about who may do what. It is not a security control and its
    name says so.
    """

    async def allow_tool(self, scope: Scope, tool: str, args: dict[str, Any]) -> Decision:
        _ = scope, tool, args
        return Decision.allow()

    async def allow_run(self, scope: Scope, version: Version) -> Decision:
        _ = scope, version
        return Decision.allow()
