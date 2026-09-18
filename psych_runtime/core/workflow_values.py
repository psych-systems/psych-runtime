"""Values a workflow step reads: paths, mappings, conditions, schemas.

A workflow moves data between steps without a template language. What it has
instead is declarative: a ``ValuePath`` names a value, a ``Mapping`` selects
several by name, a ``Condition`` compares one. This module evaluates those
against a ``WorkflowScope``, the snapshot of everything a step may read at the
moment it starts. Every function here is pure, so the engine records what it
resolved and a reader can resolve it again from the log by hand.

## Roots

A path starts with one of ``ROOTS``:

- ``input``: the Run's input, as admitted.
- ``steps.<name>``: a step that has completed in this workflow, with
  ``output``, ``failure``, ``skipped`` and ``status`` beneath it. Steps are
  addressed by name from anywhere in the tree, which is why step names are
  unique across a workflow's branches and loop bodies.
- ``state``: the workflow state, the Spec's ``initial_state`` under every
  completed ``set_state`` step so far.
- ``item`` and ``index``: inside a ``foreach`` body, the current element and
  its position.
- ``iteration``: inside a ``loop`` body, the iteration number from 1.

There is no ``event`` root: the payload a ``wait`` or ``human`` step received
is that step's output, read as ``steps.<name>.output`` like any other.

## Why a subset of JSON Schema

Input, output and event payloads are checked against JSON Schema documents a
consumer writes. Pulling in a full validator for that would add a dependency
to the core package, which imports nothing; and the checks a workflow author
actually writes (type, required, enum, bounds, nested properties and items)
are a small, well-defined subset. ``validate_schema`` implements that subset
and says so: a keyword outside it is ignored rather than guessed at.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from typing import Any, Final

from psych_runtime.core.spec import Condition, LiteralValue, ValuePath, ValueRef

__all__ = [
    "ROOTS",
    "SchemaViolation",
    "UnresolvedPath",
    "WorkflowScope",
    "evaluate_condition",
    "resolve_mapping",
    "resolve_path",
    "validate_schema",
]

ROOTS: Final = ("input", "steps", "state", "item", "index", "iteration")
"""The names a ``ValuePath`` may start with."""

_SEGMENT: Final = re.compile(r"([a-zA-Z_][a-zA-Z0-9_\-]*)|\[(\d+)\]")

_MISSING: Final = object()


class UnresolvedPath(Exception):
    """A path names something the workflow does not have yet, or cannot index.

    Raised rather than resolving to ``None`` because a step fed ``None`` where
    it expected a customer id fails later and further from the cause. The
    engine records this as a step failure of kind ``unresolved_path``.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class SchemaViolation(Exception):
    """A value did not satisfy the JSON Schema it was checked against."""

    def __init__(self, location: str, message: str) -> None:
        self.location = location
        super().__init__(f"{location or '$'}: {message}")


@dataclass(frozen=True, slots=True)
class WorkflowScope:
    """Everything a step may read when it starts, as plain data.

    ``steps`` maps a completed step's name to a dict with ``output``,
    ``failure``, ``skipped`` and ``status``; the engine builds it from the
    reducer's step records. ``item``, ``index`` and ``iteration`` are set only
    inside the body they belong to and are ``_MISSING`` otherwise, so reading
    one outside a loop is an error rather than ``None``.
    """

    input: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    item: Any = _MISSING
    index: int | None = None
    iteration: int | None = None

    def inside(
        self,
        *,
        item: Any = _MISSING,
        index: int | None = None,
        iteration: int | None = None,
    ) -> WorkflowScope:
        """The same scope, positioned inside a loop body."""
        return WorkflowScope(
            input=self.input,
            steps=self.steps,
            state=self.state,
            item=self.item if item is _MISSING else item,
            index=self.index if index is None else index,
            iteration=self.iteration if iteration is None else iteration,
        )

    def with_step(self, name: str, entry: dict[str, Any]) -> WorkflowScope:
        """The same scope with one more completed step readable."""
        return WorkflowScope(
            input=self.input,
            steps={**self.steps, name: entry},
            state=self.state,
            item=self.item,
            index=self.index,
            iteration=self.iteration,
        )

    def with_state(self, state: dict[str, Any]) -> WorkflowScope:
        return WorkflowScope(
            input=self.input,
            steps=self.steps,
            state=state,
            item=self.item,
            index=self.index,
            iteration=self.iteration,
        )


def _segments(path: str) -> list[str | int]:
    """Split ``a.b[2].c`` into ``["a", "b", 2, "c"]``.

    Raises:
        UnresolvedPath: the path is not well formed.
    """
    out: list[str | int] = []
    position = 0
    while position < len(path):
        if path[position] == ".":
            position += 1
            continue
        match = _SEGMENT.match(path, position)
        if match is None:
            raise UnresolvedPath(path, f"unexpected character at offset {position}")
        name, index = match.group(1), match.group(2)
        out.append(int(index) if index is not None else name)
        position = match.end()
    if not out:
        raise UnresolvedPath(path, "an empty path names nothing")
    return out


def resolve_path(scope: WorkflowScope, path: str) -> Any:
    """The value ``path`` names in ``scope``.

    Raises:
        UnresolvedPath: the root is unknown, an intermediate value is missing,
            or an index does not fit the value it is applied to.
    """
    segments = _segments(path)
    root = segments[0]
    if not isinstance(root, str) or root not in ROOTS:
        raise UnresolvedPath(path, f"a path starts with one of {', '.join(ROOTS)}")

    current: Any
    match root:
        case "input":
            current = scope.input
        case "steps":
            current = scope.steps
        case "state":
            current = scope.state
        case "item":
            if scope.item is _MISSING:
                raise UnresolvedPath(path, "`item` is only readable inside a foreach body")
            current = scope.item
        case "index":
            if scope.index is None:
                raise UnresolvedPath(path, "`index` is only readable inside a foreach body")
            current = scope.index
        case _:
            if scope.iteration is None:
                raise UnresolvedPath(path, "`iteration` is only readable inside a loop body")
            current = scope.iteration

    for depth, segment in enumerate(segments[1:], start=1):
        current = _descend(current, segment, path, segments[:depth])
    return current


def _descend(value: Any, segment: str | int, path: str, so_far: list[str | int]) -> Any:
    where = _render(so_far)
    if isinstance(segment, int):
        if isinstance(value, list | tuple):
            if segment >= len(value):
                raise UnresolvedPath(
                    path, f"{where} has {len(value)} element(s); index {segment} is past the end"
                )
            return value[segment]
        raise UnresolvedPath(path, f"{where} is not a list, so it cannot be indexed")
    if isinstance(value, MappingABC):
        if segment not in value:
            known = ", ".join(sorted(str(k) for k in value)) or "nothing"
            raise UnresolvedPath(path, f"{where} has no field {segment!r} (it has: {known})")
        return value[segment]
    raise UnresolvedPath(path, f"{where} is a {type(value).__name__}, not an object with fields")


def _render(segments: list[str | int]) -> str:
    text = ""
    for segment in segments:
        text += f"[{segment}]" if isinstance(segment, int) else (f".{segment}" if text else segment)
    return text or "$"


def resolve_ref(scope: WorkflowScope, ref: ValuePath | LiteralValue) -> Any:
    if isinstance(ref, LiteralValue):
        return ref.value
    return resolve_path(scope, ref.path)


def resolve_mapping(scope: WorkflowScope, mapping: dict[str, ValueRef]) -> dict[str, Any]:
    """Every field of ``mapping`` resolved, in the mapping's own order."""
    return {name: resolve_ref(scope, ref) for name, ref in mapping.items()}


def evaluate_condition(scope: WorkflowScope, condition: Condition) -> bool:
    """Whether ``condition`` holds in ``scope``.

    A leaf whose path does not resolve is false for ``exists`` and raises
    ``UnresolvedPath`` for every other operator: a comparison against a value
    that is not there is a workflow bug, not a false result.
    """
    if condition.all_of:
        result = all(evaluate_condition(scope, child) for child in condition.all_of)
    elif condition.any_of:
        result = any(evaluate_condition(scope, child) for child in condition.any_of)
    else:
        assert condition.path is not None  # the Spec validator guarantees it
        result = _leaf(scope, condition)
    return not result if condition.negate else result


def _leaf(scope: WorkflowScope, condition: Condition) -> bool:  # noqa: PLR0911 - one per operator
    path = condition.path or ""
    if condition.op == "exists":
        try:
            resolve_path(scope, path)
        except UnresolvedPath:
            return False
        return True
    actual = resolve_path(scope, path)
    expected = condition.value
    match condition.op:
        case "truthy":
            return bool(actual)
        case "eq":
            return bool(actual == expected)
        case "ne":
            return bool(actual != expected)
        case "gt" | "gte" | "lt" | "lte":
            return _ordered(actual, expected, condition.op, path)
        case "in":
            if not isinstance(expected, list | tuple | str | MappingABC):
                raise UnresolvedPath(path, "`in` needs a list, string or object in `value`")
            return actual in expected
        case "contains":
            if not isinstance(actual, list | tuple | str | MappingABC):
                raise UnresolvedPath(
                    path, "`contains` needs the value to be a list, string or object"
                )
            return expected in actual
        case _:
            if not isinstance(actual, str):
                raise UnresolvedPath(path, "`matches` needs the value to be a string")
            return re.search(str(expected), actual) is not None


def _ordered(actual: Any, expected: Any, op: str, path: str) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        raise UnresolvedPath(path, f"`{op}` does not order booleans")
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return _compare(float(actual), float(expected), op)
    if isinstance(actual, str) and isinstance(expected, str):
        return _compare(actual, expected, op)
    raise UnresolvedPath(path, f"`{op}` compares numbers with numbers or strings with strings")


def _compare[T: (float, str)](left: T, right: T, op: str) -> bool:
    match op:
        case "gt":
            return left > right
        case "gte":
            return left >= right
        case "lt":
            return left < right
        case _:
            return left <= right


# ---------------------------------------------------------------------------
# JSON Schema subset
# ---------------------------------------------------------------------------

_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "object": (MappingABC,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def validate_schema(value: Any, schema: dict[str, Any], *, location: str = "") -> None:
    """Check ``value`` against the supported subset of JSON Schema.

    Supported: ``type`` (a name or a list), ``enum``, ``const``, ``required``,
    ``properties``, ``additionalProperties`` (``false`` or a schema),
    ``items``, ``minItems``, ``maxItems``, ``minLength``, ``maxLength``,
    ``pattern``, ``minimum``, ``maximum``, ``exclusiveMinimum``,
    ``exclusiveMaximum``, ``anyOf``, ``allOf``, ``oneOf``, ``not``. Anything
    else is ignored. An empty schema accepts everything.

    Raises:
        SchemaViolation: the first violation found, with the location.
    """
    if not schema:
        return
    if "type" in schema:
        _check_type(value, schema["type"], location)
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaViolation(location, f"must be one of {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        raise SchemaViolation(location, f"must equal {schema['const']!r}")
    if isinstance(value, MappingABC):
        _check_object(value, schema, location)
    if isinstance(value, list | tuple):
        _check_array(value, schema, location)
    if isinstance(value, str):
        _check_string(value, schema, location)
    if isinstance(value, int | float) and not isinstance(value, bool):
        _check_number(value, schema, location)
    for sub in schema.get("allOf", ()):
        validate_schema(value, sub, location=location)
    if "anyOf" in schema and not any(_passes(value, sub) for sub in schema["anyOf"]):
        raise SchemaViolation(location, "matches none of anyOf")
    if "oneOf" in schema and sum(1 for sub in schema["oneOf"] if _passes(value, sub)) != 1:
        raise SchemaViolation(location, "must match exactly one of oneOf")
    if "not" in schema and _passes(value, schema["not"]):
        raise SchemaViolation(location, "must not match the `not` schema")


def _passes(value: Any, schema: dict[str, Any]) -> bool:
    try:
        validate_schema(value, schema)
    except SchemaViolation:
        return False
    return True


def _check_type(value: Any, declared: Any, location: str) -> None:
    names = declared if isinstance(declared, list) else [declared]
    if any(_is_type(value, str(name)) for name in names):
        return
    raise SchemaViolation(location, f"expected type {declared!r}, got {type(value).__name__}")


def _is_type(value: Any, name: str) -> bool:
    """JSON's types, not Python's: a bool is not a number and ``3.0`` is an
    integer, because a value that crossed JSON has no way to say otherwise."""
    if isinstance(value, bool):
        return name == "boolean"
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, int | float) and math.isfinite(value)
    accepted = _TYPES.get(name)
    return accepted is not None and isinstance(value, accepted)


def _check_object(value: MappingABC[Any, Any], schema: dict[str, Any], location: str) -> None:
    for name in schema.get("required", ()):
        if name not in value:
            raise SchemaViolation(location, f"missing required field {name!r}")
    properties = schema.get("properties", {})
    for name, sub in properties.items():
        if name in value:
            validate_schema(value[name], sub, location=f"{location}.{name}" if location else name)
    additional = schema.get("additionalProperties", True)
    if additional is not True:
        for name in value:
            if name in properties:
                continue
            where = f"{location}.{name}" if location else str(name)
            if additional is False:
                raise SchemaViolation(where, "additional field is not allowed")
            validate_schema(value[name], additional, location=where)


def _check_array(value: list[Any] | tuple[Any, ...], schema: dict[str, Any], location: str) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise SchemaViolation(location, f"needs at least {schema['minItems']} item(s)")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise SchemaViolation(location, f"allows at most {schema['maxItems']} item(s)")
    items = schema.get("items")
    if isinstance(items, dict):
        for position, element in enumerate(value):
            validate_schema(element, items, location=f"{location}[{position}]")


def _check_string(value: str, schema: dict[str, Any], location: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise SchemaViolation(location, f"shorter than {schema['minLength']} character(s)")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaViolation(location, f"longer than {schema['maxLength']} character(s)")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        raise SchemaViolation(location, f"does not match pattern {pattern!r}")


def _check_number(value: int | float, schema: dict[str, Any], location: str) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaViolation(location, f"below the minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaViolation(location, f"above the maximum {schema['maximum']}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        raise SchemaViolation(location, f"must exceed {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        raise SchemaViolation(location, f"must be below {schema['exclusiveMaximum']}")
