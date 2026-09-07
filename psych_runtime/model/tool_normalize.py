"""Deterministic order for tool definitions inside a ``ModelRequest``.

DESIGN.md §13/§19 make prompt assembly cache-deliberate, and tool
definitions sit inside the cached prefix a provider matches on
(``psych_runtime.model.prompt``). A cache hit needs the prefix to be byte-identical, so
anything that reorders those bytes between two otherwise-identical requests
throws away every hit on it, for two reasons that are not bugs in the caller:

- **Tool order.** ``psych_runtime.core.spec`` already sorts a Spec's own ``tools`` at
  validation, which fixes the static half of this (see that module's
  ``_tools_are_a_sorted_set``). It cannot fix the other half: an MCP server's
  catalogue is resolved fresh every turn (``psych_runtime.tools.resolver``, DESIGN.md
  §10.2), in whatever order the live server or transport happened to answer,
  which is nothing Psych's own code controls. A consumer accumulating tools
  from a ``set`` hits the same problem from its own side: the same source
  code emits a different order on every process restart, because a ``set``'s
  iteration order follows Python's per-process hash seed and a plain
  ``dict``'s does not.
- **Key order inside ``input_schema``.** JSON objects carry no defined key
  order. ``psych_runtime.tools.registry._derive_schema`` gets its order from
  ``model_json_schema()``, which follows a function's own parameter order --
  useful for reading the code, meaningless for what the model is told, and a
  refactor nobody expects to have a billing consequence.

## Why this normalises here, and not in ``psych_runtime.tools.resolver``

``ToolResolver.resolve`` (``psych_runtime.tools.resolver``) is *one* caller that
builds a tool list, not the wire itself: a workflow step or a future subagent
path can assemble a ``ModelRequest`` some other way, and every provider
adapter -- ``psych_runtime.model.openai_compat`` today, ``psych_runtime.testing.fake_model``,
a native adapter still to come -- reads ``request.tools``, never
``ResolvedTools`` directly. Import-linter's layering also rules the resolver
out: ``psych_runtime.tools`` and ``psych_runtime.model`` are siblings in the "layers point
inward" contract (``pyproject.toml``), so neither may import the other, and a
fix that lived in ``psych_runtime.tools.resolver`` could never be shared by an
adapter ``psych_runtime.model`` owns. ``psych_runtime.model.port.ModelRequest`` is the one
shape every path that ends at a provider is required to build, so
normalising there -- once, in a validator that runs at construction -- is the
point nothing downstream can route around, rather than a rule every adapter
has to remember to apply.

## What stays untouched, and why

Tool order and schema key order carry no meaning in Psych: there is no
per-tool cache-breakpoint marker to preserve (Psych exposes exactly one
breakpoint, after the system message -- see
``psych_runtime.model.prompt.cache_breakpoints`` -- never one per tool), so sorting
here cannot silently drop something a provider needed to see in a particular
place. JSON *array* order is a different matter and is never touched:
``oneOf``, ``anyOf``, ``allOf``, ``prefixItems`` and ``enum`` are all
order-carrying per the JSON Schema spec, so this recurses into arrays without
ever reordering their elements -- only a nested object's own keys, wherever
it appears, get sorted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from psych_runtime.core.messages import ToolDefinition

__all__ = ["normalize_tools"]


def normalize_tools(tools: Sequence[ToolDefinition]) -> tuple[ToolDefinition, ...]:
    """Tool definitions in deterministic order, with a deterministic schema.

    Sorts by name; every object's keys inside ``input_schema`` are sorted
    recursively, arrays keep their element order (see the module docstring).

    Idempotent by construction: sorting an already-sorted sequence with the
    same key produces the same order, and sorting an already-sorted dict's
    keys again is a no-op. Running this twice on the same input therefore
    yields byte-identical output, which is what keeps a retried turn or a
    resumed Attempt rebuilding the same request from silently reintroducing
    the churn this exists to remove.
    """
    normalized = [_normalize_definition(tool) for tool in tools]
    return tuple(sorted(normalized, key=_sort_key))


def _normalize_definition(tool: ToolDefinition) -> ToolDefinition:
    """One tool, with its schema's keys sorted recursively.

    ``ToolDefinition`` is frozen, so this is a copy rather than a mutation --
    the original, exactly as the resolver or a caller's own code built it,
    is never altered in place.
    """
    return tool.model_copy(update={"input_schema": _sort_keys(tool.input_schema)})


def _sort_keys(value: Any) -> Any:
    """Recursively sort every JSON object's keys; arrays keep their order.

    There is nothing array-specific below: every ``list``/``tuple`` is simply
    recursed into, element by element, in place -- the same rule that leaves
    ``oneOf``/``anyOf``/``allOf``/``prefixItems``/``enum`` alone also leaves
    every other array alone, because a JSON array's order is always meaning-
    bearing and its *elements* (when they are themselves objects) still get
    their own keys sorted.
    """
    if isinstance(value, Mapping):
        return {key: _sort_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list | tuple):
        return [_sort_keys(item) for item in value]
    return value


def _sort_key(tool: ToolDefinition) -> tuple[str, str]:
    """Sort by name; break a tie by a hash of the tool's own canonical form.

    ``ToolDefinition.name`` is declared ``min_length=1``, so an empty name
    cannot reach here through ordinary validation. The hash tiebreak is
    unconditional anyway: it is what makes two tools that share a name --
    whatever produced the collision -- sort the same way in every process
    rather than by whatever order they happened to arrive in, and it is the
    stable key the acceptance criteria for this ticket asks an unnamed tool
    to sort by, should ``name`` ever be relaxed to allow an empty one.

    The hash is computed over ``name``, ``description``, the
    already-key-sorted ``input_schema`` and a *sorted* rendering of
    ``annotations``: ``annotations`` is a ``frozenset``, whose own iteration
    order follows the same per-process hash seed this whole module exists to
    route around, so it is turned into a sorted list before hashing rather
    than dumped as-is.
    """
    canonical = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "annotations": sorted(tool.annotations),
    }
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (tool.name, digest)
