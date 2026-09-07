"""The S3 BlobStore adapter.

## Why this does not go through the egress seam

DESIGN.md §14 puts the model client, HTTP tools and MCP behind one
`psych_runtime.model.egress` seam because those calls are tenant-directed: their
destination is data a Run's Spec or a tool call chose, so a consumer's egress
policy has to see and can refuse every one. `psych_runtime.store.dynamodb` makes the
same call for `aioboto3`'s DynamoDB client and does not route it through that
seam either, for the same reason applied to storage: the endpoint an adapter
talks to is fixed at construction by whoever wires up the `Runtime`, not by
anything a tenant, a model or a tool argument supplies. This adapter is
consistent with that precedent rather than an exception to it: it is
operator-configured infrastructure, exactly like the Postgres DSN, the MySQL
host or the DynamoDB endpoint a consumer already points Psych at directly.

## What one object holds

A blob's content type and small metadata bag both ride as S3 object metadata
(`ContentType` and `Metadata`), so nothing here needs a sidecar object the way
`psych_runtime.store.blob_fs` needs a sidecar file: S3 already has a place for exactly
this. `Metadata` values must be strings, which is what
`psych_runtime.store.blob.BlobMetadata.metadata` already requires.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aioboto3
from botocore.exceptions import ClientError

from psych_runtime.store.blob import BlobKey, BlobMetadata, BlobNotFound

__all__ = ["S3BlobStore"]


def _error_code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "")


class S3BlobStore:
    """A `BlobStore` backed by one S3 bucket (or an S3-compatible endpoint).

    Implements the `BlobStore` protocol structurally; there is no base class
    to inherit because the port is a `Protocol`. Every key's object name is
    `<prefix>/<tenant>/<run_id>/<call_id>`, mirroring
    `psych_runtime.store.blob_fs`'s directory layout so the two adapters are easy to
    reason about side by side.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "psych-blobs",
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {"region_name": region_name}
        if endpoint_url is not None:
            self._client_kwargs["endpoint_url"] = endpoint_url
        if aws_access_key_id is not None:
            self._client_kwargs["aws_access_key_id"] = aws_access_key_id
        if aws_secret_access_key is not None:
            self._client_kwargs["aws_secret_access_key"] = aws_secret_access_key

        self._client_cm: Any | None = None
        self._client: Any | None = None

    def _object_key(self, key: BlobKey) -> str:
        return f"{self._prefix}/{key.tenant}/{key.run_id}/{key.call_id}"

    async def _ensure_client(self) -> Any:
        if self._client is None:
            self._client_cm = self._session.client("s3", **self._client_kwargs)
            self._client = await self._client_cm.__aenter__()
        return self._client

    async def close(self) -> None:
        """Release the underlying connection. Idempotent."""
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
            self._client_cm = None
            self._client = None

    async def __aenter__(self) -> S3BlobStore:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def put(
        self,
        key: BlobKey,
        content: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        client = await self._ensure_client()
        await client.put_object(
            Bucket=self._bucket,
            Key=self._object_key(key),
            Body=content,
            ContentType=content_type,
            Metadata=dict(metadata or {}),
        )

    async def get(self, key: BlobKey, *, offset: int = 0, length: int | None = None) -> bytes:
        client = await self._ensure_client()
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": self._object_key(key)}
        if offset != 0 or length is not None:
            end = "" if length is None else str(offset + length - 1)
            kwargs["Range"] = f"bytes={offset}-{end}"
        try:
            response = await client.get_object(**kwargs)
        except ClientError as err:
            if _error_code(err) in {"NoSuchKey", "404"}:
                raise BlobNotFound(str(key)) from err
            raise
        async with response["Body"] as body:
            return bytes(await body.read())

    async def head(self, key: BlobKey) -> BlobMetadata | None:
        client = await self._ensure_client()
        try:
            response = await client.head_object(Bucket=self._bucket, Key=self._object_key(key))
        except ClientError as err:
            if _error_code(err) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return BlobMetadata(
            size=response["ContentLength"],
            content_type=response.get("ContentType", "application/octet-stream"),
            metadata=dict(response.get("Metadata", {})),
        )

    async def delete(self, key: BlobKey) -> None:
        client = await self._ensure_client()
        await client.delete_object(Bucket=self._bucket, Key=self._object_key(key))
