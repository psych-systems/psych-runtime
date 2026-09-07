"""`.agents/skills/` is documentation an agent acts on, so the gate checks it.

A package README that drifts is read by a person who notices. A skill that
drifts is read by a coding agent that does not: it copies the snippet into
somebody else's repository and reports the work as done. That asymmetry is why
these files get mechanical checks and prose documentation does not.

What is checkable here is narrow, and deliberately so. Nothing below judges
whether a skill is *correct* about Psych, which needs a reader. It pins the
three things that can rot silently: the frontmatter a skill is discovered by,
the Python in it being Python at all, and its cross-references naming skills
that exist.

The two skills whose examples must also *run* are covered where running them
belongs, in `tests/e2e/test_readme_example.py`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILLS = Path(__file__).resolve().parents[2] / ".agents" / "skills"
PYTHON_BLOCK = re.compile(r"```python\n(?P<code>.*?)```", re.DOTALL)
CROSS_REFERENCE = re.compile(r"`(psych-[a-z-]+)`")
FRONTMATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n", re.DOTALL)


def skill_files() -> list[Path]:
    found = sorted(SKILLS.glob("*/SKILL.md"))
    assert found, f"no skills found under {SKILLS}"
    return found


def skill_names() -> set[str]:
    return {path.parent.name for path in skill_files()}


@pytest.mark.parametrize("skill", skill_files(), ids=lambda p: p.parent.name)
class TestEverySkillIsWellFormed:
    def test_it_has_frontmatter_naming_itself(self, skill: Path) -> None:
        """The name is how a skill is addressed, so it must match its directory."""
        match = FRONTMATTER.match(skill.read_text(encoding="utf-8"))
        assert match is not None, "a skill without frontmatter is not discoverable"

        name = re.search(r"^name:\s*(\S+)", match.group("yaml"), re.MULTILINE)
        assert name is not None, "frontmatter has no name"
        assert name.group(1) == skill.parent.name, (
            "a skill's name and its directory are two spellings of one address; "
            "they cannot disagree"
        )

    def test_it_has_a_description_worth_triggering_on(self, skill: Path) -> None:
        """The description is the whole triggering mechanism.

        A one-line description reads fine and fires rarely, because nothing in
        it matches how somebody actually phrases the task. There is no correct
        length, so this only rejects the case that is definitely too thin to
        route on.
        """
        match = FRONTMATTER.match(skill.read_text(encoding="utf-8"))
        assert match is not None
        description = re.search(
            r"^description:\s*(?P<body>.*?)(?=\n[a-z_]+:|\Z)",
            match.group("yaml"),
            re.MULTILINE | re.DOTALL,
        )
        assert description is not None, "frontmatter has no description"
        assert len(description.group("body").strip()) >= 200, (
            "this description is too short to route on: say what the skill covers "
            "AND the situations that should reach for it"
        )

    def test_its_python_blocks_are_python(self, skill: Path) -> None:
        """An agent copies these, so a block that does not parse is worse than none."""
        for index, block in enumerate(PYTHON_BLOCK.finditer(skill.read_text(encoding="utf-8"))):
            code = block.group("code")
            try:
                ast.parse(code)
            except SyntaxError as error:
                pytest.fail(
                    f"python block {index} does not parse ({error.msg} on line "
                    f"{error.lineno}). Use a non-python fence for a fragment that "
                    f"is not meant to be valid Python.\n\n{code}"
                )

    def test_its_cross_references_name_skills_that_exist(self, skill: Path) -> None:
        existing = skill_names()
        referenced = set(CROSS_REFERENCE.findall(skill.read_text(encoding="utf-8")))
        assert not (referenced - existing), (
            f"points at skills that do not exist: {sorted(referenced - existing)}"
        )


class TestTheRouterIsAWholeIndex:
    def test_it_names_every_other_skill(self) -> None:
        """A sub-skill the router does not mention is one nothing routes to.

        The router is what an agent reads first and often only; a skill missing
        from its index is discoverable by directory listing alone, which is not
        how any of this gets read.
        """
        router = (SKILLS / "psych" / "SKILL.md").read_text(encoding="utf-8")
        unlisted = sorted(name for name in skill_names() - {"psych"} if f"`{name}`" not in router)
        assert not unlisted, f"the router does not index: {unlisted}"
