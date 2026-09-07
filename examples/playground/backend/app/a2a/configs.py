"""Push notification configurations, kept where a restart cannot lose them.

§4.3.1's config is a promise: register a webhook and this agent will call it
when the task moves. A store that emptied when the process restarted would
keep the API and break the promise, which is the same argument
`app.memory_store` makes at more length. So this is the same shape as that
one: a JSON file, a lock, an atomic write-and-rename.

The credential is in that file, which is why the read paths on the router
never echo it back (§13.2: "Authentication tokens ... SHOULD be treated as
secrets"). A production consumer puts these in their own secret store; the
point of the playground is to show where the seam is, not to be that store.

Configs are keyed by `(account, task, config id)`. The account is in the key
rather than only checked at the route, so a config id copied from another
tenant's response finds nothing here even if a future route forgets to check
- which is the same reasoning as pooling by `(scope, server, credential)`
rather than by URL.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.a2a.models import TaskPushNotificationConfig

__all__ = ["PushConfigStore"]


class _StoredConfig(BaseModel):
    """One registration, plus who owns it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    config: TaskPushNotificationConfig


class _ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configs: list[_StoredConfig] = Field(default_factory=list)


class PushConfigStore:
    """Every webhook this installation will call, by account and task."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._configs: dict[tuple[str, str, str], TaskPushNotificationConfig] = {}
        self._loaded = False

    def _load_unlocked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        parsed = _ConfigFile.model_validate_json(self._path.read_text())
        for stored in parsed.configs:
            key = (stored.account_id, stored.config.task_id or "", stored.config.id or "")
            self._configs[key] = stored.config

    def _flush_unlocked(self) -> None:
        if self._path is None:
            return
        state = _ConfigFile(
            configs=[
                _StoredConfig(account_id=account_id, config=config)
                for (account_id, _task, _id), config in self._configs.items()
            ]
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(self._path)

    async def create(
        self, account_id: str, config: TaskPushNotificationConfig
    ) -> TaskPushNotificationConfig:
        """Register one webhook, minting an id when the client did not send one.

        §4.3.1 lets the client choose the id, which is what makes registration
        idempotent for a client that retries: the same id replaces rather than
        duplicates, so a retried request does not double every notification.
        """
        stored = config if config.id else config.model_copy(update={"id": uuid.uuid4().hex[:16]})
        async with self._lock:
            self._load_unlocked()
            self._configs[(account_id, stored.task_id or "", stored.id or "")] = stored
            self._flush_unlocked()
        return stored

    async def get(
        self, account_id: str, task_id: str, config_id: str
    ) -> TaskPushNotificationConfig | None:
        async with self._lock:
            self._load_unlocked()
            return self._configs.get((account_id, task_id, config_id))

    async def list(self, account_id: str, task_id: str) -> tuple[TaskPushNotificationConfig, ...]:
        async with self._lock:
            self._load_unlocked()
            return tuple(
                config
                for (owner, task, _id), config in self._configs.items()
                if owner == account_id and task == task_id
            )

    async def delete(self, account_id: str, task_id: str, config_id: str) -> bool:
        """Remove one registration. `False` when there was nothing to remove,
        which the router turns into a 404 rather than a silent success."""
        async with self._lock:
            self._load_unlocked()
            existed = self._configs.pop((account_id, task_id, config_id), None) is not None
            if existed:
                self._flush_unlocked()
            return existed
