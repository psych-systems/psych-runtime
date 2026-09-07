"""The HTTP+JSON/REST binding: paths, query parameters, and SSE framing.

§11, plus the ``google.api.http`` annotations in the proto that §11 renders as
prose. Like ``psych_runtime.a2a.jsonrpc`` this module owns wire concerns only: it says
what a path looks like and how a GET's query string becomes a request message,
and it never touches a Run.

## One conflict in the specification, and how it is resolved

``SubscribeToTask`` is annotated ``get: "/tasks/{id=*}:subscribe"`` in the
proto, while §5.3's mapping table and §11.3.2 both write ``POST
/tasks/{id}:subscribe``. §1.4 makes the proto "the single authoritative
normative definition", so the proto wins and ``REST_ROUTES`` records GET. But
a client written against the prose will send POST, and refusing it would be
standing on a technicality at the cost of interoperability, so
``SUBSCRIBE_METHODS`` names both and the playground's router accepts either.
The conflict is upstream's; carrying it in one constant, with the reason, is
better than each route guessing.

## Why the tenant prefix exists

Every operation in the proto carries a second ``additional_bindings`` entry
under ``/{tenant}/``, and the ``tenant`` field's own comment defines it as "an
opaque string used for routing requests to a specific agent or tenant when
multiple agents are served behind a single A2A endpoint". That is exactly the
shape of a platform built on Psych: one deployment, many published agents. So
the tenant segment is a first-class part of the route table here, and the
playground uses it to name *which agent* a message is for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ValidationError

from psych_runtime.a2a.errors import A2AError, InvalidParamsError, rpc_status_body
from psych_runtime.a2a.jsonrpc import A2AMethod
from psych_runtime.a2a.models import (
    A2A_MEDIA_TYPE,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTasksRequest,
)

__all__ = [
    "A2A_MEDIA_TYPE",
    "REST_ROUTES",
    "SSE_MEDIA_TYPE",
    "SUBSCRIBE_METHODS",
    "TENANT_PREFIX",
    "error_body",
    "get_task_request_from_query",
    "list_push_configs_request_from_query",
    "list_tasks_request_from_query",
    "sse_frame",
]

SSE_MEDIA_TYPE: Final = "text/event-stream"

TENANT_PREFIX: Final = "/{tenant}"
"""The proto's ``additional_bindings`` prefix, present on every operation."""

REST_ROUTES: Final[Mapping[A2AMethod, tuple[str, str]]] = {
    A2AMethod.SEND_MESSAGE: ("POST", "/message:send"),
    A2AMethod.SEND_STREAMING_MESSAGE: ("POST", "/message:stream"),
    A2AMethod.GET_TASK: ("GET", "/tasks/{id}"),
    A2AMethod.LIST_TASKS: ("GET", "/tasks"),
    A2AMethod.CANCEL_TASK: ("POST", "/tasks/{id}:cancel"),
    # GET, from the proto's own http annotation. See the module docstring for
    # the specification's internal disagreement about this one.
    A2AMethod.SUBSCRIBE_TO_TASK: ("GET", "/tasks/{id}:subscribe"),
    A2AMethod.CREATE_TASK_PUSH_NOTIFICATION_CONFIG: (
        "POST",
        "/tasks/{task_id}/pushNotificationConfigs",
    ),
    A2AMethod.GET_TASK_PUSH_NOTIFICATION_CONFIG: (
        "GET",
        "/tasks/{task_id}/pushNotificationConfigs/{id}",
    ),
    A2AMethod.LIST_TASK_PUSH_NOTIFICATION_CONFIGS: (
        "GET",
        "/tasks/{task_id}/pushNotificationConfigs",
    ),
    A2AMethod.DELETE_TASK_PUSH_NOTIFICATION_CONFIG: (
        "DELETE",
        "/tasks/{task_id}/pushNotificationConfigs/{id}",
    ),
    A2AMethod.GET_EXTENDED_AGENT_CARD: ("GET", "/extendedAgentCard"),
}
"""Every operation's REST method and path, relative to an interface's base URL.

Exhaustive over ``A2AMethod`` on purpose: §5.1 requires an agent offering two
bindings to offer the same operations in both, and a table that can be checked
against the enum in one assertion is how that stays true.
"""

SUBSCRIBE_METHODS: Final = ("GET", "POST")
"""What a server should accept for ``:subscribe``, and why: see the module
docstring. GET is normative, POST is what a client written against §11.3.2
will send."""


def sse_frame(payload: Mapping[str, object], *, event: str | None = None) -> bytes:
    """One Server-Sent Event carrying a JSON payload (§9.4.2, §11.7).

    A single ``data:`` line: the JSON is serialised without newlines, so it
    cannot be split across data lines and reassembled wrongly by a client that
    is lenient about the framing.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n".encode()


def error_body(error: A2AError) -> tuple[int, dict[str, object]]:
    """§11.6: the HTTP status and the ``google.rpc.Status`` body for it."""
    return error.http_status, rpc_status_body(error)


# ---------------------------------------------------------------------------
# §11.5: query parameters for the methods with no request body
# ---------------------------------------------------------------------------


def _single(params: Mapping[str, str | Sequence[str]], name: str) -> str | None:
    """One value for ``name``, or ``None``.

    A repeated parameter takes its last value, which is what every HTTP
    framework in wide use does. §11.5 only allows repetition for repeated
    fields, and none of the three query-parameter methods has one.
    """
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return value[-1] if value else None


def _validated[M: BaseModel](model: type[M], values: Mapping[str, object], what: str) -> M:
    """Validate a request message built from a query string, or say why not.

    Every query-parameter method funnels through here so a malformed
    ``pageSize`` produces the same ``-32602``/400 pair as a malformed body
    would, which is what §5.1's "Same Error Handling" asks of two bindings.
    """
    try:
        return model.model_validate(values)
    except ValidationError as err:
        raise InvalidParamsError(f"invalid query parameters for {what}: {err}") from err


def get_task_request_from_query(
    task_id: str, params: Mapping[str, str | Sequence[str]], *, tenant: str | None = None
) -> GetTaskRequest:
    """``GET /tasks/{id}?historyLength=10`` as its request message (§11.5)."""
    values: dict[str, object] = {"id": task_id, "tenant": tenant}
    history_length = _single(params, "historyLength")
    if history_length is not None:
        values["historyLength"] = history_length
    return _validated(GetTaskRequest, values, "GetTask")


def list_tasks_request_from_query(
    params: Mapping[str, str | Sequence[str]], *, tenant: str | None = None
) -> ListTasksRequest:
    """``GET /tasks?...`` as its request message (§11.5).

    Names are camelCase, matching the JSON body spelling exactly, "to ensure
    consistency with request bodies used in POST operations". Booleans are the
    lowercase strings ``true``/``false``, enums are their ProtoJSON string
    names, and timestamps are ISO 8601 -- all of which pydantic already parses
    from strings, so this function's job is to pass through only the
    parameters that were actually sent. Passing an absent parameter as
    ``None`` would set the field explicitly, and §5.7 makes "explicitly null"
    different from "omitted".
    """
    values: dict[str, object] = {"tenant": tenant}
    for name in (
        "contextId",
        "status",
        "pageSize",
        "pageToken",
        "historyLength",
        "statusTimestampAfter",
        "includeArtifacts",
    ):
        value = _single(params, name)
        if value is not None:
            values[name] = value
    return _validated(ListTasksRequest, values, "ListTasks")


def list_push_configs_request_from_query(
    task_id: str, params: Mapping[str, str | Sequence[str]], *, tenant: str | None = None
) -> ListTaskPushNotificationConfigsRequest:
    """``GET /tasks/{id}/pushNotificationConfigs?...`` as its message."""
    values: dict[str, object] = {"taskId": task_id, "tenant": tenant}
    for name in ("pageSize", "pageToken"):
        value = _single(params, name)
        if value is not None:
            values[name] = value
    return _validated(
        ListTaskPushNotificationConfigsRequest, values, "ListTaskPushNotificationConfigs"
    )
