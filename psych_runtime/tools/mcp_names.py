"""Stable model-facing names for tools contributed by MCP servers."""

from __future__ import annotations

import hashlib
import re
from typing import Final

__all__ = ["MAX_MODEL_TOOL_NAME_LENGTH", "MCP_TOOL_SEPARATOR", "mcp_tool_name"]

MCP_TOOL_SEPARATOR: Final = "__"
MAX_MODEL_TOOL_NAME_LENGTH: Final = 64
_INVALID_MODEL_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Return a stable provider-safe name that retains the MCP server owner."""
    original = f"{server_name}{MCP_TOOL_SEPARATOR}{tool_name}"
    safe = _INVALID_MODEL_TOOL_NAME.sub("_", original)
    if safe == original and len(safe) <= MAX_MODEL_TOOL_NAME_LENGTH:
        return safe
    digest = hashlib.sha256(original.encode()).hexdigest()[:10]
    prefix_length = MAX_MODEL_TOOL_NAME_LENGTH - len(digest) - 1
    return f"{safe[:prefix_length]}_{digest}"
