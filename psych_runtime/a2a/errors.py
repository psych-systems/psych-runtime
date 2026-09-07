"""A2A's error taxonomy, and the one place each binding's codes are decided.

§5.4 gives a table mapping nine A2A error types to a JSON-RPC code, a gRPC
status and an HTTP status. §9.5 and §11.6 then give each binding's payload
shape. The temptation is to write those mappings out at each route, which is
how a ``TaskNotFoundError`` ends up a 404 over REST and a ``-32603`` over
JSON-RPC in the same server -- the exact "same error handling" requirement
§5.1 makes of an agent that offers two bindings.

So the table lives here once, on the exception type itself, and both bindings
render from it. An error type added later without a row is a type error at
definition rather than a wrong code at runtime.

## Why these are exceptions rather than a return type

The rest of Psych raises typed errors (``psych_runtime.core.errors``) and the inbound
binding is a request handler: it is going to have a ``try``/``except`` at the
edge whatever shape this takes. Raising means a helper five frames down can
refuse without every frame between it and the route passing a failure back by
hand, which is where a "not found" quietly becomes a 500.

## The detail array

Both bindings carry ``details``/``data`` as an array of objects each keyed by
``@type`` (§9.5, §11.6). §11.6 additionally *requires* a
``google.rpc.ErrorInfo`` entry for A2A-specific errors, because several of
them share one HTTP status: without it, a client seeing ``400`` cannot tell
``TaskNotCancelableError`` from ``PushNotificationNotSupportedError``. That is
why ``reason`` exists on every type below and why it is the error name in
UPPER_SNAKE_CASE with the ``Error`` suffix dropped, exactly as §11.6
prescribes.
"""

from __future__ import annotations

from typing import Any, Final

from psych_runtime.core.errors import PsychError

__all__ = [
    "A2A_ERROR_DOMAIN",
    "A2AError",
    "ContentTypeNotSupportedError",
    "ExtendedAgentCardNotConfiguredError",
    "ExtensionSupportRequiredError",
    "InternalError",
    "InvalidAgentResponseError",
    "InvalidParamsError",
    "InvalidRequestError",
    "JsonParseError",
    "MethodNotFoundError",
    "PushNotificationNotSupportedError",
    "TaskNotCancelableError",
    "TaskNotFoundError",
    "UnsupportedOperationError",
    "VersionNotSupportedError",
    "error_info_detail",
    "jsonrpc_error_object",
    "rpc_status_body",
]

A2A_ERROR_DOMAIN: Final = "a2a-protocol.org"
"""§11.6: the ``domain`` every ``google.rpc.ErrorInfo`` this server writes
carries."""

_ERROR_INFO_TYPE: Final = "type.googleapis.com/google.rpc.ErrorInfo"


class A2AError(PsychError):
    """One row of §5.4's mapping table, raised.

    Subclasses set the four class attributes and nothing else. Everything a
    binding needs to render the error is then available without the binding
    knowing which error it holds, which is what keeps the two bindings
    consistent (§5.1).

    Attributes:
        jsonrpc_code: §5.4 / §9.5.
        http_status: §5.4 / §11.6.
        grpc_status: §5.4. Carried even though Psych ships no gRPC binding,
            because the value belongs to the error rather than to a binding,
            and a consumer who does write one should not have to re-derive it
            from the specification.
        reason: the ``google.rpc.ErrorInfo`` reason (§11.6), UPPER_SNAKE_CASE
            without the ``Error`` suffix.
    """

    jsonrpc_code: int = -32603
    http_status: int = 500
    grpc_status: str = "INTERNAL"
    reason: str = "INTERNAL"

    def __init__(self, message: str, *, metadata: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.metadata = dict(metadata or {})
        """Free-form context that rides in the ``ErrorInfo`` (§11.6's example
        carries ``taskId`` and ``timestamp`` there). Strings only, because
        ``ErrorInfo.metadata`` is ``map<string, string>``."""


# ---------------------------------------------------------------------------
# The A2A-specific errors (§3.3.2, coded by §5.4)
# ---------------------------------------------------------------------------


class TaskNotFoundError(A2AError):
    """No such task, or none this caller may see.

    §13.1 is deliberate about the second half: a server "SHOULD NOT
    distinguish between 'does not exist' and 'not authorized'", so a Run
    belonging to another Scope raises this rather than a permission error that
    would confirm the Run exists.
    """

    jsonrpc_code = -32001
    http_status = 404
    grpc_status = "NOT_FOUND"
    reason = "TASK_NOT_FOUND"


class TaskNotCancelableError(A2AError):
    """The task already reached a terminal state."""

    jsonrpc_code = -32002
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "TASK_NOT_CANCELABLE"


class PushNotificationNotSupportedError(A2AError):
    """``capabilities.pushNotifications`` is false or absent (§3.3.4)."""

    jsonrpc_code = -32003
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "PUSH_NOTIFICATION_NOT_SUPPORTED"


class UnsupportedOperationError(A2AError):
    """The operation, or this aspect of it, is not supported here.

    §3.3.4 makes this the answer for streaming against an agent that does not
    declare it, and §3.1.6 makes it the answer for subscribing to a task that
    is already terminal.
    """

    jsonrpc_code = -32004
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "UNSUPPORTED_OPERATION"


class ContentTypeNotSupportedError(A2AError):
    """A media type in the request is one this agent cannot read."""

    jsonrpc_code = -32005
    http_status = 400
    grpc_status = "INVALID_ARGUMENT"
    reason = "CONTENT_TYPE_NOT_SUPPORTED"


class InvalidAgentResponseError(A2AError):
    """A peer answered with something the specification does not allow.

    Raised by the *outbound* client (``psych_runtime.tools.a2a``) rather than by the
    inbound binding: it is the error for "the agent I called misbehaved".
    """

    jsonrpc_code = -32006
    http_status = 500
    grpc_status = "INTERNAL"
    reason = "INVALID_AGENT_RESPONSE"


class ExtendedAgentCardNotConfiguredError(A2AError):
    """Support is declared but no extended card exists (§13.3)."""

    jsonrpc_code = -32007
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "EXTENDED_AGENT_CARD_NOT_CONFIGURED"


class ExtensionSupportRequiredError(A2AError):
    """A required extension was not opted into (§4.6.3)."""

    jsonrpc_code = -32008
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "EXTENSION_SUPPORT_REQUIRED"


class VersionNotSupportedError(A2AError):
    """The requested ``A2A-Version`` is not one this interface speaks
    (§3.6.2)."""

    jsonrpc_code = -32009
    http_status = 400
    grpc_status = "FAILED_PRECONDITION"
    reason = "VERSION_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# The JSON-RPC standard errors (§9.5)
# ---------------------------------------------------------------------------


class JsonParseError(A2AError):
    """§9.5: "The server received invalid JSON"."""

    jsonrpc_code = -32700
    http_status = 400
    grpc_status = "INVALID_ARGUMENT"
    reason = "JSON_PARSE"


class InvalidRequestError(A2AError):
    """§9.5: not a valid JSON-RPC Request object."""

    jsonrpc_code = -32600
    http_status = 400
    grpc_status = "INVALID_ARGUMENT"
    reason = "INVALID_REQUEST"


class MethodNotFoundError(A2AError):
    """§9.5: no such method."""

    jsonrpc_code = -32601
    http_status = 404
    grpc_status = "NOT_FOUND"
    reason = "METHOD_NOT_FOUND"


class InvalidParamsError(A2AError):
    """§9.5: the parameters did not validate.

    Also the answer for a REST body that fails validation, which is why the
    HTTP status is the 400 §3.3.2 asks for rather than JSON-RPC's own notion
    of a parameter problem.
    """

    jsonrpc_code = -32602
    http_status = 400
    grpc_status = "INVALID_ARGUMENT"
    reason = "INVALID_PARAMS"


class InternalError(A2AError):
    """§9.5: anything that went wrong on this side and is not one of the
    above."""

    jsonrpc_code = -32603
    http_status = 500
    grpc_status = "INTERNAL"
    reason = "INTERNAL"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def error_info_detail(error: A2AError) -> dict[str, Any]:
    """The ``google.rpc.ErrorInfo`` object §11.6 requires for A2A errors."""
    detail: dict[str, Any] = {
        "@type": _ERROR_INFO_TYPE,
        "reason": error.reason,
        "domain": A2A_ERROR_DOMAIN,
    }
    if error.metadata:
        detail["metadata"] = dict(error.metadata)
    return detail


def jsonrpc_error_object(error: A2AError) -> dict[str, Any]:
    """§9.5's error object: code, message, and ``data`` as a typed array.

    ``data`` is an array rather than the free-form object JSON-RPC 2.0 itself
    allows, because §9.5 says so: "Error Details: Mapped to ``error.data``
    (array of objects, each containing a ``@type`` key)".
    """
    return {
        "code": error.jsonrpc_code,
        "message": error.message,
        "data": [error_info_detail(error)],
    }


def rpc_status_body(error: A2AError) -> dict[str, Any]:
    """§11.6's body: a ``google.rpc.Status`` under an ``error`` key.

    ``code`` is the HTTP status, not the gRPC integer: §11.6 says "Error Code:
    Mapped to the HTTP status code and the ``error.code`` field", and its own
    example shows ``"code": 404`` beside ``"status": "NOT_FOUND"``.
    """
    return {
        "error": {
            "code": error.http_status,
            "status": error.grpc_status,
            "message": error.message,
            "details": [error_info_detail(error)],
        }
    }
