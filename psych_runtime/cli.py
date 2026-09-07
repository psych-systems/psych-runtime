"""``psych``: the command line, and the three things it is allowed to do.

A library's first hour decides whether there is a second one, and Psych's was
spent assembling. ``psych new`` writes a project that runs on the next command
with no API key and no database; ``psych skills install`` gives a coding agent
the 26 guides it needs to write correct Psych code; ``psych doctor`` says why
nothing works, which is the question a stuck developer actually has.

## What this is not, and must never become

DESIGN.md §1 refuses an HTTP server, a scheduler, a UI and the rest, and a
command line is where refused things get smuggled back in one flag at a time:
``psych serve``, ``psych run``, ``psych worker``. So the rule is narrower than
"do not add commands", because that one bends. **This module never executes a
Run.** It writes files, copies files, and reports what is installed. It imports
``psych`` to read its version and to check what an install can reach, and it
calls nothing that touches a Store, a Worker or a model.

A consumer running Psych in production runs *their* process, calling
``psych_runtime.dispatch()`` from their own scheduler. The moment this file can start a
Worker, Psych has grown an operational surface it would then have to keep
working, version, and answer questions about, and the library has become a
platform (§1's actual test). ``psych new`` writes a ``main.py`` the developer
owns and runs themselves, and that indirection is the whole point rather than
an inconvenience.

## Why the standard library

Psych's dependency floor is pydantic and httpx. ``click`` and ``typer`` are
better argument parsers and neither is worth adding to the install of a library
whose value is that it stays out of a consumer's dependency tree.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path

from psych_runtime import __version__
from psych_runtime.templates import TEMPLATES, render

__all__ = ["main"]

SKILLS_PACKAGE_DIR = "_skills"
"""Where the wheel keeps ``.agents/skills``.

The canonical copy lives at ``.agents/skills/`` in the repository, so this
repository's own agents find it without a build step, and ``pyproject.toml``
force-includes it here at build time. One copy, two locations, and
``_skills_root`` below is the only code that has to know that.
"""


# ---------------------------------------------------------------------------
# psych new
# ---------------------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> int:
    """Write a project that runs on the next command."""
    destination = Path(args.name).resolve()
    project = destination.name

    if destination.exists() and any(destination.iterdir()) and not args.force:
        _fail(
            f"{destination} already exists and is not empty. Pass --force to write into it anyway."
        )
        return 1

    try:
        files = render(args.template, project=project)
    except KeyError:
        _fail(f"unknown template {args.template!r}. Available: {', '.join(TEMPLATES)}")
        return 1

    for relative, content in sorted(files.items()):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  created {path.relative_to(Path.cwd()) if _under_cwd(path) else path}")

    print(f"\n{project} is ready. It runs with no API key:\n")
    print(f"    cd {args.name}")
    print("    python main.py\n")
    print("Set OPENAI_API_KEY to point the same file at a real provider.")
    print("`psych skills install` gives your coding agent 26 guides to this library.")
    return 0


def _under_cwd(path: Path) -> bool:
    return path.is_relative_to(Path.cwd())


# ---------------------------------------------------------------------------
# psych skills
# ---------------------------------------------------------------------------


def _skills_root() -> Path | None:
    """Where the skills are on this machine, installed or in a source checkout.

    Returns ``None`` rather than raising when neither exists, because a wheel
    built without them is a packaging bug this command should report in words
    rather than a traceback.
    """
    installed = Path(str(resources.files("psych_runtime").joinpath(SKILLS_PACKAGE_DIR)))
    if installed.is_dir():
        return installed

    # An editable install or a source checkout: the canonical copy, four levels
    # up from this file.
    source = Path(__file__).resolve().parent.parent / ".agents" / "skills"
    return source if source.is_dir() else None


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(child for child in root.iterdir() if (child / "SKILL.md").is_file())


def cmd_skills_list(args: argparse.Namespace) -> int:
    _ = args
    root = _skills_root()
    if root is None:
        _fail("no skills found in this installation.")
        return 1

    skills = _skill_dirs(root)
    print(f"{len(skills)} skills in {root}\n")
    for skill in skills:
        print(f"  {skill.name:24s} {_summary_of(skill / 'SKILL.md')}")
    return 0


def _summary_of(skill_md: Path) -> str:
    """The first sentence of a skill's description, for a one-line listing."""
    text = skill_md.read_text(encoding="utf-8")
    marker = "description: >-"
    if marker not in text:
        return ""
    body = text.split(marker, 1)[1]
    words = " ".join(line.strip() for line in body.splitlines() if line.strip())
    sentence = words.split(". ")[0]
    return sentence[:96] + ("..." if len(sentence) > 96 else "")


def cmd_skills_install(args: argparse.Namespace) -> int:
    """Copy the skills into a consumer's own repository."""
    root = _skills_root()
    if root is None:
        _fail(
            "no skills found in this installation. This is a packaging bug: "
            "report it rather than working around it."
        )
        return 1

    destination = Path(args.dest).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    copied, skipped = 0, 0
    for skill in _skill_dirs(root):
        target = destination / skill.name
        if target.exists() and not args.force:
            skipped += 1
            continue
        shutil.copytree(skill, target, dirs_exist_ok=True)
        copied += 1

    print(f"installed {copied} skills into {destination}")
    if skipped:
        print(f"skipped {skipped} that already existed; pass --force to overwrite")
    if copied:
        print("\nYour coding agent will pick them up. Start it at the `psych` skill.")
    return 0


# ---------------------------------------------------------------------------
# psych doctor
# ---------------------------------------------------------------------------

_EXTRAS: tuple[tuple[str, str, str], ...] = (
    ("asyncpg", "postgres", "PostgresStore"),
    ("aiomysql", "mysql", "MySQLStore"),
    ("aioboto3", "dynamodb", "DynamoDBStore and S3BlobStore"),
    ("opentelemetry", "otel", "OTelTelemetry"),
)

_ENV: tuple[tuple[str, str], ...] = (
    ("OPENAI_API_KEY", "a real model provider"),
    ("PSYCH_BASE_URL", "a non-default OpenAI-compatible endpoint"),
    ("PSYCH_MODEL", "which model a scaffolded project names"),
)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what is installed and configured, and nothing else.

    Deliberately makes no network call and opens no database connection. A
    doctor that hangs on an unreachable DSN is worse than one that says which
    DSN it would have used, and a consumer's egress policy has not been
    consulted here (there is no ``Scope`` to consult it with).
    """
    _ = args
    # No Python version check: `requires-python = ">=3.12"` means an installer
    # already refused anything older, so a check here could only ever pass, and
    # a green tick nobody could fail teaches a reader to trust the other ticks
    # less.
    print(f"psych {__version__} on Python {sys.version.split()[0]} ({sys.executable})")

    print("\nOptional adapters")
    for module, extra, what in _EXTRAS:
        available = importlib.util.find_spec(module) is not None
        mark = "✓" if available else "·"
        note = what if available else f"install with: pip install 'psych-runtime[{extra}]'"
        print(f"  {mark} {module:16s} {note}")

    print("\nEnvironment")
    for name, what in _ENV:
        value = os.getenv(name)
        shown = "set" if value else "unset"
        print(f"  {'✓' if value else '·'} {name:16s} {shown} ({what})")

    root = _skills_root()
    print("\nAgent skills")
    if root is None:
        print("  ✗ not found in this installation")
    else:
        print(f"  ✓ {len(_skill_dirs(root))} available, from {root}")
    local = Path.cwd() / ".agents" / "skills"
    if local.is_dir():
        print(f"  ✓ {len(_skill_dirs(local))} installed in this project")
    else:
        print("  · none in this project: psych skills install")

    print("\nNothing above opened a socket or a database connection.")
    print("Next: psych new demo && cd demo && python main.py")
    return 0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _fail(message: str) -> None:
    print(f"psych: {message}", file=sys.stderr)


def _help_for(parser: argparse.ArgumentParser) -> Callable[[argparse.Namespace], int]:
    """What a command with subcommands does when given none.

    Printing help beats argparse's default of a bare usage line, because
    ``psych skills`` on its own is somebody looking for the subcommand names.
    """

    def show(args: argparse.Namespace) -> int:
        _ = args
        parser.print_help()
        return 0

    return show


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="psych",
        description=(
            "Developer tooling for the Psych agent runtime. "
            "It scaffolds and reports; it never runs an agent."
        ),
    )
    parser.add_argument("--version", action="version", version=f"psych {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    new = sub.add_parser("new", help="scaffold a project that runs with no API key")
    new.add_argument("name", help="directory to create")
    new.add_argument(
        "--template",
        default="minimal",
        choices=TEMPLATES,
        help="minimal is one agent and one tool; tour walks through the features",
    )
    new.add_argument("--force", action="store_true", help="write into a non-empty directory")
    new.set_defaults(func=cmd_new)

    skills = sub.add_parser("skills", help="the agent-facing guides to this library")
    skills_sub = skills.add_subparsers(dest="skills_command", metavar="<subcommand>")

    install = skills_sub.add_parser("install", help="copy them into this project")
    install.add_argument("--dest", default=".agents/skills", help="where to write them")
    install.add_argument("--force", action="store_true", help="overwrite existing skills")
    install.set_defaults(func=cmd_skills_install)

    listing = skills_sub.add_parser("list", help="what is available")
    listing.set_defaults(func=cmd_skills_list)
    skills.set_defaults(func=_help_for(skills))

    doctor = sub.add_parser("doctor", help="what is installed and configured, and what is not")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    result = args.func(args)
    return int(result)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
