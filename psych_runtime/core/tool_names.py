"""Stable model-facing names for tools contributed by MCP servers.

In ``psych_runtime.core`` because both sides of the layering need it and
neither may import the other (DESIGN.md §21): ``psych_runtime.tools`` mints
these names when it resolves a catalogue, and ``psych_runtime.core.spec``
reads them when it decides whether a ``code_execution.bindings`` entry names a
dynamically discovered tool rather than a typo. A copy of the rule in the Spec
would be a copy that drifts, and drift here means a Spec refused at publish for
naming a binding that is perfectly valid at run time.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "MAX_MODEL_TOOL_NAME_LENGTH",
    "MCP_TOOL_SEPARATOR",
    "mcp_tool_name",
    "qualified_owner",
]

MCP_TOOL_SEPARATOR: Final = "__"
MAX_MODEL_TOOL_NAME_LENGTH: Final = 64
_DIGEST_CHARS: Final = 10
_INVALID_MODEL_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Return a stable provider-safe name that retains the MCP server owner."""
    original = f"{server_name}{MCP_TOOL_SEPARATOR}{tool_name}"
    safe = _INVALID_MODEL_TOOL_NAME.sub("_", original)
    if safe == original and len(safe) <= MAX_MODEL_TOOL_NAME_LENGTH:
        return safe
    digest = hashlib.sha256(original.encode()).hexdigest()[:_DIGEST_CHARS]
    prefix_length = MAX_MODEL_TOOL_NAME_LENGTH - len(digest) - 1
    return f"{safe[:prefix_length]}_{digest}"


def qualified_owner(name: str, owners: Iterable[str]) -> str | None:
    """Which of ``owners`` a model-facing tool name is addressed to, if any.

    The inverse of ``mcp_tool_name`` as far as it can be inverted: the raw tool
    name is not recoverable from a name that was hashed and cut, and does not
    need to be, because the only question asked offline is "does this name
    belong to something this Spec connects to". Answering that without a
    server is what lets ``code_execution.bindings`` name an MCP tool at publish
    time without publication depending on a server being up.

    Matching is on the *minted* prefix rather than the configured one, so a
    server called ``records.eu`` owns ``records_eu__find``: that is the name
    the model is shown, and comparing against the unsanitised alias would
    reject the very name the resolver produced. The longest owner wins, so
    ``github`` does not answer for a name belonging to ``github-enterprise``.

    Returns:
        The owner's configured name, or ``None`` when the name is not
        addressed to any of them.
    """
    head_length = MAX_MODEL_TOOL_NAME_LENGTH - _DIGEST_CHARS - 1
    for owner in sorted(owners, key=len, reverse=True):
        prefix = _INVALID_MODEL_TOOL_NAME.sub("_", f"{owner}{MCP_TOOL_SEPARATOR}")
        if name.startswith(prefix):
            return owner
        # An owner whose own name is longer than the space a hashed name
        # leaves has its prefix cut short too; all that survives of the owner
        # is the head, and a full-length name is the only shape that can be.
        if len(name) == MAX_MODEL_TOOL_NAME_LENGTH and name.startswith(prefix[:head_length]):
            return owner
    return None
