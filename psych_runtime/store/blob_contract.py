"""The BlobStore contract suite.

Same shape and same reason as `psych_runtime.store.contract` for `Store`: three
implementations with no shared test is three divergent behaviours discovered
in production. Not itself collected by pytest (no `blob_store` fixture, and
its class name does not start with `Test`); an adapter is added to the suite
by subclassing `BlobStoreContractSuite` and registering a `blob_store` fixture
that returns a fresh instance of the adapter under test.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.ids import RunId, ToolCallId, new_run_id, new_tool_call_id
from psych_runtime.store.blob import BlobKey, BlobNotFound, BlobStore

__all__ = ["BlobStoreContractSuite"]


def _key(
    tenant: str = "acme", run_id: RunId | None = None, call_id: ToolCallId | None = None
) -> BlobKey:
    return BlobKey(
        tenant=tenant,
        run_id=run_id if run_id is not None else new_run_id(),
        call_id=call_id if call_id is not None else new_tool_call_id(),
    )


class BlobStoreContractSuite:
    """Subclass this and provide a `blob_store` fixture yielding an empty
    `BlobStore`. Every test method is a coroutine; `pytest-asyncio` is
    configured with `asyncio_mode = "auto"` at the project level, so
    subclasses need no additional marker.
    """

    @pytest.fixture
    def blob_store(self) -> BlobStore:
        raise NotImplementedError(
            "subclasses of BlobStoreContractSuite must override the "
            "`blob_store` fixture to return the adapter under test"
        )

    # -- put, get, head, delete ---------------------------------------------

    async def test_put_then_get_round_trips(self, blob_store: BlobStore) -> None:
        key = _key()
        await blob_store.put(key, b"hello world", content_type="text/plain")
        assert await blob_store.get(key) == b"hello world"

    async def test_get_is_exact_bytes_not_text_decoded(self, blob_store: BlobStore) -> None:
        """Content that is not valid UTF-8 round-trips unchanged: a BlobStore
        holds bytes, and never assumes anything about their encoding."""
        key = _key()
        payload = bytes(range(256))
        await blob_store.put(key, payload, content_type="application/octet-stream")
        assert await blob_store.get(key) == payload

    async def test_put_twice_replaces_the_content(self, blob_store: BlobStore) -> None:
        key = _key()
        await blob_store.put(key, b"first", content_type="text/plain")
        await blob_store.put(key, b"second", content_type="text/plain")
        assert await blob_store.get(key) == b"second"

    async def test_get_missing_key_raises_blob_not_found(self, blob_store: BlobStore) -> None:
        with pytest.raises(BlobNotFound):
            await blob_store.get(_key())

    async def test_head_missing_key_returns_none(self, blob_store: BlobStore) -> None:
        assert await blob_store.head(_key()) is None

    async def test_head_reports_size_and_content_type_with_no_content_read(
        self, blob_store: BlobStore
    ) -> None:
        key = _key()
        await blob_store.put(key, b"twelve bytes", content_type="text/csv")
        meta = await blob_store.head(key)
        assert meta is not None
        assert meta.size == len(b"twelve bytes")
        assert meta.content_type == "text/csv"

    async def test_head_carries_metadata_put_alongside_the_content(
        self, blob_store: BlobStore
    ) -> None:
        key = _key()
        await blob_store.put(key, b"data", content_type="text/plain", metadata={"line_count": "42"})
        meta = await blob_store.head(key)
        assert meta is not None
        assert meta.metadata == {"line_count": "42"}

    async def test_head_with_no_metadata_given_reports_an_empty_bag(
        self, blob_store: BlobStore
    ) -> None:
        key = _key()
        await blob_store.put(key, b"data", content_type="text/plain")
        meta = await blob_store.head(key)
        assert meta is not None
        assert meta.metadata == {}

    async def test_delete_removes_the_blob(self, blob_store: BlobStore) -> None:
        key = _key()
        await blob_store.put(key, b"gone soon", content_type="text/plain")
        await blob_store.delete(key)
        assert await blob_store.head(key) is None
        with pytest.raises(BlobNotFound):
            await blob_store.get(key)

    async def test_delete_of_a_missing_key_is_not_an_error(self, blob_store: BlobStore) -> None:
        await blob_store.delete(_key())  # must not raise

    # -- range reads ----------------------------------------------------------

    async def test_get_with_offset_and_length_returns_exactly_that_slice(
        self, blob_store: BlobStore
    ) -> None:
        key = _key()
        content = b"0123456789abcdef"
        await blob_store.put(key, content, content_type="application/octet-stream")
        assert await blob_store.get(key, offset=4, length=3) == b"456"

    async def test_get_with_offset_only_returns_to_the_end(self, blob_store: BlobStore) -> None:
        key = _key()
        content = b"0123456789"
        await blob_store.put(key, content, content_type="application/octet-stream")
        assert await blob_store.get(key, offset=7) == b"789"

    async def test_get_with_a_length_past_the_end_returns_what_exists(
        self, blob_store: BlobStore
    ) -> None:
        """A range past the end is not an error, the same way a Python slice
        does not raise when asked for more than exists."""
        key = _key()
        content = b"short"
        await blob_store.put(key, content, content_type="application/octet-stream")
        assert await blob_store.get(key, offset=0, length=1_000) == content

    async def test_get_whole_content_by_default(self, blob_store: BlobStore) -> None:
        key = _key()
        content = b"the whole thing"
        await blob_store.put(key, content, content_type="application/octet-stream")
        assert await blob_store.get(key) == content

    # -- tenant scoping: the security property --------------------------------

    async def test_two_tenants_with_the_same_run_and_call_id_never_collide(
        self, blob_store: BlobStore
    ) -> None:
        """The one test that exists to prove the security property in
        `psych_runtime.store.blob`'s module docstring: tenant is part of the address,
        not decoration on it. Two keys differing only in tenant, sharing a
        run_id and call_id (impossible in practice, since both are minted
        per-Run, but exactly the case that matters if that ever stopped being
        true) must resolve to two entirely independent blobs.
        """
        run_id = new_run_id()
        call_id = new_tool_call_id()
        key_a = _key(tenant="tenant-a", run_id=run_id, call_id=call_id)
        key_b = _key(tenant="tenant-b", run_id=run_id, call_id=call_id)

        await blob_store.put(key_a, b"tenant a's data", content_type="text/plain")

        assert await blob_store.get(key_a) == b"tenant a's data"
        assert await blob_store.head(key_b) is None
        with pytest.raises(BlobNotFound):
            await blob_store.get(key_b)

        await blob_store.put(key_b, b"tenant b's data", content_type="text/plain")
        assert await blob_store.get(key_a) == b"tenant a's data"
        assert await blob_store.get(key_b) == b"tenant b's data"

        await blob_store.delete(key_a)
        assert await blob_store.head(key_a) is None
        assert await blob_store.get(key_b) == b"tenant b's data"
