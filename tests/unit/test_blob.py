"""Pure logic in `psych_runtime.store.blob`: `BlobKey` construction and scoping.

No IO, no adapter: everything here is a plain dataclass and a plain function.
The adapters' actual storage behaviour is `tests/functional/test_blob_*.py`,
against `psych_runtime.store.blob_contract`.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.ids import RunId, ToolCallId, new_run_id, new_tool_call_id
from psych_runtime.core.scope import Scope
from psych_runtime.store.blob import BlobKey, blob_key

pytestmark = pytest.mark.unit


def test_str_is_tenant_slash_run_id_slash_call_id() -> None:
    key = BlobKey(tenant="acme", run_id=RunId("run_1"), call_id=ToolCallId("call_1"))
    assert str(key) == "acme/run_1/call_1"


def test_blob_key_builds_from_a_scope_run_and_call() -> None:
    scope = Scope(tenant="acme", principal="user-1")
    run_id = new_run_id()
    call_id = new_tool_call_id()

    key = blob_key(scope, run_id, call_id)

    assert key.tenant == "acme"
    assert key.run_id == run_id
    assert key.call_id == call_id


def test_blob_key_ignores_principal_the_way_scope_pool_key_does() -> None:
    """Tenant is the isolation boundary (DESIGN.md §14); principal is not part
    of the address, the same way `Scope.pool_key` only carries tenant and
    principal but a blob's address only needs the tenant half of that."""
    scope_a = Scope(tenant="acme", principal="user-1")
    scope_b = Scope(tenant="acme", principal="user-2")
    run_id = new_run_id()
    call_id = new_tool_call_id()

    assert blob_key(scope_a, run_id, call_id) == blob_key(scope_b, run_id, call_id)


@pytest.mark.parametrize("tenant", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_a_key_segment_that_could_escape_a_path_or_prefix_is_refused(tenant: str) -> None:
    with pytest.raises(ValueError, match="tenant"):
        BlobKey(tenant=tenant, run_id=RunId("run_1"), call_id=ToolCallId("call_1"))


def test_a_normal_tenant_string_with_punctuation_is_fine() -> None:
    """The guard is narrow: it refuses what would escape a path segment, not
    ordinary tenant naming conventions (dots, dashes, underscores)."""
    BlobKey(tenant="acme-inc.prod_1", run_id=RunId("run_1"), call_id=ToolCallId("call_1"))


def test_two_tenants_never_produce_the_same_key_for_the_same_run_and_call() -> None:
    run_id = new_run_id()
    call_id = new_tool_call_id()
    key_a = BlobKey(tenant="tenant-a", run_id=run_id, call_id=call_id)
    key_b = BlobKey(tenant="tenant-b", run_id=run_id, call_id=call_id)

    assert key_a != key_b
    assert str(key_a) != str(key_b)
