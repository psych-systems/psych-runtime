"""The JSON-RPC 2.0 binding: method names, envelopes, and parsing.

§9. This module owns the wire envelope and the method table; it does not know
what a task is or where one comes from. A route hands it a decoded body, gets
back a method and a validated parameter object, does the work, and hands the
result back to be wrapped. That split is what lets the whole binding be tested
without a socket.

## The method names, resolved from the specification rather than from memory

The eleven strings in ``A2AMethod`` are bare PascalCase: ``"SendMessage"``,
``"GetTask"``, ``"CancelTask"``, and so on. Three spellings are in
circulation and only one is current:

- ``"message/send"`` and its siblings are **pre-1.0**. They appear in 0.3-era
  material and nowhere in the v1.0 specification.
- ``"a2a.SendMessage"``, a fully-qualified gRPC-style name, is what a reader
  might infer from the proto's ``package lf.a2a.v1``, but the JSON-RPC binding
  does not qualify.
- Bare PascalCase is what §9.1 states ("PascalCase method names matching gRPC
  conventions"), what §9.4.1's example request carries, and what §5.3's
  mapping table lists in its JSON-RPC column for all eleven operations.

## Why ``params`` validation lives in a table

Each method has exactly one parameter message in the proto, and §5.1 requires
both bindings to accept the same thing. A per-route ``SendMessageRequest(...)``
call would be one place for the REST binding to drift from this one, so both
resolve their parameter type from ``METHOD_PARAMS`` here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from psych_runtime.a2a.errors import (
    A2AError,
    InvalidParamsError,
    InvalidRequestError,
    JsonParseError,
    MethodNotFoundError,
    jsonrpc_error_object,
)
from psych_runtime.a2a.models import (
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTasksRequest,
    SendMessageRequest,
    StreamResponse,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
    wire_dict,
)

__all__ = [
    "JSONRPC_VERSION",
    "METHOD_PARAMS",
    "STREAMING_METHODS",
    "A2AMethod",
    "JsonRpcId",
    "error_response",
    "parse_body",
    "parse_request",
    "stream_frame",
    "success_response",
]

JSONRPC_VERSION: Final = "2.0"

JsonRpcId = str | int | None
"""JSON-RPC 2.0 allows a string, a number or null. A2A defines no
notifications, so a null id is answered rather than treated as fire-and-forget:
a client that omits the id still gets its result, which is more useful than
silence and costs nothing."""


class A2AMethod(StrEnum):
    """The eleven core operations (§3.1), as JSON-RPC method strings (§5.3)."""

    SEND_MESSAGE = "SendMessage"
    SEND_STREAMING_MESSAGE = "SendStreamingMessage"
    GET_TASK = "GetTask"
    LIST_TASKS = "ListTasks"
    CANCEL_TASK = "CancelTask"
    SUBSCRIBE_TO_TASK = "SubscribeToTask"
    CREATE_TASK_PUSH_NOTIFICATION_CONFIG = "CreateTaskPushNotificationConfig"
    GET_TASK_PUSH_NOTIFICATION_CONFIG = "GetTaskPushNotificationConfig"
    LIST_TASK_PUSH_NOTIFICATION_CONFIGS = "ListTaskPushNotificationConfigs"
    DELETE_TASK_PUSH_NOTIFICATION_CONFIG = "DeleteTaskPushNotificationConfig"
    GET_EXTENDED_AGENT_CARD = "GetExtendedAgentCard"


METHOD_PARAMS: Final[Mapping[A2AMethod, type[BaseModel]]] = {
    A2AMethod.SEND_MESSAGE: SendMessageRequest,
    # Same request message as SendMessage in the proto; only the response
    # differs (a stream of StreamResponse rather than one SendMessageResponse).
    A2AMethod.SEND_STREAMING_MESSAGE: SendMessageRequest,
    A2AMethod.GET_TASK: GetTaskRequest,
    A2AMethod.LIST_TASKS: ListTasksRequest,
    A2AMethod.CANCEL_TASK: CancelTaskRequest,
    A2AMethod.SUBSCRIBE_TO_TASK: SubscribeToTaskRequest,
    # The proto's CreateTaskPushNotificationConfig takes the config itself as
    # its request message rather than a wrapper, which is why this row is not
    # a `...Request` type like its neighbours.
    A2AMethod.CREATE_TASK_PUSH_NOTIFICATION_CONFIG: TaskPushNotificationConfig,
    A2AMethod.GET_TASK_PUSH_NOTIFICATION_CONFIG: GetTaskPushNotificationConfigRequest,
    A2AMethod.LIST_TASK_PUSH_NOTIFICATION_CONFIGS: ListTaskPushNotificationConfigsRequest,
    A2AMethod.DELETE_TASK_PUSH_NOTIFICATION_CONFIG: DeleteTaskPushNotificationConfigRequest,
    A2AMethod.GET_EXTENDED_AGENT_CARD: GetExtendedAgentCardRequest,
}
"""Every method's parameter message. Exhaustive by construction: a method
added to ``A2AMethod`` without a row here fails ``parse_request`` immediately
and visibly rather than being dispatched with unvalidated parameters."""

STREAMING_METHODS: Final = frozenset(
    {A2AMethod.SEND_STREAMING_MESSAGE, A2AMethod.SUBSCRIBE_TO_TASK}
)
"""The two methods that answer with SSE rather than a single JSON body
(§9.4.2, §9.4.6). A route needs to know before it dispatches, because the
response's content type is chosen first."""


def parse_request(payload: object) -> tuple[A2AMethod, BaseModel, JsonRpcId]:
    """Validate one JSON-RPC request envelope and its parameters.

    Args:
        payload: the already-decoded body. Decoding is the caller's, because
            the framework has usually done it already and doing it twice is
            how a body gets read as two different values.

    Returns:
        The method, its validated parameter object, and the request id to echo.

    Raises:
        InvalidRequestError: not an object, wrong ``jsonrpc`` version, missing
            or non-string ``method``, or a ``params`` that is not an object.
            A2A defines no method taking positional parameters, so a JSON-RPC
            array ``params`` is refused rather than guessed at.
        MethodNotFoundError: a well-formed request for a method A2A does not
            define.
        InvalidParamsError: the parameters do not validate against the
            method's message. The pydantic error text is included, because
            "invalid parameters" alone sends the caller back to guessing.
    """
    if not isinstance(payload, dict):
        raise InvalidRequestError("a JSON-RPC request must be a JSON object")

    version = payload.get("jsonrpc")
    if version != JSONRPC_VERSION:
        raise InvalidRequestError(
            f"jsonrpc must be {JSONRPC_VERSION!r}, got {version!r}",
        )

    raw_id = payload.get("id")
    if (raw_id is not None and not isinstance(raw_id, str | int)) or isinstance(raw_id, bool):
        # bool is an int subclass in Python, and `"id": true` is not an id.
        raise InvalidRequestError("id must be a string, a number, or null")
    request_id: JsonRpcId = raw_id

    raw_method = payload.get("method")
    if not isinstance(raw_method, str):
        raise InvalidRequestError("method must be a string")
    try:
        method = A2AMethod(raw_method)
    except ValueError as err:
        raise MethodNotFoundError(
            f"{raw_method!r} is not an A2A method; this agent implements "
            + ", ".join(sorted(m.value for m in A2AMethod))
        ) from err

    raw_params = payload.get("params")
    if raw_params is None:
        # §9.4.8's own GetExtendedAgentCard example omits `params` entirely.
        # Every A2A parameter message has all-optional fields except the ones
        # a method genuinely requires, so an empty object validates exactly
        # when the method takes no required parameter and fails with the right
        # error when it does.
        raw_params = {}
    if not isinstance(raw_params, dict):
        raise InvalidRequestError("params must be an object; A2A defines no positional parameters")

    model = METHOD_PARAMS[method]
    try:
        return method, model.model_validate(raw_params), request_id
    except ValidationError as err:
        raise InvalidParamsError(f"invalid parameters for {method.value}: {err}") from err


def parse_body(body: bytes | str, decode: Callable[[bytes | str], object]) -> object:
    """Decode a request body, mapping a JSON failure to §9.5's ``-32700``.

    ``decode`` is the JSON loader to use -- normally ``json.loads``. Injected
    rather than imported so a consumer whose framework already uses a faster
    loader reports its failures with the same code as everyone else.

    Raises:
        JsonParseError: the body is not JSON.
    """
    try:
        return decode(body)
    except Exception as err:
        raise JsonParseError(f"invalid JSON payload: {err}") from err


def success_response(
    request_id: JsonRpcId, result: BaseModel | Mapping[str, Any]
) -> dict[str, Any]:
    """Wrap a result in the JSON-RPC envelope (§9.4).

    A pydantic model is rendered by ``wire_dict``, which applies §5.5's
    camelCase and §5.7's field-presence rules in one place for every binding.
    """
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": _dump(result),
    }


def error_response(request_id: JsonRpcId, error: A2AError) -> dict[str, Any]:
    """Wrap a failure in the JSON-RPC envelope (§9.5)."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": jsonrpc_error_object(error),
    }


def stream_frame(request_id: JsonRpcId, event: StreamResponse) -> dict[str, Any]:
    """One SSE ``data:`` payload for a streaming method (§9.4.2).

    Every frame repeats the request id, which is what lets a client
    multiplexing several streams over one connection tell them apart.
    """
    return success_response(request_id, event)


def _dump(value: BaseModel | Mapping[str, Any]) -> Any:
    if isinstance(value, BaseModel):
        return wire_dict(value)
    return dict(value)
