"""The rules a subagent composed at run time is composed inside (DESIGN.md §17).

Pure tests: no store, no model, no clock. Everything here is a function of a
Spec, an envelope and a request, which is why these rules live in
``psych_runtime.runtime.subagent`` as functions rather than inside the Runtime that
calls them.

The one that matters most is ``TestNarrowingOnlyNarrows``. A parent writing its
own child is a parent writing its own grant, and if that grant could name a tool
the parent does not hold, delegation becomes a way to launder access rather than
to divide work.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.spec import (
    AgentSpec,
    CodeTool,
    Limits,
    ModelRef,
    SpawnEnvelope,
)
from psych_runtime.core.version import compute_hash
from psych_runtime.runtime.subagent import (
    SpawnRequest,
    check_alive,
    check_definition,
    check_depth,
    compose_child_spec,
    composed_tool_names,
    envelope_models,
    envelope_tools,
    message_definition,
    resolve_child_depth,
    spawn_definition,
)

pytestmark = pytest.mark.unit


def parent(**kwargs: object) -> AgentSpec:
    base: dict[str, object] = {
        "name": "coordinator",
        "instructions": "Coordinate.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"), CodeTool(name="refund")),
        "spawn": SpawnEnvelope(),
    }
    base.update(kwargs)
    return AgentSpec(**base)  # type: ignore[arg-type]  # kwargs are Spec fields by construction


def request(**kwargs: object) -> SpawnRequest:
    base: dict[str, object] = {
        "name": "researcher",
        "purpose": "Research supplier pricing for the quote.",
        "task": "Find the current list price of part 88-B from every supplier we use.",
        "deliverable": "A list of supplier, price, and the page you read it on.",
    }
    base.update(kwargs)
    return SpawnRequest(**base)  # type: ignore[arg-type]  # kwargs are request fields


class TestNarrowingOnlyNarrows:
    """Access narrows and never widens, across the composition boundary too."""

    def test_a_parent_cannot_write_itself_a_child_with_more_access(self) -> None:
        """The single most important line in this feature.

        The parent holds ``lookup``. It composes a child asking for ``refund``
        and for a tool nobody has ever registered. It gets neither, because
        ``narrow`` selects from the plane above rather than unioning with it,
        so there is no request that can name a tool the parent does not hold.
        """
        spec = parent(tools=(CodeTool(name="lookup"),), spawn=SpawnEnvelope())
        composed = compose_child_spec(
            spec,
            request(tools=("refund", "delete_everything", "lookup")),
            ["lookup"],
        )
        assert composed.tools == ("lookup",)
        assert [tool.name for tool in composed.spec.tools] == ["lookup"]

    def test_the_envelope_is_a_ceiling_not_a_grant(self) -> None:
        """A parent holding two tools may compose children with only one of
        them, and asking for the other gets nothing rather than an error: a
        narrowed-away tool is silently absent everywhere else too."""
        spec = parent(spawn=SpawnEnvelope(tools=("lookup",)))
        composed = compose_child_spec(spec, request(tools=("refund",)), ["lookup", "refund"])
        assert composed.tools == ()

    def test_asking_for_nothing_means_everything_the_envelope_allows(self) -> None:
        """Empty means everything the plane above permits, the same rule
        ``psych_runtime.tools.narrowing`` states for every other plane. Inverting it
        here alone would make `spawn` silently useless."""
        spec = parent(spawn=SpawnEnvelope(tools=("lookup",)))
        assert composed_tool_names(SpawnEnvelope(tools=("lookup",)), ["lookup", "refund"], []) == [
            "lookup"
        ]
        composed = compose_child_spec(spec, request(), ["lookup", "refund"])
        assert composed.tools == ("lookup",)

    def test_a_pattern_selects_from_what_the_parent_holds(self) -> None:
        envelope = SpawnEnvelope(tools=("look*",))
        assert envelope_tools(envelope, ["lookup", "lookahead", "refund"]) == [
            "lookup",
            "lookahead",
        ]
        # And a pattern cannot introduce a name the parent never had.
        assert envelope_tools(SpawnEnvelope(tools=("*",)), ["lookup"]) == ["lookup"]

    def test_an_agent_with_no_envelope_composes_nothing(self) -> None:
        with pytest.raises(AccessDenied, match="may not compose"):
            compose_child_spec(parent(spawn=None), request(), ["lookup"])


class TestModels:
    def test_an_empty_model_list_means_the_parents_own_model(self) -> None:
        """A model the author never wrote down is a model they never priced."""
        assert envelope_models(SpawnEnvelope(), "fake-standard") == ["fake-standard"]
        composed = compose_child_spec(parent(), request(), ["lookup"])
        assert composed.model == "fake-standard"

    def test_a_model_outside_the_envelope_is_refused(self) -> None:
        spec = parent(spawn=SpawnEnvelope(models=("fake-standard",)))
        with pytest.raises(AccessDenied, match="fake-standard"):
            compose_child_spec(spec, request(model="expensive-o1"), ["lookup"])

    def test_a_listed_model_is_allowed(self) -> None:
        spec = parent(spawn=SpawnEnvelope(models=("fake-standard", "fake-reasoning")))
        composed = compose_child_spec(spec, request(model="fake-reasoning"), ["lookup"])
        assert composed.model == "fake-reasoning"


class TestTheBrief:
    """A one-line brief is refused at the boundary, not debugged later."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("purpose", "research"),
            ("task", "find prices"),
            ("deliverable", "a list"),
        ],
    )
    def test_a_thin_brief_is_refused(self, field: str, value: str) -> None:
        with pytest.raises(ValidationError, match=field):
            request(**{field: value})

    def test_a_name_that_is_not_a_name_is_refused(self) -> None:
        """The name reaches a Spec, a log and the model's own later calls."""
        with pytest.raises(ValidationError, match="name"):
            request(name="not a name!")

    def test_an_unknown_field_is_refused(self) -> None:
        """`extra="forbid"`: a model inventing an argument gets told, rather
        than having it silently dropped and wondering why nothing happened."""
        with pytest.raises(ValidationError):
            request(escalate=True)


class TestWhatAChildInherits:
    def test_the_task_is_not_in_the_instructions(self) -> None:
        """Two spawns sharing a purpose, deliverable, tool set and model publish
        one Version. The task travels as the child's first message instead."""
        one = compose_child_spec(parent(), request(task="A" * 60), ["lookup"])
        two = compose_child_spec(parent(), request(task="B" * 60), ["lookup"])
        assert one.spec == two.spec

    def test_the_purpose_and_deliverable_are_in_the_instructions(self) -> None:
        composed = compose_child_spec(parent(), request(), ["lookup"])
        assert "Research supplier pricing" in composed.spec.instructions
        assert "A list of supplier, price" in composed.spec.instructions

    def test_a_composed_child_cannot_compose_its_own(self) -> None:
        """An envelope that propagated into agents nobody wrote would be a
        permission that grants itself."""
        composed = compose_child_spec(parent(), request(), ["lookup"])
        assert composed.spec.spawn is None
        assert composed.spec.subagents == ()

    def test_a_composed_child_cannot_stop_and_ask_a_person(self) -> None:
        """Nobody is watching a background child."""
        spec = parent()
        composed = compose_child_spec(spec, request(), ["lookup"])
        assert composed.spec.suspension.may_ask_questions is False

    def test_limits_are_inherited_whole(self) -> None:
        spec = parent(limits=Limits(max_turns=7, max_steps=9))
        composed = compose_child_spec(spec, request(), ["lookup"])
        assert composed.spec.limits.max_turns == 7
        assert composed.spec.limits.max_steps == 9

    def test_mcp_servers_are_not_inherited(self) -> None:
        """The envelope's ceiling is a list of names an author can read. A
        server's catalogue is resolved per turn and can grow tomorrow."""
        composed = compose_child_spec(parent(), request(), ["lookup"])
        assert composed.spec.mcp_servers == ()


class TestCaps:
    def test_the_alive_cap_counts_children_still_running(self) -> None:
        check_alive(2, max_alive=3)
        with pytest.raises(AccessDenied, match="still running"):
            check_alive(3, max_alive=3)

    def test_depth_stays_monotone_for_a_composed_child(self) -> None:
        assert resolve_child_depth(2) == 3
        with pytest.raises(AccessDenied, match="limit is 2"):
            check_depth(resolve_child_depth(2), max_depth=2)


class TestTheToolsOffered:
    def test_no_envelope_means_no_tools(self) -> None:
        spec = parent(spawn=None)
        assert spawn_definition(spec, ["lookup"]) is None
        assert check_definition(spec) is None
        assert message_definition(spec) is None

    def test_the_spawn_tool_advertises_what_a_child_could_hold(self) -> None:
        """A model composing against a menu it does not have composes badly."""
        spec = parent(spawn=SpawnEnvelope(tools=("lookup",)))
        definition = spawn_definition(spec, ["lookup", "refund"])
        assert definition is not None
        assert "lookup" in definition.description
        assert "refund" not in definition.description
        assert definition.input_schema["properties"]["tools"]["items"]["enum"] == ["lookup"]

    def test_messaging_can_be_turned_off(self) -> None:
        spec = parent(spawn=SpawnEnvelope(may_message=False))
        assert message_definition(spec) is None
        assert check_definition(spec) is not None


class TestTheEnvelopeIsPinned:
    """It is a permission, so it belongs in the Version hash.

    An agent allowed to write its own subagents is a different agent from one
    that is not, in exactly the way DESIGN.md §4 means: what it may do changed,
    so what it is changed. A Runtime flag would have let the same Version mean
    two different things on two deployments.
    """

    def test_granting_an_envelope_changes_the_hash(self) -> None:
        assert compute_hash(parent(spawn=None)) != compute_hash(parent(spawn=SpawnEnvelope()))

    def test_widening_the_envelope_changes_the_hash(self) -> None:
        narrow_envelope = parent(spawn=SpawnEnvelope(tools=("lookup",)))
        wide = parent(spawn=SpawnEnvelope(tools=("lookup", "refund")))
        assert compute_hash(narrow_envelope) != compute_hash(wide)

    def test_the_order_it_was_written_in_does_not(self) -> None:
        """Two authors writing the same envelope in a different order wrote the
        same agent, so they must publish one Version rather than two."""
        one = parent(spawn=SpawnEnvelope(tools=("lookup", "refund")))
        two = parent(spawn=SpawnEnvelope(tools=("refund", "lookup")))
        assert compute_hash(one) == compute_hash(two)
