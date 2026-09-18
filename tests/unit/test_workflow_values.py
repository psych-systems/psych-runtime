"""Paths, mappings, conditions and the schema subset a workflow step reads.

Everything here is pure, and every failure mode is a named exception with the
path in it, because a step fed ``None`` where it expected an id fails later and
further from the cause than a step that refuses to start.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from psych_runtime.core.spec import Condition, LiteralValue, ValuePath
from psych_runtime.core.workflow_values import (
    SchemaViolation,
    UnresolvedPath,
    WorkflowScope,
    evaluate_condition,
    resolve_mapping,
    resolve_path,
    validate_schema,
)

pytestmark = pytest.mark.unit

SCOPE = WorkflowScope(
    input={"customer": {"id": "c-1", "tags": ["vip", "eu"]}, "n": 3},
    steps={"fetch": {"output": {"rows": [{"id": 1}, {"id": 2}]}, "status": "completed"}},
    state={"total": 7},
)


class TestPaths:
    def test_every_root_resolves(self) -> None:
        inside = SCOPE.inside(item={"sku": "x"}, index=2, iteration=4)
        assert resolve_path(inside, "input.customer.id") == "c-1"
        assert resolve_path(inside, "steps.fetch.output.rows[1].id") == 2
        assert resolve_path(inside, "state.total") == 7
        assert resolve_path(inside, "item.sku") == "x"
        assert resolve_path(inside, "index") == 2
        assert resolve_path(inside, "iteration") == 4

    def test_an_unknown_root_names_the_roots(self) -> None:
        with pytest.raises(UnresolvedPath, match="starts with one of"):
            resolve_path(SCOPE, "prior.fetch")

    def test_a_missing_field_names_what_is_there(self) -> None:
        with pytest.raises(UnresolvedPath, match=r"has no field 'email'.*it has: id, tags"):
            resolve_path(SCOPE, "input.customer.email")

    def test_an_index_past_the_end_says_so(self) -> None:
        with pytest.raises(UnresolvedPath, match="index 5 is past the end"):
            resolve_path(SCOPE, "input.customer.tags[5]")

    def test_indexing_an_object_is_refused(self) -> None:
        with pytest.raises(UnresolvedPath, match="not a list"):
            resolve_path(SCOPE, "input.customer[0]")

    def test_loop_roots_outside_a_loop_are_errors_not_none(self) -> None:
        with pytest.raises(UnresolvedPath, match="only readable inside a foreach"):
            resolve_path(SCOPE, "item")
        with pytest.raises(UnresolvedPath, match="only readable inside a loop"):
            resolve_path(SCOPE, "iteration")

    def test_a_malformed_path_is_refused(self) -> None:
        with pytest.raises(UnresolvedPath):
            resolve_path(SCOPE, "input..x")


class TestMappings:
    def test_literals_and_paths_mix(self) -> None:
        resolved = resolve_mapping(
            SCOPE, {"id": ValuePath(path="input.customer.id"), "plan": LiteralValue(value="pro")}
        )
        assert resolved == {"id": "c-1", "plan": "pro"}

    def test_the_first_unresolved_field_stops_the_mapping(self) -> None:
        with pytest.raises(UnresolvedPath, match=r"input\.missing"):
            resolve_mapping(SCOPE, {"a": ValuePath(path="input.missing")})


class TestConditions:
    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            ("eq", 3, True),
            ("ne", 3, False),
            ("gt", 2, True),
            ("gte", 3, True),
            ("lt", 3, False),
            ("lte", 3, True),
            ("in", [1, 3], True),
            ("truthy", None, True),
        ],
    )
    def test_leaf_operators(self, op: str, value: object, expected: bool) -> None:
        condition = Condition(path="input.n", op=op, value=value)  # type: ignore[arg-type]
        assert evaluate_condition(SCOPE, condition) is expected

    def test_contains_and_matches(self) -> None:
        assert evaluate_condition(
            SCOPE, Condition(path="input.customer.tags", op="contains", value="vip")
        )
        assert evaluate_condition(
            SCOPE, Condition(path="input.customer.id", op="matches", value=r"^c-\d+$")
        )

    def test_exists_is_false_rather_than_an_error(self) -> None:
        assert not evaluate_condition(SCOPE, Condition(path="input.nope", op="exists"))
        assert evaluate_condition(SCOPE, Condition(path="input.n", op="exists"))

    def test_other_operators_on_a_missing_path_raise(self) -> None:
        with pytest.raises(UnresolvedPath):
            evaluate_condition(SCOPE, Condition(path="input.nope", op="eq", value=1))

    def test_ordering_mixed_types_is_refused(self) -> None:
        with pytest.raises(UnresolvedPath, match="numbers with numbers"):
            evaluate_condition(SCOPE, Condition(path="input.customer.id", op="gt", value=1))

    def test_compound_and_negate(self) -> None:
        both = Condition(
            all_of=(
                Condition(path="input.n", op="gt", value=1),
                Condition(path="state.total", op="eq", value=7),
            )
        )
        either = Condition(
            any_of=(Condition(path="input.n", op="gt", value=10), Condition(path="state.total")),
            negate=True,
        )
        assert evaluate_condition(SCOPE, both)
        assert not evaluate_condition(SCOPE, either)

    def test_a_condition_is_exactly_one_shape(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            Condition(path="input.n", all_of=(Condition(path="input.n"),))
        with pytest.raises(ValueError, match="exactly one of"):
            Condition()

    def test_matches_needs_a_valid_regex(self) -> None:
        with pytest.raises(ValueError, match="regular expression"):
            Condition(path="input.n", op="matches", value="(")


class TestSchemaSubset:
    SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "required": ["id", "amount"],
        "properties": {
            "id": {"type": "string", "pattern": "^c-"},
            "amount": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "tier": {"enum": ["free", "pro"]},
        },
        "additionalProperties": False,
    }

    def test_a_conforming_value_passes(self) -> None:
        validate_schema({"id": "c-1", "amount": 5, "tags": ["a"], "tier": "pro"}, self.SCHEMA)

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ({"amount": 5}, "missing required field 'id'"),
            ({"id": "x", "amount": 5}, "id: does not match pattern"),
            ({"id": "c-1", "amount": 100}, "amount: must be below 100"),
            ({"id": "c-1", "amount": -1}, "amount: below the minimum 0"),
            ({"id": "c-1", "amount": 1, "tags": ["a", "b", "c"]}, "tags: allows at most 2"),
            ({"id": "c-1", "amount": 1, "tags": [1]}, r"tags\[0\]: expected type 'string'"),
            ({"id": "c-1", "amount": 1, "tier": "gold"}, "tier: must be one of"),
            ({"id": "c-1", "amount": 1, "extra": 1}, "extra: additional field is not allowed"),
            ([], "expected type 'object'"),
        ],
    )
    def test_each_violation_names_its_location(self, value: object, message: str) -> None:
        with pytest.raises(SchemaViolation, match=message):
            validate_schema(value, self.SCHEMA)

    def test_booleans_are_not_numbers(self) -> None:
        with pytest.raises(SchemaViolation):
            validate_schema(True, {"type": "integer"})
        validate_schema(3, {"type": "number"})
        validate_schema(3.0, {"type": "integer"})

    def test_combinators(self) -> None:
        either = {"anyOf": [{"type": "string"}, {"type": "null"}]}
        validate_schema(None, either)
        with pytest.raises(SchemaViolation, match="anyOf"):
            validate_schema(1, either)
        exactly = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
        with pytest.raises(SchemaViolation, match="exactly one"):
            validate_schema(1, exactly)
        with pytest.raises(SchemaViolation, match="not"):
            validate_schema("x", {"not": {"type": "string"}})

    def test_an_empty_schema_accepts_everything(self) -> None:
        validate_schema({"anything": [1, 2]}, {})
