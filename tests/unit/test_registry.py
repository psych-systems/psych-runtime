"""The tool registry.

DESIGN.md §10.1. The invariant under test: a Spec holds a name, the registry
holds the function, and the two meet only here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from psych_runtime.tools.registry import ToolRegistry, ToolSchemaError

pytestmark = pytest.mark.unit


class Address(BaseModel):
    """At module level on purpose.

    Under `from __future__ import annotations` a hint is a string resolved
    against the function's module globals, so a model defined inside a test
    method is not visible to `get_type_hints`. Module level is how consumers
    actually write this, and the registry says so in its error when it is not.
    """

    city: str
    postcode: str


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


class TestRegistration:
    def test_a_function_registers_under_its_own_name(self, registry: ToolRegistry) -> None:
        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return order_id

        assert registry.names == {"lookup"}

    def test_the_decorator_returns_the_function_unchanged(self, registry: ToolRegistry) -> None:
        """The consumer's own code still calls it directly. What Psych keeps is
        the name."""

        @registry.register
        def double(value: int) -> int:
            """Double a number."""
            return value * 2

        assert double(21) == 42

    def test_an_explicit_name_wins(self, registry: ToolRegistry) -> None:
        @registry.register(name="refund")
        def issue_refund(order_id: str) -> str:
            """Issue a refund."""
            return order_id

        assert registry.names == {"refund"}

    def test_the_description_defaults_to_the_first_docstring_paragraph(
        self, registry: ToolRegistry
    ) -> None:
        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order by id.

            Longer detail the model does not need in every prompt.
            """
            return order_id

        assert registry.require("lookup").description == "Look up an order by id."

    def test_a_tool_with_no_description_is_refused(self, registry: ToolRegistry) -> None:
        """The model chooses tools by their descriptions, so an undescribed tool
        is one it will either ignore or misuse."""
        with pytest.raises(ToolSchemaError, match="no description and no docstring"):

            @registry.register
            def lookup(order_id: str) -> str:
                return order_id

    def test_registering_one_name_twice_is_refused(self, registry: ToolRegistry) -> None:
        @registry.register(name="x")
        def first() -> str:
            """First."""
            return "a"

        with pytest.raises(ToolSchemaError, match="already registered"):

            @registry.register(name="x")
            def second() -> str:
                """Second."""
                return "b"

    def test_flags_are_recorded(self, registry: ToolRegistry) -> None:
        @registry.register(interruptible=False, safe_to_retry=True, annotations={"destructive"})
        def refund(order_id: str) -> str:
            """Issue a refund."""
            return order_id

        tool = registry.require("refund")
        assert not tool.interruptible
        assert tool.safe_to_retry
        assert tool.annotations == frozenset({"destructive"})

    def test_interruptible_defaults_true_and_safe_to_retry_defaults_false(
        self, registry: ToolRegistry
    ) -> None:
        """Assuming a side effect is repeatable is how double refunds happen."""

        @registry.register
        def anything(value: str) -> str:
            """Do a thing."""
            return value

        tool = registry.require("anything")
        assert tool.interruptible
        assert not tool.safe_to_retry


class TestSchemaDerivation:
    def test_the_schema_comes_from_type_hints(self, registry: ToolRegistry) -> None:
        @registry.register
        def search(query: str, limit: int = 10) -> list[str]:
            """Search for things."""
            return [query] * limit

        schema = registry.require("search").input_schema
        assert schema["properties"]["query"]["type"] == "string"
        assert schema["properties"]["limit"]["type"] == "integer"
        assert schema["required"] == ["query"]

    def test_a_pydantic_model_argument_is_described_fully(self, registry: ToolRegistry) -> None:
        @registry.register
        def ship(to: Address) -> str:
            """Ship a parcel."""
            return to.city

        schema = registry.require("ship").input_schema
        assert "$defs" in schema
        assert "Address" in schema["$defs"]

    def test_an_unhinted_parameter_is_refused_at_registration(self, registry: ToolRegistry) -> None:
        """Boot is the right time to find this, not mid-conversation."""
        with pytest.raises(ToolSchemaError, match="has no type hint"):

            @registry.register
            def broken(value) -> str:  # type: ignore[no-untyped-def]
                """Do a thing."""
                return str(value)

    def test_var_args_are_refused(self, registry: ToolRegistry) -> None:
        """A model cannot be told what to put there, so it will guess."""
        with pytest.raises(ToolSchemaError, match=r"\*args"):

            @registry.register
            def broken(*args: str) -> str:
                """Do a thing."""
                return "".join(args)

    def test_the_synthetic_model_title_is_stripped(self, registry: ToolRegistry) -> None:
        """It tells the model nothing and costs prompt tokens every turn."""

        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return order_id

        assert "title" not in registry.require("lookup").input_schema

    def test_extra_arguments_are_forbidden_by_the_schema(self, registry: ToolRegistry) -> None:
        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return order_id

        assert registry.require("lookup").input_schema.get("additionalProperties") is False


class TestCalling:
    async def test_an_async_tool_is_awaited(self, registry: ToolRegistry) -> None:
        @registry.register
        async def fetch(url: str) -> str:
            """Fetch something."""
            await asyncio.sleep(0)
            return f"got {url}"

        assert await registry.call("fetch", {"url": "x"}) == "got x"

    async def test_a_sync_tool_runs_off_the_event_loop(self, registry: ToolRegistry) -> None:
        """DESIGN.md §10.1 accepts sync functions on purpose: a consumer with an
        existing codebase should not have to rewrite it to register a tool. That
        only holds if the function cannot block the loop."""
        seen: dict[str, int] = {}

        @registry.register
        def blocking(value: str) -> str:
            """Do something slow."""
            seen["thread"] = threading.get_ident()
            return value

        result = await registry.call("blocking", {"value": "ok"})
        assert result == "ok"
        assert seen["thread"] != threading.get_ident()

    async def test_arguments_are_validated_before_the_function_sees_them(
        self, registry: ToolRegistry
    ) -> None:
        @registry.register
        def count(items: list[str]) -> int:
            """Count items."""
            return len(items)

        with pytest.raises(ValidationError):
            await registry.call("count", {"items": "not a list"})

    async def test_an_invented_argument_is_rejected(self, registry: ToolRegistry) -> None:
        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return order_id

        with pytest.raises(ValidationError):
            await registry.call("lookup", {"order_id": "A", "invented": True})

    async def test_a_missing_required_argument_is_rejected(self, registry: ToolRegistry) -> None:
        @registry.register
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return order_id

        with pytest.raises(ValidationError):
            await registry.call("lookup", {})

    async def test_defaults_are_applied(self, registry: ToolRegistry) -> None:
        @registry.register
        def search(query: str, limit: int = 3) -> int:
            """Search."""
            return limit

        assert await registry.call("search", {"query": "x"}) == 3

    async def test_calling_an_unregistered_tool_names_what_is_registered(
        self, registry: ToolRegistry
    ) -> None:
        @registry.register(name="present")
        def present() -> str:
            """A tool."""
            return "here"

        with pytest.raises(ToolSchemaError, match="Registered: present"):
            await registry.call("absent", {})


class TestDefinitions:
    def test_definitions_describe_every_registered_tool(self, registry: ToolRegistry) -> None:
        @registry.register
        def one(a: str) -> str:
            """First tool."""
            return a

        @registry.register
        def two(b: int) -> int:
            """Second tool."""
            return b

        assert {definition.name for definition in registry.definitions()} == {"one", "two"}

    def test_definitions_can_be_restricted_to_a_narrowed_set(self, registry: ToolRegistry) -> None:
        """This is how the resolver hands the model only what narrowing allowed."""

        @registry.register
        def one(a: str) -> str:
            """First tool."""
            return a

        @registry.register
        def two(b: int) -> int:
            """Second tool."""
            return b

        assert [d.name for d in registry.definitions(["two"])] == ["two"]

    def test_a_definition_carries_its_annotations(self, registry: ToolRegistry) -> None:
        @registry.register(annotations={"read-only"})
        def peek(a: str) -> str:
            """Look at something."""
            return a

        assert registry.definitions(["peek"])[0].annotations == frozenset({"read-only"})


class TestNoCallablesEscape:
    def test_the_registry_holds_the_function_and_the_spec_holds_the_name(
        self, registry: ToolRegistry
    ) -> None:
        """The whole reason this module exists. A Spec referencing this tool
        carries the string 'refund' and nothing else."""

        @registry.register(name="refund")
        def issue_refund(order_id: str) -> str:
            """Issue a refund."""
            return order_id

        from psych_runtime.core.spec import CodeTool

        spec_entry = CodeTool(name="refund")
        assert spec_entry.name == "refund"
        assert isinstance(spec_entry.name, str)

        dumped: dict[str, Any] = spec_entry.model_dump()
        assert all(not callable(value) for value in dumped.values())
        assert registry.require(spec_entry.name).fn is issue_refund


class TestHintResolution:
    def test_a_type_defined_inside_another_function_is_refused_with_advice(self) -> None:
        """A real limitation of string annotations, named rather than papered
        over. The error tells the author what to do about it."""
        registry = ToolRegistry()

        class Local(BaseModel):
            value: str

        with pytest.raises(ToolSchemaError, match="Move the type to module level"):

            @registry.register
            def uses_local(payload: Local) -> str:
                """Do a thing."""
                return payload.value


def _dynamic_lookup_model() -> type[BaseModel]:
    """A schema built the way a consumer with no signature would build one:
    ``create_model`` at runtime, with ``extra="forbid"`` set on purpose."""
    return create_model(
        "lookup_Arguments",
        __config__=ConfigDict(extra="forbid"),
        order_id=(str, ...),
    )


def _dynamic_lookup(order_id: str) -> str:
    return order_id


class TestDynamicRegistration:
    """A tool whose schema is a runtime-built Pydantic model rather
    than a Python signature, registered through the second door."""

    def test_a_schema_and_a_callable_register_without_a_signature(
        self, registry: ToolRegistry
    ) -> None:
        registry.register_dynamic(
            "lookup",
            _dynamic_lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
        )

        assert registry.names == {"lookup"}
        tool = registry.require("lookup")
        assert tool.description == "Look up an order by its id."
        assert tool.fn is _dynamic_lookup

    def test_a_missing_name_is_refused(self, registry: ToolRegistry) -> None:
        with pytest.raises(ToolSchemaError, match="non-empty name"):
            registry.register_dynamic(
                "",
                _dynamic_lookup,
                _dynamic_lookup_model(),
                description="Look up an order.",
            )

    def test_a_supplied_schema_with_no_description_is_refused(self, registry: ToolRegistry) -> None:
        """The same standard the derived path holds a docstring-less function
        to: a model chooses tools by their descriptions."""
        model = _dynamic_lookup_model()  # no docstring, and no description= given
        with pytest.raises(ToolSchemaError, match="no description and no docstring"):
            registry.register_dynamic("lookup", _dynamic_lookup, model)

    def test_a_description_can_come_from_the_models_own_docstring(
        self, registry: ToolRegistry
    ) -> None:
        model = _dynamic_lookup_model()
        model.__doc__ = "Look up an order by its id.\n\nMore detail nobody needs."
        registry.register_dynamic("lookup", _dynamic_lookup, model)

        assert registry.require("lookup").description == "Look up an order by its id."

    def test_a_schema_without_extra_forbid_is_rejected_not_corrected(
        self, registry: ToolRegistry
    ) -> None:
        """Decision recorded in the module docstring: silently rebuilding the
        consumer's model to force extra="forbid" would drop anything not
        representable from bare field info (a validator, a computed field), so
        this rejects rather than papering over it."""
        loose_model = create_model("lookup_Arguments", order_id=(str, ...))
        assert loose_model.model_config.get("extra") != "forbid"

        with pytest.raises(ToolSchemaError, match='extra="forbid"'):
            registry.register_dynamic(
                "lookup",
                _dynamic_lookup,
                loose_model,
                description="Look up an order.",
            )

    def test_registering_a_name_that_collides_with_a_decorated_tool_is_refused(
        self, registry: ToolRegistry
    ) -> None:
        """One duplicate-name check, regardless of which door either tool came
        through."""

        @registry.register(name="lookup")
        def decorated(order_id: str) -> str:
            """Look up an order."""
            return order_id

        with pytest.raises(ToolSchemaError, match="already registered"):
            registry.register_dynamic(
                "lookup",
                _dynamic_lookup,
                _dynamic_lookup_model(),
                description="Look up an order by its id.",
            )

    async def test_arguments_are_validated_with_the_same_strictness(
        self, registry: ToolRegistry
    ) -> None:
        registry.register_dynamic(
            "lookup",
            _dynamic_lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
        )

        assert await registry.call("lookup", {"order_id": "A1"}) == "A1"
        with pytest.raises(ValidationError):
            await registry.call("lookup", {"order_id": "A1", "invented": True})
        with pytest.raises(ValidationError):
            await registry.call("lookup", {})

    def test_the_schema_forbids_extra_properties_the_same_way(self, registry: ToolRegistry) -> None:
        registry.register_dynamic(
            "lookup",
            _dynamic_lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
        )
        assert registry.require("lookup").input_schema.get("additionalProperties") is False


class TestBothDoorsProduceTheSameRegisteredTool:
    """The bar for the second door: not a second execution model. A tool built
    from a signature and an equivalent tool built from a schema must come out
    identical in every field the resolver, narrowing and the failure-streak
    guard read."""

    def test_a_derived_and_a_dynamic_registration_of_the_same_shape_agree(self) -> None:
        derived = ToolRegistry()
        dynamic = ToolRegistry()

        @derived.register(annotations={"read-only"}, safe_to_retry=True)
        def lookup(order_id: str) -> str:
            """Look up an order by its id."""
            return order_id

        dynamic.register_dynamic(
            "lookup",
            lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
            annotations={"read-only"},
            safe_to_retry=True,
        )

        derived_tool = derived.require("lookup")
        dynamic_tool = dynamic.require("lookup")

        # Same everything a caller reads off the tool, field by field, rather
        # than relying on dataclass equality tripping over the unequal
        # TypeAdapter and function identity fields.
        assert derived_tool.name == dynamic_tool.name
        assert derived_tool.description == dynamic_tool.description
        assert derived_tool.input_schema == dynamic_tool.input_schema
        assert derived_tool.is_async == dynamic_tool.is_async
        assert derived_tool.interruptible == dynamic_tool.interruptible
        assert derived_tool.safe_to_retry == dynamic_tool.safe_to_retry
        assert derived_tool.annotations == dynamic_tool.annotations

        # What the model is offered is identical too.
        assert derived_tool.definition() == dynamic_tool.definition()

    async def test_the_resolver_cannot_tell_the_two_doors_apart(self) -> None:
        """The resolver reads a ``RegisteredTool``'s ``.definition()`` and
        nothing else about how it was built."""
        from psych_runtime.core.scope import Scope
        from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef
        from psych_runtime.tools.resolver import ToolResolver

        derived = ToolRegistry()
        dynamic = ToolRegistry()

        @derived.register
        def lookup(order_id: str) -> str:
            """Look up an order by its id."""
            return order_id

        dynamic.register_dynamic(
            "lookup",
            lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
        )

        spec = AgentSpec(
            name="support",
            instructions="Help.",
            model=ModelRef(model="fake"),
            tools=(CodeTool(name="lookup"),),
            limits=Limits(),
        )
        scope = Scope(tenant="acme")

        derived_resolved = await ToolResolver(derived).resolve(spec, scope)
        dynamic_resolved = await ToolResolver(dynamic).resolve(spec, scope)

        assert derived_resolved.definitions == dynamic_resolved.definitions

    async def test_the_failure_streak_guard_treats_both_the_same(self) -> None:
        """The guard withholds by tool name and streak count only; it never
        looks at how the tool was registered."""
        from psych_runtime.tools.failure_streak import assess

        derived = ToolRegistry()
        dynamic = ToolRegistry()

        @derived.register
        def lookup(order_id: str) -> str:
            """Look up an order by its id."""
            return order_id

        dynamic.register_dynamic(
            "lookup",
            lookup,
            _dynamic_lookup_model(),
            description="Look up an order by its id.",
        )

        derived_verdict = assess("lookup", 5, threshold=3, hard_stop=6)
        dynamic_verdict = assess("lookup", 5, threshold=3, hard_stop=6)
        assert derived_verdict == dynamic_verdict
        # Neither registry object is even consulted by the guard: both tools
        # are withheld by name alone, which is the point.
        assert "lookup" in derived.names
        assert "lookup" in dynamic.names
