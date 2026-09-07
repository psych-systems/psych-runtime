"""The filesystem BlobStore adapter.

For a consumer running Psych on one machine, or against a shared network
filesystem, without wanting to stand up S3 for what might be a handful of
oversized tool results a day. Every blob is one file; metadata (content type
and the small string bag) is a JSON sidecar next to it, because a bare file has
nowhere else to carry them and a second file is simpler than a custom header
format only this adapter would ever parse.

Layout: `<root>/<tenant>/<run_id>/<call_id>` for content, plus
`<root>/<tenant>/<run_id>/<call_id>.meta.json` alongside it.
`BlobKey.__post_init__` refuses any field that is not a single safe path
segment before this module ever builds a path from it, so tenant isolation
holds even against a hostile tenant string, not just against a well-behaved one
(see `psych_runtime.store.blob`'s module docstring).

Blocking file IO runs through `asyncio.to_thread` rather than a bespoke async
file library: this adapter's whole reason to exist is "no extra infrastructure
to run," and a thread-pool hop is a smaller dependency than an async file
layer for what is, at Psych's scale, an occasional large write.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from psych_runtime.store.blob import BlobKey, BlobMetadata, BlobNotFound

__all__ = ["FilesystemBlobStore"]


class FilesystemBlobStore:
    """A `BlobStore` backed by ordinary files under `root`.

    Implements the `BlobStore` protocol structurally; there is no base class
    to inherit because the port is a `Protocol`. `root` is created if it does
    not already exist; nothing about this adapter provisions retention or
    cleanup for it (DESIGN.md §1, and `psych_runtime.store.blob`'s module docstring):
    that is the consumer's, the same as any other directory or bucket they
    point Psych at.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _content_path(self, key: BlobKey) -> Path:
        return self._root / key.tenant / key.run_id / key.call_id

    def _meta_path(self, key: BlobKey) -> Path:
        return self._content_path(key).with_suffix(".meta.json")

    async def put(
        self,
        key: BlobKey,
        content: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        await asyncio.to_thread(self._put_sync, key, content, content_type, dict(metadata or {}))

    def _put_sync(
        self, key: BlobKey, content: bytes, content_type: str, metadata: dict[str, str]
    ) -> None:
        content_path = self._content_path(key)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_bytes(content)
        self._meta_path(key).write_text(
            json.dumps({"content_type": content_type, "metadata": metadata}), encoding="utf-8"
        )

    async def get(self, key: BlobKey, *, offset: int = 0, length: int | None = None) -> bytes:
        return await asyncio.to_thread(self._get_sync, key, offset, length)

    def _get_sync(self, key: BlobKey, offset: int, length: int | None) -> bytes:
        content_path = self._content_path(key)
        if not content_path.exists():
            raise BlobNotFound(str(key))
        with content_path.open("rb") as handle:
            handle.seek(offset)
            return handle.read() if length is None else handle.read(length)

    async def head(self, key: BlobKey) -> BlobMetadata | None:
        return await asyncio.to_thread(self._head_sync, key)

    def _head_sync(self, key: BlobKey) -> BlobMetadata | None:
        content_path = self._content_path(key)
        meta_path = self._meta_path(key)
        if not content_path.exists() or not meta_path.exists():
            return None
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        return BlobMetadata(
            size=content_path.stat().st_size,
            content_type=raw["content_type"],
            metadata=raw["metadata"],
        )

    async def delete(self, key: BlobKey) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: BlobKey) -> None:
        self._content_path(key).unlink(missing_ok=True)
        self._meta_path(key).unlink(missing_ok=True)
