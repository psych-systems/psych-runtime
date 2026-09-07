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

import pytest

from psych_runtime.store.contract import StoreContractSuite
from psych_runtime.store.dynamodb import DynamoDBStore
from psych_runtime.store.port import Store

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
