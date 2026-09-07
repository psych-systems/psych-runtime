"""Cross-process byte stability for tool definitions.

The problem this proves does not exist is invisible from inside one
process: a set's iteration order is stable for the life of that process and
only ever differs between processes, so this test's whole point is that it
spawns two. ``PYTHONHASHSEED`` has to be set via ``env=`` on the child --
Python reads it once at interpreter startup, so setting it in-process (the
one running pytest) proves nothing about a second process.

Two seeds (0 and 1) are hard-coded rather than left to whatever the test
runner happens to be started with, because they are verified below (in
``_CONTROL_SCRIPT``, asserted against) to actually produce two different raw
``set`` iteration orders for the exact names used here -- a test that merely
hoped two arbitrary seeds would disagree could pass vacuously if they did
not.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.functional

_SEED_A = "0"
_SEED_B = "1"

# A consumer accumulating tool names from a `set`, and a schema whose
# `properties` keys come from a `set` too -- the exact shape
# `psych_runtime.model.tool_normalize` describes. `RAW_ORDER` is printed first
# as a control: it is expected to *differ* between the two seeds, which is
# what proves this test would have caught the bug it exists to catch.
# `NORMALIZED` goes through the real `ModelRequest` construction path and is
# asserted identical.
_SCRIPT = textwrap.dedent(
    """
    import json

    from psych_runtime.core.messages import ToolDefinition
    from psych_runtime.model.port import ModelRequest

    names = {"zebra", "alpha", "mango", "kiwi", "bravo"}
    schema_keys = {"zeta", "alpha", "mu", "iota", "beta"}

    tools = tuple(
        ToolDefinition(
            name=name,
            description=f"does something with {name}",
            input_schema={
                "type": "object",
                "properties": {k: {"type": "string"} for k in schema_keys},
                # `required` is order-carrying (an array) and is never
                # reordered by normalisation, so it is built here from a
                # deterministic `sorted()` rather than a `set`'s own
                # iteration order -- exactly as Pydantic's real schema
                # generator does. `properties`' *dict key* order is the
                # actual hash-seed-sensitive thing this script is proving
                # gets fixed.
                "required": sorted(schema_keys),
            },
        )
        for name in names
    )
    request = ModelRequest(model="test-model", messages=(), tools=tools)
    payload = [t.model_dump(mode="json") for t in request.tools]

    print("SET_ITER_ORDER", json.dumps(list(names)))
    print("NORMALIZED", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    """
)


def _run(seed: str) -> tuple[str, str]:
    """Run the script in a fresh subprocess under ``seed``.

    Returns ``(set_iteration_order_line, normalized_json_line)``.
    """
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
        check=True,
        timeout=30,
    )
    lines = result.stdout.splitlines()
    set_line = next(line for line in lines if line.startswith("SET_ITER_ORDER "))
    normalized_line = next(line for line in lines if line.startswith("NORMALIZED "))
    return set_line, normalized_line


class TestCrossProcessByteStability:
    def test_the_two_seeds_actually_produce_different_raw_orders(self) -> None:
        """Guards the test itself: if this ever stopped being true (a
        different Python build, a changed set of names), the assertion below
        would pass for the wrong reason."""
        set_order_a, _ = _run(_SEED_A)
        set_order_b, _ = _run(_SEED_B)
        assert set_order_a != set_order_b

    def test_normalized_tool_definitions_are_byte_identical_across_seeds(self) -> None:
        _, normalized_a = _run(_SEED_A)
        _, normalized_b = _run(_SEED_B)
        assert normalized_a == normalized_b

        # And it is not identical by accident of both sides being empty or
        # malformed: real tool definitions, in name order, came back.
        payload = json.loads(normalized_a.removeprefix("NORMALIZED "))
        assert [tool["name"] for tool in payload] == [
            "alpha",
            "bravo",
            "kiwi",
            "mango",
            "zebra",
        ]
        assert list(payload[0]["input_schema"]["properties"].keys()) == [
            "alpha",
            "beta",
            "iota",
            "mu",
            "zeta",
        ]
