"""The layout rules from DESIGN.md §21, asserted rather than remembered.

import-linter enforces the dependency direction in CI. These tests cover the
things it does not: that every package exists, is importable, and carries the
README §22 requires.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

PACKAGES = [
    "a2a",
    "core",
    "runtime",
    "tools",
    "model",
    "store",
    "sandbox",
    "memory",
    "telemetry",
    "report",
    "builder",
    "testing",
]


@pytest.mark.unit
@pytest.mark.parametrize("name", PACKAGES)
def test_package_is_importable(name: str) -> None:
    module = importlib.import_module(f"psych_runtime.{name}")
    assert module.__doc__, f"psych_runtime.{name} must say what it owns in its docstring"


@pytest.mark.unit
@pytest.mark.parametrize("name", PACKAGES)
def test_package_documents_itself(name: str) -> None:
    """A README that does not match its code is a bug (DESIGN.md §22)."""
    readme = Path(__file__).resolve().parents[2] / "psych_runtime" / name / "README.md"
    assert readme.is_file(), f"psych_runtime/{name} has no README.md"
    text = readme.read_text(encoding="utf-8")
    assert "## Owns" in text, f"psych_runtime/{name}/README.md must say what it owns"
    assert "## Does not own" in text, f"psych_runtime/{name}/README.md must say what it refuses"


@pytest.mark.unit
def test_psych_is_typed() -> None:
    """Type hints everywhere means shipping the marker that says so."""
    marker = Path(__file__).resolve().parents[2] / "psych_runtime" / "py.typed"
    assert marker.is_file(), "psych/py.typed is missing, so consumers get no types"
