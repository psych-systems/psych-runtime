"""The span schema, declared as data.

DESIGN.md §13.5: span names and attributes are declared in a schema and
checked by conformance tests, so spans cannot drift from their contract as
the code changes. The declaration is data rather than types, so nothing about
it is checked when the module is imported. What checks it is
``psych_runtime.telemetry.conformance.check_schema_conformance``, reading this module
at run time against the spans an implementation actually emitted. A schema with
no reader would be exactly as decorative as a comment.

## What a span definition fixes

- ``parent``: which span names may legally contain this one. ``root_or_external``
  means the span has no parent captured under this schema at all -- either it
  is a genuine root, or its parent is an OpenTelemetry span from outside
  Psych's own tree (a consumer's request span, say) that this schema has no
  opinion about. ``any`` means no constraint. A named ``spans`` set means
  exactly those span names and nothing else, including no parent.
- ``start_attributes`` / ``end_attributes``: each maps an attribute name to
  its ``AttributeDefinition``. Both are checked against the span's *final*
  merged attribute set at settlement, the ``Telemetry`` port having no
  separate "start" and "end" write calls, so
  a ``start_attributes`` entry with ``required=True`` must be present by the
  time the span closes, and an attribute name not declared in either map is
  an undeclared attribute. ``end_attributes`` entries are never required,
  matching the reality that you may not know a call's outcome until it
  settles.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "PSYCH_SCHEMA",
    "AnyParent",
    "AttributeDefinition",
    "AttributeType",
    "NamedParents",
    "ParentConstraint",
    "RootOrExternalParent",
    "SpanDefinition",
    "TelemetrySchema",
]


class AttributeType(StrEnum):
    """The value shapes a declared attribute may hold, matching
    ``psych_runtime.telemetry.port.SpanAttributeValue``."""

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING_ARRAY = "string_array"


class AttributeDefinition(BaseModel):
    """One attribute a span may or must carry.

    Attributes:
        type: the value shape.
        required: only meaningful in a span's ``start_attributes`` map; an
            end attribute is always optional.
        values: a closed set of legal values, for a low-cardinality string
            attribute with a known vocabulary (an enum, a finish reason). Only
            meaningful when ``type`` is ``STRING``.
        cardinality: documentation for whoever wires up a metrics backend --
            "high" cardinality attributes (an id, a call id) make bad metric
            dimensions even though they are perfectly good span attributes.
        description: what the attribute means.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: AttributeType
    required: bool = False
    values: tuple[str, ...] | None = None
    cardinality: Literal["low", "high"] = "low"
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def _values_only_constrain_strings(self) -> AttributeDefinition:
        if self.values is not None and self.type is not AttributeType.STRING:
            raise ValueError(
                f"a closed set of `values` only makes sense for a STRING attribute, "
                f"got type={self.type.value}"
            )
        return self


class AnyParent(BaseModel):
    """No constraint on this span's parent."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["any"] = "any"


class RootOrExternalParent(BaseModel):
    """This span has no parent captured under this schema: it is either a
    genuine root or nested under a span this schema does not declare."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["root_or_external"] = "root_or_external"


class NamedParents(BaseModel):
    """This span's parent must be one of exactly these declared span names."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["spans"] = "spans"
    spans: tuple[str, ...] = Field(min_length=1)


ParentConstraint = Annotated[
    AnyParent | RootOrExternalParent | NamedParents,
    Field(discriminator="kind"),
]


class SpanDefinition(BaseModel):
    """One span Psych may emit.

    Attributes:
        description: what the span covers, in a sentence.
        parent: the parentage constraint (see module docstring).
        start_attributes: attributes known when the span opens. ``required``
            ones must be present in the final attribute set by settlement.
        end_attributes: attributes only known once the span's work is done.
            Always optional.
        status_error_when: a human-readable description of the condition that
            produces an automatic ``ERROR`` status. Documentation for readers,
            not executable logic -- the actual automatic-status mechanism is
            implemented once, uniformly, by every ``Telemetry`` adapter
            (``psych_runtime.telemetry.port``), never per span.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parent: ParentConstraint
    start_attributes: dict[str, AttributeDefinition] = Field(default_factory=dict)
    end_attributes: dict[str, AttributeDefinition] = Field(default_factory=dict)
    status_error_when: str = Field(min_length=1)

    @model_validator(mode="after")
    def _start_and_end_attributes_do_not_collide(self) -> SpanDefinition:
        overlap = self.start_attributes.keys() & self.end_attributes.keys()
        if overlap:
            raise ValueError(
                f"span {self.name!r}: {sorted(overlap)} declared in both "
                "start_attributes and end_attributes; an attribute belongs in exactly one"
            )
        return self


class TelemetrySchema(BaseModel):
    """The whole declared shape of what Psych emits.

    Attributes:
        version: bumped when a span's shape changes in a way a consumer's
            dashboard or alert might depend on.
        spans: every span, keyed by its own name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    spans: dict[str, SpanDefinition]

    @model_validator(mode="after")
    def _keys_match_names(self) -> TelemetrySchema:
        for key, definition in self.spans.items():
            if key != definition.name:
                raise ValueError(
                    f"span keyed {key!r} declares name {definition.name!r}; "
                    "the dict key and the span's own name must match"
                )
        return self

    @model_validator(mode="after")
    def _named_parents_reference_declared_spans(self) -> TelemetrySchema:
        for definition in self.spans.values():
            if isinstance(definition.parent, NamedParents):
                unknown = [s for s in definition.parent.spans if s not in self.spans]
                if unknown:
                    raise ValueError(
                        f"span {definition.name!r} names {unknown} as legal parents, "
                        "but no span with that name is declared in this schema"
                    )
        return self


# ---------------------------------------------------------------------------
# Attribute name constants
# ---------------------------------------------------------------------------
# `gen_ai.*` are OpenTelemetry semantic conventions, used wherever one exists.
# `psych.*` covers everything DESIGN.md defines that the conventions do not.

_GEN_AI_SYSTEM: Final = "gen_ai.system"
_GEN_AI_OPERATION_NAME: Final = "gen_ai.operation.name"
_GEN_AI_REQUEST_MODEL: Final = "gen_ai.request.model"
_GEN_AI_RESPONSE_MODEL: Final = "gen_ai.response.model"
_GEN_AI_RESPONSE_FINISH_REASONS: Final = "gen_ai.response.finish_reasons"
_GEN_AI_USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
_GEN_AI_USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"

# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------

_RUN: Final = SpanDefinition(
    name="psych.run",
    description="One admitted Run, from RunAdmitted to RunSettled.",
    parent=RootOrExternalParent(),
    start_attributes={
        "psych.run.id": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="The Run's id.",
        ),
        "psych.scope.tenant": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="The tenant this Run belongs to (DESIGN.md §14).",
        ),
        "psych.version.hash": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="The content hash of the Version this Run pinned at admission.",
        ),
    },
    end_attributes={
        "psych.run.terminal_state": AttributeDefinition(
            type=AttributeType.STRING,
            values=("completed", "failed", "aborted", "abandoned", "force_settled"),
            description="How the Run ended (psych_runtime.core.records.TerminalState).",
        ),
    },
    status_error_when="the Run settles as failed or force_settled",
)

_ATTEMPT: Final = SpanDefinition(
    name="psych.attempt",
    description=(
        "One execution pass by one Worker holding the lease. A crash and reclaim "
        "produces a second sibling attempt span, not a reopened one."
    ),
    parent=NamedParents(spans=("psych.run",)),
    start_attributes={
        "psych.worker.id": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="The Worker process that claimed the lease.",
        ),
        "psych.attempt.number": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="Increments by one per claim, including reclaims.",
        ),
        "psych.attempt.reclaimed_expired_lease": AttributeDefinition(
            type=AttributeType.BOOL,
            required=True,
            description=(
                "True when this Attempt took over an expired lease: the "
                "crash-recovery path rather than a fresh dispatch."
            ),
        ),
    },
    status_error_when="the Attempt raises or is force-settled before it completes",
)

_TURN: Final = SpanDefinition(
    name="psych.turn",
    description="One assistant response plus the tool batch it produced.",
    parent=NamedParents(spans=("psych.attempt", "psych.step")),
    start_attributes={
        "psych.turn.number": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="1-indexed position of this turn within its Run.",
        ),
        "psych.step.id": AttributeDefinition(
            type=AttributeType.STRING,
            cardinality="high",
            description="Set when this turn belongs to a workflow step rather than a bare agent.",
        ),
    },
    status_error_when="the turn's model call fails and exhausts its retry budget",
)

_MODEL_CALL: Final = SpanDefinition(
    name="psych.model_call",
    description="One call to a ModelClient, wrapping a single provider request.",
    parent=NamedParents(spans=("psych.turn",)),
    start_attributes={
        _GEN_AI_SYSTEM: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="The model provider identifier.",
        ),
        _GEN_AI_OPERATION_NAME: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            values=("chat",),
            description="The gen_ai operation this call performs.",
        ),
        _GEN_AI_REQUEST_MODEL: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description=(
                "The model id requested (DESIGN.md §19: no router, so this is the Spec's model)."
            ),
        ),
        "psych.turn.number": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="Which turn this call belongs to.",
        ),
    },
    end_attributes={
        _GEN_AI_RESPONSE_MODEL: AttributeDefinition(
            type=AttributeType.STRING,
            description="The model id the provider actually served the request with.",
        ),
        _GEN_AI_RESPONSE_FINISH_REASONS: AttributeDefinition(
            type=AttributeType.STRING_ARRAY,
            description="The provider's finish reason, as a single-element array per convention.",
        ),
        _GEN_AI_USAGE_INPUT_TOKENS: AttributeDefinition(
            type=AttributeType.INT,
            description="Uncached input tokens billed at the input rate.",
        ),
        _GEN_AI_USAGE_OUTPUT_TOKENS: AttributeDefinition(
            type=AttributeType.INT,
            description="Generated output tokens.",
        ),
        "psych.usage.cache_read_tokens": AttributeDefinition(
            type=AttributeType.INT,
            description=(
                "Tokens read from the prompt cache (psych_runtime.core.usage.Usage.cache_read)."
            ),
        ),
        "psych.usage.cache_write_tokens": AttributeDefinition(
            type=AttributeType.INT,
            description="Tokens written to the prompt cache.",
        ),
        "psych.cost.usd": AttributeDefinition(
            type=AttributeType.FLOAT,
            description=(
                "Computed cost in US dollars. Absent, never zero, when the model has "
                "no known price (DESIGN.md §13.2)."
            ),
        ),
        "psych.model_call.time_to_first_token_seconds": AttributeDefinition(
            type=AttributeType.FLOAT,
            description="Latency from request to the first streamed token (DESIGN.md §13.3).",
        ),
    },
    status_error_when="the call raises, times out, or returns without a StreamDone event",
)

_TOOL_CALL: Final = SpanDefinition(
    name="psych.tool_call",
    description="One tool execution: code, HTTP, or MCP (DESIGN.md §10).",
    parent=NamedParents(spans=("psych.turn",)),
    start_attributes={
        "psych.tool.name": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="The tool's registered name, as resolved for this turn.",
        ),
        "psych.tool.call_id": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="The id the model (or Psych, if the model omitted one) assigned this call.",
        ),
        "psych.tool.interruptible": AttributeDefinition(
            type=AttributeType.BOOL,
            required=True,
            description=(
                "Whether an abort cancels this call rather than letting it finish (DESIGN.md §9)."
            ),
        ),
    },
    end_attributes={
        "psych.tool.outcome": AttributeDefinition(
            type=AttributeType.STRING,
            values=("ok", "error", "aborted", "unknown"),
            description="How the call ended (psych_runtime.core.records.ToolOutcome).",
        ),
    },
    status_error_when="the outcome is 'error'",
)

_STEP: Final = SpanDefinition(
    name="psych.step",
    description="One checkpointed unit of work in a Workflow (DESIGN.md §5).",
    parent=NamedParents(spans=("psych.attempt", "psych.step")),
    start_attributes={
        "psych.step.id": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            cardinality="high",
            description="Derived, stable across a replay, so memoisation can find it again.",
        ),
        "psych.step.name": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="The step's name as authored in the Workflow.",
        ),
        "psych.step.kind": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            values=("agent", "tool", "workflow", "subagent"),
            description="What kind of work this step performs.",
        ),
        "psych.step.attempt_number": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="Retries of this one step, incrementing by exactly one.",
        ),
    },
    end_attributes={
        "psych.step.outcome": AttributeDefinition(
            type=AttributeType.STRING,
            values=("succeeded", "failed", "aborted"),
            description="How the step ended.",
        ),
    },
    status_error_when="the step's outcome is 'failed'",
)

_SUBAGENT_DELEGATION: Final = SpanDefinition(
    name="psych.subagent_delegation",
    description="One subagent step delegating to a nested Run (DESIGN.md §17).",
    parent=NamedParents(spans=("psych.step",)),
    start_attributes={
        "psych.subagent.name": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="The subagent being delegated to.",
        ),
        "psych.delegation.depth": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="Monotone recursion depth; never lower than the parent's (DESIGN.md §17).",
        ),
    },
    end_attributes={
        "psych.subagent.child_run_id": AttributeDefinition(
            type=AttributeType.STRING,
            cardinality="high",
            description="The nested Run's id, once one was created.",
        ),
    },
    status_error_when="the child Run settles as failed",
)

_COMPACTION: Final = SpanDefinition(
    name="psych.compaction",
    description=(
        "The model call that summarises a conversation so it fits the context "
        "window. A sibling of psych_runtime.turn rather than a child: it is not a turn, "
        "it produces no assistant message, and nothing it does reaches the user."
    ),
    parent=NamedParents(spans=("psych.attempt", "psych.step")),
    start_attributes={
        _GEN_AI_SYSTEM: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="The model provider serving the summarising call.",
        ),
        _GEN_AI_OPERATION_NAME: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description="Always 'chat'. Summarising is an ordinary completion.",
        ),
        _GEN_AI_REQUEST_MODEL: AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description=(
                "The model asked to summarise, which is CompactionPolicy.model when "
                "the Spec names one and the agent's own model otherwise. Recorded "
                "because a bill that does not separate the two cannot be reconciled."
            ),
        ),
        "psych.compaction.reason": AttributeDefinition(
            type=AttributeType.STRING,
            required=True,
            description=(
                "Why it fired: 'threshold' when the last prompt crossed "
                "CompactionPolicy.trigger_tokens, 'overflow' when the provider "
                "rejected a request as too long."
            ),
        ),
        "psych.compaction.replaced_to_seq": AttributeDefinition(
            type=AttributeType.INT,
            required=True,
            description="The last record sequence the summary stands in for.",
        ),
    },
    end_attributes={
        _GEN_AI_USAGE_INPUT_TOKENS: AttributeDefinition(
            type=AttributeType.INT,
            description="Uncached input tokens billed at the input rate.",
        ),
        _GEN_AI_USAGE_OUTPUT_TOKENS: AttributeDefinition(
            type=AttributeType.INT,
            description="Tokens the summary itself cost.",
        ),
    },
    status_error_when="the summarising call fails or returns no text",
)
"""Declared here and not only emitted, which is the whole reason this schema
exists.

A span the runtime opens and the schema does not name is a span no consumer's
pipeline is expecting and the conformance suite does not check, so it drifts
without anything failing. Compaction is a real model call that spends real
tokens, and a trace that shows the turns but not the calls between them cannot
be reconciled against a provider's bill."""

PSYCH_SCHEMA: Final[TelemetrySchema] = TelemetrySchema(
    version=1,
    spans={
        definition.name: definition
        for definition in (
            _RUN,
            _ATTEMPT,
            _TURN,
            _MODEL_CALL,
            _TOOL_CALL,
            _STEP,
            _SUBAGENT_DELEGATION,
            _COMPACTION,
        )
    },
)
"""The span schema Psych ships. DESIGN.md §13.5's minimum -- the run, an
attempt, a turn, a model call, a tool call, a step and a subagent delegation --
plus the compaction call, which is a model call the minimum did not anticipate
because compaction had no writer when it was written."""
