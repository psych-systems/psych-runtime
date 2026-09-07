"""The in-memory Store against the shared contract suite.

DESIGN.md §22: the in-memory adapter tests other components quickly and never
proves the Store contract on its own. Running it here proves the opposite
direction instead, that ``psych_runtime.store.contract`` is satisfiable at all and
that the adapter meant to stand in for a real store during fast unit-style
runs does not itself violate the semantics everything else assumes.
"""

from __future__ import annotations

import pytest

from psych_runtime.store.contract import StoreContractSuite
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store


class TestMemoryStore(StoreContractSuite):
    @pytest.fixture
    def store(self) -> Store:
        return InMemoryStore()
