"""``StoreBackedMemory``, the default ``InMemoryStore`` adapter, against a real store.

DESIGN.md §15. The property that matters most: memories written under
one Scope must be unreachable from another, including from a different
principal in the same tenant when the end-user id differs. Every test in
``TestIsolation`` would fail if ``MemoryKey`` were built carelessly (say, from
``scope.principal`` instead of the end-user id, or by string concatenation that
let two distinct pairs collide).
"""

from __future__ import annotations

import pytest

from psych_runtime.core.scope import Scope
from psych_runtime.memory.store_backed import StoreBackedMemory
from psych_runtime.store.memory import InMemoryStore

pytestmark = pytest.mark.functional


@pytest.fixture
def memory() -> StoreBackedMemory:
    """The default adapter, backed by a real ``Store`` (the in-memory one)."""
    return StoreBackedMemory(InMemoryStore())


ACME = Scope(tenant="acme", principal="agent-1")
ACME_OTHER_PRINCIPAL = Scope(tenant="acme", principal="agent-2")
GLOBEX = Scope(tenant="globex", principal="agent-1")


class TestRemember:
    async def test_remembering_a_fact_returns_it(self, memory: StoreBackedMemory) -> None:
        fact = await memory.remember(ACME, "user-1", "prefers email over phone")

        assert fact.content == "prefers email over phone"
        assert fact.key.tenant == "acme"
        assert fact.key.end_user_id == "user-1"

    async def test_two_facts_get_different_ids(self, memory: StoreBackedMemory) -> None:
        first = await memory.remember(ACME, "user-1", "fact one")
        second = await memory.remember(ACME, "user-1", "fact two")

        assert first.id != second.id

    async def test_remembering_never_deduplicates(self, memory: StoreBackedMemory) -> None:
        """Deduplication is a judgement call about when two facts are 'the
        same', which this port does not make on the consumer's behalf."""
        await memory.remember(ACME, "user-1", "the same fact")
        await memory.remember(ACME, "user-1", "the same fact")

        facts = await memory.recall(ACME, "user-1")

        assert len(facts) == 2


class TestRecall:
    async def test_recalls_every_fact_oldest_first(self, memory: StoreBackedMemory) -> None:
        await memory.remember(ACME, "user-1", "first")
        await memory.remember(ACME, "user-1", "second")
        await memory.remember(ACME, "user-1", "third")

        facts = await memory.recall(ACME, "user-1")

        assert [fact.content for fact in facts] == ["first", "second", "third"]

    async def test_an_end_user_with_no_memories_gets_an_empty_tuple(
        self, memory: StoreBackedMemory
    ) -> None:
        assert await memory.recall(ACME, "nobody-yet") == ()

    async def test_limit_truncates_without_reordering(self, memory: StoreBackedMemory) -> None:
        await memory.remember(ACME, "user-1", "first")
        await memory.remember(ACME, "user-1", "second")
        await memory.remember(ACME, "user-1", "third")

        facts = await memory.recall(ACME, "user-1", limit=2)

        assert [fact.content for fact in facts] == ["first", "second"]


class TestForget:
    async def test_forgetting_a_real_fact_removes_it(self, memory: StoreBackedMemory) -> None:
        fact = await memory.remember(ACME, "user-1", "temporary fact")

        removed = await memory.forget(ACME, "user-1", fact.id)

        assert removed is True
        assert await memory.recall(ACME, "user-1") == ()

    async def test_forgetting_an_unknown_id_returns_false_not_an_error(
        self, memory: StoreBackedMemory
    ) -> None:
        assert await memory.forget(ACME, "user-1", "mem_does_not_exist") is False

    async def test_forgetting_leaves_other_facts_for_the_same_user_alone(
        self, memory: StoreBackedMemory
    ) -> None:
        keep = await memory.remember(ACME, "user-1", "keep me")
        drop = await memory.remember(ACME, "user-1", "drop me")

        await memory.forget(ACME, "user-1", drop.id)

        facts = await memory.recall(ACME, "user-1")
        assert [fact.id for fact in facts] == [keep.id]


class TestErase:
    async def test_erases_every_fact_for_the_end_user(self, memory: StoreBackedMemory) -> None:
        await memory.remember(ACME, "user-1", "one")
        await memory.remember(ACME, "user-1", "two")

        deleted = await memory.erase(ACME, "user-1")

        assert deleted == 2
        assert await memory.recall(ACME, "user-1") == ()

    async def test_erasing_an_end_user_with_nothing_remembered_deletes_zero(
        self, memory: StoreBackedMemory
    ) -> None:
        assert await memory.erase(ACME, "nobody") == 0

    async def test_erasure_does_not_touch_another_end_user_in_the_same_tenant(
        self, memory: StoreBackedMemory
    ) -> None:
        await memory.remember(ACME, "user-1", "erase me")
        await memory.remember(ACME, "user-2", "keep me")

        await memory.erase(ACME, "user-1")

        assert await memory.recall(ACME, "user-2") != ()


class TestIsolation:
    """The property to test hardest, per DESIGN.md §15: a cross-tenant read
    must be impossible, asserted by a test."""

    async def test_a_different_tenant_cannot_recall_another_tenants_memories(
        self, memory: StoreBackedMemory
    ) -> None:
        await memory.remember(ACME, "user-1", "acme's secret preference")

        assert await memory.recall(GLOBEX, "user-1") == ()

    async def test_a_different_tenant_cannot_forget_another_tenants_memory(
        self, memory: StoreBackedMemory
    ) -> None:
        """A cross-tenant forget must not silently succeed against the wrong
        tenant's data merely because the memory id happens to match."""
        fact = await memory.remember(ACME, "user-1", "acme's fact")

        removed = await memory.forget(GLOBEX, "user-1", fact.id)

        assert removed is False
        assert await memory.recall(ACME, "user-1") != ()

    async def test_a_different_tenant_cannot_erase_another_tenants_memories(
        self, memory: StoreBackedMemory
    ) -> None:
        await memory.remember(ACME, "user-1", "acme's fact")

        deleted = await memory.erase(GLOBEX, "user-1")

        assert deleted == 0
        assert await memory.recall(ACME, "user-1") != ()

    async def test_a_different_principal_in_the_same_tenant_cannot_reach_another_end_user(
        self, memory: StoreBackedMemory
    ) -> None:
        """The key is tenant plus end-user id, not principal. Two principals in
        one tenant must still be isolated from each other's end users."""
        await memory.remember(ACME, "user-1", "user 1's fact")

        assert await memory.recall(ACME_OTHER_PRINCIPAL, "user-2") == ()

    async def test_a_different_principal_serving_the_same_end_user_does_see_the_memories(
        self, memory: StoreBackedMemory
    ) -> None:
        """The inverse of the isolation property: principal is not part of the
        key, so two principals acting for the same end user share memories."""
        await memory.remember(ACME, "user-1", "shared across principals")

        facts = await memory.recall(ACME_OTHER_PRINCIPAL, "user-1")

        assert [fact.content for fact in facts] == ["shared across principals"]

    async def test_identical_end_user_ids_in_different_tenants_stay_isolated(
        self, memory: StoreBackedMemory
    ) -> None:
        """Two tenants both using 'user-1' as an end-user id must not collide,
        which is exactly the bug an ad hoc string key invites."""
        await memory.remember(ACME, "user-1", "acme's user-1 fact")
        await memory.remember(GLOBEX, "user-1", "globex's user-1 fact")

        acme_facts = await memory.recall(ACME, "user-1")
        globex_facts = await memory.recall(GLOBEX, "user-1")

        assert [fact.content for fact in acme_facts] == ["acme's user-1 fact"]
        assert [fact.content for fact in globex_facts] == ["globex's user-1 fact"]
