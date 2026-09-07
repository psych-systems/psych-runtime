"""The span schema is well-formed, and the checker catches drift.

DESIGN.md §13.5: spans cannot drift from their contract as the code changes.
The schema is data, so nothing enforces it at import time.
``check_schema_conformance`` is what enforces it at run time, and this file is
what proves the checker actually catches the shapes of drift it exists to
catch.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import psych_runtime
from psych_runtime.telemetry.conformance import (
    CapturedSpan,
    RecordingTelemetry,
    SpanEvent,
    _emit_psych_schema_span_tree,
    check_schema_conformance,
)
from psych_runtime.telemetry.port import SpanAttributeValue, SpanStatus
from psych_runtime.telemetry.schema import (
    PSYCH_SCHEMA,
    AttributeDefinition,
    AttributeType,
    NamedParents,
    RootOrExternalParent,
    SpanDefinition,
    TelemetrySchema,
)

pytestmark = pytest.mark.unit


def _span(
    name: str,
    parent: str | None,
    attributes: dict[str, SpanAttributeValue] | None = None,
    *,
    end_sequence: int = 0,
) -> CapturedSpan:
    return CapturedSpan(
        name=name,
        parent=parent,
        attributes=dict(attributes or {}),
        status=SpanStatus.OK,
        status_description="",
        events=(),
        end_sequence=end_sequence,
        had_exception=False,
    )


# ---------------------------------------------------------------------------
# The schema itself is well-formed
# ---------------------------------------------------------------------------


class TestPsychSchemaIsWellFormed:
    def test_declares_the_seven_spans_design_md_names_at_minimum(self) -> None:
        required = {
            "psych.run",
            "psych.attempt",
            "psych.turn",
            "psych.model_call",
            "psych.tool_call",
            "psych.step",
            "psych.subagent_delegation",
        }
        assert required <= PSYCH_SCHEMA.spans.keys()

    def test_the_compaction_span_is_declared(self) -> None:
        """A model call the minimum in DESIGN.md §13.5 did not anticipate.

        Compaction had no writer when that list was written, and when it got
        one the runtime opened a `psych_runtime.compaction` span the schema had never
        heard of. An undeclared span is a span no consumer's pipeline expects
        and nothing checks, so it drifts silently -- and this one spends real
        tokens, so a trace missing it cannot be reconciled against a bill.
        """
        assert "psych.compaction" in PSYCH_SCHEMA.spans

    def test_every_span_the_runtime_opens_is_declared(self) -> None:
        """The direction that catches the real mistake.

        The case above checks the schema against the conformance walk, and both
        of those are written by whoever is thinking about telemetry. The bug
        that actually happened was in code that was not: `psych_runtime.compaction` was
        opened by the agent loop for weeks with the schema never mentioning it,
        so no consumer's pipeline expected it and nothing checked it.

        Reading the source is crude and it is the only way to catch a span
        nobody remembered to declare, because a span that is never opened in a
        test is a span no test can see.
        """
        source_root = Path(psych_runtime.__file__).parent
        opened: dict[str, str] = {}
        for path in source_root.rglob("*.py"):
            # `psych_runtime.telemetry` is the schema and its own test kit. The
            # conformance suite opens spans called "parent" and "child" on
            # purpose, to check that the checker catches a name no schema
            # declares, so scanning it would fail on the thing it is testing.
            if path.is_relative_to(source_root / "telemetry"):
                continue
            for match in re.finditer(r'start_span\(\s*"([^"]+)"', path.read_text()):
                opened[match.group(1)] = str(path.relative_to(source_root))

        assert opened, "the pattern stopped matching; this test would pass vacuously"
        undeclared = {
            name: where for name, where in opened.items() if name not in PSYCH_SCHEMA.spans
        }
        assert undeclared == {}, f"opened but not declared in PSYCH_SCHEMA: {undeclared}"

    def test_every_declared_span_is_emitted_by_the_conformance_walk(self) -> None:
        """The mechanical version of the case above, so the next one is caught.

        `emit_reference_tree` is what proves an implementation handles Psych's
        spans. A span declared and never emitted there is a span the
        conformance suite silently does not check, which is how a gap becomes
        permanent.
        """
        recorder = RecordingTelemetry()
        asyncio.run(_emit_psych_schema_span_tree(recorder))
        assert {span.name for span in recorder.spans} == set(PSYCH_SCHEMA.spans)

    def test_every_named_parent_is_itself_a_declared_span(self) -> None:
        for definition in PSYCH_SCHEMA.spans.values():
            if isinstance(definition.parent, NamedParents):
                for parent_name in definition.parent.spans:
                    assert parent_name in PSYCH_SCHEMA.spans

    def test_no_attribute_is_declared_in_both_start_and_end(self) -> None:
        for definition in PSYCH_SCHEMA.spans.values():
            overlap = definition.start_attributes.keys() & definition.end_attributes.keys()
            assert not overlap

    def test_a_dict_key_must_match_the_spans_own_name(self) -> None:
        with pytest.raises(ValueError, match="declares name"):
            TelemetrySchema(
                version=1,
                spans={
                    "wrong.key": SpanDefinition(
                        name="psych.actual",
                        description="d",
                        parent=RootOrExternalParent(),
                        status_error_when="never",
                    )
                },
            )

    def test_a_named_parent_referencing_an_undeclared_span_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no span with that name is declared"):
            TelemetrySchema(
                version=1,
                spans={
                    "psych.child": SpanDefinition(
                        name="psych.child",
                        description="d",
                        parent=NamedParents(spans=("psych.nonexistent",)),
                        status_error_when="never",
                    )
                },
            )

    def test_closed_values_only_make_sense_on_a_string_attribute(self) -> None:
        with pytest.raises(ValueError, match="STRING attribute"):
            AttributeDefinition(type=AttributeType.INT, values=("a", "b"), description="d")


# ---------------------------------------------------------------------------
# The checker: a schema with no runtime reader is decorative
# ---------------------------------------------------------------------------

_SIMPLE_SCHEMA = TelemetrySchema(
    version=1,
    spans={
        "root": SpanDefinition(
            name="root",
            description="a root span",
            parent=RootOrExternalParent(),
            start_attributes={
                "id": AttributeDefinition(
                    type=AttributeType.STRING, required=True, description="d"
                ),
            },
            end_attributes={
                "outcome": AttributeDefinition(
                    type=AttributeType.STRING, values=("ok", "bad"), description="d"
                ),
            },
            status_error_when="outcome is bad",
        ),
        "child": SpanDefinition(
            name="child",
            description="a child span",
            parent=NamedParents(spans=("root",)),
            start_attributes={
                "count": AttributeDefinition(
                    type=AttributeType.INT, required=True, description="d"
                ),
            },
            status_error_when="never",
        ),
    },
)


class TestCheckSchemaConformance:
    def test_a_correctly_shaped_span_produces_no_violations(self) -> None:
        spans = [_span("root", None, {"id": "r1", "outcome": "ok"})]
        assert check_schema_conformance(_SIMPLE_SCHEMA, spans) == ()

    def test_an_unknown_span_name_is_a_violation(self) -> None:
        spans = [_span("not-in-schema", None)]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert len(violations) == 1
        assert violations[0].kind == "unknown_span"

    def test_a_missing_required_start_attribute_is_caught(self) -> None:
        spans = [_span("root", None, {})]  # `id` is required and absent
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        kinds = [v.kind for v in violations]
        assert "missing_required" in kinds
        assert any("id" in v.detail for v in violations if v.kind == "missing_required")

    def test_an_undeclared_attribute_is_caught(self) -> None:
        spans = [_span("root", None, {"id": "r1", "extra.field": "surprise"})]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert len(violations) == 1
        assert violations[0].kind == "undeclared_attribute"
        assert "extra.field" in violations[0].detail

    def test_a_wrong_parent_is_caught(self) -> None:
        # `child`'s only legal parent is `root`; attaching it to itself is illegal.
        spans = [
            _span("root", None, {"id": "r1"}),
            _span("child", "some-other-span", {"count": 1}),
        ]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert len(violations) == 1
        assert violations[0].kind == "illegal_parent"
        assert violations[0].span_name == "child"

    def test_root_or_external_rejects_an_in_schema_parent(self) -> None:
        spans = [
            _span("root", None, {"id": "r1"}),
            _span(
                "root", "root", {"id": "r2"}
            ),  # root_or_external, but parented under a declared span
        ]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert any(v.kind == "illegal_parent" for v in violations)

    def test_root_or_external_accepts_no_parent_and_an_out_of_schema_parent(self) -> None:
        spans = [
            _span("root", None, {"id": "r1"}),
            _span("root", "some-external-otel-span", {"id": "r2"}),
        ]
        assert check_schema_conformance(_SIMPLE_SCHEMA, spans) == ()

    def test_a_wrong_typed_value_is_caught(self) -> None:
        spans = [_span("child", "root", {"count": "not-an-int"})]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert any(v.kind == "wrong_type" for v in violations)

    def test_a_value_outside_a_closed_enum_is_caught(self) -> None:
        spans = [_span("root", None, {"id": "r1", "outcome": "not-a-legal-value"})]
        violations = check_schema_conformance(_SIMPLE_SCHEMA, spans)
        assert any(v.kind == "invalid_enum_value" for v in violations)

    def test_events_do_not_affect_conformance(self) -> None:
        spans = [
            CapturedSpan(
                name="root",
                parent=None,
                attributes={"id": "r1"},
                status=SpanStatus.OK,
                status_description="",
                events=(SpanEvent(name="anything", attributes={"whatever": "goes"}),),
                end_sequence=0,
                had_exception=False,
            )
        ]
        assert check_schema_conformance(_SIMPLE_SCHEMA, spans) == ()
