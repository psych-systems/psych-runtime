"""``psych_runtime.model.tool_normalize``: sorting tools and their schemas.

Pure logic, no IO -- cross-process byte stability under different
``PYTHONHASHSEED`` values needs a real subprocess and lives in
``tests/functional/test_tool_normalize_cross_process.py`` instead; what
belongs here is everything that does not need a second process to observe:
the sort itself, idempotency, and that array order survives it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from psych_runtime.core.messages import ToolDefinition
from psych_runtime.model.port import ModelRequest
from psych_runtime.model.tool_normalize import normalize_tools

pytestmark = pytest.mark.unit


def _tool(
    name: str,
    *,
    description: str | None = None,
    input_schema: dict[str, Any] | None = None,
    annotations: frozenset[str] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"tool {name}",
        input_schema=input_schema or {},
        annotations=annotations or frozenset(),
    )


# ---------------------------------------------------------------------------
# Tool order
# ---------------------------------------------------------------------------


class TestToolOrder:
    def test_tools_sort_by_name(self) -> None:
        tools = [_tool("zebra"), _tool("alpha"), _tool("mango")]
        normalized = normalize_tools(tools)
        assert [t.name for t in normalized] == ["alpha", "mango", "zebra"]

    def test_order_is_independent_of_input_order(self) -> None:
        """Whatever order the caller happened to build the list in -- a
        ``set``'s hash-seed-dependent iteration among them -- the output is
        the same, which is the property that actually matters: this test
        checks it in-process across several input permutations rather than
        across processes (see the functional test for the cross-process
        proof)."""
        names = ["delta", "alpha", "charlie", "bravo", "echo"]
        expected = tuple(sorted(names))

        import itertools

        for permutation in itertools.permutations(names):
            normalized = normalize_tools([_tool(n) for n in permutation])
            assert tuple(t.name for t in normalized) == expected

    def test_does_not_change_which_tools_are_offered(self) -> None:
        """Sorting is bytes, not membership: the same set of names comes back,
        never a name added or dropped."""
        tools = [_tool("c"), _tool("a"), _tool("b")]
        normalized = normalize_tools(tools)
        assert {t.name for t in normalized} == {"a", "b", "c"}
        assert len(normalized) == 3


# ---------------------------------------------------------------------------
# Schema key order
# ---------------------------------------------------------------------------


class TestSchemaKeyOrder:
    def test_top_level_keys_sort(self) -> None:
        schema = {"zeta": 1, "alpha": 2, "mu": 3}
        (normalized,) = normalize_tools([_tool("t", input_schema=schema)])
        assert list(normalized.input_schema.keys()) == ["alpha", "mu", "zeta"]

    def test_nested_object_keys_sort_recursively(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "zeta": {"z_inner": 1, "a_inner": 2},
                "alpha": {"type": "string"},
            },
        }
        (normalized,) = normalize_tools([_tool("t", input_schema=schema)])
        assert list(normalized.input_schema.keys()) == ["properties", "type"]
        properties = normalized.input_schema["properties"]
        assert list(properties.keys()) == ["alpha", "zeta"]
        assert list(properties["zeta"].keys()) == ["a_inner", "z_inner"]

    def test_key_order_does_not_depend_on_which_keys_were_inserted_first(self) -> None:
        """A refactor that reorders a function's own parameters must not
        change the bytes the model is offered (the ticket's own example)."""
        schema_a = {"b": {"type": "string"}, "a": {"type": "string"}}
        schema_b = {"a": {"type": "string"}, "b": {"type": "string"}}
        (norm_a,) = normalize_tools([_tool("t", input_schema=schema_a)])
        (norm_b,) = normalize_tools([_tool("t", input_schema=schema_b)])
        assert norm_a.input_schema == norm_b.input_schema
        assert list(norm_a.input_schema.keys()) == list(norm_b.input_schema.keys())


# ---------------------------------------------------------------------------
# Array order is never touched
# ---------------------------------------------------------------------------


class TestArrayOrderPreserved:
    def test_one_of_keeps_its_order(self) -> None:
        schema = {
            "oneOf": [
                {"type": "string", "b": 1, "a": 2},
                {"type": "integer"},
                {"type": "boolean"},
            ]
        }
        (normalized,) = normalize_tools([_tool("t", input_schema=schema)])
        one_of = normalized.input_schema["oneOf"]
        assert [entry.get("type") for entry in one_of] == ["string", "integer", "boolean"]
        # The object *inside* the array still gets its own keys sorted --
        # only the array's element order is left alone.
        assert list(one_of[0].keys()) == ["a", "b", "type"]

    def test_any_of_all_of_prefix_items_and_enum_keep_their_order(self) -> None:
        schema = {
            "anyOf": [{"type": "z"}, {"type": "a"}],
            "allOf": [{"type": "b"}, {"type": "y"}],
            "prefixItems": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}],
            "enum": ["gamma", "alpha", "beta"],
        }
        (normalized,) = normalize_tools([_tool("t", input_schema=schema)])
        assert [e["type"] for e in normalized.input_schema["anyOf"]] == ["z", "a"]
        assert [e["type"] for e in normalized.input_schema["allOf"]] == ["b", "y"]
        assert [e["type"] for e in normalized.input_schema["prefixItems"]] == [
            "string",
            "number",
            "boolean",
        ]
        assert normalized.input_schema["enum"] == ["gamma", "alpha", "beta"]

    def test_an_arbitrary_nested_array_keeps_its_order_too(self) -> None:
        """Preserving array order is not a special case for the five named
        keywords -- it is what this function always does with a list."""
        schema = {
            "examples": [
                {"z": 1, "a": 2},
                {"m": 1},
            ],
            "x-ordered-steps": ["third", "first", "second"],
        }
        (normalized,) = normalize_tools([_tool("t", input_schema=schema)])
        assert [list(e.keys()) for e in normalized.input_schema["examples"]] == [
            ["a", "z"],
            ["m"],
        ]
        assert normalized.input_schema["x-ordered-steps"] == ["third", "first", "second"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_running_twice_produces_byte_identical_output(self) -> None:
        tools = [
            _tool(
                "zebra",
                input_schema={
                    "oneOf": [{"type": "string"}, {"type": "integer"}],
                    "properties": {"z": 1, "a": 2},
                },
                annotations=frozenset({"write", "destructive"}),
            ),
            _tool("alpha", input_schema={"properties": {"b": 1, "a": 2}}),
        ]
        once = normalize_tools(tools)
        twice = normalize_tools(once)

        once_bytes = json.dumps(
            [t.model_dump(mode="json") for t in once],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        twice_bytes = json.dumps(
            [t.model_dump(mode="json") for t in twice],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        assert once_bytes == twice_bytes

    def test_model_request_construction_is_itself_idempotent(self) -> None:
        """``ModelRequest`` normalises ``tools`` in its own validator (see
        ``psych_runtime.model.port``), so building a second request from an already-
        normalised one's tools must not move anything further."""
        tools = [_tool("b", input_schema={"y": 1, "x": 2}), _tool("a")]
        first = ModelRequest(model="m", messages=(), tools=tuple(tools))
        second = ModelRequest(model="m", messages=(), tools=first.tools)
        assert first.tools == second.tools


# ---------------------------------------------------------------------------
# A stable key for tools that collide on name (or, per the acceptance
# criteria, have none)
# ---------------------------------------------------------------------------


class TestStableKeyForUnnamedOrCollidingTools:
    def test_two_tools_sharing_a_name_sort_the_same_way_regardless_of_input_order(
        self,
    ) -> None:
        first = _tool("dup", description="first variant", input_schema={"a": 1})
        second = _tool("dup", description="second variant", input_schema={"b": 2})

        forward = normalize_tools([first, second])
        backward = normalize_tools([second, first])

        assert [t.description for t in forward] == [t.description for t in backward]

    def test_an_unnamed_tool_sorts_by_a_hash_of_its_canonical_form(self) -> None:
        """``ToolDefinition.name`` is ``min_length=1``, so this can only be
        reached by bypassing validation (``model_construct``), exactly as a
        hand-built model in a test, or a future relaxation of that
        constraint, would. The acceptance criterion is stability: two
        differently-shaped unnamed tools must sort the same way regardless
        of which order they were handed in."""
        empty_one = ToolDefinition.model_construct(
            name="", description="one", input_schema={"k": 1}, annotations=frozenset()
        )
        empty_two = ToolDefinition.model_construct(
            name="", description="two", input_schema={"k": 2}, annotations=frozenset()
        )

        forward = normalize_tools([empty_one, empty_two])
        backward = normalize_tools([empty_two, empty_one])

        assert [t.description for t in forward] == [t.description for t in backward]
