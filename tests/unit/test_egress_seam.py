"""One seam, and nothing goes around it.

DESIGN.md §14: all outbound HTTP from tools, MCP clients and the model client
goes through one seam, so a consumer-supplied egress policy covers every path
*including paths added later*. And the sentence that makes this test worth
writing:

> A control that covers three of four routes is worse than none, because it
> will be believed.

Every executor has its own tests proving it uses the transport it was given.
None of them would catch a *new* executor that constructed an ``httpx.Client``
of its own, because a new file has no test until someone writes one. This is the
standing check that fails when that happens, and it works by reading the source
rather than by exercising a path, which is the only way to catch code nobody
called yet.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

_PACKAGE: Final = Path(__file__).resolve().parents[2] / "psych_runtime"

_SEAM_MODULE: Final = "psych_runtime/model/egress.py"
"""The one file allowed to construct an HTTP client. Everything else takes a
transport as an argument."""

_HTTP_CLIENT_NAMES: Final = frozenset(
    {
        "AsyncClient",
        "Client",
        "AsyncHTTPTransport",
        "HTTPTransport",
    }
)
"""Constructing any of these is constructing a route out of the process."""

_FORBIDDEN_MODULES: Final = frozenset(
    {
        "aiohttp",
        "requests",
        "urllib3",
        "http.client",
    }
)
"""Libraries nothing in Psych has any business touching.

``httpx`` is deliberately absent. It is the seam's own library, and a module that
catches ``httpx.ConnectError`` or annotates an ``httpx.Response`` has to import
it. Forbidding the import outright would push those modules into catching bare
``Exception``, which is worse than the thing being prevented. What matters is
that nothing outside the seam *constructs a client*, and that is checked
separately and precisely.

``urllib.request`` is also absent, because ``urllib.parse`` is a legitimate and
common import and the two share a prefix. Constructing an opener from it would
be caught by the client-construction check.
"""


def _python_files() -> list[Path]:
    return sorted(path for path in _PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(_PACKAGE.parent).as_posix()


class TestNothingBypassesTheSeam:
    def test_nothing_reaches_for_another_http_library(self) -> None:
        """One library, one seam.

        A module that pulls in aiohttp or requests has decided to make its own
        way out of the process, and no egress policy written against the seam
        will see it.
        """
        offenders: list[str] = []

        for path in _python_files():
            relative = _relative(path)
            if relative == _SEAM_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                            offenders.append(f"{relative}: import {alias.name}")
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] in _FORBIDDEN_MODULES
                ):
                    offenders.append(f"{relative}: from {node.module} import ...")

        assert not offenders, (
            "these modules reach for an HTTP library other than the seam's own "
            "(DESIGN.md §14):\n  " + "\n  ".join(offenders)
        )

    def test_only_the_seam_constructs_an_http_client(self) -> None:
        """Belt and braces: a module could import the symbol under an alias.

        Checking the call as well as the import means renaming the import does
        not slip past.
        """
        offenders: list[str] = []

        for path in _python_files():
            relative = _relative(path)
            if relative == _SEAM_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node.func)
                if name in _HTTP_CLIENT_NAMES:
                    offenders.append(f"{relative}:{node.lineno}: {name}(...)")

        assert not offenders, (
            "these lines construct an HTTP client outside the egress seam "
            "(DESIGN.md §14):\n  " + "\n  ".join(offenders)
        )

    def test_the_seam_itself_still_exists_where_this_test_expects(self) -> None:
        """A guard that points at a moved file passes forever and guards nothing."""
        assert (_PACKAGE.parent / _SEAM_MODULE).is_file(), (
            f"{_SEAM_MODULE} is gone or moved. If the seam moved, move this test's "
            "expectation with it; do not delete the check."
        )

    def test_the_check_would_actually_catch_a_bypass(self) -> None:
        """A test whose failure mode has never been observed is a test nobody
        should trust. This runs the same walk over a module that does bypass the
        seam and asserts it is caught."""
        source = "import aiohttp\n\n\nasync def fetch(url: str) -> str:\n    return url\n"
        tree = ast.parse(source)
        found = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.split(".")[0] in _FORBIDDEN_MODULES
        ]
        assert found == ["aiohttp"]

        call_source = "client = httpx.AsyncClient()\n"
        calls = [
            _called_name(node.func)
            for node in ast.walk(ast.parse(call_source))
            if isinstance(node, ast.Call)
        ]
        assert "AsyncClient" in calls


def _called_name(func: ast.expr) -> str | None:
    """The bare name of whatever is being called, through an attribute chain."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None
