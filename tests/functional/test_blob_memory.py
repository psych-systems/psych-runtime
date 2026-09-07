"""`InMemoryBlobStore` against the shared contract suite.

Proves nothing about the contract by itself (`psych_runtime.store.blob_memory`'s own
docstring), the same disclaimer `psych_runtime.store.memory`'s tests carry. It exists
so the fake used by every other component's tests is held to the same
behaviour as the real adapters.
"""

from __future__ import annotations

import pytest

from psych_runtime.store.blob import BlobStore
from psych_runtime.store.blob_contract import BlobStoreContractSuite
from psych_runtime.store.blob_memory import InMemoryBlobStore

pytestmark = pytest.mark.functional


class TestInMemoryBlobStore(BlobStoreContractSuite):
    @pytest.fixture
    def blob_store(self) -> BlobStore:
        return InMemoryBlobStore()
