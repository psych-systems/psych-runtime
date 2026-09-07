"""The BlobStore port.

## The tension this exists to resolve

DESIGN.md §10.8 says a large tool result is elided in the model's context but
"the log always holds the whole thing." DESIGN.md §7 says DynamoDB is a
first-class target for the Store, not a compromise, on the strength of one
constraint: every write is a single conditional operation against one item or
row. DynamoDB's own limit on that item is 400KB. Above it, "the log holds the
whole result" and "DynamoDB is first-class" cannot both describe the same
write, because there is no such write.

What gives is *where the bytes live*, not the guarantee. "The log always holds
the whole thing" is kept in the sense that actually matters to DESIGN.md §10.8:
nothing a tool returned is ever discarded, and every byte of it stays durably
retrievable for as long as the Run's log exists. It stops being kept as one
physical write of the `ToolCallFinished` record. Above a threshold sized against
the tightest backend (`psych_runtime.tools.large_results`'s offload threshold, well
under DynamoDB's 400KB item cap with headroom for the record's other fields),
the record carries a reference, a size and a content type instead of the
payload, and the payload itself lives here: a BlobStore, keyed by
`(tenant, run_id, call_id)`.

This is a second, independent decision from elision. Elision decides what the
*model* sees and is a Spec-level threshold (`Limits.large_result_bytes`),
because it is about context economy and belongs to the agent's own budget.
Offload decides where the *bytes* physically live and is a Runtime-level
concern (`Runtime.blob`, `psych_runtime.tools.large_results`'s offload threshold), not
part of the Spec: a Run built in Python or from a dict must produce the same
Version hash and run identically (DESIGN.md §23 item 1) regardless of which
Store or BlobStore backend a given deployment happens to be wired to, so which
storage engine can and cannot hold one item inline is not something a Spec's
content hash should ever depend on. A 100KB result can sit in the log inline
while still being elided for the model; a 5MB one cannot sit in the log at all,
on any adapter, because the offload threshold is chosen against the tightest
one and applied uniformly so behaviour does not vary by which Store a consumer
picked (DESIGN.md §23 item 8).

## Tenant scoping: the same discipline as `Scope.pool_key`

DESIGN.md §10.4 is blunt about MCP client pooling: never pool by URL alone,
pool by `(scope, server, credential)`, because pooling by URL alone will
eventually hand tenant A's connection to tenant B. A flat string key for a
blob is the same mistake waiting to happen: a handle is just an opaque string
a call argument could carry, and if the address a caller can construct from
one were ever taken directly from untrusted input, the tenant that minted it
would not be part of the address at all. `BlobKey` makes tenant a mandatory,
structural field of the address instead of a convention someone has to
remember. Every adapter below folds it into where the bytes are actually kept
(a filesystem subdirectory, an S3 key prefix), so two tenants can never collide
even if a `run_id`/`call_id` pair somehow repeated across them.

The stronger guarantee lives one layer up, in `psych_runtime.tools.large_results`:
`read_tool_output` never constructs a `BlobKey` from anything the model sent.
It resolves the model's handle against `RunStateView.result_handles` first
(already scoped to Records this exact Run produced), then builds the key from
that Run's own `scope` and `run_id`, never from the stored
`ToolCallFinished.result_blob_key` string. The stored string is kept purely as
a human-readable reference for a report or an operator; it is never trusted as
an address.

## Retention is the consumer's

Same as the database (DESIGN.md §1): Psych does not expire, garbage-collect or
size-cap what accumulates here. A consumer wiring up a `BlobStore` needs their
own lifecycle policy for it, exactly as they need one for their Postgres
instance or their S3 bucket's other objects. An S3 lifecycle rule keyed on the
prefix this module writes under, a cron job on the filesystem adapter's root, or
simply deleting a Run's blobs when that Run's log itself is deleted. Nothing in
this module accumulates an expiry story on the consumer's behalf, on purpose:
doing so quietly would be one more way Psych could turn into the platform it
refuses to be (DESIGN.md §1's refusals).

## Range reads, and why they matter enough to be part of the port

`read_tool_output` answers an offset/limit window, not "give me everything."
Fetching an entire 5MB object out of S3 to hand back a 2KB window is the wrong
shape twice over: once in latency, once in the egress bill. `get`'s `offset`
and `length` let a caller ask for exactly the byte range it needs, and `head`
lets it learn size, content type and (optionally) small string metadata
without reading any content at all. `psych_runtime.tools.large_results` uses `head`
plus a growing prefix `get` to answer a plain slice from as little of a large
text result as it can, and falls back to reading the whole thing only when the
window cannot be answered from a bounded prefix (a pattern search, which needs
`total_matches` across the whole result, or a JSON-typed result, which cannot
be parsed from an arbitrary byte prefix).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.errors import StoreError
from psych_runtime.core.ids import RunId, ToolCallId
from psych_runtime.core.scope import Scope

__all__ = ["BlobKey", "BlobMetadata", "BlobNotFound", "BlobStore", "blob_key"]


@dataclass(frozen=True, slots=True)
class BlobKey:
    """A blob's address: tenant, Run and call, never fewer.

    Deliberately not a bare string. See the module docstring's tenant-scoping
    section: the point of this type existing is that a caller cannot construct
    a valid address without a tenant, the same way `Scope.pool_key` makes
    tenancy a mandatory half of an MCP pool key rather than something a call
    site has to remember to include.
    """

    tenant: str
    run_id: RunId
    call_id: ToolCallId

    def __post_init__(self) -> None:
        # A Scope's tenant is a free-form consumer string (psych_runtime.core.scope
        # places no charset restriction on it), unlike run_id and call_id,
        # which are always minted by this codebase. The filesystem adapter
        # turns every field of this key into a path segment; a tenant string
        # of ".." or one containing "/" would walk outside the directory that
        # segment was supposed to confine it to. Checked here, once, for every
        # field and every adapter, rather than in each adapter that happens to
        # care today: an adapter added later inherits the guarantee for free.
        segments = (("tenant", self.tenant), ("run_id", self.run_id), ("call_id", self.call_id))
        for field_name, value in segments:
            _check_key_segment(field_name, value)

    def __str__(self) -> str:
        return f"{self.tenant}/{self.run_id}/{self.call_id}"


def _check_key_segment(field_name: str, value: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(
            f"BlobKey.{field_name} is {value!r}, which cannot address a single "
            "path segment or object key component safely. This must never come "
            "from unvalidated input: tenant is the one field of a BlobKey a "
            "consumer controls, so a Scope with a hostile tenant string is "
            "refused here rather than followed into a filesystem or an S3 key."
        )


def blob_key(scope: Scope, run_id: RunId, call_id: ToolCallId) -> BlobKey:
    """The one place a `BlobKey` gets built, so every call site agrees on the
    fields that make one tenant's blob unreachable from another's."""
    return BlobKey(tenant=scope.tenant, run_id=run_id, call_id=call_id)


class BlobNotFound(StoreError):
    """No blob exists at this key.

    A `StoreError`, not a bespoke root: a missing blob is the same shape of
    problem as a missing Run or a missing Version, and a consumer catching
    `StoreError` should not have to know a fourth name to catch this too.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"no blob at {key}")


class BlobMetadata(BaseModel):
    """What `head` returns: everything about a blob except its content.

    Attributes:
        size: the exact byte length of the stored content.
        content_type: how to decode it, exactly as passed to `put`.
        metadata: small string key-value pairs stored alongside the content,
            for a caller that wants to attach something cheap to check without
            reading the payload. `psych_runtime.tools.large_results` uses this for a
            text result's line count, computed once at offload time so a
            later windowed read can report `total_lines` accurately without
            reading the whole object to recount it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int = Field(ge=0)
    content_type: str = Field(min_length=1)
    metadata: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class BlobStore(Protocol):
    """Content-addressable-by-caller storage for payloads too large for a log
    record, put/get/delete by `BlobKey`, with range reads.

    Every method is one operation against one object: no listing, no
    versioning, no multipart upload management exposed to a caller. The three
    adapters (`psych_runtime.store.blob_memory`, `psych_runtime.store.blob_fs`,
    `psych_runtime.store.blob_s3`) all pass `psych_runtime.store.blob_contract`, the same
    discipline `psych_runtime.store.contract` applies to `Store`.
    """

    async def put(
        self,
        key: BlobKey,
        content: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """Write `content` at `key`, replacing whatever was there.

        Not conditional: unlike `Store.append`, a blob key is minted once per
        tool call and never contended, so there is no second-writer race to
        guard against here.
        """
        ...

    async def get(self, key: BlobKey, *, offset: int = 0, length: int | None = None) -> bytes:
        """The bytes at `key`, or a slice of them.

        `offset` and `length` both count bytes. `length=None` means "to the
        end." A range past the end of the content returns whatever is left,
        including nothing, the same way a Python slice does; it is not an
        error to ask for more than exists.

        Raises:
            BlobNotFound: no blob exists at `key`.
        """
        ...

    async def head(self, key: BlobKey) -> BlobMetadata | None:
        """`key`'s metadata with no content read, or `None` if it does not
        exist. `None` rather than raising: a caller checking existence should
        not have to catch an exception to do it."""
        ...

    async def delete(self, key: BlobKey) -> None:
        """Remove the blob at `key`.

        Idempotent: deleting a key that does not exist is not an error, the
        same tolerance `Store.release` extends to a non-holder (DESIGN.md §7):
        a caller cleaning up after itself should never have to check existence
        first to avoid an exception on a double delete.
        """
        ...
