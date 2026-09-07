"""``psych_runtime`` claims to be the whole public API, and this is what makes that true.

The module docstring says "the public API is this module and nothing else", and
that claim was false in a way nobody notices until they try to write typed code
against it: ``psych_runtime.RunReport`` was exported and ``ToolCallReport`` was not, so
a consumer annotating a function that takes one had to import through a
submodule path the same docstring calls internal. Same for ``WorkflowSpec``,
which was exported without the three step types it cannot be constructed
without.

The rule this pins is narrow on purpose, and it is mechanical rather than a
matter of taste: **a type reachable as a field of an exported model must itself
be exported**. Anything a consumer can hold, they can name. It says nothing
about what else belongs on the surface, which stays a judgement call.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel

import psych_runtime

pytestmark = pytest.mark.unit


def _psych_types_in(annotation: object) -> set[str]:
    """Every Psych-owned type named anywhere inside one annotation."""
    found: set[str] = set()
    for level_one in [annotation, *typing.get_args(annotation)]:
        for level_two in [level_one, *typing.get_args(level_one)]:
            name = getattr(level_two, "__name__", None)
            module = getattr(level_two, "__module__", "")
            if name is not None and module.startswith("psych."):
                found.add(name)
    return found


class TestEveryPublicFieldTypeIsNameable:
    def test_no_exported_model_has_a_field_of_an_unexported_type(self) -> None:
        public = set(psych_runtime.__all__)
        unreachable: dict[str, set[str]] = {}

        for name in public:
            exported = getattr(psych_runtime, name)
            if not (isinstance(exported, type) and issubclass(exported, BaseModel)):
                continue
            for field_name, field in exported.model_fields.items():
                for referenced in _psych_types_in(field.annotation) - public:
                    unreachable.setdefault(referenced, set()).add(f"{name}.{field_name}")

        named = {key: sorted(value) for key, value in unreachable.items()}
        assert not named, (
            "these types are reachable from the public surface but cannot be named "
            "from it, so a consumer annotating them must import a submodule path "
            f"psych_runtime's own docstring calls internal: {named}"
        )


class TestTheSurfaceIsSelfConsistent:
    def test_everything_in_all_actually_exists(self) -> None:
        missing = [name for name in psych_runtime.__all__ if not hasattr(psych_runtime, name)]
        assert not missing

    def test_a_workflow_can_be_built_without_leaving_the_public_module(self) -> None:
        """The gap that prompted this file, as the thing a consumer would write."""
        spec = psych_runtime.WorkflowSpec(
            name="onboard",
            steps=(
                psych_runtime.ToolStep(
                    name="create", tool="create_account", arguments={"plan": "x"}
                ),
                psych_runtime.AgentStep(
                    name="welcome",
                    spec=psych_runtime.AgentSpec(
                        name="greeter", model=psych_runtime.ModelRef(model="gpt-4o-mini")
                    ),
                ),
            ),
        )
        assert [step.name for step in spec.steps] == ["create", "welcome"]

    def test_the_ports_a_consumer_implements_are_nameable(self) -> None:
        """A consumer writing their own adapter annotates against these."""
        for port in (
            "Store",
            "BlobStore",
            "ModelClient",
            "Policy",
            "SecretResolver",
            "Telemetry",
            "Sandbox",
            "PriceResolver",
            "EgressPolicy",
        ):
            assert hasattr(psych_runtime, port), f"{port} is a port consumers implement"
