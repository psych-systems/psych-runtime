"""The in-memory BlobStore adapter.

Same role here as `psych_runtime.store.memory.InMemoryStore` plays for `Store` (see that
module's docstring): fast, no cleanup, no isolation concerns, and it proves
nothing about the contract by itself. `psych_runtime.store.blob_contract` is what
proves the contract, run against this adapter and every real one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from psych_runtime.store.blob import BlobKey, BlobMetadata, BlobNotFound

__all__ = ["InMemoryBlobStore"]


class InMemoryBlobStore:
    """A `BlobStore` backed by a plain dict, guarded by one `asyncio.Lock`.

    Implements the `BlobStore` protocol structurally; there is no base class
    to inherit because the port is a `Protocol`.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._blobs: dict[str, tuple[bytes, str, dict[str, str]]] = {}

    async def put(
        self,
        key: BlobKey,
        content: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        async with self._lock:
            self._blobs[str(key)] = (content, content_type, dict(metadata or {}))

    async def get(self, key: BlobKey, *, offset: int = 0, length: int | None = None) -> bytes:
        async with self._lock:
            entry = self._blobs.get(str(key))
            if entry is None:
                raise BlobNotFound(str(key))
            content, _, _ = entry
            end = len(content) if length is None else offset + length
            return content[offset:end]

    async def head(self, key: BlobKey) -> BlobMetadata | None:
        async with self._lock:
            entry = self._blobs.get(str(key))
            if entry is None:
                return None
            content, content_type, metadata = entry
            return BlobMetadata(
                size=len(content), content_type=content_type, metadata=dict(metadata)
            )

    async def delete(self, key: BlobKey) -> None:
        async with self._lock:
            self._blobs.pop(str(key), None)
