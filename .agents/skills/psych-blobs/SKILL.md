---
name: psych-blobs
description: >-
  Handle oversized tool results in Psych: the `BlobStore` port with in-memory,
  filesystem and S3 adapters, `BlobKey` as a tenant-scoped address, the
  `read_tool_output` handle the model uses, and the two independent thresholds
  (elision, a Spec field; offload, a Runtime one). Use whenever a Psych tool
  returns a large payload, someone hits a DynamoDB 400KB item limit, asks why a
  tool result is truncated for the model, wires `blob=` on the Runtime, or asks
  where large results are stored. Read before returning big payloads from a
  tool, because without `blob=` a result too large to sit inline records an
  explicit failure, and because elision and offload answer different questions
  and are deliberately not the same number.
---

# Large results and blobs

Two decisions, deliberately independent because they answer different questions.

| | Decides | Where it lives | Default |
|---|---|---|---|
| **Elision** | What the **model** sees | `Limits.large_result_bytes`, a **Spec** field | 32 KB |
| **Offload** | Where the **bytes** physically live | `Runtime.blob_offload_bytes` | 300 KB, under DynamoDB's 400 KB item cap |

Elision is about context economy and belongs to the agent's own budget. Offload
is about storage engines. Keeping offload off the Spec is what makes a Version
hash independent of which backend a deployment happens to be wired to: the same
Spec must run identically on all four stores.

A 100KB result can sit in the log inline while still being elided for the model.
A 5MB one cannot sit in the log on any adapter, because the offload threshold is
chosen against the tightest backend and applied uniformly so behaviour does not
vary by which store you picked.

An offloaded result **always** elides too, regardless of the Spec's own
threshold, because the record's `result` is `None` once the payload moves and
the conversation projection needs a handle to know that.

## The guarantee that is kept, and the one that changes

The design says a large result is elided in the model's context but "the log
always holds the whole thing". DynamoDB caps one item at 400KB. Above that,
"the log holds the whole result" and "DynamoDB is first-class" cannot both
describe the same write, because there is no such write.

What gives is **where the bytes live**, not the guarantee. Nothing a tool
returned is ever discarded, and every byte stays durably retrievable for as long
as the Run's log exists. It stops being one physical write of the
`ToolCallFinished` record: above the threshold the record carries a reference, a
size and a content type, and the payload lives in a `BlobStore`.

## Wiring

```python
from psych_runtime.store.blob_fs import FilesystemBlobStore
from psych_runtime.store.blob_s3 import S3BlobStore
from psych_runtime.store.blob_memory import InMemoryBlobStore

runtime = psych_runtime.Runtime(..., blob=FilesystemBlobStore(root="/var/lib/psych/blobs"))
```

Without `blob=`, large results still elide for the model exactly as before, but
one large enough to need offload records an **explicit failure** rather than
being written inline. That failure is the honest answer for a consumer who has
not wired one up, not a reason to force a blob store into every Runtime that
never produces results this large.

`FilesystemBlobStore` creates `root` if it does not exist. Retention and cleanup
for it are yours, the same as any other directory or bucket.

## How the model reads an elided result

It gets a handle and calls `read_tool_output`. That is the only handle Psych
has, which is why `invalid_deferred_handle` guards it.

`read_tool_output` **never constructs a `BlobKey` from anything the model
sent.** It resolves the handle against the Run's own recorded handles first, so
a model cannot address a blob by making one up.

## `BlobKey` is `(tenant, run_id, call_id)`, never a bare string

The same discipline as pooling an MCP client by `(scope, server, credential)`
rather than by URL. A flat string key is that mistake waiting to happen: a
handle is an opaque string a call argument could carry, and if the address a
caller could construct from one ever came from untrusted input, the tenant that
minted it would not be part of the address at all.

Every adapter folds the tenant into where bytes are actually kept, a filesystem
subdirectory or an S3 key prefix, so two tenants cannot collide even if a
`run_id`/`call_id` pair somehow repeated.

## Compact bytes, both ends

A structured result's preview is rendered with `separators=(",", ":")`. The
threshold and the preview the model reads are measured from the same compact
bytes, never from pretty-printed whitespace nobody downstream pays for. A `str`
result never reaches that `json.dumps` call at all: it is text the tool
returned, not a structure Psych serialises, so it is left byte for byte alone.

## Adding an adapter

Same shape as a `Store`: subclass `BlobStoreContractSuite` from
`psych_runtime.store.blob_contract` in `tests/functional/test_blob_<name>.py` with a
`blob_store` fixture.

Psych's own S3 tests run against a real local S3-compatible server (`moto`'s
`ThreadedMotoServer`) rather than skipping.

## Reading it back in a report

```python
for call in report.tool_calls:
    call.result_bytes  # the full size, always
    call.result_handle  # set when offloaded
    call.preview  # what the model saw when elided
```

## Gotchas

- **`read_tool_output` is a reserved tool name.**
- **Elision is not truncation of the record.** The log's record is complete or
  carries a reference; only what reaches the model is shortened.
- **`Limits.large_result_bytes` changes the Version hash.** Offload's threshold
  does not exist on the Spec at all, which is the point.
