"""The DynamoDB Store adapter.

DESIGN.md §7: partition key ``run_id``, sort key ``seq`` on the records table,
``ConditionExpression="attribute_not_exists(seq)"`` turned into ``SeqConflict``
on ``ConditionalCheckFailedException``. Leases are a conditional ``UpdateItem``
on the run header item. No transactions, no joins, no ``SELECT ... FOR
UPDATE``: every write below is exactly one conditional ``PutItem`` or
``UpdateItem`` against one item.

## Four tables, not one

- ``<prefix>-records``: pk ``run_id`` (S), sk ``seq`` (N). One attribute,
  ``data``, holding the whole Record as a JSON string produced by
  ``RECORD_ADAPTER``. A Record's ``Usage``/``Cost`` fields carry floats, and
  DynamoDB has neither a float type (boto3 would demand a ``Decimal`` at every
  call site) nor tolerance for ``nan``/``inf`` (which a real cost or a token
  count only reaching the model as a rounding artifact could produce).
  Serialising the whole Record to one string sidesteps both problems and keeps
  this adapter exactly as ignorant of Record shape as ``InMemoryStore`` is.
- ``<prefix>-versions``: pk ``version_hash`` (S). Same reasoning: a Spec's
  ``ModelRef`` can carry floats (temperature, per-token price overrides).
- ``<prefix>-runs``: pk ``run_id`` (S). Every ``RunHeader`` field is its own
  attribute (all strings and ints; a Scope has no floats either), plus four
  sparse GSI helper attributes, described below.
- ``<prefix>-idempotency``: pk ``idempotency_key`` (S), one attribute
  ``run_id``. A separate table rather than a GSI on ``-runs``, because
  idempotent admission needs a strongly consistent
  ``attribute_not_exists(idempotency_key)`` check at write time and a GSI is
  only ever eventually consistent, real DynamoDB never offers a consistent
  read on one.

## The claim index: how ``claim()`` avoids a Scan

DESIGN.md is blunt about the alternative: "a full Scan on every claim is a
production incident". ``claim()`` takes no ``run_id``, so it needs to find
*some* runnable Run cheaply. The postgres adapter gets this from a partial
index (``WHERE state IN ('runnable', 'running')``); a DynamoDB GSI has no
``WHERE`` clause, but it has the same effect through sparsity: an item that is
missing a GSI's key attributes is invisible to that GSI, so ``claim_gsi_pk``
and ``claim_gsi_sk`` are written only while a Run is RUNNABLE or RUNNING, and
dropped the instant it becomes SUSPENDED or SETTLED (``_gsi_attrs_for_header``
below, applied on every full rewrite of the item). The index then holds
exactly the candidate set and nothing else.

``claim_gsi_sk`` is one number standing in for whichever gates apply to the
header's current state. ``InMemoryStore._is_claimable`` checks ``runnable_at``
for every reclaimable state and additionally ``lease_expires_at`` while
RUNNING; ``_claim_sort_key`` takes the *later* of the gates that apply, so
"``claim_gsi_sk <= now``" in the GSI Query is true only once every applicable
gate has passed. That Query only produces *candidates*: the actual claim is a
conditional ``UpdateItem`` against the base table re-checking the identical
predicate with strong consistency and the real ``now``, so a GSI that lags
behind (DynamoDB Local applies updates to a GSI synchronously, but real
DynamoDB does not promise that) can only make ``claim()`` try a candidate that
turns out to already be taken, never hand out a Run twice.

``expired_leases()`` reuses this same GSI with a ``FilterExpression`` narrowing
it to RUNNING, rather than a fifth index, since its candidate set is a subset
of ``claim()``'s.

## The deadline index

``<prefix>-runs`` carries a second sparse GSI, keyed by ``deadline_gsi_pk``
(a constant) and ``deadline_gsi_sk`` (``deadline_at`` in epoch microseconds),
present on every header except a SETTLED one. This mirrors postgres's
``psych_runs_deadline_idx`` exactly: ``overdue_deadlines()`` is a Query, never
a Scan, and a settled Run ages out of it by no longer carrying the attribute
rather than by anything pruning it later.

## Numbers, and why every instant is also stored as a string

boto3 returns ``Decimal`` for every DynamoDB number, so ``seq`` is cast back
to ``int`` on every read; ``RECORD_ADAPTER`` and the rest of Psych never see a
``Decimal``, only this module does. Instants (``created_at``, ``deadline_at``,
``lease_expires_at``, ``runnable_at``) are stored as fixed-width, zero-padded
ISO-8601 UTC strings (``_iso``) so a ``RunHeader`` round-trips through Pydantic
with no adapter-specific parsing. The GSI sort keys derived from those same
instants are stored *separately* as epoch-microsecond integers (``_micros``),
because a GSI range key needs DynamoDB's numeric comparison, not a
lexicographic one, and because microsecond resolution keeps a lease-expiry
comparison correct even when two events in the contract suite's
``asyncio.gather`` contention tests land in the same millisecond.

## What does not fit a single conditional write

Every method here is one conditional ``PutItem`` or ``UpdateItem`` except
``release`` and ``set_runnable_at``: both need to recompute the sparse GSI
attributes from the header's *other* current fields (the target state for
``release``, the current state for ``set_runnable_at``), and an
``UpdateExpression`` cannot branch on a value it just read. Both read the
header (strongly consistent), compute the correct new item, and write it back
with ``PutItem`` conditioned on the field that would prove a concurrent writer
got there first, retrying a bounded number of times on conflict. DESIGN.md §7
promises conditional writes, not single-round-trip writes; read, then write
conditioned on what was read, is still exactly one conditional write.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import aioboto3
from botocore.exceptions import ClientError

from psych_runtime.core.errors import RunNotFound, SeqConflict, StoreError
from psych_runtime.core.ids import RunId, VersionHash, WorkerId
from psych_runtime.core.records import RECORD_ADAPTER, Record
from psych_runtime.core.version import Version
from psych_runtime.store.port import RunHeader, RunState

__all__ = ["DynamoDBStore"]

_CLAIM_GSI_NAME: Final = "claimable-index"
_CLAIM_GSI_PK_VALUE: Final = "C"
_DEADLINE_GSI_NAME: Final = "deadline-index"
_DEADLINE_GSI_PK_VALUE: Final = "D"
_RECLAIMABLE_STATES: Final = frozenset({RunState.RUNNABLE, RunState.RUNNING})

_QUERY_PAGE_SIZE: Final = 100
"""Internal fetch size for every Query loop in this module, deliberately well
under DynamoDB's 1MB page cap. The contract suite's 250-record pagination test
exists to exercise multi-page reassembly regardless of how small each Record
serialises, so pagination here is driven by item count, not by waiting for a
real 1MB boundary."""

_CLAIM_MAX_CANDIDATES: Final = 500
"""``claim()`` gives up after scanning this many GSI candidates with no
successful UpdateItem, rather than looping the whole claimable set forever
under pathological contention. A real deployment runs many Workers each
claiming independently; one Worker failing to win this round tries again on
its next poll."""

_MAX_OPTIMISTIC_RETRIES: Final = 8
"""Bound on the read-modify-write retry loop in ``release`` and
``set_runnable_at``. Both are optimistic-concurrency writes racing at most a
handful of contenders (the current lease holder plus, at most, the Worker that
just reclaimed an expired lease), so a handful of retries is generous rather
than tight."""


def _error_code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "")


def _iso(dt: datetime) -> str:
    """Fixed-width ISO-8601 UTC, sortable as a plain string.

    ``datetime.isoformat()`` omits the fractional part when microseconds are
    zero, which would make two otherwise-adjacent instants compare in the
    wrong order lexicographically. ``%f`` is always six digits, so this format
    never has that problem.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _micros(dt: datetime) -> int:
    """Epoch microseconds, for a GSI sort key that needs numeric comparison."""
    return int(dt.astimezone(UTC).timestamp() * 1_000_000)


def _claim_sort_key(
    state: RunState, runnable_at: datetime | None, lease_expires_at: datetime | None
) -> int:
    """The instant a header with this state becomes claimable, mirroring
    ``InMemoryStore._is_claimable``: ``runnable_at`` gates every reclaimable
    state, and RUNNING additionally waits for its lease to expire. Taking the
    later of whichever gates apply means "``claim_gsi_sk <= now``" is true
    exactly when every applicable gate has passed.

    Deliberately never floored at ``created_at``: a caller's ``now`` can
    legitimately be a hair older than a Run's creation timestamp (``now`` is
    often snapshotted before the store call that creates the Run), and a gate
    that could exceed a real, current ``now`` would make a genuinely
    claimable Run invisible to the very next ``claim()``. Fairness among Runs
    tied on this gate (most commonly several RUNNABLE Runs, none scheduled)
    is handled separately in ``claim()``, by sorting each fetched page rather
    than by inflating the index key.
    """
    gates = [0]
    if runnable_at is not None:
        gates.append(_micros(runnable_at))
    if state == RunState.RUNNING and lease_expires_at is not None:
        gates.append(_micros(lease_expires_at))
    return max(gates)


def _gsi_attrs_for_header(header: RunHeader) -> dict[str, Any]:
    """The sparse claim/deadline GSI attributes for ``header``'s current
    state. Absent entirely (rather than present with a sentinel) is what
    keeps a SUSPENDED or SETTLED Run out of the claim index, and a SETTLED Run
    out of the deadline index.
    """
    attrs: dict[str, Any] = {}
    if header.state in _RECLAIMABLE_STATES:
        attrs["claim_gsi_pk"] = _CLAIM_GSI_PK_VALUE
        attrs["claim_gsi_sk"] = _claim_sort_key(
            header.state, header.runnable_at, header.lease_expires_at
        )
    if header.state != RunState.SETTLED:
        attrs["deadline_gsi_pk"] = _DEADLINE_GSI_PK_VALUE
        attrs["deadline_gsi_sk"] = _micros(header.deadline_at)
    return attrs


def _header_to_item(header: RunHeader) -> dict[str, Any]:
    """A full DynamoDB item for ``header``, including its GSI attributes.

    Optional fields are omitted rather than written as ``None``: a
    ``PutItem`` is a full replace, so an omitted attribute here is genuinely
    absent afterwards, which is what keeps the sparse GSIs sparse.
    """
    item: dict[str, Any] = {
        "run_id": str(header.run_id),
        "scope": header.scope.model_dump(mode="json"),
        "version_hash": str(header.version_hash),
        "state": header.state.value,
        "created_at": _iso(header.created_at),
        "deadline_at": _iso(header.deadline_at),
        "attempt_count": header.attempt_count,
        "delegation_depth": header.delegation_depth,
    }
    if header.lease_holder is not None:
        item["lease_holder"] = str(header.lease_holder)
    if header.lease_expires_at is not None:
        item["lease_expires_at"] = _iso(header.lease_expires_at)
    if header.idempotency_key is not None:
        item["idempotency_key"] = header.idempotency_key
    if header.runnable_at is not None:
        item["runnable_at"] = _iso(header.runnable_at)
    if header.parent_run_id is not None:
        item["parent_run_id"] = str(header.parent_run_id)
    if header.continues_run_id is not None:
        item["continues_run_id"] = str(header.continues_run_id)
    item.update(_gsi_attrs_for_header(header))
    return item


def _item_to_header(item: dict[str, Any]) -> RunHeader:
    """The inverse of ``_header_to_item``, ignoring the GSI helper attributes:
    ``RunHeader`` is ``extra="forbid"``, so only its own fields are passed.
    """
    return RunHeader.model_validate(
        {
            "run_id": item["run_id"],
            "scope": item["scope"],
            "version_hash": item["version_hash"],
            "state": item["state"],
            "created_at": item["created_at"],
            "deadline_at": item["deadline_at"],
            "lease_holder": item.get("lease_holder"),
            "lease_expires_at": item.get("lease_expires_at"),
            "idempotency_key": item.get("idempotency_key"),
            "attempt_count": int(item.get("attempt_count", 0)),
            "runnable_at": item.get("runnable_at"),
            "parent_run_id": item.get("parent_run_id"),
            "delegation_depth": int(item.get("delegation_depth", 0)),
            "continues_run_id": item.get("continues_run_id"),
        }
    )


class DynamoDBStore:
    """A ``Store`` backed by DynamoDB (or DynamoDB Local).

    Implements the ``Store`` protocol structurally; there is no base class to
    inherit because the port is a ``Protocol``. Usable directly, or as an
    async context manager, which is equivalent to calling ``ensure_tables()``
    yourself: the context manager only opens and closes the underlying
    connection, it does not create tables, since a production deployment
    provisions tables once and connects to them many times over.

    ``types_aiobotocore_dynamodb`` (the package that would give
    ``session.resource("dynamodb")`` a concrete return type) is not installed
    alongside ``types-aioboto3`` in this environment, so the DynamoDB resource
    and table objects held here are typed ``Any``; every method on this class
    still carries a precise signature, which is what ``mypy --strict`` and the
    ``Store`` protocol actually hold this adapter to.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
        table_prefix: str = "psych",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ) -> None:
        self._session = aioboto3.Session()
        self._resource_kwargs: dict[str, Any] = {"region_name": region_name}
        if endpoint_url is not None:
            self._resource_kwargs["endpoint_url"] = endpoint_url
        if aws_access_key_id is not None:
            self._resource_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key is not None:
            self._resource_kwargs["aws_secret_access_key"] = aws_secret_access_key

        self._records_table_name = f"{table_prefix}-records"
        self._versions_table_name = f"{table_prefix}-versions"
        self._runs_table_name = f"{table_prefix}-runs"
        self._idempotency_table_name = f"{table_prefix}-idempotency"

        self._resource_cm: Any | None = None
        self._resource: Any | None = None

    async def _ensure_resource(self) -> Any:
        if self._resource is None:
            self._resource_cm = self._session.resource("dynamodb", **self._resource_kwargs)
            self._resource = await self._resource_cm.__aenter__()
        return self._resource

    async def close(self) -> None:
        """Release the underlying connection. Idempotent."""
        if self._resource_cm is not None:
            await self._resource_cm.__aexit__(None, None, None)
            self._resource_cm = None
            self._resource = None

    async def __aenter__(self) -> DynamoDBStore:
        await self._ensure_resource()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # -- table lifecycle ------------------------------------------------

    async def ensure_tables(self) -> None:
        """Create every table and GSI this adapter needs, if they do not
        already exist. Safe to call every time a process boots: a
        ``ResourceInUseException`` from a table that already exists is
        swallowed, so this is idempotent rather than one-shot.
        """
        resource = await self._ensure_resource()
        await self._create_table_if_absent(
            resource,
            self._records_table_name,
            key_schema=[
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "seq", "KeyType": "RANGE"},
            ],
            attribute_definitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "seq", "AttributeType": "N"},
            ],
        )
        await self._create_table_if_absent(
            resource,
            self._versions_table_name,
            key_schema=[{"AttributeName": "version_hash", "KeyType": "HASH"}],
            attribute_definitions=[{"AttributeName": "version_hash", "AttributeType": "S"}],
        )
        await self._create_table_if_absent(
            resource,
            self._idempotency_table_name,
            key_schema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            attribute_definitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        )
        await self._create_table_if_absent(
            resource,
            self._runs_table_name,
            key_schema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
            attribute_definitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "claim_gsi_pk", "AttributeType": "S"},
                {"AttributeName": "claim_gsi_sk", "AttributeType": "N"},
                {"AttributeName": "deadline_gsi_pk", "AttributeType": "S"},
                {"AttributeName": "deadline_gsi_sk", "AttributeType": "N"},
            ],
            global_secondary_indexes=[
                {
                    "IndexName": _CLAIM_GSI_NAME,
                    "KeySchema": [
                        {"AttributeName": "claim_gsi_pk", "KeyType": "HASH"},
                        {"AttributeName": "claim_gsi_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": _DEADLINE_GSI_NAME,
                    "KeySchema": [
                        {"AttributeName": "deadline_gsi_pk", "KeyType": "HASH"},
                        {"AttributeName": "deadline_gsi_sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )

    async def _create_table_if_absent(
        self,
        resource: Any,
        table_name: str,
        *,
        key_schema: list[dict[str, str]],
        attribute_definitions: list[dict[str, str]],
        global_secondary_indexes: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "KeySchema": key_schema,
            "AttributeDefinitions": attribute_definitions,
            "BillingMode": "PAY_PER_REQUEST",
        }
        if global_secondary_indexes is not None:
            kwargs["GlobalSecondaryIndexes"] = global_secondary_indexes
        try:
            table = await resource.create_table(**kwargs)
            await table.wait_until_exists()
        except ClientError as err:
            if _error_code(err) != "ResourceInUseException":
                raise

    async def drop_tables(self) -> None:
        """Delete every table this adapter created. Idempotent: a table that
        is already gone is not an error. Not part of the ``Store`` protocol;
        this is operational teardown, the counterpart to ``ensure_tables``.
        """
        resource = await self._ensure_resource()
        for table_name in (
            self._records_table_name,
            self._versions_table_name,
            self._runs_table_name,
            self._idempotency_table_name,
        ):
            try:
                table = await resource.Table(table_name)
                await table.delete()
            except ClientError as err:
                if _error_code(err) != "ResourceNotFoundException":
                    raise

    # -- the log ----------------------------------------------------------

    async def append(self, run_id: RunId, seq: int, record: Record) -> None:
        resource = await self._ensure_resource()
        table = await resource.Table(self._records_table_name)
        payload = RECORD_ADAPTER.dump_json(record).decode("utf-8")
        try:
            await table.put_item(
                Item={"run_id": str(run_id), "seq": seq, "data": payload},
                ConditionExpression="attribute_not_exists(seq)",
            )
        except ClientError as err:
            if _error_code(err) == "ConditionalCheckFailedException":
                raise SeqConflict(run_id, seq) from err
            raise

    async def read(self, run_id: RunId, after: int = 0, limit: int | None = None) -> list[Record]:
        resource = await self._ensure_resource()
        table = await resource.Table(self._records_table_name)
        records: list[Record] = []
        exclusive_start_key: dict[str, Any] | None = None
        while limit is None or len(records) < limit:
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": "run_id = :r AND seq > :a",
                "ExpressionAttributeValues": {":r": str(run_id), ":a": after},
                "ScanIndexForward": True,
                "Limit": _QUERY_PAGE_SIZE,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await table.query(**query_kwargs)
            for item in response.get("Items", []):
                records.append(RECORD_ADAPTER.validate_json(item["data"]))
                if limit is not None and len(records) >= limit:
                    break
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return records

    async def head(self, run_id: RunId) -> int:
        resource = await self._ensure_resource()
        table = await resource.Table(self._records_table_name)
        response = await table.query(
            KeyConditionExpression="run_id = :r",
            ExpressionAttributeValues={":r": str(run_id)},
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        if not items:
            return 0
        return int(items[0]["seq"])

    # -- versions -----------------------------------------------------------

    async def put_version(self, version: Version) -> None:
        resource = await self._ensure_resource()
        table = await resource.Table(self._versions_table_name)
        await table.put_item(
            Item={"version_hash": str(version.hash), "data": version.model_dump_json()}
        )

    async def get_version(self, version_hash: VersionHash) -> Version | None:
        resource = await self._ensure_resource()
        table = await resource.Table(self._versions_table_name)
        response = await table.get_item(Key={"version_hash": str(version_hash)})
        item = response.get("Item")
        if item is None:
            return None
        return Version.model_validate_json(item["data"])

    # -- runs -----------------------------------------------------------------

    async def create_run(self, header: RunHeader) -> RunHeader:
        resource = await self._ensure_resource()
        if header.idempotency_key is not None:
            existing = await self._claim_idempotency_key(resource, header)
            if existing is not None:
                return existing
        runs_table = await resource.Table(self._runs_table_name)
        try:
            await runs_table.put_item(
                Item=_header_to_item(header),
                ConditionExpression="attribute_not_exists(run_id)",
            )
        except ClientError as err:
            if _error_code(err) != "ConditionalCheckFailedException":
                raise
            existing_header = await self.get_run(header.run_id)
            if existing_header is None:
                # The condition failure means an item with this run_id exists;
                # it disappearing before the very next read would mean
                # something outside Psych deleted a run header, which nothing
                # in this codebase does.
                raise RunNotFound(header.run_id) from err
            return existing_header
        return header

    @staticmethod
    def _idempotency_id(header: RunHeader) -> str:
        """The idempotency table's key: the tenant and the consumer's key.

        Keyed by the key alone, a key one tenant chose ("order-1234", a webhook
        delivery id) admitted nothing for the next tenant to use it and handed
        them the first tenant's run_id instead. ``\x00`` separates the two
        because it cannot appear in either half.
        """
        return f"{header.scope.tenant}\x00{header.idempotency_key}"

    async def _claim_idempotency_key(self, resource: Any, header: RunHeader) -> RunHeader | None:
        """Try to become the writer for ``header.idempotency_key``.

        Returns ``None`` when this call won the key and should proceed to
        create the Run header; returns the existing header when a previous
        call already used this key.
        """
        idempotency_table = await resource.Table(self._idempotency_table_name)
        try:
            await idempotency_table.put_item(
                Item={
                    "idempotency_key": self._idempotency_id(header),
                    "run_id": str(header.run_id),
                },
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
        except ClientError as err:
            if _error_code(err) != "ConditionalCheckFailedException":
                raise
            response = await idempotency_table.get_item(
                Key={"idempotency_key": self._idempotency_id(header)}, ConsistentRead=True
            )
            item = response.get("Item")
            if item is None:
                # The winner's write landed between our failed conditional put
                # and this read. Never observed against DynamoDB Local, which
                # applies a PutItem before returning the conflict to the
                # loser, but a correct caller must not fabricate a Run here.
                raise RunNotFound(header.run_id) from err
            existing_run_id = RunId(item["run_id"])
            existing_header = await self.get_run(existing_run_id)
            if existing_header is None:
                raise RunNotFound(existing_run_id) from err
            return existing_header
        return None

    async def get_run(self, run_id: RunId) -> RunHeader | None:
        resource = await self._ensure_resource()
        table = await resource.Table(self._runs_table_name)
        response = await table.get_item(Key={"run_id": str(run_id)}, ConsistentRead=True)
        item = response.get("Item")
        if item is None:
            return None
        return _item_to_header(item)

    async def claim(self, worker_id: WorkerId, now: datetime, lease_seconds: float) -> RunId | None:
        resource = await self._ensure_resource()
        runs_table = await resource.Table(self._runs_table_name)
        now_micros = _micros(now)
        exclusive_start_key: dict[str, Any] | None = None
        scanned = 0
        while scanned < _CLAIM_MAX_CANDIDATES:
            query_kwargs: dict[str, Any] = {
                "IndexName": _CLAIM_GSI_NAME,
                "KeyConditionExpression": "claim_gsi_pk = :pk AND claim_gsi_sk <= :now",
                "ExpressionAttributeValues": {":pk": _CLAIM_GSI_PK_VALUE, ":now": now_micros},
                "ScanIndexForward": True,
                "Limit": _QUERY_PAGE_SIZE,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await runs_table.query(**query_kwargs)
            items = response.get("Items", [])
            scanned += len(items)
            # Ties on claim_gsi_sk (most commonly several RUNNABLE Runs, none
            # scheduled, all sorting to 0) have no guaranteed order from the
            # index itself. Sorting each page by created_at gives the same
            # first-created-first-claimed fairness InMemoryStore.claim gets for
            # free by walking an insertion-ordered dict, without making the
            # index key itself a correctness hazard (see _claim_sort_key).
            for item in sorted(items, key=lambda it: it["created_at"]):
                run_id = RunId(item["run_id"])
                if await self._try_claim_one(runs_table, run_id, worker_id, now, lease_seconds):
                    return run_id
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return None

    async def _try_claim_one(
        self,
        runs_table: Any,
        run_id: RunId,
        worker_id: WorkerId,
        now: datetime,
        lease_seconds: float,
    ) -> bool:
        """One conditional UpdateItem, re-checking the exact claimability
        predicate ``_claim_sort_key`` approximated, against the base table
        with strong consistency and the real ``now``. This is what makes a
        stale GSI candidate safe rather than merely likely-safe.
        """
        new_lease_expires_at = now + timedelta(seconds=lease_seconds)
        try:
            await runs_table.update_item(
                Key={"run_id": str(run_id)},
                UpdateExpression=(
                    "SET #st = :running, lease_holder = :worker, "
                    "lease_expires_at = :lease_iso, "
                    "attempt_count = attempt_count + :one, "
                    "claim_gsi_sk = :lease_micros"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(runnable_at) OR runnable_at <= :now_iso) AND ("
                    "#st = :runnable OR "
                    "(#st = :running AND "
                    "(attribute_not_exists(lease_expires_at) OR lease_expires_at <= :now_iso))"
                    ")"
                ),
                ExpressionAttributeNames={"#st": "state"},
                ExpressionAttributeValues={
                    ":running": RunState.RUNNING.value,
                    ":runnable": RunState.RUNNABLE.value,
                    ":worker": str(worker_id),
                    ":lease_iso": _iso(new_lease_expires_at),
                    ":lease_micros": _micros(new_lease_expires_at),
                    ":one": 1,
                    ":now_iso": _iso(now),
                },
            )
        except ClientError as err:
            if _error_code(err) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    async def renew(
        self, run_id: RunId, worker_id: WorkerId, now: datetime, lease_seconds: float
    ) -> bool:
        resource = await self._ensure_resource()
        runs_table = await resource.Table(self._runs_table_name)
        new_lease_expires_at = now + timedelta(seconds=lease_seconds)
        try:
            await runs_table.update_item(
                Key={"run_id": str(run_id)},
                UpdateExpression="SET lease_expires_at = :lease_iso, claim_gsi_sk = :lease_micros",
                ConditionExpression="lease_holder = :worker",
                ExpressionAttributeValues={
                    ":worker": str(worker_id),
                    ":lease_iso": _iso(new_lease_expires_at),
                    ":lease_micros": _micros(new_lease_expires_at),
                },
            )
        except ClientError as err:
            if _error_code(err) == "ConditionalCheckFailedException":
                # No item, no lease_holder attribute, or a different holder:
                # the Store contract collapses all three to the same answer,
                # and a nonexistent item never gets created by this branch
                # because the failed condition blocks the write entirely.
                return False
            raise
        return True

    async def release(self, run_id: RunId, worker_id: WorkerId, state: RunState) -> None:
        runs_table = await (await self._ensure_resource()).Table(self._runs_table_name)
        for _ in range(_MAX_OPTIMISTIC_RETRIES):
            response = await runs_table.get_item(Key={"run_id": str(run_id)}, ConsistentRead=True)
            item = response.get("Item")
            if item is None or item.get("lease_holder") != str(worker_id):
                # Tolerant by design (Store.release docstring): no holder, or
                # a different holder already reclaimed the lease.
                return
            new_header = _item_to_header(item).model_copy(
                update={"state": state, "lease_holder": None, "lease_expires_at": None}
            )
            try:
                await runs_table.put_item(
                    Item=_header_to_item(new_header),
                    ConditionExpression="lease_holder = :worker",
                    ExpressionAttributeValues={":worker": str(worker_id)},
                )
            except ClientError as err:
                if _error_code(err) == "ConditionalCheckFailedException":
                    continue  # a concurrent write moved the item; re-read and retry
                raise
            else:
                return
        raise StoreError(
            f"release: could not settle run {run_id} after {_MAX_OPTIMISTIC_RETRIES} retries"
        )

    async def settle_inline(self, run_id: RunId) -> bool:
        """One conditional ``UpdateItem``, touching two attributes and no others.

        Not a read followed by a full ``PutItem``: a whole-item replace built
        from a snapshot silently reverts anything written between the two
        calls, and the condition cannot catch it, because a concurrent writer
        that changes some other attribute leaves the state ``nested`` and the
        condition still holds. An update names the attributes it changes, so
        everything it does not name survives by construction.

        The GSI attributes go because the deadline index is sparse: a settled
        Run ages out of ``overdue_deadlines`` by no longer carrying them, the
        same way ``release`` and every other settle leaves it.
        """
        runs_table = await (await self._ensure_resource()).Table(self._runs_table_name)
        try:
            await runs_table.update_item(
                Key={"run_id": str(run_id)},
                UpdateExpression="SET #state = :settled REMOVE deadline_gsi_pk, deadline_gsi_sk",
                # Unlike ``release``, a failed condition here is an answer and
                # not a race to retry: it means the Run is not NESTED, and a
                # Run in any other state is one this may not touch. A
                # nonexistent item fails the same way and is the same answer.
                ConditionExpression="#state = :nested",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":settled": RunState.SETTLED.value,
                    ":nested": RunState.NESTED.value,
                },
            )
        except ClientError as err:
            if _error_code(err) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    async def set_runnable_at(self, run_id: RunId, runnable_at: datetime | None) -> None:
        runs_table = await (await self._ensure_resource()).Table(self._runs_table_name)
        for _ in range(_MAX_OPTIMISTIC_RETRIES):
            response = await runs_table.get_item(Key={"run_id": str(run_id)}, ConsistentRead=True)
            item = response.get("Item")
            if item is None:
                raise RunNotFound(run_id)
            current_header = _item_to_header(item)
            update: dict[str, object] = {"runnable_at": runnable_at}
            if runnable_at is None and current_header.state is RunState.SUSPENDED:
                # Clearing the parking on a suspended Run is a resume, and
                # resume is the only path back to claimable because nobody
                # holds the lease at that point.
                update["state"] = RunState.RUNNABLE
            new_header = current_header.model_copy(update=update)
            try:
                await runs_table.put_item(
                    Item=_header_to_item(new_header),
                    ConditionExpression="#st = :state",
                    ExpressionAttributeNames={"#st": "state"},
                    ExpressionAttributeValues={":state": current_header.state.value},
                )
            except ClientError as err:
                if _error_code(err) == "ConditionalCheckFailedException":
                    continue  # the header moved between our read and our write; retry
                raise
            else:
                return
        raise StoreError(
            f"set_runnable_at: could not update run {run_id} after "
            f"{_MAX_OPTIMISTIC_RETRIES} retries"
        )

    # -- supervision --------------------------------------------------------

    async def expired_leases(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        resource = await self._ensure_resource()
        runs_table = await resource.Table(self._runs_table_name)
        now_micros = _micros(now)
        headers: list[RunHeader] = []
        exclusive_start_key: dict[str, Any] | None = None
        while len(headers) < limit:
            query_kwargs: dict[str, Any] = {
                "IndexName": _CLAIM_GSI_NAME,
                "KeyConditionExpression": "claim_gsi_pk = :pk AND claim_gsi_sk <= :now",
                "FilterExpression": "#st = :running",
                "ExpressionAttributeNames": {"#st": "state"},
                "ExpressionAttributeValues": {
                    ":pk": _CLAIM_GSI_PK_VALUE,
                    ":now": now_micros,
                    ":running": RunState.RUNNING.value,
                },
                "ScanIndexForward": True,
                "Limit": _QUERY_PAGE_SIZE,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await runs_table.query(**query_kwargs)
            for item in response.get("Items", []):
                headers.append(_item_to_header(item))
                if len(headers) >= limit:
                    break
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return headers

    async def overdue_deadlines(self, now: datetime, limit: int = 100) -> list[RunHeader]:
        resource = await self._ensure_resource()
        runs_table = await resource.Table(self._runs_table_name)
        now_micros = _micros(now)
        headers: list[RunHeader] = []
        exclusive_start_key: dict[str, Any] | None = None
        while len(headers) < limit:
            query_kwargs: dict[str, Any] = {
                "IndexName": _DEADLINE_GSI_NAME,
                "KeyConditionExpression": "deadline_gsi_pk = :pk AND deadline_gsi_sk <= :now",
                "ExpressionAttributeValues": {":pk": _DEADLINE_GSI_PK_VALUE, ":now": now_micros},
                "ScanIndexForward": True,
                "Limit": _QUERY_PAGE_SIZE,
            }
            if exclusive_start_key is not None:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = await runs_table.query(**query_kwargs)
            for item in response.get("Items", []):
                headers.append(_item_to_header(item))
                if len(headers) >= limit:
                    break
            exclusive_start_key = response.get("LastEvaluatedKey")
            if exclusive_start_key is None:
                break
        return headers
