"""The README's example, extracted from the README and executed.

`tests/e2e/test_documented_example.py` already does this for `docs/api.md`, and
the reasoning is the same one: a worked example that does not run is a lie with
a long half-life, because it is the first thing a new consumer copies and it
fails for them in a way that looks like their own mistake.

The README's example gets its own file because it is doing a different job. The
one in `docs/api.md` is a real integration with Postgres and a real provider,
transcribed into a test with two substitutions. This one is already meant to run
as-is, with no substitutions at all, which is what makes running it *verbatim*
the whole point: the block is read out of the README, written to a file, and
executed as a subprocess exactly as a reader would.

A subprocess rather than an `exec` because the snippet ends in `asyncio.run`,
and a reader runs it as a script rather than inside somebody else's event loop.
Testing it the way it is used is what makes the test meaningful.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

README = Path(__file__).resolve().parents[2] / "README.md"
# The heading, then whatever prose introduces the block, then the first python
# fence under it. The prose may change; the heading is the contract.
BLOCK = re.compile(r"## What it looks like\n.*?```python\n(?P<code>.*?)```", re.DOTALL)


def readme_example() -> str:
    match = BLOCK.search(README.read_text(encoding="utf-8"))
    assert match is not None, (
        "the README's '## What it looks like' python block has moved or been renamed; this "
        "test cannot check an example it cannot find, so fix the pattern rather "
        "than deleting the test"
    )
    return match.group("code")


class TestTheReadmeExampleRuns:
    def test_it_executes_and_prints_the_answer(self, tmp_path: Path) -> None:
        script = tmp_path / "readme_example.py"
        script.write_text(readme_example(), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, (
            f"the README example failed to run.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "A1 has shipped." in result.stdout

    def test_the_router_skills_example_runs_too(self, tmp_path: Path) -> None:
        """`.agents/skills/psych/SKILL.md` is what a coding agent reads first.

        Its example has the same job as the README's and a wider blast radius:
        an agent that copies a broken snippet writes broken code into somebody
        else's repository and reports it as done.
        """
        skill = README.parent / ".agents" / "skills" / "psych" / "SKILL.md"
        blocks = re.findall(r"```python\n(.*?)```", skill.read_text(encoding="utf-8"), re.DOTALL)
        assert blocks, "the router skill has no python example left to check"

        script = tmp_path / "router_example.py"
        script.write_text(blocks[0], encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, (
            f"the router skill's example failed to run.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        assert "A1 has shipped." in result.stdout

    def test_it_reaches_only_for_names_the_readme_shows(self) -> None:
        """No hidden import the reader would have to guess at.

        The block imports `asyncio`, `psych` and the fake model, and nothing
        else. A snippet that quietly needed a fourth import would still pass the
        test above, because this file writes the whole block out; it would fail
        for a reader who copied only the part that looked like the example.
        """
        imports = {
            line.strip()
            for line in readme_example().splitlines()
            if line.startswith(("import ", "from "))
        }
        assert imports == {
            "import asyncio",
            "import psych_runtime",
            "from psych_runtime.testing.fake_model import FakeModel",
        }
