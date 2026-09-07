"""`FilesystemBlobStore` against the shared contract suite and a real temp dir.

DESIGN.md §22's no-mocked-store rule extends here in spirit: this adapter's
whole reason to exist is real file IO, so it is tested against a real,
disposable directory (`tmp_path`) rather than a faked filesystem.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from psych_runtime.store.blob import BlobKey, BlobStore
from psych_runtime.store.blob_contract import BlobStoreContractSuite
from psych_runtime.store.blob_fs import FilesystemBlobStore

pytestmark = pytest.mark.functional


class TestFilesystemBlobStore(BlobStoreContractSuite):
    @pytest.fixture
    def blob_store(self, tmp_path: Path) -> BlobStore:
        return FilesystemBlobStore(tmp_path)


async def test_content_and_metadata_are_separate_real_files(tmp_path: Path) -> None:
    """Not part of the contract suite (it is specific to this adapter's own
    layout), but worth pinning: a consumer inspecting the directory directly
    should find exactly the shape the module docstring promises."""
    store = FilesystemBlobStore(tmp_path)
    key = BlobKey(tenant="acme", run_id="run_1", call_id="call_1")  # type: ignore[arg-type]

    await store.put(key, b"payload bytes", content_type="text/plain", metadata={"k": "v"})

    content_path = tmp_path / "acme" / "run_1" / "call_1"
    meta_path = tmp_path / "acme" / "run_1" / "call_1.meta.json"
    assert content_path.read_bytes() == b"payload bytes"
    assert meta_path.exists()


async def test_a_hostile_tenant_string_is_refused_before_touching_the_filesystem(
    tmp_path: Path,
) -> None:
    """`BlobKey` itself refuses this (see `psych_runtime.store.blob`'s module
    docstring on tenant scoping); pinned here too because a filesystem
    adapter is exactly the place a path-traversal tenant string would do
    real damage if that guard were ever weakened."""
    with pytest.raises(ValueError, match="tenant"):
        BlobKey(tenant="../../etc", run_id="run_1", call_id="call_1")  # type: ignore[arg-type]
