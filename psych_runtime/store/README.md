# psych_runtime.store

## Owns
The `Store` port, the in-memory, PostgreSQL, MySQL and DynamoDB adapters, and the
contract suite all four must pass.

The `BlobStore` port, the in-memory, filesystem and S3 adapters, and the
contract suite all three must pass (`blob.py`, `blob_memory.py`, `blob_fs.py`,
`blob_s3.py`, `blob_contract.py`). This exists so an oversized tool result has
somewhere to live: DESIGN.md §10.8 says the log always holds the whole thing,
DynamoDB caps one item at 400KB, and above that size the only way both stay
true is for the record to hold a reference instead of the payload. See
`blob.py`'s module docstring for the full reasoning.

## Does not own
A database. Psych uses one through this port and never provisions or migrates the
consumer's (DESIGN.md §1). No queue broker either: a runnable Run is one whose
lease is unheld or expired, so `claim()` is the queue (§7).

Retention for either port. Psych does not expire, garbage-collect or size-cap
what accumulates in a `Store` or a `BlobStore`; that lifecycle policy is the
consumer's, the same as the database or bucket itself (DESIGN.md §1).

## Ports
Defines `Store`, the optional `Queue`, and `BlobStore`.

## The rules that live here
Conditional writes only. No transactions, no joins, no `SELECT ... FOR UPDATE`,
which is what makes DynamoDB a first-class target rather than a compromise (§7).
The contract suite is the deliverable as much as the adapters: divergent
implementations with no shared test are divergent behaviours waiting to be
discovered in production. The in-memory adapters test other components and
never prove either contract (§22).

A `BlobKey` is `(tenant, run_id, call_id)`, never a bare string: the same
discipline DESIGN.md §10.4 requires for pooling an MCP client by
`(scope, server, credential)` rather than by URL alone, so one tenant's blob
can never be reached through another tenant's handle.

## Status

`Store`: `port.py`, `memory.py`, `postgres.py`, `mysql.py`, `dynamodb.py` and
`contract.py` are all done, each adapter with a
`tests/functional/test_store_<name>.py` subclassing `StoreContractSuite`.

`BlobStore`: `blob.py`, `blob_memory.py`, `blob_fs.py`, `blob_s3.py` and
`blob_contract.py` are all done, each adapter with a
`tests/functional/test_blob_<name>.py` subclassing `BlobStoreContractSuite`.
`test_blob_s3.py` runs against a real local S3-compatible server (`moto`'s
`ThreadedMotoServer`) rather than skipping outright; one contract test is
skipped there specifically, with the reason recorded in that file, for a
metadata-preservation limitation in that local test double rather than in
`S3BlobStore` itself.

## Adding an adapter

**Store:** subclass `StoreContractSuite` from `contract.py` in a
`tests/functional/test_store_<name>.py` file and override its `store` fixture to
return a fresh instance of the adapter. Nothing else in the suite changes; that
is what keeps every implementation honest against one definition of correct
rather than an independent reading of DESIGN.md §7 per adapter.

**BlobStore:** the same shape, subclassing `BlobStoreContractSuite` from
`blob_contract.py` in a `tests/functional/test_blob_<name>.py` file with a
`blob_store` fixture.
