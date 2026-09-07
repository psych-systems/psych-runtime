"""What ``psych new`` writes into a fresh project.

The templates are real files rather than strings inside the CLI, for the reason
every worked example in this repository is executed rather than described: a
scaffold that does not run is the first thing a new developer meets, and it
fails for them in a way that looks like their own mistake.
``tests/functional/test_cli.py`` generates each template into a temporary
directory and runs it, so a template that stops working fails the gate rather
than a stranger's afternoon.

They carry a ``.py.template`` suffix so that neither ``ruff`` nor ``mypy``
treats them as part of the package: they are data this package copies, and the
project they land in is not this one. The trade-off is real and accepted -- the
gate cannot lint them, so the test that executes them is the only thing keeping
them honest, which is why that test asserts on their output rather than only on
their exit code.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["TEMPLATES", "render"]

TEMPLATES: tuple[str, ...] = ("minimal", "tour", "fastapi")
"""The scaffolds ``psych new --template`` accepts, in the order it lists them.

Three, because they answer three questions in the order people ask them.
``minimal`` is "does this work at all", and stays short enough to read in one
sitting. ``tour`` is "what else is there", and walks an approval, a skill, a
durable fact, a memoised workflow step and the report. ``fastapi`` is "how do I
put this behind my API", and is the one that shows the split every real
integration makes: a request handler admits a Run, a separate Worker process
executes it, and they share a Store and nothing else.

Three rather than eight because every template is a promise to keep it running,
and a scaffold nobody generates is a scaffold nobody notices breaking. Each of
these is generated and executed by ``tests/functional/test_cli.py``.
"""

SCRIPT_TEMPLATES: tuple[str, ...] = ("minimal", "tour")
"""The ones whose ``main.py`` runs to completion and exits.

``fastapi``'s starts a server and blocks, so it is driven through
``TestClient`` instead. Named here rather than special-cased in the test so the
distinction is a property of the template rather than a fact the test knows.
"""


def render(template: str, *, project: str) -> dict[str, str]:
    """The files one template produces, as ``{relative path: content}``.

    Args:
        template: one of ``TEMPLATES``.
        project: the project's name, substituted wherever a template says
            ``__PROJECT__``. Substitution is a plain string replace rather than
            ``str.format`` or an f-string, because the templates are full of
            braces that belong to Python and escaping every one of them would
            make the files unreadable and unrunnable on their own.

    Raises:
        KeyError: no such template. Raised rather than falling back to a
            default, because silently generating something other than what was
            asked for is worse than refusing.
    """
    if template not in TEMPLATES:
        raise KeyError(template)

    root = resources.files(__package__).joinpath(template)
    rendered: dict[str, str] = {}
    for entry in _walk(root):
        text = entry.read_text(encoding="utf-8")
        rendered[_destination(entry, root)] = text.replace("__PROJECT__", project)
    return rendered


def _walk(root: resources.abc.Traversable) -> list[resources.abc.Traversable]:
    """Every file under ``root``, name-sorted so generation is reproducible."""
    found: list[resources.abc.Traversable] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.is_dir():
            found.extend(_walk(entry))
        else:
            found.append(entry)
    return found


def _destination(entry: resources.abc.Traversable, root: resources.abc.Traversable) -> str:
    """Where one template file lands, with the ``.template`` suffix dropped.

    ``main.py.template`` becomes ``main.py`` and ``README.md`` stays as it is,
    so a template file is named for what it will be rather than for the fact
    that it is a template.
    """
    relative = str(Path(str(entry)).relative_to(Path(str(root))))
    return relative.removesuffix(".template")
