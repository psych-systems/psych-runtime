#!/usr/bin/env python
"""Generate the API reference from the library, so it cannot describe a past version.

Documentation drifts because it is written twice: once as a docstring beside the
code, once as a page somebody has to remember to edit. This removes the second
copy. Every symbol on ``psych_runtime.__all__`` is read out of the live module
here -- its signature, its kind, its docstring -- and written as MDX.

The point is not the saved typing. It is that ``scripts/check.sh`` runs this and
fails when the output differs from what is committed, so a pull request that
changes a docstring and not the docs is a red build rather than a page that
quietly starts lying. Nobody has to notice.

What this deliberately does not do is invent prose. It emits what the code says
and nothing else, so a thin docstring produces a thin page. That is honest: the
fix for a thin page is a better docstring, which is also the fix for the reader
who found the symbol through their editor instead.
"""

from __future__ import annotations

import argparse
import enum
import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, cast, get_type_hints

from pydantic import BaseModel

import psych_runtime

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "web" / "site" / "content" / "docs" / "next" / "reference"

GENERATED_BY = "scripts/generate_docs.py"

VALUE_DOCS = {
    "DEFAULT_PRICE_CATALOG_VERSION": (
        "The snapshot date of the bundled default price catalog, in YYYY-MM-DD format."
    ),
}

# Which page each exported name lands on, and in what order the pages read. A
# name absent from every group below fails the run rather than being dropped:
# a new export with no home is a decision somebody has to make, not something
# this script should guess at.
GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "calls",
        "Calls",
        "The functions a consumer calls. This is the whole verb surface.",
        (
            "session",
            "publish",
            "dispatch",
            "stream",
            "stream_text",
            "status",
            "state",
            "answer",
            "thread",
            "report",
            "records",
            "resume",
            "send",
            "interrupt",
            "agent",
            "workflow",
        ),
    ),
    (
        "specs",
        "Spec models",
        "What an agent or workflow is, as data. None of these holds a callable.",
        (
            "Spec",
            "AgentSpec",
            "WorkflowSpec",
            "AgentStep",
            "ToolStep",
            "WorkflowStepRef",
            "ModelRef",
            "Limits",
            "SuspensionPolicy",
            "CompactionPolicy",
            "CodeExecution",
            "CodeExecutionLimits",
            "OutputPolicy",
            "ArtifactPolicy",
            "IsolationLevel",
            "NetworkAccess",
            "WorkspacePolicy",
            "OutputPreservation",
            "ArtifactCollection",
            "SpawnEnvelope",
            "SubagentRef",
            "Skill",
            "CodeTool",
            "HttpTool",
            "McpServer",
            "McpOAuth",
            "A2APeer",
            "Version",
            "ValidationContext",
            "AgentBuilder",
            "WorkflowBuilder",
        ),
    ),
    (
        "runtime",
        "Runtime",
        "What executes a Run, and what you hand it.",
        (
            "Runtime",
            "Worker",
            "Session",
            "Dispatched",
            "ToolRegistry",
            "Scope",
            "McpPool",
            "McpTools",
            "A2APool",
            "A2ATools",
            "HttpTransport",
            "OpenAICompatibleClient",
            "AllowAll",
            "Decision",
            "ToolClass",
            "ResolvedCredential",
            "SandboxLimits",
            "SandboxResult",
            "SandboxFailure",
            "SandboxLimit",
            "SandboxProfile",
            "SandboxProfiles",
            "SandboxDescription",
            "SandboxGuarantees",
            "SandboxArtifact",
            "OutputCapture",
            "Enforcement",
            "CodeExecutionGrant",
            "BindingBudget",
            "HostBinding",
            "BlobKey",
            "RunHeader",
            "RunState",
            "ModelRequest",
            "ModelTimings",
            "ToolDefinition",
            "Memory",
            "MemoryKey",
            "InMemoryStore",
            "InMemoryBlobStore",
            "DEFAULT_PRICES",
            "DEFAULT_PRICE_CATALOG_VERSION",
            "ModelPrice",
            "StaticPriceTable",
            "CostPolicy",
        ),
    ),
    (
        "ports",
        "Ports",
        "The interfaces you implement. Psych Runtime calls back only through these.",
        (
            "Store",
            "BlobStore",
            "MemoryStore",
            "ModelClient",
            "Policy",
            "SecretResolver",
            "Telemetry",
            "Sandbox",
            "CodeExecutionPolicy",
            "PriceResolver",
            "EgressPolicy",
        ),
    ),
    (
        "reading-a-run",
        "Reading a Run",
        "Projections over the log. None is a second copy, so no two can disagree.",
        (
            "RunReport",
            "RunStatus",
            "RunStateView",
            "AnswerView",
            "ThreadView",
            "MessageView",
            "WorkTurn",
            "ToolCallView",
            "TotalsReport",
            "LatencyReport",
            "SubtreeTotals",
            "SubagentReport",
            "StepReport",
            "ToolCallReport",
            "ResultAttachment",
            "ModelCallReport",
            "SuspensionReport",
            "CompactionReport",
            "FailureStreakTrip",
            "PendingApproval",
            "PendingQuestion",
            "AskedQuestion",
            "QuestionOption",
            "Component",
            "Task",
            "TaskStatus",
            "Record",
            "Usage",
            "Cost",
            "Lifecycle",
            "TerminalState",
            "SuspendReason",
            "ToolOutcome",
            "QueueKind",
            "ToolFailure",
            "RunId",
            "VersionHash",
            "AttemptId",
            "StepId",
            "ToolCallId",
            "WorkerId",
        ),
    ),
    (
        "errors",
        "Errors",
        "What is raised, and what each one means about the state you are in.",
        (
            "PsychError",
            "AccessDenied",
            "RunNotFound",
            "RunNotSuspended",
            "RunAlreadySettled",
            "RunEndedWithoutAnswer",
            "RunAborted",
            "RunFailed",
            "SuspensionExpired",
            "SpecValidationError",
            "ValidationIssue",
            "StoreError",
            "SeqConflict",
            "LeaseLost",
            "DeadlineExceeded",
            "TransientError",
            "CorruptLog",
            "CorruptionReason",
            "CredentialNotFound",
            "BuilderError",
        ),
    ),
)


def kind_of(obj: Any) -> str:
    """What a reader needs to know before reading the signature.

    A `Protocol` and a class look identical in a signature and mean opposite
    things: one you implement, the other you construct. Saying which is the
    single most useful word on the page.
    """
    if inspect.isfunction(obj) or inspect.iscoroutinefunction(obj):
        return "function"
    if not isinstance(obj, type):
        return "value"
    checks: tuple[tuple[bool, str], ...] = (
        (issubclass(obj, enum.Enum), "enum"),
        (issubclass(obj, BaseException), "exception"),
        (bool(getattr(obj, "_is_protocol", False)), "protocol"),
        (hasattr(obj, "model_fields"), "model"),
    )
    for matched, label in checks:
        if matched:
            return label
    return "class"


def signature_of(name: str, obj: Any) -> str | None:
    """The call signature, when the symbol has one worth showing."""
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    if isinstance(obj, type):
        prefix = "class "
        rendered = f"{prefix}{name}{signature}"
        return rendered.removesuffix(" -> None")
    return f"{prefix}{name}{signature}"


def first_paragraph(doc: str) -> str:
    """The summary line, which is the only part a listing has room for."""
    for block in doc.strip().split("\n\n"):
        return " ".join(part.strip() for part in block.splitlines()).strip()
    return ""


def fields_of(model: type[BaseModel]) -> list[tuple[str, str, str]]:
    """Name, type and description for a pydantic model's fields."""
    rows: list[tuple[str, str, str]] = []
    try:
        hints = get_type_hints(model)
    except Exception:
        hints = {}
    for field_name, field in model.model_fields.items():
        annotation = hints.get(field_name, field.annotation)
        rendered = getattr(annotation, "__name__", None) or str(annotation)
        rendered = rendered.replace("psych_runtime.core.", "").replace("typing.", "")
        rows.append((field_name, rendered, field.description or ""))
    return rows


def escape(text: str) -> str:
    """MDX reads `{` and `<` as syntax; Psych's docstrings are full of both."""
    return text.replace("{", "\\{").replace("<", "&lt;")


def render_symbol(name: str, obj: Any) -> str:
    lines = [f"### `{name}`", ""]

    kind = kind_of(obj)
    lines.append(f"*{kind}*")
    lines.append("")

    signature = signature_of(name, obj)
    if signature:
        lines += ["```python", signature, "```", ""]

    # Primitive values inherit their type's docstring. Rendering that would make
    # a constant's documentation both misleading and Python-version-dependent.
    doc = VALUE_DOCS.get(name, inspect.getdoc(obj) or "")
    if doc:
        lines += [escape(doc).rstrip(), ""]
    else:
        lines += [
            "_No docstring. That is a bug in the library rather than in this page: "
            "the reader who found this symbol in their editor sees the same gap._",
            "",
        ]

    if kind == "model":
        rows = fields_of(cast("type[BaseModel]", obj))
        if rows:
            lines += ["| Field | Type | Notes |", "|---|---|---|"]
            for field_name, rendered, description in rows:
                summary = escape(first_paragraph(description)) if description else ""
                lines.append(f"| `{field_name}` | `{escape(rendered)}` | {summary} |")
            lines.append("")

    if kind == "enum":
        lines += ["| Member | Value |", "|---|---|"]
        for member in cast("type[enum.Enum]", obj):
            lines.append(f"| `{member.name}` | `{member.value}` |")
        lines.append("")

    return "\n".join(lines)


def render_page(title: str, description: str, names: tuple[str, ...]) -> str:
    header = [
        "---",
        f'title: "{title}"',
        f'description: "{description}"',
        "---",
        "",
        f"{{/* Generated by {GENERATED_BY} from psych_runtime itself. Edit the docstrings. */}}",
        "",
    ]
    body: list[str] = []
    for name in names:
        obj = getattr(psych_runtime, name)
        body.append(render_symbol(name, obj))
    return "\n".join(header) + "\n".join(body)


def unplaced() -> list[str]:
    placed = {name for _, _, _, names in GROUPS for name in names}
    return sorted(
        name for name in psych_runtime.__all__ if name not in placed and name != "__version__"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed pages differ from what the code says",
    )
    args = parser.parse_args()

    missing = unplaced()
    if missing:
        print(
            f"generate_docs: these exports have no page: {missing}\n"
            "Add each to a group in GROUPS. A new public name is a documentation "
            "decision, not something this script should guess at.",
            file=sys.stderr,
        )
        return 1

    REFERENCE.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for slug, title, description, names in GROUPS:
        target = REFERENCE / f"{slug}.mdx"
        target.write_text(render_page(title, description, names), encoding="utf-8")
        written.append(target)

    meta = REFERENCE / "meta.json"
    # "index" first: the folder's own landing page is authored by hand and is
    # what /docs/<version>/reference resolves to. Without it in this list the
    # URL is a 404 that every link to the section walks into.
    pages = ["index", "api", *[slug for slug, _, _, _ in GROUPS]]
    meta.write_text(
        '{\n  "title": "Reference",\n  "pages": ' + str(pages).replace("'", '"') + "\n}\n",
        encoding="utf-8",
    )
    written.append(meta)

    print(f"generate_docs: wrote {len(written)} reference pages from psych_runtime.__all__")

    if args.check:
        diff = subprocess.run(
            ["git", "diff", "--stat", "HEAD", "--", *[str(p) for p in written]],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.stdout.strip():
            print(
                "\ngenerate_docs: the committed reference is out of date with the code.\n"
                "Run `python scripts/generate_docs.py` and commit the result. This is "
                "the check that stops a docstring change from leaving a page describing "
                "the previous version.\n\n" + diff.stdout,
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
