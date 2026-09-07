"""The webhook sender: the one part of push notifications that does IO.

`psych_runtime.a2a.push` decides what a notification contains, how it authenticates
and how long to wait between attempts. This posts it, through
`HttpTransport` -- the same egress seam the model client, HTTP tools and MCP
go through (DESIGN.md §14). That is also where §13.2's SSRF requirement is
met: a deployment that wants private address ranges refused writes one
`EgressPolicy`, and it covers this route along with every other.

## Why a background task rather than inline

A notification is for a client that is *not* holding a connection open, so
sending one must not delay the response to whoever triggered the work. The
sender therefore watches a Run's log in the background and posts each event as
it lands. If the process dies mid-Run, undelivered notifications are lost;
that is a real limitation of a single-process example and the honest fix in a
production consumer is their own queue, which is exactly the kind of thing
DESIGN.md §1 refuses to put in Psych.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from app.a2a.service import A2AService
from app.accounts import Account
from app.auth import scope_for
from psych_runtime.a2a.models import StreamResponse, TaskPushNotificationConfig
from psych_runtime.a2a.push import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    backoff_delays,
    push_body,
    push_headers,
)
from psych_runtime.core.ids import RunId
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport

__all__ = ["PushSender"]

_LOG = logging.getLogger("app.a2a.push")


class PushSender:
    """Delivers task events to the webhooks registered for a task."""

    def __init__(
        self,
        *,
        transport: HttpTransport,
        service: A2AService,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport
        self._service = service
        self._max_attempts = max_attempts
        self._timeout = timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    def watch(self, account: Account, run_id: RunId) -> None:
        """Follow one Run in the background and notify as it moves.

        Started only for a task that has at least one webhook registered by the
        time the Run is dispatched, and re-checked as events arrive: a client
        that registers a webhook mid-Run still gets the rest.
        """
        task = asyncio.create_task(self._watch(account, run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Cancel every in-flight watcher. Called from the app's lifespan."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _watch(self, account: Account, run_id: RunId) -> None:
        try:
            async for event in self._service.subscribe(account, run_id):
                configs = await self._service.configs_for(account.id, str(run_id))
                if configs:
                    await self.deliver(configs, event, scope=scope_for(account))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A webhook failing must never take the Run with it: the Run is the
            # work, and the notification is a courtesy about the work.
            _LOG.exception("push notification watcher for %s stopped", run_id)

    async def deliver(
        self,
        configs: Sequence[TaskPushNotificationConfig],
        event: StreamResponse,
        *,
        scope: Scope,
    ) -> None:
        """Post one event to every registered webhook, retrying each.

        ``scope`` is the task owner's, so an egress policy can refuse one
        tenant's webhook host without refusing another's -- the same reason
        every other call through the seam carries one.
        """
        for config in configs:
            await self._deliver_one(config, event, scope=scope)

    async def _deliver_one(
        self, config: TaskPushNotificationConfig, event: StreamResponse, *, scope: Scope
    ) -> None:
        body = push_body(event)
        headers = push_headers(config)
        delays = list(backoff_delays(attempts=self._max_attempts))
        for attempt in range(self._max_attempts):
            try:
                response = await self._transport.request(
                    "POST",
                    config.url,
                    scope=scope,
                    headers=headers,
                    json=body,
                    timeout=self._timeout,
                )
            except Exception as err:
                _LOG.warning("push to %s failed (%s)", config.url, err)
            else:
                # §13.2: "Clients MUST respond with HTTP 2xx status codes to
                # acknowledge successful receipt", so anything else is a
                # delivery that did not happen.
                if 200 <= response.status_code < 300:
                    return
                _LOG.warning("push to %s answered HTTP %s", config.url, response.status_code)
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
        _LOG.warning("giving up on push to %s after %d attempts", config.url, self._max_attempts)
