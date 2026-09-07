"""HTTP tools: entirely data, executed through the one egress seam.

DESIGN.md §10.1: an HTTP tool is a URL, a method, a JSON schema and a
credential reference, creatable at runtime by an end user with no code and no
redeploy. This module is what turns that data into a real request and a
result the model can read.

## Everything goes through the egress seam, without importing it

DESIGN.md §14 requires every outbound HTTP call, this one included, to go
through the one seam a consumer's egress policy can see. The seam itself is
``psych_runtime.model.egress.HttpTransport``, but ``psych_runtime.tools`` and
``psych_runtime.model`` sit in the same import-linter layer (see
``pyproject.toml``) and neither may
import the other, so this module cannot name that class directly. Instead it
declares ``HttpToolTransport`` below: a Protocol shaped exactly like the real
``HttpTransport``'s ``request`` method. The runtime layer, which sits above
both, constructs the real transport and hands it in here, satisfying the
Protocol structurally. ``psych_runtime.tools.mcp`` does the same thing for MCP
connections, for the same reason.

## Argument templating

The model sends one flat JSON object of arguments, shaped by the tool's
``input_schema``. Turning that into a request follows three rules, in order:

1. **Path parameters.** Every ``{name}`` token in ``tool.url`` is filled from
   ``arguments[name]``, URL-escaped, and removed from the remaining
   arguments. A token with no matching argument is a permanent failure: the
   model sent an incomplete call, and retrying the identical call will not
   fix that.
2. **GET and DELETE.** Every argument left after path substitution becomes a
   query string parameter.
3. **POST, PUT and PATCH.** Every argument left after path substitution
   becomes the JSON request body, as one object. There is no way to mix a
   query parameter into one of these three methods: a tool needing both
   states one of the values in the URL as a path parameter instead, which
   keeps the rule simple enough to predict from the method alone.

This is deliberately simpler than a full OpenAPI parameter binding (no
``in: header`` or ``in: cookie``, no per-property override): the trade-off is
that a Spec author cannot express every possible API shape, in exchange for
an end user creating an HTTP tool at runtime never having to learn a second
schema dialect to say where an argument goes.

## Credentials

``tool.credential`` names a value the ``SecretResolver`` port resolves fresh
for the calling Scope. ``HttpTool``'s own validator already refuses a literal
``Authorization`` header in a Spec, so when a credential is configured it is
the only source of one: resolved, then sent as ``Authorization: Bearer
<value>``, the same convention ``psych_runtime.tools.mcp`` uses for its MCP
connections. A consumer whose API wants a different header shape puts a
non-secret template in ``tool.headers`` and keeps the secret half in
``credential``, or fronts the call with a proxy that adds the header it wants.

## Failure classification

A non-2xx response becomes ``HttpToolError``, carrying the status code as a
``status_code`` attribute. ``psych_runtime.model.transient.is_transient`` (called by
the agent loop that owns the generic except block, not by this module, for
the same layering reason as above) reads that attribute through
``classify_status`` and retries a 5xx or a 429 but not a 400. A request that
times out or never reaches the server at all raises
``psych_runtime.core.errors.TransientError`` directly, which
``psych_runtime.model.transient`` always retries: a network blip is exactly the kind
of failure a second attempt might not repeat.

## Bounding what comes back

Two different things are bounded, for two different reasons. A failure's body
excerpt is capped short (``_MAX_FAILURE_EXCERPT_CHARS``) because it becomes
part of ``ToolFailure.message``, which has an 8192-character ceiling of its
own; going over that turns a failed tool call into a Pydantic validation
error instead of the result the model was supposed to read. A success body is
capped much more generously (``max_response_chars`` on the executor,
independent of any Spec setting) purely so one HTTP call cannot hand back an
unbounded string; the Spec's own ``limits.large_result_bytes`` elision
(DESIGN.md §10.8) happens afterwards, in the agent loop, against whatever this
executor returns, and is a separate, smaller threshold aimed at the model's
context rather than at this executor's own return value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from psych_runtime.core.errors import PsychError, TransientError
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import HttpTool
from psych_runtime.tools.secrets import CredentialNotFound, SecretResolver

__all__ = ["HttpToolError", "HttpToolExecutor", "HttpToolTransport"]

_PATH_PARAM: Final = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_BODY_METHODS: Final = frozenset({"POST", "PUT", "PATCH"})

_MAX_FAILURE_EXCERPT_CHARS: Final = 2000
"""Keeps a failure message safely under ToolFailure.message's 8192-character
cap even after the surrounding prose is added."""

_DEFAULT_MAX_RESPONSE_CHARS: Final = 65_536
"""A defensive ceiling on a successful body, independent of the Spec's own
``limits.large_result_bytes`` (default 32768) elision. That threshold trims
what the *model* sees and still keeps the full result in the log; this one
bounds what this executor hands back at all, before any of that happens."""


@runtime_checkable
class HttpToolTransport(Protocol):
    """The outbound HTTP capability this module needs, without a layering
    violation. Structurally, not nominally, typed against
    ``psych_runtime.model.egress.HttpTransport.request``; see the module docstring.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, str] | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response: ...


class HttpToolError(PsychError):
    """An HTTP tool call reached the server and got a non-2xx response.

    Carries ``status_code`` so ``psych_runtime.model.transient.is_transient`` (which
    reads that attribute off any exception, not just this one) can classify a
    5xx or 429 as worth retrying and a 400 as not, without this module having
    to import that classifier itself.
    """

    def __init__(self, *, tool: str, status_code: int, message: str) -> None:
        self.tool = tool
        self.status_code = status_code
        super().__init__(message)


class HttpToolExecutor:
    """Turns one ``HttpTool`` plus one call's arguments into a real request.

    Holds no per-Run state: the transport and the secret resolver are process
    wide, and every other input arrives as an argument to ``call``, so one
    instance serves every tenant's Runs concurrently. Isolation comes from
    always resolving the credential fresh for the Scope handed to ``call``,
    never from anything cached on this object.
    """

    def __init__(
        self,
        transport: HttpToolTransport,
        secrets: SecretResolver,
        *,
        max_response_chars: int = _DEFAULT_MAX_RESPONSE_CHARS,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self._max_response_chars = max_response_chars

    async def call(self, scope: Scope, tool: HttpTool, arguments: Mapping[str, Any]) -> Any:
        """Execute ``tool`` with ``arguments`` for ``scope``.

        Returns:
            The parsed JSON body when the response says it is JSON and it
            still fits under ``max_response_chars``; the raw text otherwise.

        Raises:
            CredentialNotFound: ``tool.credential`` names a credential this
                Scope has no value for.
            ValueError: the URL names a path parameter ``arguments`` did not
                supply. A malformed call from the model, not a network
                problem, so it is not retried.
            TransientError: the request timed out or the server was
                unreachable.
            HttpToolError: the server answered with a non-2xx status.
        """
        url, params, body = _build_request(tool, arguments)
        headers = dict(tool.headers)
        if tool.credential is not None:
            credential = await self._secrets.resolve(scope, tool.credential)
            if credential is None:
                raise CredentialNotFound(scope, tool.credential)
            headers["Authorization"] = f"Bearer {credential.secret.get_secret_value()}"

        try:
            response = await self._transport.request(
                tool.method,
                url,
                scope=scope,
                headers=headers,
                params=params,
                json=body,
                timeout=tool.timeout_seconds,
            )
        except httpx.TimeoutException as err:
            raise TransientError(
                f"http tool {tool.name!r} timed out after {tool.timeout_seconds}s calling "
                f"{tool.method} {url}"
            ) from err
        except httpx.HTTPError as err:
            # Everything else httpx raises before a response exists: DNS
            # failure, connection refused, connection reset mid-request. The
            # network failed, not the server's logic, so a second attempt
            # after a beat is exactly what might succeed.
            raise TransientError(
                f"http tool {tool.name!r} could not reach {tool.method} {url}: {err}"
            ) from err

        if not (200 <= response.status_code < 300):
            raise HttpToolError(
                tool=tool.name,
                status_code=response.status_code,
                message=_failure_message(tool, url, response),
            )

        return _success_result(response, self._max_response_chars)

    def bind(self, scope: Scope) -> Callable[[HttpTool, Mapping[str, Any]], Any]:
        """A closure matching ``ToolExecutor``'s ``http_caller`` shape.

        ``psych_runtime.runtime.agent.ToolExecutor.call`` invokes its ``http_caller``
        as ``http_caller(tool, arguments)``, with no Scope: the ``Runtime`` it
        lives on is built once per process and shared across every tenant's
        Runs (DESIGN.md §21 holds no per-Run state there). Whoever assembles a
        ``ToolExecutor`` for one Run's attempt closes over that Run's Scope by
        calling this once and passing the result as ``http_caller``, so the
        Scope this method's ``call`` needs travels through the closure instead
        of a parameter ``ToolExecutor`` does not have.
        """

        async def caller(tool: HttpTool, arguments: Mapping[str, Any]) -> Any:
            return await self.call(scope, tool, arguments)

        return caller


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


def _fill_path(url: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Substitute every ``{name}`` in ``url`` from ``arguments``, and return
    what is left over for the query string or the body."""
    remaining = dict(arguments)

    def substitute(match: re.Match[str]) -> str:
        param = match.group(1)
        if param not in remaining:
            raise ValueError(
                f"url {url!r} names path parameter {{{param}}} but no argument "
                f"named {param!r} was given"
            )
        return quote(str(remaining.pop(param)), safe="")

    filled = _PATH_PARAM.sub(substitute, url)
    return filled, remaining


def _build_request(
    tool: HttpTool, arguments: Mapping[str, Any]
) -> tuple[str, dict[str, str] | None, dict[str, Any] | None]:
    """The filled URL, and either query parameters or a JSON body from
    whatever arguments the path did not consume. See the module docstring for
    the three-rule templating contract this implements."""
    url, remaining = _fill_path(tool.url, dict(arguments))

    if tool.method in _BODY_METHODS:
        return url, None, (remaining or None)

    params = {key: _stringify(value) for key, value in remaining.items() if value is not None}
    return url, (params or None), None


def _stringify(value: Any) -> str:
    """Predictable query-string rendering: ``True``/``False`` become the
    lowercase JSON spelling a receiving API is most likely to expect, and
    everything else is Python's own ``str()``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


def _failure_message(tool: HttpTool, url: str, response: httpx.Response) -> str:
    text = response.text
    excerpt = text[:_MAX_FAILURE_EXCERPT_CHARS]
    if len(text) > _MAX_FAILURE_EXCERPT_CHARS:
        excerpt += f" ...[{len(text) - _MAX_FAILURE_EXCERPT_CHARS} more characters omitted]"
    return f"{tool.method} {url} returned HTTP {response.status_code}: {excerpt}"


def _success_result(response: httpx.Response, max_chars: int) -> Any:
    content_type = response.headers.get("content-type", "")
    text = response.text
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n...[truncated: showing {max_chars} of {len(text)} characters]"
    if "application/json" in content_type:
        try:
            return response.json()
        except ValueError:
            pass  # Server lied about its content-type; fall through to text.
    return text
