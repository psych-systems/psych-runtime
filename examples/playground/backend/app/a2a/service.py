"""The A2A operations, in terms of Runs. No HTTP in this file.

`psych_runtime.a2a` holds the protocol and knows nothing about a store. `router.py`
holds the routes and knows nothing about the protocol. This is what joins them:
eleven operations, each one a few calls into `psych` and one call into
`psych_runtime.a2a.mapping`.

Keeping it out of the router is not tidiness. It is what makes both bindings
provably identical, which §5.1 requires of an agent that offers two: the
JSON-RPC route and the REST route call the *same method here*, so there is no
second implementation to drift.

## What a task is here

A task is a Run, and a context is a conversation. That is not a translation
layer this file invents; it is what `psych_runtime.a2a.mapping` established, and this
file only supplies the store reads that make it concrete:

- `taskId` is a `RunId`. §3.4.2 forbids a client minting one, and `dispatch`
  mints it server-side anyway.
- `contextId` is the root of the `continues_run_id` chain, walked through the
  playground's own run index.
- A message naming an existing task **resumes** it when it is waiting, which
  is how `INPUT_REQUIRED` round-trips without a second task id (§3.4.3).
- A message naming only a context starts a new Run that continues the newest
  Run in that context, which is `dispatch(continues=...)`.

## Which agent

One deployment, many published agents, one A2A endpoint. That is exactly the
case the proto's `tenant` field describes -- "an opaque string used for routing
requests to a specific agent or tenant when multiple agents are served behind a
single A2A endpoint" -- so `tenant` here is a playground agent id, and every
Agent Card this backend serves declares it on its interface.

## Isolation

Every operation takes the caller's `Account` and derives its `Scope` through
`scope_for`, the same function the console's own routes use. A task belonging
to another account is answered `TaskNotFoundError` and never
`AccessDenied`, because §13.1 says a server "SHOULD NOT distinguish between
'does not exist' and 'not authorized'": telling a caller that a task exists but
is not theirs is itself a leak.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import psych_runtime
from app.a2a.configs import PushConfigStore
from app.accounts import Account
from app.auth import scope_for
from app.store_index import PlaygroundIndex, RunEntry
from psych_runtime.a2a.errors import (
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from psych_runtime.a2a.mapping import (
    MessageIntent,
    context_id_of,
    resolve_message,
    stream_events,
    task_of,
    task_state_of,
)
from psych_runtime.a2a.models import (
    DEFAULT_PAGE_SIZE,
    INTERRUPTED_STATES,
    TERMINAL_STATES,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    Task,
    TaskPushNotificationConfig,
    TaskState,
)
from psych_runtime.core.answer import split_answer
from psych_runtime.core.ids import RunId, VersionHash
from psych_runtime.core.records import Record
from psych_runtime.core.status import RunStatus
from psych_runtime.core.thread_view import message_views
from psych_runtime.core.version import Version
from psych_runtime.store.port import Store

__all__ = ["DEFAULT_WAIT_SECONDS", "A2AService"]

DEFAULT_WAIT_SECONDS = 120.0
"""How long a blocking `SendMessage` waits before answering with the task as it
stands.

§3.2.2 says a call with `return_immediately=false` waits for a terminal or
interrupted state and sets no bound on that wait, which is fine for a protocol
and impossible for an HTTP handler: a Run may legitimately take longer than any
client's socket timeout. So the wait is bounded and the answer on timeout is
the task in its real state (`WORKING`), never a fabricated terminal one. A
client that wants to keep watching has `SubscribeToTask`, and a client that
would rather not wait at all has `return_immediately`.
"""


class A2AService:
    """Every A2A operation, for one deployment.

    Holds the store, the run index and the push-notification configs, and
    nothing per request: the caller's account is an argument on every method,
    so one instance serves every tenant and no method can accidentally read
    state left by the last caller.
    """

    def __init__(
        self,
        *,
        store: Store,
        index: PlaygroundIndex,
        push_configs: PushConfigStore,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
    ) -> None:
        self._store = store
        self._index = index
        self._push = push_configs
        self._wait = wait_seconds

    # -- tasks --------------------------------------------------------------

    async def send_message(
        self, account: Account, request: SendMessageRequest
    ) -> SendMessageResponse:
        """§3.1.1. Start a task, start one in an existing context, or continue
        one that is waiting."""
        run_id = await self._apply(account, request)
        wait = not (request.configuration is not None and request.configuration.return_immediately)
        if wait:
            await self._await_settled_or_waiting(account, run_id)
        history_length = (
            request.configuration.history_length if request.configuration is not None else None
        )
        task = await self.task_for(account, run_id, history_length=history_length)
        return SendMessageResponse(task=task)

    async def stream_message(
        self, account: Account, request: SendMessageRequest
    ) -> AsyncIterator[StreamResponse]:
        """§3.1.2. The same operation, delivered as it happens.

        The events come from `psych_runtime.stream`, which tails the Run's own log, so
        §3.5.2's ordering requirement is satisfied by the log's sequence rather
        than by anything this method does. A second subscriber to the same task
        reads the same log and therefore sees the same events in the same
        order, which is §3.5.2's multiple-streams rule too.
        """
        run_id = await self._apply(account, request)
        async for event in self.subscribe(account, run_id):
            yield event

    async def subscribe(
        self, account: Account, run_id: RunId, *, refuse_terminal: bool = False
    ) -> AsyncIterator[StreamResponse]:
        """Stream a task's updates until it stops.

        Args:
            refuse_terminal: raise rather than replay for a task that has
                already finished. `SubscribeToTask` (§3.1.6) sets it, because
                the specification makes subscribing to a terminal task an
                `UnsupportedOperationError`. `SendStreamingMessage` does not,
                because it just created the task.

        Raises:
            TaskNotFoundError: no such Run for this account.
            UnsupportedOperationError: as above.
        """
        entry = await self._entry(account, run_id)
        status = await self._status(account, run_id)
        if refuse_terminal and task_state_of(status) in TERMINAL_STATES:
            raise UnsupportedOperationError(
                f"task {run_id} is already {task_state_of(status).value}; there will be no "
                "further updates to subscribe to",
                metadata={"taskId": str(run_id)},
            )
        context_id = await self._context_id(account, entry)

        buffered: list[Record] = []
        emitted = 0
        async for record in psych_runtime.stream(
            self._store, run_id, after=0, scope=scope_for(account)
        ):
            buffered.append(record)
            # Derived from the whole prefix rather than from this record alone:
            # a state change is a property of the log so far, and
            # `stream_events` suppresses a repeat of the state it already
            # emitted only because it can see what came before. The answer is
            # split from the same buffer, so the artifact event carries what
            # the Run actually finished on.
            events = stream_events(buffered, context_id=context_id, answer=split_answer(buffered))
            for event in events[emitted:]:
                yield event
                if _is_final(event):
                    return
            emitted = len(events)

    async def get_task(self, account: Account, request: GetTaskRequest) -> Task:
        """§3.1.3."""
        return await self.task_for(
            account, RunId(request.id), history_length=request.history_length
        )

    async def list_tasks(self, account: Account, request: ListTasksRequest) -> ListTasksResponse:
        """§3.1.4, scoped to the caller.

        §13.1 is explicit that this "MUST only return tasks visible to the
        authenticated client ... Even when contextId or other filter parameters
        are not specified", which is why the account's own index is the source
        and the filters narrow it rather than widening anything.
        """
        entries = await self._index.list_runs(account.id)
        page_size = request.page_size or DEFAULT_PAGE_SIZE
        offset = _page_offset(request.page_token)

        matched: list[Task] = []
        for entry in entries:
            task = await self.task_for(
                account,
                entry.run_id,
                history_length=request.history_length,
                include_artifacts=bool(request.include_artifacts),
            )
            if request.context_id is not None and task.context_id != request.context_id:
                continue
            if request.status is not None and task.status.state is not request.status:
                continue
            if request.status_timestamp_after is not None:
                stamp = task.status.timestamp
                if stamp is None or stamp < request.status_timestamp_after:
                    continue
            matched.append(task)

        page = matched[offset : offset + page_size]
        next_token = str(offset + page_size) if offset + page_size < len(matched) else ""
        return ListTasksResponse(
            tasks=tuple(page),
            next_page_token=next_token,
            page_size=page_size,
            total_size=len(matched),
        )

    async def cancel_task(self, account: Account, request: CancelTaskRequest) -> Task:
        """§3.1.5.

        Raises:
            TaskNotCancelableError: the task already reached a terminal state
                (§5.4). An interrupt on a settled Run would be recorded and
                change nothing, so refusing is the honest answer.
        """
        run_id = RunId(request.id)
        status = await self._status(account, run_id)
        if task_state_of(status) in TERMINAL_STATES:
            raise TaskNotCancelableError(
                f"task {run_id} is already {task_state_of(status).value}",
                metadata={"taskId": str(run_id)},
            )
        await psych_runtime.interrupt(self._store, run_id, reason="canceled over A2A")
        return await self.task_for(account, run_id)

    # -- push notification configs (§3.1.7 to §3.1.10) ----------------------

    async def create_push_config(
        self, account: Account, config: TaskPushNotificationConfig
    ) -> TaskPushNotificationConfig:
        if not config.task_id:
            raise PushNotificationNotSupportedError(
                "a push notification config must name the task it is for; this deployment "
                "does not accept a configuration that would apply to every future task"
            )
        await self._entry(account, RunId(config.task_id))
        return await self._push.create(account.id, config)

    async def get_push_config(
        self, account: Account, request: GetTaskPushNotificationConfigRequest
    ) -> TaskPushNotificationConfig:
        await self._entry(account, RunId(request.task_id))
        found = await self._push.get(account.id, request.task_id, request.id)
        if found is None:
            raise TaskNotFoundError(
                f"no push notification config {request.id!r} for task {request.task_id!r}",
                metadata={"taskId": request.task_id},
            )
        return found

    async def list_push_configs(
        self, account: Account, request: ListTaskPushNotificationConfigsRequest
    ) -> ListTaskPushNotificationConfigsResponse:
        await self._entry(account, RunId(request.task_id))
        configs = await self._push.list(account.id, request.task_id)
        return ListTaskPushNotificationConfigsResponse(configs=tuple(configs))

    async def delete_push_config(
        self, account: Account, request: DeleteTaskPushNotificationConfigRequest
    ) -> None:
        await self._entry(account, RunId(request.task_id))
        if not await self._push.delete(account.id, request.task_id, request.id):
            raise TaskNotFoundError(
                f"no push notification config {request.id!r} for task {request.task_id!r}",
                metadata={"taskId": request.task_id},
            )

    async def configs_for(
        self, account_id: str, task_id: str
    ) -> Sequence[TaskPushNotificationConfig]:
        """Every webhook registered for one task, for the sender."""
        return await self._push.list(account_id, task_id)

    async def version_of(self, version_hash: VersionHash) -> Version | None:
        """One published Version, for the card routes.

        On the service rather than reached for through the store from a route,
        so that every store read this feature makes goes through one object
        and a future consumer swapping the store has one place to look.
        """
        return await self._store.get_version(version_hash)

    # -- shared reads -------------------------------------------------------

    async def task_for(
        self,
        account: Account,
        run_id: RunId,
        *,
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> Task:
        """One Run as a Task, assembled from the projections Psych ships."""
        entry = await self._entry(account, run_id)
        status = await self._status(account, run_id)
        scope = scope_for(account)
        answer = (
            await psych_runtime.answer(self._store, run_id, scope=scope)
            if include_artifacts
            else None
        )
        records = await psych_runtime.records(self._store, run_id, scope=scope)
        return task_of(
            status,
            context_id=await self._context_id(account, entry),
            answer=answer,
            history=message_views(records),
            history_length=history_length,
            include_artifacts=include_artifacts,
            timestamp=records[-1].at if records else datetime.now(UTC),
        )

    async def events_for(self, account: Account, run_id: RunId) -> tuple[StreamResponse, ...]:
        """Every event one Run's log implies, for the push sender."""
        entry = await self._entry(account, run_id)
        scope = scope_for(account)
        records = await psych_runtime.records(self._store, run_id, scope=scope)
        answer = await psych_runtime.answer(self._store, run_id, scope=scope)
        return stream_events(
            records, context_id=await self._context_id(account, entry), answer=answer
        )

    # -- internals ----------------------------------------------------------

    async def _apply(self, account: Account, request: SendMessageRequest) -> RunId:
        """Turn one validated message into the Run it means, and return its id."""
        known = await self._known_contexts(account)
        resolved = resolve_message(request, known_context_of=known)

        if resolved.intent is MessageIntent.CONTINUE_TASK:
            assert resolved.task_id is not None  # resolve_message guarantees it
            return await self._continue(account, resolved.task_id, resolved.text)

        continues: RunId | None = None
        if resolved.intent is MessageIntent.NEW_TASK_IN_CONTEXT:
            continues = await self._newest_in_context(account, resolved.context_id or "")
            if continues is None:
                raise TaskNotFoundError(
                    f"context {resolved.context_id!r} has no tasks in it",
                    metadata={"contextId": resolved.context_id or ""},
                )
        return await self._dispatch(account, request.tenant, resolved.text, continues=continues)

    async def _continue(self, account: Account, run_id: RunId, text: str) -> RunId:
        """A message for an existing task: an answer if it is waiting, a new
        Run in the same conversation if it is not.

        Both keep the client's own model intact. A waiting task resumes and
        keeps its task id, which is what §3.4.3 describes for `INPUT_REQUIRED`.
        A settled task cannot be resumed -- Psych's log is append-only and a
        settled Run is over -- so the message starts the next Run in the
        conversation, and the response carries the *new* task id. That is
        honest rather than convenient: pretending the old task woke up would
        mean returning a task whose log does not contain the message that was
        just sent.
        """
        status = await self._status(account, run_id)
        state = task_state_of(status)
        if state in INTERRUPTED_STATES:
            await psych_runtime.resume(
                self._store,
                run_id,
                payload={"message": text, "answer": text},
                approved=True if state is TaskState.INPUT_REQUIRED else None,
                by=account.id,
            )
            return run_id
        if state not in TERMINAL_STATES:
            # Still working: this is exactly what the steering queue is for
            # (DESIGN.md §9), and the task id stays the same because the Run
            # does.
            await psych_runtime.send(self._store, run_id, message=text)
            return run_id
        entry = await self._entry(account, run_id)
        return await self._dispatch(account, entry.agent_id, text, continues=run_id)

    async def _dispatch(
        self, account: Account, tenant: str | None, text: str, *, continues: RunId | None
    ) -> RunId:
        agent_id, version_hash = await self._target(account, tenant, continues)
        version = await self._store.get_version(version_hash)
        if version is None:
            raise TaskNotFoundError(f"no published agent version {version_hash!r}")
        scope = scope_for(account)
        dispatched = await psych_runtime.dispatch(
            self._store,
            version_hash,
            scope,
            input={"message": text, "end_user_id": account.id},
            continues=continues,
        )
        await self._index.put_run(
            RunEntry(
                run_id=dispatched.run_id,
                agent_id=agent_id,
                name=version.spec.name,
                tenant=scope.tenant,
                started_at=datetime.now(UTC),
                version_hash=version_hash,
                message=text,
            )
        )
        return dispatched.run_id

    async def _target(
        self, account: Account, tenant: str | None, continues: RunId | None
    ) -> tuple[str, VersionHash]:
        """Which agent this message is for, and which Version it pins.

        A continuation stays with the agent and Version the conversation
        started on, for the reason the console's own dispatch route gives: a
        second message belongs to the agent somebody was already talking to,
        whatever has been published since.
        """
        if continues is not None:
            prior = await self._index.get_run(continues)
            if prior is not None and prior.tenant == account.id and prior.agent_id:
                return prior.agent_id, prior.version_hash
        agents = await self._index.list_agents(account.id)
        if not agents:
            raise InvalidParamsError(
                "this account has published no agents, so there is nothing to send a message to"
            )
        if tenant is None:
            if len(agents) > 1:
                raise InvalidParamsError(
                    "this endpoint serves several agents, so the request must name one in "
                    "its tenant field (§4.4.6): "
                    + ", ".join(sorted(pointer.agent_id for pointer, _ in agents))
                )
            pointer, _ = agents[0]
            return pointer.agent_id, pointer.version_hash
        found = await self._index.get_agent(account.id, tenant)
        if found is None:
            raise InvalidParamsError(
                f"tenant {tenant!r} does not name an agent this account has published"
            )
        pointer, _entry = found
        return pointer.agent_id, pointer.version_hash

    async def _await_settled_or_waiting(self, account: Account, run_id: RunId) -> None:
        """Block until the task is terminal or interrupted (§3.2.2).

        Reads the Run's log rather than polling its status: the stream ends
        when the Run settles, and a suspension is a record like any other, so
        both stop conditions come from the same source the client would see.
        """
        scope = scope_for(account)

        async def watch() -> None:
            async for _ in psych_runtime.stream(self._store, run_id, after=0, scope=scope):
                status = await psych_runtime.status(self._store, run_id, scope=scope)
                state = task_state_of(status)
                if state in TERMINAL_STATES or state in INTERRUPTED_STATES:
                    return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(watch(), timeout=self._wait)

    async def _entry(self, account: Account, run_id: RunId) -> RunEntry:
        entry = await self._index.get_run(run_id)
        if entry is None or entry.tenant != account.id:
            # §13.1: never distinguish "does not exist" from "not yours".
            raise TaskNotFoundError(
                f"task {run_id} does not exist or is not accessible",
                metadata={"taskId": str(run_id)},
            )
        return entry

    async def _status(self, account: Account, run_id: RunId) -> RunStatus:
        await self._entry(account, run_id)
        return await psych_runtime.status(self._store, run_id, scope=scope_for(account))

    async def _known_contexts(self, account: Account) -> dict[RunId, str]:
        """Every task this account owns, with the context it belongs to.

        Loaded in one pass so `resolve_message` can enforce §3.4.3's
        mismatched-pair rule without a store read per candidate.
        """
        entries = await self._index.list_runs(account.id)
        parents = {entry.run_id: (await self._store.get_run(entry.run_id)) for entry in entries}
        chain = {
            run_id: header.continues_run_id if header is not None else None
            for run_id, header in parents.items()
        }
        return {run_id: context_id_of(run_id, chain) for run_id in chain}

    async def _context_id(self, account: Account, entry: RunEntry) -> str:
        chain: dict[RunId, RunId | None] = {}
        current: RunId | None = entry.run_id
        seen: set[RunId] = set()
        while current is not None and current not in seen:
            seen.add(current)
            header = await self._store.get_run(current)
            if header is None or header.scope.tenant != account.id:
                break
            chain[current] = header.continues_run_id
            current = header.continues_run_id
        return context_id_of(entry.run_id, chain)

    async def _newest_in_context(self, account: Account, context_id: str) -> RunId | None:
        known = await self._known_contexts(account)
        in_context = [run_id for run_id, ctx in known.items() if ctx == context_id]
        if not in_context:
            return None
        entries = {entry.run_id: entry for entry in await self._index.list_runs(account.id)}
        return max(in_context, key=lambda run_id: entries[run_id].started_at)


def _is_final(event: StreamResponse) -> bool:
    """Whether this event ends the stream (§11.7).

    Terminal *and* interrupted both close it: a task in `INPUT_REQUIRED` is
    waiting on the client, and holding the connection open while it thinks
    would keep a socket for as long as a person takes to answer.
    """
    update = event.status_update
    if update is None:
        return False
    return update.status.state in TERMINAL_STATES or update.status.state in INTERRUPTED_STATES


def _page_offset(token: str | None) -> int:
    """Read a page token, refusing one this server did not mint.

    The token is an offset as a decimal string. Opaque to clients per §3.1.4,
    and validated here because a token that is not a number would otherwise
    silently page from zero and hand the client the first page again forever.
    """
    if not token:
        return 0
    try:
        offset = int(token)
    except ValueError as err:
        raise InvalidParamsError(f"pageToken {token!r} was not issued by this server") from err
    if offset < 0:
        raise InvalidParamsError(f"pageToken {token!r} was not issued by this server")
    return offset
