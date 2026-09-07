"""The InMemoryStore port's own types: pure logic, no IO.

DESIGN.md §15. ``MemoryKey`` is the isolation boundary and the whole point of
making it a real type instead of a string built at each call site: a bug that
omits tenant or end-user id must fail to construct a key, not silently build a
narrower or wider one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef
from psych_runtime.memory.port import Memory, MemoryKey, memory_key
from psych_runtime.memory.store_backed import StoreBackedMemory
from psych_runtime.store.memory import InMemoryStore as StoreMemoryStore
from psych_runtime.tools.builtins import register_builtins
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.unit


class TestMemoryKey:
    def test_both_fields_are_required(self) -> None:
        with pytest.raises(ValidationError):
            MemoryKey(tenant="acme")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            MemoryKey(end_user_id="user-1")  # type: ignore[call-arg]

    def test_an_empty_tenant_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryKey(tenant="", end_user_id="user-1")

    def test_an_empty_end_user_id_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MemoryKey(tenant="acme", end_user_id="")

    def test_is_frozen(self) -> None:
        key = MemoryKey(tenant="acme", end_user_id="user-1")
        with pytest.raises(ValidationError):
            key.tenant = "other"  # type: ignore[misc]

    def test_is_hashable_so_it_can_address_a_dict(self) -> None:
        a = MemoryKey(tenant="acme", end_user_id="user-1")
        b = MemoryKey(tenant="acme", end_user_id="user-1")
        c = MemoryKey(tenant="acme", end_user_id="user-2")

        assert hash(a) == hash(b)
        assert a == b
        assert a != c
        assert len({a, b, c}) == 2


class TestMemoryKeyConstruction:
    def test_builds_from_a_scope_and_an_end_user_id(self) -> None:
        scope = Scope(tenant="acme", principal="agent-1")

        key = memory_key(scope, "user-1")

        assert key == MemoryKey(tenant="acme", end_user_id="user-1")

    def test_principal_plays_no_part_in_the_key(self) -> None:
        """DESIGN.md §15: keyed by Scope plus an end-user id. The design is
        explicit that the key is tenant and end-user, not principal:
        two principals serving the same end user must land on the same key."""
        first_principal = memory_key(Scope(tenant="acme", principal="agent-1"), "user-1")
        second_principal = memory_key(Scope(tenant="acme", principal="agent-2"), "user-1")

        assert first_principal == second_principal

    def test_different_tenants_never_produce_the_same_key(self) -> None:
        acme = memory_key(Scope(tenant="acme"), "user-1")
        globex = memory_key(Scope(tenant="globex"), "user-1")

        assert acme != globex

    def test_different_end_users_in_one_tenant_never_produce_the_same_key(self) -> None:
        first = memory_key(Scope(tenant="acme"), "user-1")
        second = memory_key(Scope(tenant="acme"), "user-2")

        assert first != second


class TestMemory:
    def test_content_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            Memory(
                id="mem_1",
                key=MemoryKey(tenant="acme", end_user_id="user-1"),
                content="",
                created_at=datetime.now(UTC),
            )

    def test_is_frozen(self) -> None:
        memory = Memory(
            id="mem_1",
            key=MemoryKey(tenant="acme", end_user_id="user-1"),
            content="likes concise answers",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            memory.content = "something else"  # type: ignore[misc]


def _spec() -> AgentSpec:
    return AgentSpec(name="support", model=ModelRef(model="gpt-4o"))


class TestRegisterBuiltinsMemoryWiring:
    """``register_builtins`` (``psych_runtime.tools.builtins``) is memory's other half
    of the port: this only covers the pure validation at its boundary, not
    execution, which the e2e suite covers end to end."""

    def test_memory_without_an_end_user_id_is_rejected(self) -> None:
        memory = StoreBackedMemory(StoreMemoryStore())
        with pytest.raises(ValueError, match="both"):
            register_builtins(ToolRegistry(), _spec(), Scope(tenant="acme"), memory=memory)

    def test_an_end_user_id_without_memory_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="both"):
            register_builtins(ToolRegistry(), _spec(), Scope(tenant="acme"), end_user_id="user-1")

    def test_neither_memory_nor_end_user_id_is_fine_and_offers_no_memory_tools(self) -> None:
        definitions = register_builtins(ToolRegistry(), _spec(), Scope(tenant="acme"))
        assert {definition.name for definition in definitions} == set()

    def test_both_given_registers_remember_and_forget(self) -> None:
        memory = StoreBackedMemory(StoreMemoryStore())
        definitions = register_builtins(
            ToolRegistry(),
            _spec(),
            Scope(tenant="acme"),
            memory=memory,
            end_user_id="user-1",
        )
        assert {definition.name for definition in definitions} == {"remember", "forget"}
