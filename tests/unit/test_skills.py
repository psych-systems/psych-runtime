"""The ``load_skill`` built-in and its data-not-exception contract.

DESIGN.md §16. Descriptions sit in the system prompt as an index; bodies load
only on ``load_skill``. The invariant under test here is what happens at the
edges: an unknown name, a repeat load, and a body that links to other skills.
"""

from __future__ import annotations

from typing import Any

import pytest

from psych_runtime.core.spec import AgentSpec, ModelRef, Skill
from psych_runtime.model.prompt import skills_index
from psych_runtime.tools.skills import LoadedSkills, linked_skill_names, make_load_skill

pytestmark = pytest.mark.unit


def agent(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {"name": "support", "model": ModelRef(model="gpt-4o")}
    base.update(kwargs)
    return AgentSpec(**base)


def skill(name: str, description: str = "does a thing", body: str = "Do the thing.") -> Skill:
    return Skill(name=name, description=description, body=body)


class TestLoadingAKnownSkill:
    def test_returns_the_body(self) -> None:
        spec = agent(skills=(skill("refunds", body="Full refund instructions here."),))
        load_skill = make_load_skill(spec)

        result = load_skill("refunds")

        assert result["found"] is True
        assert result["name"] == "refunds"
        assert result["body"] == "Full refund instructions here."

    def test_first_load_is_not_flagged_as_a_repeat(self) -> None:
        spec = agent(skills=(skill("refunds"),))
        load_skill = make_load_skill(spec)

        result = load_skill("refunds")

        assert result["already_loaded"] is False
        assert "note" not in result

    def test_a_second_load_says_so_but_still_returns_the_body(self) -> None:
        """A model that lost the body from context needs it back, so the
        repeat still returns the full body rather than just the reminder."""
        spec = agent(skills=(skill("refunds", body="Full refund instructions."),))
        load_skill = make_load_skill(spec)

        load_skill("refunds")
        result = load_skill("refunds")

        assert result["already_loaded"] is True
        assert result["body"] == "Full refund instructions."
        assert "already loaded" in result["note"]

    def test_a_shared_tracker_persists_across_separate_closures(self) -> None:
        """Two turns build two closures over the same Spec; the tracker is what
        makes the second one know the first already happened."""
        spec = agent(skills=(skill("refunds"),))
        tracker = LoadedSkills()
        make_load_skill(spec, tracker)("refunds")

        result = make_load_skill(spec, tracker)("refunds")

        assert result["already_loaded"] is True


class TestAnUnknownSkill:
    def test_is_data_not_an_exception(self) -> None:
        spec = agent(skills=(skill("refunds"),))
        load_skill = make_load_skill(spec)

        result = load_skill("nonexistent")

        assert result["found"] is False
        assert "nonexistent" in result["error"]

    def test_names_the_available_skills_so_the_model_can_act_on_it(self) -> None:
        spec = agent(skills=(skill("refunds"), skill("returns")))
        load_skill = make_load_skill(spec)

        result = load_skill("nonexistent")

        assert result["available_skills"] == ["refunds", "returns"]

    def test_no_skills_at_all_gives_an_empty_available_list_not_an_error(self) -> None:
        spec = agent()
        load_skill = make_load_skill(spec)

        result = load_skill("anything")

        assert result["found"] is False
        assert result["available_skills"] == []


class TestLinkedSkills:
    def test_a_body_with_no_links_mentions_none(self) -> None:
        spec = agent(skills=(skill("refunds", body="No links here."),))
        load_skill = make_load_skill(spec)

        result = load_skill("refunds")

        assert "linked_skills" not in result

    def test_a_body_that_links_names_the_linked_skill(self) -> None:
        spec = agent(
            skills=(
                skill("refunds", body="See also [[skill:returns]] for the policy."),
                skill("returns", body="The return policy."),
            )
        )
        load_skill = make_load_skill(spec)

        result = load_skill("refunds")

        assert result["linked_skills"] == ["returns"]
        assert "returns" in result["linked_skills_note"]

    def test_multiple_links_are_all_named_in_first_appearance_order(self) -> None:
        body = "See [[skill:b]] then [[skill:a]] then [[skill:b]] again."
        assert linked_skill_names(body) == ("b", "a")

    def test_an_index_skill_whose_body_is_mostly_links_still_works(self) -> None:
        """DESIGN.md §16: index skills and multi-load must both work."""
        spec = agent(
            skills=(
                skill(
                    "playbooks",
                    body="Available playbooks: [[skill:refund-playbook]], "
                    "[[skill:return-playbook]].",
                ),
                skill("refund-playbook", body="Refund steps."),
                skill("return-playbook", body="Return steps."),
            )
        )
        load_skill = make_load_skill(spec)

        result = load_skill("playbooks")

        assert set(result["linked_skills"]) == {"refund-playbook", "return-playbook"}


class TestTheSkillsIndex:
    """``psych_runtime.model.prompt.skills_index`` puts descriptions in the system
    prompt and never bodies (DESIGN.md §16, §19's cache-deliberate ordering).
    Asserted here because it is the other half of the contract this module's
    ``load_skill`` is the second half of: the index tells the model a skill
    exists, ``load_skill`` is how it reads one."""

    def test_contains_every_skill_name_and_description(self) -> None:
        skills = (skill("refunds", description="How to process a refund."),)

        index = skills_index(skills)

        assert "refunds" in index
        assert "How to process a refund." in index

    def test_never_contains_a_skill_body(self) -> None:
        secret_body = "SECRET-INTERNAL-REFUND-STEPS-12345"
        skills = (skill("refunds", body=secret_body),)

        index = skills_index(skills)

        assert secret_body not in index

    def test_no_skills_gives_an_empty_index(self) -> None:
        assert skills_index(()) == ""
