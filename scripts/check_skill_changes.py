"""Require public behavior changes to update their matching Psych skill.

The mapping is intentionally limited to source files with one clear owning
skill. Cross-cutting changes are still governed by the maintenance rule in the
root ``psych`` skill; this check catches the common omissions without forcing an
unrelated skill edit for every change to a shared module.
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rule:
    sources: tuple[str, ...]
    skill: str


RULES = (
    Rule(
        (
            "psych_runtime/tools/mcp.py",
            "psych_runtime/core/tool_names.py",
            "psych_runtime/tools/deferred.py",
        ),
        ".agents/skills/psych-mcp/SKILL.md",
    ),
    Rule(("psych_runtime/tools/oauth/",), ".agents/skills/psych-mcp-oauth/SKILL.md"),
    Rule(("psych_runtime/model/egress.py",), ".agents/skills/psych-multitenancy/SKILL.md"),
    Rule(
        (
            "psych_runtime/model/pricing.py",
            "psych_runtime/model/default_prices.json",
            "psych_runtime/model/openai_compat.py",
            "psych_runtime/model/port.py",
            "psych_runtime/core/usage.py",
        ),
        ".agents/skills/psych-pricing/SKILL.md",
    ),
    Rule(
        ("psych_runtime/report/build.py", "psych_runtime/report/model.py"),
        ".agents/skills/psych-report/SKILL.md",
    ),
    Rule(("psych_runtime/testing/mcp_stub.py",), ".agents/skills/psych-testing/SKILL.md"),
    Rule(("psych_runtime/tools/large_results.py",), ".agents/skills/psych-blobs/SKILL.md"),
)


def _git(*args: str) -> str:
    return subprocess.run(
        ("git", *args), check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _base(candidate: str | None) -> str | None:
    if candidate and set(candidate) != {"0"}:
        try:
            _git("rev-parse", "--verify", candidate)
        except subprocess.CalledProcessError:
            pass
        else:
            return candidate
    try:
        return _git("rev-parse", "HEAD^")
    except subprocess.CalledProcessError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    args = parser.parse_args()
    base = _base(args.base)
    if base is None:
        return 0
    changed = set(_git("diff", "--name-only", f"{base}...HEAD").splitlines())
    changed.update(_git("diff", "--name-only").splitlines())
    changed.update(_git("diff", "--cached", "--name-only").splitlines())
    changed.update(_git("ls-files", "--others", "--exclude-standard").splitlines())
    missing: list[tuple[str, list[str]]] = []
    for rule in RULES:
        sources = sorted(
            path
            for path in changed
            if any(path == source or path.startswith(source) for source in rule.sources)
        )
        if sources and rule.skill not in changed:
            missing.append((rule.skill, sources))
    if not missing:
        return 0
    for skill, sources in missing:
        print(f"{skill} must be updated because these owned sources changed:")
        for source in sources:
            print(f"  - {source}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
