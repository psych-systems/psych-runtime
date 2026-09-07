"""The A2A routes: both HTTP bindings, over one service.

DESIGN.md §1 refuses to put an HTTP server or a route table in Psych, and A2A
is a transport, so this file is the part a consumer has to write and the part
they can copy. There is nothing protocol-shaped in it beyond wiring:
`psych_runtime.a2a.jsonrpc` parses the envelope, `psych_runtime.a2a.rest` names the paths and
frames the SSE, `app.a2a.service` does the work, and the same service method
answers both bindings so §5.1's "identical functionality" is structural rather
than asserted.

## Every request goes through the same three checks

In this order, before anything is dispatched:

1. **Authentication.** A bearer token that resolves to an account, or 401.
   The card declares exactly this scheme (§4.5.3) and this is what it means.
2. **Version.** `A2A-Version`, or the query parameter §3.6.1 allows instead.
   Absent means 0.3 (§3.6.2), which this deployment does not speak, so it is
   refused with `VersionNotSupportedError` rather than served 1.0 semantics.
3. **Extensions.** `A2A-Extensions` is parsed, and any extension the card
   marks `required` must be among them (§4.6.3). This deployment declares
   none, so nothing is currently required -- the check is here so that
   declaring one later cannot be forgotten at the routes.

## Why the routes are mounted outside `/api`

The console's session middleware guards `/api/*` with a cookie. An A2A peer
has no cookie and no reason to have one: it authenticates with a bearer token
on every request. Mounting at `/a2a` keeps the two front doors separate, which
is also why this router does its own authentication rather than reusing the
middleware's.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app.a2a.cards import CardFactory
from app.a2a.push import PushSender
from app.a2a.service import A2AService
from app.accounts import Account
from app.settings_store import SettingsStore
from app.store_index import AgentEntry, AgentPointer, PlaygroundIndex
from psych_runtime.a2a.errors import (
    A2AError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    TaskNotFoundError,
)
from psych_runtime.a2a.jsonrpc import (
    STREAMING_METHODS,
    A2AMethod,
    JsonRpcId,
    error_response,
    parse_body,
    parse_request,
    stream_frame,
    success_response,
)
from psych_runtime.a2a.models import (
    A2A_MEDIA_TYPE,
    AGENT_CARD_WELL_KNOWN_PATH,
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
    wire_dict,
)
from psych_runtime.a2a.negotiation import (
    A2A_EXTENSIONS_HEADER,
    A2A_VERSION_HEADER,
    A2A_VERSION_QUERY_PARAM,
    negotiate_version,
    parse_extensions_header,
    require_declared_extensions,
)
from psych_runtime.a2a.rest import (
    SSE_MEDIA_TYPE,
    error_body,
    get_task_request_from_query,
    list_push_configs_request_from_query,
    list_tasks_request_from_query,
    sse_frame,
)
from psych_runtime.core.ids import RunId
from psych_runtime.core.spec import AgentSpec

__all__ = ["A2ADeps", "build_router"]

_LOG = logging.getLogger("app.a2a")


class A2ADeps:
    """What the routes need, assembled once in the app's lifespan."""

    def __init__(
        self,
        *,
        service: A2AService,
        cards: CardFactory,
        index: PlaygroundIndex,
        settings: SettingsStore,
        push: PushSender,
    ) -> None:
        self.service = service
        self.cards = cards
        self.index = index
        self.settings = settings
        self.push = push


def build_router(deps_of: Callable[[Request], A2ADeps]) -> APIRouter:
    """The A2A router, reading its dependencies off the app on each request.

    A factory rather than a module-level router with globals: the app builds
    its state in `lifespan`, and a router that captured it at import time would
    capture nothing.

    Split into three mount functions by binding, so that the JSON-RPC surface
    and the REST surface can be read side by side against §9 and §11 rather
    than as one long block.
    """
    router = APIRouter(tags=["a2a"])
    _mount_discovery(router, deps_of)
    _mount_jsonrpc(router, deps_of)
    _mount_rest(router, deps_of)
    _mount_rest_push_configs(router, deps_of)
    return router


def _mount_discovery(router: APIRouter, deps_of: Callable[[Request], A2ADeps]) -> None:
    """§8.2's Agent Card, at the well-known path and at a direct URL."""

    @router.get(AGENT_CARD_WELL_KNOWN_PATH)
    async def well_known_card(request: Request, agent: str | None = None) -> Response:
        """§8.2's path, with the selector this multi-agent deployment needs.

        Public. §8.2 puts the card at a well-known path so a client can read
        it *before* it knows how to authenticate, and the card is what says
        how. A caller who does send a token is answered from their own
        agents, so ``?agent=`` may be omitted when they have one; an anonymous
        caller names the agent, or gets the only one this deployment serves.
        """
        deps = deps_of(request)
        try:
            card = await _public_card(request, deps, agent)
        except A2AError as err:
            return _rest_error(err)
        return _card_response(card)

    @router.get("/a2a/v1/agents/{agent_id}/agent-card.json")
    async def agent_card_by_id(request: Request, agent_id: str) -> Response:
        """A direct per-agent card URL, for pasting into a peer's config. Public,
        as the well-known path is; an agent id is opaque and unguessable."""
        deps = deps_of(request)
        try:
            card = await _public_card(request, deps, agent_id)
        except A2AError as err:
            return _rest_error(err)
        return _card_response(card)


def _mount_jsonrpc(router: APIRouter, deps_of: Callable[[Request], A2ADeps]) -> None:
    """§9's binding: one endpoint, every method, with the tenant either in the
    body or in the path."""

    @router.post("/a2a/v1/rpc")
    async def jsonrpc(request: Request) -> Response:
        return await _handle_jsonrpc(deps_of(request), request)

    @router.post("/a2a/v1/{tenant}/rpc")
    async def jsonrpc_for_tenant(request: Request, tenant: str) -> Response:
        """The proto's `additional_bindings` tenant prefix, for a client that
        would rather put the routing identifier in the path than the body."""
        return await _handle_jsonrpc(deps_of(request), request, tenant=tenant)


def _mount_rest(router: APIRouter, deps_of: Callable[[Request], A2ADeps]) -> None:
    """§11's binding: one route per operation, exactly as `REST_ROUTES` says."""

    @router.post("/a2a/v1/message:send")
    async def rest_send(request: Request) -> Response:
        return await _rest_send(deps_of(request), request, tenant=None)

    @router.post("/a2a/v1/{tenant}/message:send")
    async def rest_send_for_tenant(request: Request, tenant: str) -> Response:
        return await _rest_send(deps_of(request), request, tenant=tenant)

    @router.post("/a2a/v1/message:stream")
    async def rest_stream(request: Request) -> Response:
        return await _rest_stream(deps_of(request), request, tenant=None)

    @router.post("/a2a/v1/{tenant}/message:stream")
    async def rest_stream_for_tenant(request: Request, tenant: str) -> Response:
        return await _rest_stream(deps_of(request), request, tenant=tenant)

    @router.get("/a2a/v1/tasks")
    async def rest_list_tasks(request: Request) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            listed = await deps.service.list_tasks(
                account, list_tasks_request_from_query(dict(request.query_params))
            )
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(listed)

    # Registered before `/tasks/{task_id}`: FastAPI matches in registration
    # order and a path parameter happily swallows the `:subscribe` suffix, so
    # the other order sends every subscription to GetTask, which answers 404
    # for a task id nobody minted.
    # Both verbs, because the proto annotates GET and §11.3.2 writes POST. See
    # `psych_runtime.a2a.rest` for the conflict and why refusing one would be standing
    # on a technicality at the cost of interoperability.
    @router.get("/a2a/v1/tasks/{task_id}:subscribe")
    @router.post("/a2a/v1/tasks/{task_id}:subscribe")
    async def rest_subscribe(request: Request, task_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            events = deps.service.subscribe(account, RunId(task_id), refuse_terminal=True)
            # The first event is awaited here rather than inside the response
            # body so that a task that cannot be subscribed to is refused with
            # a status code, not with a 200 whose stream immediately errors.
            first = await anext(events, None)
        except A2AError as err:
            return _rest_error(err)
        return StreamingResponse(
            _rest_sse(first, events), media_type=SSE_MEDIA_TYPE, headers=_SSE_HEADERS
        )

    @router.get("/a2a/v1/tasks/{task_id}")
    async def rest_get_task(request: Request, task_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            task = await deps.service.get_task(
                account, get_task_request_from_query(task_id, dict(request.query_params))
            )
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(task)

    @router.post("/a2a/v1/tasks/{task_id}:cancel")
    async def rest_cancel(request: Request, task_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            task = await deps.service.cancel_task(account, CancelTaskRequest(id=task_id))
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(task)


def _mount_rest_push_configs(router: APIRouter, deps_of: Callable[[Request], A2ADeps]) -> None:
    """§11.3.3: the four push-notification config operations, plus the
    extended-card route that answers what this deployment actually offers."""

    @router.post("/a2a/v1/tasks/{task_id}/pushNotificationConfigs")
    async def rest_create_push_config(request: Request, task_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            body = await _json_body(request)
            config = _validate(TaskPushNotificationConfig, {**body, "taskId": task_id})
            created = await deps.service.create_push_config(account, config)
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(created, status=201)

    @router.get("/a2a/v1/tasks/{task_id}/pushNotificationConfigs")
    async def rest_list_push_configs(request: Request, task_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            listed = await deps.service.list_push_configs(
                account,
                list_push_configs_request_from_query(task_id, dict(request.query_params)),
            )
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(listed)

    @router.get("/a2a/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}")
    async def rest_get_push_config(request: Request, task_id: str, config_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            config = await deps.service.get_push_config(
                account,
                GetTaskPushNotificationConfigRequest(task_id=task_id, id=config_id),
            )
        except A2AError as err:
            return _rest_error(err)
        return _rest_ok(config)

    @router.delete("/a2a/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}")
    async def rest_delete_push_config(request: Request, task_id: str, config_id: str) -> Response:
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            await deps.service.delete_push_config(
                account,
                DeleteTaskPushNotificationConfigRequest(task_id=task_id, id=config_id),
            )
        except A2AError as err:
            return _rest_error(err)
        # The proto returns google.protobuf.Empty, which is an empty JSON
        # object rather than no body at all.
        return JSONResponse({}, media_type=A2A_MEDIA_TYPE)

    @router.get("/a2a/v1/extendedAgentCard")
    async def rest_extended_card(request: Request, agent: str | None = None) -> Response:
        """§13.3: the card an authenticated caller gets, which says more than
        the public one. ``?agent=`` selects, as on the well-known path."""
        deps = deps_of(request)
        try:
            account = await _prepare(request, deps)
            card = await _card_for(deps, account, agent, extended=True)
        except A2AError as err:
            return _rest_error(err)
        return _card_response(card)


# ---------------------------------------------------------------------------
# Shared request handling
# ---------------------------------------------------------------------------

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
"""Both matter behind a proxy: without them nginx buffers the whole response
and the stream arrives at once, when the task ends."""


async def _handle_jsonrpc(
    deps: A2ADeps, request: Request, *, tenant: str | None = None
) -> Response:
    """One JSON-RPC request, streaming or not (§9)."""
    request_id: JsonRpcId = None
    try:
        account = await _prepare(request, deps)
        payload = parse_body(await request.body(), json.loads)
        method, params, request_id = parse_request(payload)
        if tenant is not None:
            params = params.model_copy(update={"tenant": tenant})
        if method in STREAMING_METHODS:
            events = _jsonrpc_stream(deps, account, method, params)
            first = await anext(events, None)
            return StreamingResponse(
                _jsonrpc_sse(request_id, first, events),
                media_type=SSE_MEDIA_TYPE,
                headers=_SSE_HEADERS,
            )
        result = await _dispatch(deps, account, method, params)
    except A2AError as err:
        return JSONResponse(error_response(request_id, err), status_code=200)
    except Exception as err:
        # A JSON-RPC transport error is still a 200 with an error object: the
        # request reached the method and the method failed, which is what
        # `-32603` means. Logged rather than echoed, so an internal traceback
        # never leaves the process.
        _LOG.exception("A2A JSON-RPC call failed")
        return JSONResponse(
            error_response(request_id, InternalError(f"the agent failed to answer: {err}")),
            status_code=200,
        )
    return JSONResponse(success_response(request_id, result))


async def _dispatch(  # noqa: PLR0911 - one return per method is the clearest shape
    deps: A2ADeps, account: Account, method: A2AMethod, params: Any
) -> Any:
    """The one place a method name becomes a service call.

    Both bindings route through here for the non-streaming methods, which is
    what makes §5.1's functional-equivalence requirement a property of the code
    rather than a claim in a README.
    """
    service = deps.service
    if method is A2AMethod.SEND_MESSAGE:
        response = await service.send_message(account, params)
        _watch_for_push(deps, account, response)
        return response
    if method is A2AMethod.GET_TASK:
        return await service.get_task(account, params)
    if method is A2AMethod.LIST_TASKS:
        return await service.list_tasks(account, params)
    if method is A2AMethod.CANCEL_TASK:
        return await service.cancel_task(account, params)
    if method is A2AMethod.CREATE_TASK_PUSH_NOTIFICATION_CONFIG:
        created = await service.create_push_config(account, params)
        if created.task_id:
            deps.push.watch(account, RunId(created.task_id))
        return created
    if method is A2AMethod.GET_TASK_PUSH_NOTIFICATION_CONFIG:
        return await service.get_push_config(account, params)
    if method is A2AMethod.LIST_TASK_PUSH_NOTIFICATION_CONFIGS:
        return await service.list_push_configs(account, params)
    if method is A2AMethod.DELETE_TASK_PUSH_NOTIFICATION_CONFIG:
        await service.delete_push_config(account, params)
        return {}
    if method is A2AMethod.GET_EXTENDED_AGENT_CARD:
        tenant = params.tenant if isinstance(params, GetExtendedAgentCardRequest) else None
        return await _card_for(deps, account, tenant, extended=True)
    raise InvalidRequestError(f"{method.value} is not routed by this server")


async def _jsonrpc_stream(
    deps: A2ADeps, account: Account, method: A2AMethod, params: Any
) -> AsyncIterator[Any]:
    if method is A2AMethod.SEND_STREAMING_MESSAGE:
        async for event in deps.service.stream_message(account, params):
            yield event
        return
    if not isinstance(params, SubscribeToTaskRequest):  # pragma: no cover - METHOD_PARAMS
        raise InvalidRequestError(f"{method.value} did not carry a subscribe request")
    subscribe = params
    async for event in deps.service.subscribe(account, RunId(subscribe.id), refuse_terminal=True):
        yield event


async def _jsonrpc_sse(
    request_id: JsonRpcId, first: Any, events: AsyncIterator[Any]
) -> AsyncIterator[bytes]:
    if first is not None:
        yield sse_frame(stream_frame(request_id, first))
    async for event in events:
        yield sse_frame(stream_frame(request_id, event))


async def _rest_sse(first: Any, events: AsyncIterator[Any]) -> AsyncIterator[bytes]:
    """§11.7's framing: the bare `StreamResponse`, with no envelope."""
    if first is not None:
        yield sse_frame(wire_dict(first))
    async for event in events:
        yield sse_frame(wire_dict(event))


async def _rest_send(deps: A2ADeps, request: Request, *, tenant: str | None) -> Response:
    try:
        account = await _prepare(request, deps)
        body = await _json_body(request)
        send = _validate(SendMessageRequest, _with_tenant(body, tenant))
        response = await deps.service.send_message(account, send)
        _watch_for_push(deps, account, response)
    except A2AError as err:
        return _rest_error(err)
    return _rest_ok(response)


async def _rest_stream(deps: A2ADeps, request: Request, *, tenant: str | None) -> Response:
    try:
        account = await _prepare(request, deps)
        body = await _json_body(request)
        send = _validate(SendMessageRequest, _with_tenant(body, tenant))
        events = deps.service.stream_message(account, send)
        first = await anext(events, None)
    except A2AError as err:
        return _rest_error(err)
    return StreamingResponse(
        _rest_sse(first, events), media_type=SSE_MEDIA_TYPE, headers=_SSE_HEADERS
    )


def _with_tenant(body: Mapping[str, Any], tenant: str | None) -> dict[str, Any]:
    """The path's tenant wins over the body's, since a client that put it in
    the path chose that route deliberately."""
    merged = dict(body)
    if tenant is not None:
        merged["tenant"] = tenant
    return merged


def _watch_for_push(deps: A2ADeps, account: Account, response: Any) -> None:
    """Start notifying webhooks for a task that has any registered.

    Called after a send rather than inside the service, because delivering a
    notification is transport and the service is deliberately transport-free.
    """
    task = getattr(response, "task", None)
    if task is not None:
        deps.push.watch(account, RunId(task.id))


# ---------------------------------------------------------------------------
# The three checks
# ---------------------------------------------------------------------------


async def _prepare(request: Request, deps: A2ADeps) -> Account:
    """Authenticate, negotiate the version, and settle extensions."""
    account = await _authenticate(request, deps)
    negotiate_version(
        request.headers.get(A2A_VERSION_HEADER) or request.query_params.get(A2A_VERSION_QUERY_PARAM)
    )
    # This deployment's cards declare no extensions, so nothing is required and
    # nothing activates. The call is here rather than skipped so that declaring
    # one later cannot be forgotten at the routes (§4.6.3).
    require_declared_extensions(
        (), parse_extensions_header(request.headers.get(A2A_EXTENSIONS_HEADER))
    )
    return account


class _Unauthenticated(A2AError):
    """No usable credential. §3.3.2 puts this outside the A2A error table --
    "Example error codes: HTTP 401 Unauthorized ... JSON-RPC custom error" --
    so it carries a 401 and the generic internal JSON-RPC code rather than
    borrowing a code that means something else."""

    jsonrpc_code = -32000
    http_status = 401
    grpc_status = "UNAUTHENTICATED"
    reason = "UNAUTHENTICATED"


async def _authenticate(request: Request, deps: A2ADeps) -> Account:
    """Resolve the bearer token to an account, or refuse (§7.3).

    The token is a console session token: the card declares exactly that
    (`app.a2a.cards.BEARER_SCHEME_NAME`), so a caller reading the card knows
    what to send. A production consumer swaps this for their own credential
    check and changes the declared scheme to match -- those two must move
    together, which is why the scheme name and this function are the only two
    places authentication is mentioned.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _Unauthenticated(
            "this endpoint requires a bearer token; the Agent Card declares the scheme "
            "under securitySchemes"
        )
    state = await deps.settings.load_file()
    account = state.accounts.account_for_token(token.strip(), datetime.now(UTC))
    if account is None:
        raise _Unauthenticated("that token does not identify an account")
    return account


async def _public_card(request: Request, deps: A2ADeps, agent_id: str | None) -> AgentCard:
    """The discovery card, with or without a credential (§8.2)."""
    if request.headers.get("Authorization", ""):
        return await _card_for(deps, await _authenticate(request, deps), agent_id)
    if agent_id is None:
        if await deps.index.count_agents() != 1:
            raise InvalidParamsError(
                "this endpoint serves several agents; name one with ?agent=<id>, or send a "
                "bearer token to be answered from your own"
            )
        found = await deps.index.find_only_agent()
    else:
        found = await deps.index.find_agent_any_owner(agent_id)
    if found is None:
        raise TaskNotFoundError(f"no agent {agent_id!r}")
    return await _card_of(deps, *found, extended=False)


async def _card_for(
    deps: A2ADeps, account: Account, agent_id: str | None, *, extended: bool = False
) -> AgentCard:
    """The card for one agent, or the account's only agent."""
    agents = await deps.index.list_agents(account.id)
    if not agents:
        raise TaskNotFoundError("this account has published no agents")
    if agent_id is None:
        if len(agents) > 1:
            raise InvalidParamsError(
                "this endpoint serves several agents; name one with ?agent=<id>: "
                + ", ".join(sorted(pointer.agent_id for pointer, _ in agents))
            )
        pointer, entry = agents[0]
    else:
        found = await deps.index.get_agent(account.id, agent_id)
        if found is None:
            raise TaskNotFoundError(f"no agent {agent_id!r}")
        pointer, entry = found
    return await _card_of(deps, pointer, entry, extended=extended)


async def _card_of(
    deps: A2ADeps, pointer: AgentPointer, entry: AgentEntry, *, extended: bool
) -> AgentCard:
    version = await deps.service.version_of(pointer.version_hash)
    if version is None or not isinstance(version.spec, AgentSpec):
        # A pointer to a Version the store has lost, or to a workflow rather
        # than an agent. Neither can be described as an A2A agent, and saying
        # "not found" is better than serving a card for something that cannot
        # answer a message.
        raise TaskNotFoundError(f"agent {pointer.agent_id!r} has no published agent version")
    return deps.cards.build(
        version.spec,
        agent_id=pointer.agent_id,
        version=entry.published_at.isoformat(),
        extended=extended,
    )


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    payload = parse_body(await request.body(), json.loads)
    if not isinstance(payload, dict):
        raise InvalidRequestError("the request body must be a JSON object")
    return payload


def _validate[M: BaseModel](model: type[M], body: Mapping[str, Any]) -> M:
    """Validate a REST body, reporting a failure the way both bindings do."""
    try:
        return model.model_validate(dict(body))
    except ValidationError as err:
        raise InvalidParamsError(f"invalid request body: {err}") from err


def _rest_ok(result: Any, *, status: int = 200) -> Response:
    return JSONResponse(wire_dict(result), status_code=status, media_type=A2A_MEDIA_TYPE)


def _rest_error(error: A2AError) -> Response:
    status, body = error_body(error)
    return JSONResponse(body, status_code=status, media_type=A2A_MEDIA_TYPE)


def _card_response(card: AgentCard) -> Response:
    return JSONResponse(wire_dict(card), media_type="application/json")
