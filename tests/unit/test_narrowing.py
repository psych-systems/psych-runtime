"""Access narrowing.

DESIGN.md §10.5. The properties matter more than the examples here: narrowing
that can widen is a privilege-escalation bug, and it is the kind that survives
review because each individual plane looks right.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from psych_runtime.tools.narrowing import matches_pattern, narrow, tenant_permitted

pytestmark = pytest.mark.unit

CATALOGUE = ["read_orders", "read_customers", "write_order", "delete_order"]


class TestEmptyMeansEverything:
    """The rule most likely to be inverted by someone reading this fresh."""

    def test_an_empty_tenant_list_permits_everything_offered(self) -> None:
        assert tenant_permitted(CATALOGUE, []) == CATALOGUE

    def test_an_empty_spec_grant_takes_everything_the_tenant_permits(self) -> None:
        assert narrow(CATALOGUE, ["read_orders"], []) == ["read_orders"]

    def test_both_empty_gives_the_whole_catalogue(self) -> None:
        assert narrow(CATALOGUE) == CATALOGUE

    def test_an_empty_callable_now_really_does_mean_nothing(self) -> None:
        """Unlike the static planes. This one is a live answer, and 'nothing is
        callable right now' is a real answer that must not be reinterpreted."""
        assert narrow(CATALOGUE, callable_now=[]) == []

    def test_callable_now_of_none_means_no_restriction(self) -> None:
        assert narrow(CATALOGUE, callable_now=None) == CATALOGUE


class TestNarrowingNeverWidens:
    def test_a_spec_cannot_grant_what_the_tenant_revoked(self) -> None:
        """The Spec plane narrows the already-narrowed tenant result, never the
        raw catalogue. Narrowing against the catalogue here would let a Spec
        re-add a revoked tool."""
        result = narrow(CATALOGUE, tenant_allows=["read_orders"], spec_grants=["delete_order"])
        assert result == []

    def test_a_revoked_tool_is_silently_absent_rather_than_an_error(self) -> None:
        """Asking for something you do not have access to just does not get it.
        The caller decides whether that is worth telling anyone about."""
        assert narrow(CATALOGUE, ["read_orders"], ["read_orders", "delete_order"]) == [
            "read_orders"
        ]

    def test_a_stale_tenant_entry_for_a_removed_tool_drops_quietly(self) -> None:
        assert tenant_permitted(["read_orders"], ["read_orders", "gone_tool"]) == ["read_orders"]

    def test_a_spec_cannot_reach_a_tool_the_server_never_offered(self) -> None:
        assert narrow(["read_orders"], [], ["invented_tool"]) == []


class TestPatterns:
    def test_a_trailing_star_is_a_prefix_match(self) -> None:
        assert narrow(CATALOGUE, [], ["read_*"]) == ["read_orders", "read_customers"]

    def test_a_bare_star_matches_everything(self) -> None:
        assert narrow(CATALOGUE, ["*"]) == CATALOGUE

    def test_an_exact_name_matches_only_itself(self) -> None:
        assert narrow(CATALOGUE, [], ["write_order"]) == ["write_order"]

    def test_a_pattern_matching_nothing_yields_nothing(self) -> None:
        assert narrow(CATALOGUE, [], ["nothing_*"]) == []

    def test_a_star_in_the_middle_is_not_a_wildcard(self) -> None:
        """Deliberately not fnmatch. A full glob brings ? and character classes
        and an escaping story, and each is a way for a security-relevant pattern
        to mean something other than what its author read."""
        assert not matches_pattern("read_orders", "read*orders")

    def test_a_pattern_cannot_introduce_a_name_the_plane_above_lacked(self) -> None:
        assert narrow(["read_orders"], ["read_*"], ["read_*"]) == ["read_orders"]


class TestOrdering:
    def test_the_server_order_is_preserved(self) -> None:
        """A report and a prompt both read better when tool order is stable, and
        a prompt whose tool order churns invalidates the provider's cache."""
        assert narrow(CATALOGUE, ["delete_order", "read_orders"]) == [
            "read_orders",
            "delete_order",
        ]


class TestProperties:
    names = st.lists(st.text(alphabet="abcdefgh_", min_size=1, max_size=6), max_size=8, unique=True)

    @given(names, names, names)
    def test_the_result_is_always_a_subset_of_what_the_server_offered(
        self, offers: list[str], tenant: list[str], spec: list[str]
    ) -> None:
        """The invariant. Anything else is privilege escalation."""
        assert set(narrow(offers, tenant, spec)) <= set(offers)

    @given(names, names, names)
    def test_each_plane_only_shrinks(
        self, offers: list[str], tenant: list[str], spec: list[str]
    ) -> None:
        permitted = tenant_permitted(offers, tenant)
        granted = narrow(offers, tenant, spec)
        assert set(granted) <= set(permitted) <= set(offers)

    @given(names, names, names, names)
    def test_adding_a_live_restriction_never_adds_a_tool(
        self, offers: list[str], tenant: list[str], spec: list[str], live: list[str]
    ) -> None:
        without = narrow(offers, tenant, spec)
        with_live = narrow(offers, tenant, spec, callable_now=live)
        assert set(with_live) <= set(without)

    @given(names, names, names)
    def test_narrowing_is_idempotent(
        self, offers: list[str], tenant: list[str], spec: list[str]
    ) -> None:
        """Running the chain over its own output changes nothing. If it did, the
        validator and the runtime would disagree simply by running at different
        times."""
        once = narrow(offers, tenant, spec)
        twice = narrow(once, tenant, spec)
        assert once == twice

    @given(names, names)
    def test_the_result_never_contains_a_duplicate(
        self, offers: list[str], tenant: list[str]
    ) -> None:
        result = narrow(offers, tenant)
        assert len(result) == len(set(result))
