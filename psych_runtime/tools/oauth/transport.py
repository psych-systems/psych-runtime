"""The outbound HTTP capability this package needs, without importing
``psych_runtime.model``.

Structurally, not nominally, typed against ``psych_runtime.model.egress.HttpTransport``:
``psych_runtime.tools`` and ``psych_runtime.model`` sit in the same import-linter layer and
neither may import the other (see ``pyproject.toml``), so this module cannot
name that class directly. ``psych_runtime.tools.mcp`` and ``psych_runtime.tools.http`` each
declare their own copy of this same Protocol for the same reason; this is
this package's copy. The runtime layer, which sits above both ``psych_runtime.tools``
and ``psych_runtime.model``, constructs the real ``HttpTransport`` and passes it in
here, satisfying this Protocol by shape.

Every discovery, registration, and token request this package makes goes
through whatever satisfies this Protocol, so DESIGN.md §14's one-egress-seam
rule holds for OAuth the same way it holds for MCP and HTTP tool calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from psych_runtime.core.scope import Scope

__all__ = ["OAuthTransport"]


@runtime_checkable
class OAuthTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response: ...
