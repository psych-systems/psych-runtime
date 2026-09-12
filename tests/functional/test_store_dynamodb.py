"""The DynamoDB Store against the shared contract suite.

DESIGN.md §22: store tests run against real DynamoDB Local, never a mock, and
the contract suite is the deliverable as much as the adapter. Each test gets a
freshly named set of tables (a random ``table_prefix`` per test function) and
tears them down afterward: ``claim()``, ``expired_leases()`` and
``overdue_deadlines()`` take no ``run_id`` and see every header currently in
the table, so a table shared and left dirty across tests (unlike the
Postgres/MySQL pattern the contract suite's own docstring describes, where
unique ids are enough) would let one test's leftover RUNNABLE Run answer
another test's "nothing is claimable" assertion. DynamoDB Local is fast enough
that creating four small tables per test is not a meaningful cost.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from psych_runtime.core.ids import RunId, VersionHash, new_run_id
from psych_runtime.core.scope import Scope
from psych_runtime.store.contract import StoreContractSuite
from psych_runtime.store.dynamodb import DynamoDBStore
from psych_runtime.store.port import RunHeader, RunState, Store

pytestmark = pytest.mark.dynamodb


class TestDynamoDBStore(StoreContractSuite):
    @pytest.fixture
    async def store(self, dynamodb_endpoint: str) -> AsyncIterator[Store]:
        prefix = f"psych-test-{uuid.uuid4().hex[:16]}"
        adapter = DynamoDBStore(
            endpoint_url=dynamodb_endpoint,
            region_name="us-east-1",
            table_prefix=prefix,
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
        await adapter.ensure_tables()
        try:
            yield adapter
        finally:
            await adapter.drop_tables()
            await adapter.close()


class _Recorder:
    """A table that notes which write it was asked for, and forwards it."""

    def __init__(self, table: Any, calls: list[str]) -> None:
        self._table = table
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._table, name)
        if name not in {"get_item", "put_item", "update_item", "delete_item"}:
            return attribute

        async def recorded(**kwargs: Any) -> Any:
            self._calls.append(name)
            return await attribute(**kwargs)

        return recorded


class TestSettleInlineIsOneAtomicUpdate:
    """How the settle is written, not just what it leaves behind.

    The contract suite can prove the header ends up right. It cannot see the
    difference between an item rewritten from a snapshot and an item updated
    in place, and that difference is the whole risk here: a read followed by a
    full ``PutItem`` reverts whatever a concurrent writer changed in between,
    and the state condition does not notice, because that writer left the
    state alone. So the write itself is the assertion.
    """

    @pytest.fixture
    async def adapter(self, dynamodb_endpoint: str) -> AsyncIterator[DynamoDBStore]:
        prefix = f"psych-test-{uuid.uuid4().hex[:16]}"
        store = DynamoDBStore(
            endpoint_url=dynamodb_endpoint,
            region_name="us-east-1",
            table_prefix=prefix,
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
        await store.ensure_tables()
        try:
            yield store
        finally:
            await store.drop_tables()
            await store.close()

    async def _nested_run(self, adapter: DynamoDBStore) -> RunId:
        run_id = new_run_id()
        created = datetime.now(UTC)
        await adapter.create_run(
            RunHeader(
                run_id=run_id,
                scope=Scope(tenant="acme"),
                version_hash=VersionHash("sha256:" + "0" * 64),
                state=RunState.NESTED,
                created_at=created,
                deadline_at=created + timedelta(seconds=60),
            )
        )
        return run_id

    async def _item(self, adapter: DynamoDBStore, run_id: RunId) -> dict[str, Any]:
        resource = await adapter._ensure_resource()
        table = await resource.Table(adapter._runs_table_name)
        response = await table.get_item(Key={"run_id": str(run_id)}, ConsistentRead=True)
        item = response.get("Item")
        assert item is not None
        return dict(item)

    async def test_the_settle_is_an_update_and_never_a_whole_item_write(
        self, adapter: DynamoDBStore
    ) -> None:
        run_id = await self._nested_run(adapter)
        resource = await adapter._ensure_resource()
        calls: list[str] = []
        real_table = resource.Table

        async def recording_table(name: str) -> Any:
            # The adapter asks the resource for a fresh table object per call,
            # so the recording has to be installed here rather than on one
            # table instance.
            table = await real_table(name)
            return _Recorder(table, calls)

        resource.Table = recording_table
        try:
            assert await adapter.settle_inline(run_id) is True
        finally:
            resource.Table = real_table

        assert calls == ["update_item"], calls

    async def test_settling_leaves_the_sparse_deadline_index(self, adapter: DynamoDBStore) -> None:
        """A settled Run ages out of `overdue_deadlines` by losing the keys."""
        run_id = await self._nested_run(adapter)
        before = await self._item(adapter, run_id)
        assert "deadline_gsi_pk" in before
        assert "deadline_gsi_sk" in before

        assert await adapter.settle_inline(run_id) is True

        after = await self._item(adapter, run_id)
        assert "deadline_gsi_pk" not in after
        assert "deadline_gsi_sk" not in after

    async def test_an_attribute_written_after_the_read_is_not_reverted(
        self, adapter: DynamoDBStore
    ) -> None:
        """The race a read-and-replace loses, written out.

        A concurrent writer parks the Run while it is still nested, which
        leaves the state alone and so passes any condition on it. An adapter
        holding a snapshot from before that write puts it back.
        """
        run_id = await self._nested_run(adapter)
        parked_at = datetime.now(UTC) + timedelta(hours=1)
        await adapter.set_runnable_at(run_id, parked_at)

        assert await adapter.settle_inline(run_id) is True

        item = await self._item(adapter, run_id)
        assert item["state"] == RunState.SETTLED.value
        assert "runnable_at" in item, "the concurrent parking was overwritten"
