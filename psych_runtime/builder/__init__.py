"""The typed Python builder that produces Specs.

DESIGN.md §4: four authoring forms (chat, builder, file, import) converge on
the same ``Spec`` model, the same validator and the same publication path, and
Psych privileges none of them. This package is the second form: a typed,
fluent API a developer composes in Python. ``AgentBuilder.build()`` and
``WorkflowBuilder.build()`` return exactly the ``AgentSpec`` or ``WorkflowSpec``
an equivalent dict would validate into, so the same agent built here and built
from a dict hashes the same (``psych_runtime.core.version.compute_hash``) and runs
identically (DESIGN.md §23).

A builder never lets a callable reach a Spec. ``.tool(fn)`` registers ``fn`` in
a ``ToolRegistry`` the builder owns (or was handed) and puts only the
registered name in the Spec, which is the invariant DESIGN.md §4 says every
reviewer checks.
"""

from __future__ import annotations

from psych_runtime.builder.agent import AgentBuilder, agent
from psych_runtime.builder.errors import BuilderError
from psych_runtime.builder.workflow import WorkflowBuilder, workflow

__all__ = ["AgentBuilder", "BuilderError", "WorkflowBuilder", "agent", "workflow"]
