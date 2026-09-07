"""The ``load_skill`` built-in.

DESIGN.md §16. A Skill's description sits in the system prompt as part of the
skills index (``psych_runtime.model.prompt.skills_index``); its body loads only when
the model calls ``load_skill``. This module builds that callable, bound to one
turn's Spec, so a caller can register it into a ``ToolRegistry`` alongside
whatever code tools the consumer registered at boot.

## The ``[[skill:name]]`` link graph is validated elsewhere

``psych_runtime.core.validation._check_skill_graph`` already rejects a dangling
``[[skill:name]]`` link at publish, using the ``SKILL_LINK`` pattern this module
imports rather than redefines. By the time a Spec reaches ``make_load_skill``,
every link in every skill body names a skill that really exists in this Spec:
that is what publish-time validation is for. This module reuses the pattern
only to *find* links so it can mention what a skill leads to, never to decide
whether a link is valid.

## "Already loaded this run" is a courtesy, not a durability claim

A model that lost a skill's body out of its context window needs it back, and
telling it "you already loaded this" without still returning the body would be
useless: the whole reason it asked again is that the first answer is gone. So
``load_skill`` always returns the body and only adds a note when this is a
repeat. ``LoadedSkills`` tracks that in process memory for the life of one
Attempt. It does not need to survive a crash to be correct: the conversation
rebuilt from the log after a reclaim (DESIGN.md §6) still contains every
earlier ``load_skill`` call and its result, so a model that needs the body
again can simply call it again and get it, with or without the reminder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from psych_runtime.core.spec import AgentSpec
from psych_runtime.core.validation import SKILL_LINK

__all__ = ["LOAD_SKILL_DESCRIPTION", "LoadedSkills", "linked_skill_names", "make_load_skill"]

LOAD_SKILL_DESCRIPTION: Final = (
    "Load one skill's full instructions by name. The skills index above gives "
    "you each skill's name and a one-line description; call this before doing "
    "work a skill's description says it covers, rather than guessing at its "
    "content from the description alone."
)


class LoadedSkills:
    """Which skills this Attempt has already loaded.

    Deliberately not part of ``RunStateView``: the reducer is pure and derives
    only from the log (DESIGN.md §6), and what this class remembers changes
    the wording of a hint, never what the model is allowed to do, so it does
    not belong in state the reducer folds.
    """

    def __init__(self) -> None:
        self._loaded: set[str] = set()

    def mark(self, name: str) -> None:
        self._loaded.add(name)

    def __contains__(self, name: str) -> bool:
        return name in self._loaded


def linked_skill_names(body: str) -> tuple[str, ...]:
    """Every ``[[skill:name]]`` a body links to, first appearance order, deduped."""
    seen: list[str] = []
    for match in SKILL_LINK.finditer(body):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def make_load_skill(
    spec: AgentSpec, tracker: LoadedSkills | None = None
) -> Callable[[str], dict[str, Any]]:
    """Build the ``load_skill`` closure for one Spec.

    Args:
        spec: the pinned Spec. Its ``skills`` are the whole catalogue this
            closure can serve; there is no reaching outside it.
        tracker: shared "already loaded" state for this Attempt. A fresh one
            is created when omitted, which is correct for a caller that only
            needs one closure and does not otherwise need to inspect the
            tracker.

    Returns:
        A function taking a skill name and returning JSON-able data: never
        raises on a bad name, because a model choosing among tools by its own
        (possibly wrong) memory of what exists is the expected case, not an
        exceptional one, and the model can only correct course if the failure
        comes back as something it can read (DESIGN.md §18's "failures are
        data", applied here to a lookup rather than to code execution).
    """
    state = tracker if tracker is not None else LoadedSkills()
    by_name = {skill.name: skill for skill in spec.skills}

    def load_skill(name: str) -> dict[str, Any]:
        skill = by_name.get(name)
        if skill is None:
            return {
                "found": False,
                "error": f"no skill named {name!r} is available in this agent.",
                "available_skills": sorted(by_name),
            }

        already_loaded = name in state
        state.mark(name)

        result: dict[str, Any] = {
            "found": True,
            "name": skill.name,
            "body": skill.body,
            "already_loaded": already_loaded,
        }
        if already_loaded:
            result["note"] = (
                f"{name!r} was already loaded earlier this run. Returning it again "
                "in case it dropped out of context."
            )

        linked = linked_skill_names(skill.body)
        if linked:
            result["linked_skills"] = list(linked)
            result["linked_skills_note"] = (
                "This skill links to "
                + ", ".join(repr(link) for link in linked)
                + ". Call load_skill again with one of those names to load it too."
            )
        return result

    return load_skill
