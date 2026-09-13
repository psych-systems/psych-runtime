"""What a sandboxed program may call, and what one call may cost."""

from __future__ import annotations

from typing import Any

import pytest

from psych_runtime.core.code_execution import (
    BINDABLE_BUILTINS,
    EXCLUDED_BUILTINS,
    BindingBudget,
)
from psych_runtime.core.errors import ProgramToolRefused
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.spec import RESERVED_TOOL_NAMES
from psych_runtime.core.tool_names import mcp_tool_name, qualified_owner
from psych_runtime.tools.bindings import (
    BindableTool,
    BindingCounter,
    alias_for,
    bindable_from_definitions,
    budgeted,
    effective_bindings,
    exclusion_reason,
    handle_descriptor,
    is_handle_descriptor,
    measure_bytes,
)

pytestmark = pytest.mark.unit


def _tool(name: str, origin: Any = "code", server: str | None = None) -> BindableTool:
    return BindableTool(definition=ToolDefinition(name=name), origin=origin, server=server)


class TestWhichBuiltinsAreBindable:
    def test_every_reserved_name_has_been_decided_about(self) -> None:
        """The assertion that stops a new built-in becoming program-callable.

        A tool Psych registers is reachable from a program the moment it
        appears in a turn's resolved set, so "nobody thought about it" would
        mean "yes" by default. Every reserved name has to be in one of the two
        tables, and adding a built-in without a line fails here rather than
        quietly handing programs a new capability.
        """
        decided = BINDABLE_BUILTINS | set(EXCLUDED_BUILTINS)
        assert RESERVED_TOOL_NAMES - decided == set()
        assert decided - RESERVED_TOOL_NAMES == set()

    def test_the_two_tables_do_not_overlap(self) -> None:
        assert BINDABLE_BUILTINS.isdisjoint(EXCLUDED_BUILTINS)

    def test_a_suspending_builtin_is_excluded_and_says_why(self) -> None:
        reason = exclusion_reason("ask_question")
        assert reason is not None
        assert "suspends" in reason

    def test_a_bindable_builtin_is_not_excluded(self) -> None:
        assert exclusion_reason("list_tools") is None
        assert exclusion_reason("get_tool_info") is None

    def test_excluded_builtins_are_dropped_when_the_set_is_built(self) -> None:
        built = bindable_from_definitions(
            [ToolDefinition(name=name) for name in ("ask_question", "run_code", "list_tools")],
            "builtin",
        )
        assert [tool.name for tool in built] == ["list_tools"]


class TestAliases:
    @pytest.mark.parametrize("name", ["search", "find_order", "_private", "a2"])
    def test_a_usable_identifier_gets_one(self, name: str) -> None:
        assert alias_for(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "list-repos",  # MCP names legally contain hyphens
            "find customer",
            "2fa",  # leading digit
            "class",  # keyword
            "match",  # soft keyword
            "docs.search",
            "search/all",
            "__init__",
            "поиск" + "-1",
        ],
    )
    def test_a_name_a_program_cannot_write_gets_none(self, name: str) -> None:
        assert alias_for(name) is None

    def test_a_unicode_identifier_is_still_an_identifier(self) -> None:
        # Python identifiers are not ASCII-only, and pretending otherwise
        # would deny an alias to a perfectly writable name.
        assert alias_for("поиск") == "поиск"

    def test_no_mangling(self) -> None:
        """``list-repos`` must not become ``list_repos``.

        Two servers can offer both, and a mangled alias would make one answer
        for the other -- silently, and only for whichever was aliased second.
        """
        assert alias_for("list-repos") is None


class TestEffectiveBindings:
    def test_none_means_everything_currently_callable(self) -> None:
        bindable = [_tool("lookup"), _tool("github__search", "mcp", "github")]
        effective = effective_bindings(bindable, requested=None)
        assert effective.names == ("github__search", "lookup")
        assert effective.missing == ()

    def test_an_explicit_tuple_narrows(self) -> None:
        bindable = [_tool("lookup"), _tool("refund")]
        effective = effective_bindings(bindable, requested=frozenset({"lookup"}))
        assert effective.names == ("lookup",)

    def test_it_can_never_widen(self) -> None:
        effective = effective_bindings([_tool("lookup")], requested=frozenset({"refund"}))
        assert effective.names == ()
        assert effective.missing == ("refund",)

    def test_a_named_binding_that_vanished_is_reported_not_substituted(self) -> None:
        """A renamed MCP tool must not resolve to its neighbour."""
        bindable = [_tool("github__search_issues_v2", "mcp", "github")]
        effective = effective_bindings(bindable, requested=frozenset({"github__search_issues"}))
        assert effective.names == ()
        assert effective.missing == ("github__search_issues",)

    def test_aliases_are_the_subset_a_program_can_write(self) -> None:
        bindable = [_tool("docs__list-repos", "mcp", "docs"), _tool("lookup")]
        effective = effective_bindings(bindable, requested=None)
        assert effective.names == ("docs__list-repos", "lookup")
        assert effective.aliases == ("lookup",)


class TestQualifiedOwner:
    def test_a_server_qualified_name_names_its_server(self) -> None:
        assert qualified_owner("github__search", ["github", "docs"]) == "github"

    def test_a_name_belonging_to_nobody_is_none(self) -> None:
        assert qualified_owner("lookup", ["github"]) is None
        assert qualified_owner("gitlab__search", ["github"]) is None

    def test_the_longest_owner_wins(self) -> None:
        """``github`` must not answer for a ``github-enterprise`` tool."""
        owners = ["github", "github-enterprise"]
        assert qualified_owner("github-enterprise__search", owners) == "github-enterprise"

    def test_it_matches_the_minted_name_not_the_configured_one(self) -> None:
        """A server called ``records.eu`` owns ``records_eu__find``.

        That is the name the model is shown, so comparing against the alias as
        configured would reject the very name the resolver produced.
        """
        minted = mcp_tool_name("records.eu", "find")
        assert minted.startswith("records_eu__find")
        assert qualified_owner(minted, ["records.eu"]) == "records.eu"

    def test_a_hashed_name_still_names_its_server(self) -> None:
        minted = mcp_tool_name("records", "find customer / by email" * 8)
        assert len(minted) == 64
        assert qualified_owner(minted, ["records"]) == "records"


class TestBudgets:
    async def _counting(self, budget: BindingBudget, counter: BindingCounter | None = None) -> Any:
        calls: list[str] = []

        async def host_call(name: str, arguments: dict[str, Any]) -> Any:
            calls.append(name)
            return arguments.get("echo", "ok")

        return budgeted(host_call, budget, counter), calls

    async def test_it_passes_an_ordinary_call_straight_through(self) -> None:
        call, calls = await self._counting(BindingBudget())
        assert await call("lookup", {"echo": {"qty": 2}}) == {"qty": 2}
        assert calls == ["lookup"]

    async def test_a_program_runs_out_of_calls(self) -> None:
        call, calls = await self._counting(BindingBudget(max_calls=2))
        await call("lookup", {})
        await call("lookup", {})
        with pytest.raises(ProgramToolRefused) as err:
            await call("lookup", {})
        assert err.value.kind == "binding_calls_exhausted"
        assert calls == ["lookup", "lookup"]

    async def test_a_run_runs_out_of_calls_across_programs(self) -> None:
        """A second program does not buy a fresh allowance."""
        counter = BindingCounter()
        budget = BindingBudget(max_calls=10, max_calls_per_run=2)
        first, _ = await self._counting(budget, counter)
        await first("lookup", {})
        await first("lookup", {})
        second, calls = await self._counting(budget, counter)
        with pytest.raises(ProgramToolRefused) as err:
            await second("lookup", {})
        assert err.value.kind == "binding_calls_exhausted_for_run"
        assert calls == []

    async def test_oversized_arguments_are_refused_before_the_call(self) -> None:
        call, calls = await self._counting(BindingBudget(max_argument_bytes=64))
        with pytest.raises(ProgramToolRefused) as err:
            await call("lookup", {"blob": "x" * 500})
        assert err.value.kind == "binding_arguments_too_large"
        assert calls == [], "the call must not have been made"

    async def test_an_oversized_result_is_refused_rather_than_truncated(self) -> None:
        call, _ = await self._counting(BindingBudget(max_result_bytes=64))
        with pytest.raises(ProgramToolRefused) as err:
            await call("lookup", {"echo": "y" * 500})
        assert err.value.kind == "binding_result_too_large"

    async def test_total_traffic_is_bounded(self) -> None:
        call, _ = await self._counting(BindingBudget(max_total_bytes=400))
        await call("lookup", {"echo": "z" * 150})
        with pytest.raises(ProgramToolRefused) as err:
            await call("lookup", {"echo": "z" * 150})
        assert err.value.kind == "binding_traffic_exhausted"

    def test_narrowing_takes_the_smaller_of_every_dimension(self) -> None:
        """Every dimension, including the two that meter the host's own work.

        A tenant policy narrows the whole grant, and the grant carries the
        budget, so a dimension this method forgot would be one a tenant could
        not be held to -- silently, and only for that dimension.
        """
        wide = BindingBudget(
            max_calls=1_000,
            max_total_bytes=10,
            max_produced_bytes=10,
            max_preserved_bytes_per_run=9_999,
        )
        narrow = BindingBudget(
            max_calls=5,
            max_total_bytes=1_000,
            max_produced_bytes=1_000,
            max_preserved_bytes_per_run=7,
        )
        both = wide.narrow(narrow)
        assert both.max_calls == 5
        assert both.max_total_bytes == 10
        assert both.max_produced_bytes == 10
        assert both.max_preserved_bytes_per_run == 7

    def test_every_field_is_narrowed(self) -> None:
        """The assertion that catches a dimension added without a narrowing."""
        import dataclasses

        wide = BindingBudget()
        tight = BindingBudget(**{f.name: 1 for f in dataclasses.fields(BindingBudget)})
        both = wide.narrow(tight)
        assert all(getattr(both, f.name) == 1 for f in dataclasses.fields(BindingBudget))


class TestPeerResultsInAProgram:
    """A2A is bindable, with one thing a program cannot do.

    A peer call does not suspend: it returns a result carrying a task state.
    When that state means the peer wants more input, the *next* step is a
    judgement, and the thing holding it is the model, which is not running
    while the program is. So the program is stopped with the task id rather
    than handed a result it would have to interpret.

    These exercise the two helpers directly. There is no end-to-end
    peer-inside-a-program test; what is covered end to end is the MCP path,
    which shares every other part of the route.
    """

    def test_an_ordinary_answer_passes_through(self) -> None:
        from psych_runtime.runtime.agent import _refuse_if_peer_needs_input

        _refuse_if_peer_needs_input("billing__quote", {"state": "TASK_STATE_COMPLETED"})

    def test_a_peer_asking_for_input_stops_the_program_with_its_task_id(self) -> None:
        from psych_runtime.runtime.agent import _refuse_if_peer_needs_input

        with pytest.raises(ProgramToolRefused) as err:
            _refuse_if_peer_needs_input(
                "billing__quote",
                {"state": "TASK_STATE_INPUT_REQUIRED", "task_id": "task-7"},
            )
        assert err.value.kind == "input_required_in_program"
        assert "task-7" in str(err.value)
        assert "directly" in str(err.value)

    def test_a_state_this_version_does_not_know_is_not_an_error(self) -> None:
        """A peer answering with a state we cannot parse is a reason to let the
        result through, not to fail a call that succeeded."""
        from psych_runtime.runtime.agent import _refuse_if_peer_needs_input

        _refuse_if_peer_needs_input("billing__quote", {"state": "TASK_STATE_FROM_THE_FUTURE"})

    def test_a_model_result_is_dumped_rather_than_stringified(self) -> None:
        """A program branching on `result["state"]` is why that type exists;
        `str(model)` would hand it a repr to parse."""
        from psych_runtime.a2a.models import TaskState
        from psych_runtime.runtime.agent import _json_shaped
        from psych_runtime.tools.a2a import A2ACallResult

        shaped = _json_shaped(
            A2ACallResult(peer="billing", task_id="task-7", state=TaskState.COMPLETED, text="hi")
        )
        assert shaped["task_id"] == "task-7"
        assert shaped["state"] == TaskState.COMPLETED.value
        assert _json_shaped({"already": "json"}) == {"already": "json"}


class TestTheRunAllowanceHasADurableSource:
    """``max_calls_per_run`` is spent by the Run, and the Run is a log.

    The end-to-end proof is in ``tests/e2e/test_code_execution_durability.py``,
    which kills an Attempt and reclaims it. These are the unit-level halves of
    the same property: that a counter with a durable source reads it rather
    than a tally of its own, and that one without still counts.
    """

    async def _call(self, budget: BindingBudget, counter: BindingCounter) -> Any:
        async def host_call(_name: str, _arguments: dict[str, Any]) -> Any:
            return "ok"

        return budgeted(host_call, budget, counter)

    async def test_a_counter_with_a_source_reads_it(self) -> None:
        spent = 0
        counter = BindingCounter(source=lambda: spent)
        call = await self._call(BindingBudget(max_calls_per_run=2), counter)
        # Nothing has been recorded yet, so the allowance is untouched however
        # many times the wrapper has been asked.
        await call("lookup", {})
        assert counter.spent == 0

        spent = 2  # two tool_call_started records have landed
        with pytest.raises(ProgramToolRefused) as err:
            await call("lookup", {})
        assert err.value.kind == "binding_calls_exhausted_for_run"

    def test_a_counter_with_a_source_keeps_no_tally_of_its_own(self) -> None:
        """Two tallies would be two answers, and the log's is the true one."""
        counter = BindingCounter(source=lambda: 7)
        counter.spend_call()
        counter.spend_call()
        assert counter.calls == 0
        assert counter.spent == 7

    def test_a_counter_with_no_source_counts_for_itself(self) -> None:
        counter = BindingCounter()
        counter.spend_call()
        assert counter.spent == 1


class TestHandleDescriptors:
    def test_it_carries_a_name_and_nothing_to_read_with(self) -> None:
        """Everything a BlobStore needs stays on the host. What crosses is a
        handle, which is meaningless outside the Run that minted it."""
        descriptor = handle_descriptor(
            tool="support__export", handle="res_abc", size_bytes=90_000, stored="blob"
        )
        assert descriptor.as_wire() == {
            "handle": "res_abc",
            "tool": "support__export",
            "size_bytes": 90_000,
            "stored": "blob",
        }

    def test_it_is_a_type_and_not_a_shape(self) -> None:
        """The collision this closes: a tool may return any keys it likes.

        A dictionary that looks exactly like a descriptor is a dictionary. The
        distinction is carried by the type here and by the reply frame on the
        wire, so nothing a tool puts in its own result can turn that result
        into a handle.
        """
        assert is_handle_descriptor(
            handle_descriptor(tool="t", handle="h", size_bytes=1, stored="log")
        )
        assert not is_handle_descriptor(
            {"handle": "res_abc", "tool": "t", "size_bytes": 1, "stored": "blob"}
        )
        assert not is_handle_descriptor({"__psych_result_handle__": "customer-data"})
        assert not is_handle_descriptor("res_abc")
        assert not is_handle_descriptor(None)

    def test_a_descriptor_is_small_enough_to_always_fit(self) -> None:
        """It is what a program gets *because* the result did not fit, so a
        descriptor that could itself be refused would be a loop."""
        descriptor = handle_descriptor(
            tool="s" * 200, handle="h" * 256, size_bytes=2**40, stored="blob"
        )
        assert measure_bytes(descriptor.as_wire()) < 1_000
