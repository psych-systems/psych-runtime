"""MCP tool connections for Psych agents.

Psych uses the official Python MCP SDK for protocol negotiation, typed wire
validation, Streamable HTTP, the older HTTP and SSE transport, notifications,
tool calls, OAuth discovery, grants, refresh, and scope step-up. Psych owns the
runtime concerns around that client: tenant-scoped storage and pooling,
credential resolution, egress policy, catalogue freshness, access narrowing,
and model-facing tool names.

A model tool call does not contain an MCP server id. Psych therefore exposes a
remote tool as ``server__tool`` and carries that qualified name through the
run log. The server receives its original tool name.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any, Final, Literal, Protocol, runtime_checkable

import httpx
import httpx2
from mcp import Client as SdkClient
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ListenNotSupportedError, SubscriptionLost
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ToolsListChanged
from mcp_types import Implementation, InputRequiredResult, ToolListChangedNotification
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from psych_runtime.core.errors import AccessDenied, PsychError
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, McpServer
from psych_runtime.core.tool_names import mcp_tool_name
from psych_runtime.tools.narrowing import narrow
from psych_runtime.tools.oauth import (
    BearerChallenge,
    ClientIdentityConfig,
    GrantKind,
    NoActiveSession,
    OAuthClient,
    ReauthorizationRequired,
    canonicalize_resource_uri,
    find_bearer_challenge,
)
from psych_runtime.tools.secrets import CredentialNotFound, ResolvedCredential, SecretResolver

__all__ = [
    "DEFAULT_CATALOGUE_TTL_SECONDS",
    "McpConnection",
    "McpConnectionStatus",
    "McpHeaderMismatchError",
    "McpInputRequiredError",
    "McpMissingClientCapabilityError",
    "McpPool",
    "McpPoolKey",
    "McpProbe",
    "McpProtocolError",
    "McpResourceNotFoundError",
    "McpServerResolution",
    "McpServerUnreachable",
    "McpToolError",
    "McpToolResult",
    "McpTools",
    "McpTransport",
    "McpUnsupportedProtocolVersionError",
    "resolve_mcp_server",
    "resolve_mcp_servers",
]

DEFAULT_CATALOGUE_TTL_SECONDS: Final = 300.0
"""How long a cached catalogue is trusted before the background sweep or the
next ``list_tools`` call refreshes it (DESIGN.md §10.3), when the server has
not supplied its own ``ttlMs`` on the cacheable result. A server-supplied
``ttlMs`` always wins over this default; see ``_Catalogue.ttl_override_seconds``."""

_MODERN_PROTOCOL_VERSION: Final = "2026-07-28"
"""The revision this module speaks natively: stateless, per-request ``_meta``,
``server/discover``, ``resultType``, ``subscriptions/listen``."""

_CLIENT_NAME: Final = "psych"
_CLIENT_VERSION: Final = "0.1.0"

# Error code allocation, MCP basic index §Error Codes. -32000..-32019 stays
# implementation-defined (legacy); -32020..-32099 is reserved for the spec.
_HEADER_MISMATCH_CODE: Final = -32020
_MISSING_CLIENT_CAPABILITY_CODE: Final = -32021
_UNSUPPORTED_PROTOCOL_VERSION_CODE: Final = -32022
_RESOURCE_NOT_FOUND_CODE: Final = -32602
"""2026-07-28: JSON-RPC's generic "Invalid params" code, repurposed by MCP
specifically for a missing resource. Renumbered from ``-32002``."""
_LEGACY_RESOURCE_NOT_FOUND_CODE: Final = -32002
"""2025-11-25 and earlier. Clients SHOULD still accept it from older servers."""
# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class McpToolError(PsychError):
    """An MCP server ran the tool and reported that it failed.

    ``tools/call`` answers a failure in-band, with ``isError: true`` and the
    reason as content, rather than as a JSON-RPC error. Returning that to the
    loop as an ordinary value recorded the call ``ok``: the failure-streak
    guard never saw an MCP tool fail, the repetition guard counted the failures
    as identical successes, and the model was handed a result object whose
    rendering differed between stores. Raising means an MCP failure is recorded
    exactly like a code tool's -- an error outcome, with the server's own text
    as the message.
    """

    def __init__(self, server: str, tool: str, content: str) -> None:
        self.server = server
        self.tool = tool
        self.content = content
        super().__init__(f"MCP tool {tool!r} on server {server!r} reported an error: {content}")


class McpServerUnreachable(PsychError):
    """An MCP server could not be reached, or refused the connection.

    DESIGN.md §10.7: unreachable fails the Run by default for a required
    server. A server marked ``optional`` has this caught by
    ``resolve_mcp_server``, which omits its tools instead of raising.
    """

    def __init__(self, server: str, reason: str) -> None:
        self.server = server
        self.reason = reason
        super().__init__(f"MCP server {server!r} is unreachable: {reason}")


class McpProtocolError(PsychError):
    """The server responded, but not with something MCP can use.

    Distinct from ``McpServerUnreachable``: the server was reached and had
    something to say, it just was not a valid JSON-RPC result. Treating this
    the same as unreachable would let ``optional=True`` paper over a server
    that is actively misbehaving rather than merely absent. The typed
    subclasses below are all still this: a caller that wants "any protocol
    problem" can keep catching this one type.
    """


class McpInputRequiredError(McpProtocolError):
    """A server asked for roots, sampling, or elicitation input (MRTR,
    ``resultType: "input_required"``), which Psych cannot supply.

    MCP 2026-07-28 replaced server-initiated ``roots/list``,
    ``sampling/createMessage`` and ``elicitation/create`` requests with the
    Multi Round-Trip Requests pattern: a result carrying
    ``resultType: "input_required"`` and an ``inputRequests`` map the client
    is expected to fulfil before retrying the original request. Psych
    implements none of roots, sampling or elicitation (the project refusal
    list), so there is nothing to fulfil the request with. Raising here,
    namedly, is deliberate: silently returning an empty result would tell
    the model a tool call succeeded with no output, when what actually
    happened is that the server is waiting on interaction Psych cannot give
    it. ``psych_runtime.tools.mcp`` declares no roots/sampling/elicitation
    capability in ``_meta``, so a well-behaved server should raise
    ``McpMissingClientCapabilityError`` before ever reaching this point; this
    exists for a server that tries anyway.
    """

    def __init__(self, server: str, result: Mapping[str, Any]) -> None:
        self.server = server
        requests = result.get("inputRequests")
        methods: list[str] = []
        if isinstance(requests, Mapping):
            methods = sorted(
                {
                    str(req["method"])
                    for req in requests.values()
                    if isinstance(req, Mapping) and "method" in req
                }
            )
        wanted = ", ".join(methods) if methods else "input Psych does not support providing"
        super().__init__(
            f"MCP server {server!r} returned resultType='input_required' asking for "
            f"{wanted}. Psych implements no roots, sampling, or elicitation, so this "
            "call cannot be completed; this is not a successful empty result."
        )


class McpHeaderMismatchError(McpProtocolError):
    """Error code -32020. The server rejected this request's Streamable HTTP
    metadata headers (``MCP-Protocol-Version``, ``Mcp-Method``, ``Mcp-Name``)
    as missing or not matching the JSON-RPC body."""

    def __init__(self, server: str, message: str | None) -> None:
        self.server = server
        self.message_text = message
        super().__init__(f"MCP server {server!r} rejected the request headers: {message}")


class McpMissingClientCapabilityError(McpProtocolError):
    """Error code -32021. The server needed a client capability (``roots``,
    ``sampling``, ``elicitation``) that Psych's ``_meta`` never declares,
    because Psych implements none of them. This is the server telling us so
    up front, rather than Psych discovering it via a fruitless
    ``input_required`` retry it can never complete. See
    ``McpInputRequiredError``.
    """

    def __init__(self, server: str, message: str | None, data: Mapping[str, Any] | None) -> None:
        self.server = server
        required = data.get("requiredCapabilities") if data is not None else None
        self.required_capabilities = tuple(required) if isinstance(required, list) else ()
        detail = self.required_capabilities or message
        super().__init__(
            f"MCP server {server!r} requires a client capability Psych does not support: {detail}"
        )


class McpUnsupportedProtocolVersionError(McpProtocolError):
    """Error code -32022, or a ``server/discover`` response whose
    ``supportedVersions`` never includes a version this module speaks."""

    def __init__(self, server: str, message: str | None, supported: Sequence[str] = ()) -> None:
        self.server = server
        self.supported = tuple(supported)
        detail = f"; server supports {list(self.supported)}" if self.supported else ""
        reason = message or "no mutually supported protocol version"
        super().__init__(f"MCP server {server!r} rejected the protocol version: {reason}{detail}")


class McpResourceNotFoundError(McpProtocolError):
    """Error code -32602 (2026-07-28) or -32002 (2025-11-25 and earlier).

    The modern code is JSON-RPC's generic "Invalid params" repurposed by MCP
    specifically for a missing resource. This client currently only calls
    ``tools/list`` and ``tools/call``, for which -32602 has no other
    documented meaning in MCP's own error table, so mapping it here is
    unambiguous today; a future caller of ``resources/read`` reusing this
    mapping for an unrelated invalid-params failure would be misled by the
    name and should special-case it instead.
    """

    def __init__(self, server: str, message: str | None) -> None:
        self.server = server
        super().__init__(f"MCP server {server!r} reported a resource not found: {message}")


def _error_to_exception(server: str, error: Mapping[str, Any]) -> McpProtocolError:
    code = error.get("code")
    message = error.get("message")
    message_text = str(message) if message is not None else None
    data = error.get("data")
    data_map = data if isinstance(data, Mapping) else None
    if code == _HEADER_MISMATCH_CODE:
        return McpHeaderMismatchError(server, message_text)
    if code == _MISSING_CLIENT_CAPABILITY_CODE:
        return McpMissingClientCapabilityError(server, message_text, data_map)
    if code == _UNSUPPORTED_PROTOCOL_VERSION_CODE:
        supported = data_map.get("supported") if data_map is not None else None
        supported_list = (
            [v for v in supported if isinstance(v, str)] if isinstance(supported, list) else []
        )
        return McpUnsupportedProtocolVersionError(server, message_text, supported_list)
    if code in (_RESOURCE_NOT_FOUND_CODE, _LEGACY_RESOURCE_NOT_FOUND_CODE):
        return McpResourceNotFoundError(server, message_text)
    return McpProtocolError(f"MCP server {server!r} returned error {code}: {message}")


# ---------------------------------------------------------------------------
# OAuth challenges
# ---------------------------------------------------------------------------

_DEFAULT_OAUTH_GRANT: Final[GrantKind] = "client_credentials"
"""``authorization_code`` needs a redirect listener, and Psych owns no HTTP
server (DESIGN.md §1) to run one: a consumer wanting that grant constructs
its own ``AuthorizationRedirectPort`` and passes ``oauth_grant=
"authorization_code"`` to ``McpPool`` explicitly. ``client_credentials`` is
the grant that needs nothing from the host application, so it is the
default."""


@dataclass(frozen=True, slots=True)
class _OAuthChallenge:
    """A 401 or 403 this module can hand to ``OAuthClient``: the status code
    and the parsed ``Bearer`` challenge, carried together as one typed value
    rather than a formatted string a caller would have to re-parse."""

    status_code: int
    challenge: BearerChallenge


def _oauth_challenge_from(response: httpx.Response) -> _OAuthChallenge | None:
    """The ``_OAuthChallenge`` this response calls for, or ``None`` when it
    is not one: any status other than 401/403, a 401/403 with no
    ``WWW-Authenticate: Bearer`` challenge, or a 403 whose challenge is not
    ``error="insufficient_scope"`` (a plain 403 is an authorization decision
    OAuth cannot fix by re-authenticating, so it is left to fall through to
    the ordinary error handling below unchanged).
    """
    if response.status_code not in (401, 403):
        return None
    header = response.headers.get("www-authenticate")
    if header is None:
        return None
    challenge = find_bearer_challenge(header)
    if challenge is None:
        return None
    if response.status_code == 403 and challenge.error != "insufficient_scope":
        return None
    return _OAuthChallenge(status_code=response.status_code, challenge=challenge)


# ---------------------------------------------------------------------------
# The HTTP capability this module needs, without importing psych_runtime.model
# ---------------------------------------------------------------------------


@runtime_checkable
class McpTransport(Protocol):
    """The outbound HTTP capability MCP connections need.

    Structurally, not nominally, typed against
    ``psych_runtime.model.egress.HttpTransport``: ``psych_runtime.tools`` and
    ``psych_runtime.model`` sit in the same import-linter layer and neither may
    import the other, so
    this module cannot name that class directly. The runtime layer, which sits
    above both, constructs the real ``HttpTransport`` and passes it in here
    satisfying this Protocol by shape.

    Every MCP request goes through whatever satisfies this Protocol, and
    DESIGN.md §14 is explicit that all outbound HTTP, this included, must go
    through the one egress seam. This Protocol is how that requirement holds
    without a layering violation, not an exception to it.
    """

    def protocol_client(
        self,
        *,
        scope: Scope,
        auth: httpx2.Auth | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
    ) -> httpx2.AsyncClient:
        """Create an SDK-owned client whose requests still pass egress policy."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response: ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> AbstractAsyncContextManager[httpx.Response]: ...

    async def authorize(
        self,
        method: str,
        url: str,
        *,
        scope: Scope,
        params: Mapping[str, str] | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# The pool key
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpPoolKey:
    """Identifies one pooled MCP connection.

    DESIGN.md §10.4: never pool by URL alone, pool by
    ``(scope, server, credential)``. All four fields below are required,
    named, keyword-only through ``from_scope``; there is no bare tuple and no
    f-string concatenation standing in for this type, so there is no
    positional shortcut that constructs a key which silently drops the
    tenant. ``credential_identity`` is the resolved credential's stable,
    non-secret identity (``psych_runtime.tools.secrets.stable_credential_identity``),
    never the Spec's credential *name* and never the secret value.

    Two Specs naming the same server under the same alias but pointing at
    different URLs must not share a connection, which is why ``server_url``
    is the server's actual URL and not the Spec-local ``McpServer.name``.
    """

    tenant: str
    principal: str | None
    server_url: str
    transport: str
    credential_identity: str | None

    @classmethod
    def from_scope(
        cls, *, scope: Scope, server: McpServer, credential_identity: str | None
    ) -> McpPoolKey:
        tenant, principal = scope.pool_key
        return cls(
            tenant=tenant,
            principal=principal,
            server_url=server.url,
            transport=server.transport,
            credential_identity=credential_identity,
        )


# ---------------------------------------------------------------------------
# Tool call results
# ---------------------------------------------------------------------------


class McpToolResult(BaseModel):
    """The answer to one ``tools/call``, flattened to what a model reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class _Catalogue:
    tools: tuple[ToolDefinition, ...]
    etag: str
    fetched_at: float
    """``time.monotonic()`` at the last fetch. Never wall-clock: a catalogue's
    freshness must not jump when the system clock is adjusted."""
    ttl_override_seconds: float | None = None
    """The server's own ``ttlMs`` on the ``tools/list`` ``CacheableResult``,
    converted to seconds. ``None`` when the server did not supply one (every
    pre-2026-07-28 server, and any 2026-07-28 server that omits it), in which
    case ``McpConnection`` falls back to its configured default TTL."""
    cache_scope: Literal["public", "private"] | None = None
    """The server's ``cacheScope`` on that same result, recorded for
    observability. Never used to decide whether to share this catalogue
    anywhere: it already lives inside exactly one pool key's
    ``McpConnection`` and nowhere else, so "private" is satisfied by this
    module's structure rather than by a check here."""


@dataclass(slots=True)
class _SdkCommand:
    kind: Literal["list", "call", "close"]
    future: asyncio.Future[Any]
    name: str | None = None
    arguments: dict[str, Any] | None = None


class _SdkAuth(httpx2.Auth):
    """Bridge Psych's scoped credentials and OAuth client into the SDK."""

    requires_request_body = True

    def __init__(
        self,
        *,
        scope: Scope,
        server: McpServer,
        credential: ResolvedCredential | None,
        oauth: OAuthClient | None,
        oauth_identity: ClientIdentityConfig,
        oauth_grant: GrantKind,
    ) -> None:
        self.scope = scope
        self.server = server
        self.credential = credential
        self.oauth = oauth
        self.oauth_identity = oauth_identity
        self.oauth_grant = oauth_grant
        self.resource = canonicalize_resource_uri(server.url) if oauth is not None else ""
        self._lock = asyncio.Lock()

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        await request.aread()
        async with self._lock:
            await self._refresh_if_available()
            self._set_header(request)
            sent_token = self._token()
        response = yield request
        challenge = _oauth_challenge_from(response)  # type: ignore[arg-type]
        if challenge is None:
            return
        async with self._lock:
            # A concurrent request may already have satisfied the same
            # challenge while this response was in flight. Reuse that newer
            # token instead of performing a duplicate authorization or
            # scope step-up.
            if self._token() == sent_token:
                await self._authorize(challenge)
            self._set_header(request)
        yield request

    async def _refresh_if_available(self) -> None:
        if self.oauth is None:
            return
        with contextlib.suppress(NoActiveSession, ReauthorizationRequired):
            self.credential = await self.oauth.bearer_token(self.scope, resource=self.resource)

    def _token(self) -> str | None:
        if self.credential is None:
            return None
        return self.credential.secret.get_secret_value()

    def _set_header(self, request: httpx2.Request) -> None:
        if self.credential is not None:
            request.headers["Authorization"] = f"Bearer {self.credential.secret.get_secret_value()}"

    async def _authorize(self, challenge: _OAuthChallenge) -> None:
        if self.oauth is None:
            raise McpServerUnreachable(
                self.server.name,
                f"HTTP {challenge.status_code}: the server requires OAuth authorization "
                "but this MCP pool has no OAuthClient",
            )
        if challenge.status_code == 403:
            self.credential = await self.oauth.step_up(
                self.scope, resource=self.resource, challenge=challenge.challenge
            )
        else:
            self.credential = await self.oauth.start(
                self.scope,
                resource=self.resource,
                challenge=challenge.challenge,
                identity=self.oauth_identity,
                grant=self.oauth_grant,
            )


def _catalogue_etag(tools: tuple[ToolDefinition, ...]) -> str:
    """A content hash of a tool list, independent of the order the server
    returned it in or of key order inside any one tool's JSON Schema."""
    canonical = json.dumps(
        [tool.model_dump(mode="json") for tool in tools],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _coerce_ttl_seconds(ttl_ms: Any) -> float | None:
    """``ttlMs`` from a ``CacheableResult``, converted to seconds.

    ``bool`` is excluded even though it is an ``int`` subclass: a server
    sending ``"ttlMs": true`` is malformed, not a one-millisecond TTL.
    """
    if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, (int, float)):
        return None
    return ttl_ms / 1000.0


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpConnectionStatus:
    """What one pooled connection currently is, for a consumer to display.

    A platform showing "connected / 351 tools / refreshed 2 minutes ago" was
    otherwise reduced to calling ``tools_for`` again and inferring the rest,
    which is a second connection attempt dressed up as a status read. Every
    field here is already held by the connection; this is the read-only view of
    it, carrying no credential and no secret -- only the credential's stable
    identity, the same non-secret value the pool keys on.
    """

    tenant: str
    principal: str | None
    server: str
    url: str
    transport: str
    credential_identity: str | None
    era: Literal["modern", "legacy"] | None
    """Which protocol revision this connection negotiated, or ``None`` if it
    has not connected yet."""
    tool_count: int
    tool_names: tuple[str, ...]
    catalogue_etag: str | None
    catalogue_age_seconds: float | None
    """How long ago the catalogue was last refreshed. ``None`` before the first
    fetch."""
    catalogue_ttl_seconds: float
    """The TTL in force: the server's own ``ttlMs`` when it supplied one, this
    pool's default otherwise."""
    cache_scope: Literal["public", "private"] | None


@dataclass(frozen=True, slots=True)
class McpProbe:
    """The result of one deliberate connection attempt.

    What a consumer's "test this server" button needs and had to assemble
    itself: whether it worked, what it offers, and, when it did not, the real
    error rather than a boolean. ``ok=False`` never raises -- a probe is a
    question, and an exception is a worse answer than a described failure.
    """

    ok: bool
    server: str
    url: str
    tools: tuple[ToolDefinition, ...] = ()
    detail: str = ""
    error_type: str | None = None
    """The exception's class name when ``ok`` is false, so a caller can tell a
    credential problem (``CredentialNotFound``) from an unreachable host
    (``McpServerUnreachable``) from a server misbehaving (``McpProtocolError``)
    without matching on message text."""
    era: Literal["modern", "legacy"] | None = None


class McpConnection:
    """One live connection to one server, for one ``(scope, server,
    credential)``.

    Constructed and connected by ``McpPool``; nothing outside this module
    should build one directly, since a hand-built connection bypasses the
    pool's isolation guarantee entirely.
    """

    def __init__(
        self,
        *,
        scope: Scope,
        server: McpServer,
        transport: McpTransport,
        credential: ResolvedCredential | None,
        catalogue_ttl_seconds: float = DEFAULT_CATALOGUE_TTL_SECONDS,
        oauth: OAuthClient | None = None,
        oauth_identity: ClientIdentityConfig | None = None,
        oauth_grant: GrantKind = _DEFAULT_OAUTH_GRANT,
    ) -> None:
        self._scope = scope
        self._server = server
        self._transport = transport
        self._credential = credential
        self._ttl = catalogue_ttl_seconds
        self._oauth = oauth
        self._oauth_identity = (
            oauth_identity if oauth_identity is not None else ClientIdentityConfig()
        )
        self._oauth_grant = oauth_grant
        # The canonical form is only ever needed when OAuth is actually
        # configured (it is the ``resource`` sent on every OAuthClient call);
        # computed once here rather than on every challenge, and never
        # computed at all when there is no OAuthClient to hand it to, so a
        # malformed URL on a server nobody is authenticating against still
        # connects exactly as it always has.
        self._oauth_resource = canonicalize_resource_uri(server.url) if oauth is not None else ""

        self._era: Literal["modern", "legacy"] | None = None
        self._instructions: str | None = None
        """What the server says it is for, from the handshake.

        MCP's ``initialize`` result and ``server/discover`` both carry an
        optional ``instructions`` field, and this client used to drop it. It is
        the server's own answer to "what am I", which is exactly what a model
        choosing between three connected servers needs and had no way to get.
        A consumer's own description takes precedence (see
        ``McpTools.describe``); this is the sensible default for a server
        nobody has described."""
        self._catalogue: _Catalogue | None = None
        self._catalogue_lock = asyncio.Lock()
        self._closed = False
        self._sweep_task: asyncio.Task[None] | None = None
        self._sdk_task: asyncio.Task[None] | None = None
        self._sdk_commands: asyncio.Queue[_SdkCommand] = asyncio.Queue()
        self._sdk_auth: httpx2.Auth | None = None
        self._oauth_credential_identity: str | None = None
        self._catalogue_changed = asyncio.Event()

    def __repr__(self) -> str:
        # Deliberately never includes the credential secret, only its
        # identity: this is the object most likely to be printed while
        # debugging a pooling issue, which is exactly the moment a leaked
        # secret in a repr would be most damaging.
        identity = self.credential_identity
        return (
            f"McpConnection(server={self._server.name!r}, url={self._server.url!r}, "
            f"tenant={self._scope.tenant!r}, principal={self._scope.principal!r}, "
            f"credential_identity={identity!r}, era={self._era!r})"
        )

    @property
    def era(self) -> Literal["modern", "legacy"] | None:
        """Which protocol revision this connection negotiated, or ``None``
        before ``connect()`` has run."""
        return self._era

    def status(self, *, server_name: str | None = None) -> McpConnectionStatus:
        """This connection's current state, without touching the network."""
        catalogue = self._catalogue
        ttl = self._ttl
        if catalogue is not None and catalogue.ttl_override_seconds is not None:
            ttl = catalogue.ttl_override_seconds
        return McpConnectionStatus(
            tenant=self._scope.tenant,
            principal=self._scope.principal,
            server=server_name if server_name is not None else self._server.name,
            url=self._server.url,
            transport=self._server.transport,
            credential_identity=self.credential_identity,
            era=self._era,
            tool_count=len(catalogue.tools) if catalogue is not None else 0,
            tool_names=tuple(tool.name for tool in catalogue.tools) if catalogue else (),
            catalogue_etag=catalogue.etag if catalogue is not None else None,
            catalogue_age_seconds=(
                time.monotonic() - catalogue.fetched_at if catalogue is not None else None
            ),
            catalogue_ttl_seconds=ttl,
            cache_scope=catalogue.cache_scope if catalogue is not None else None,
        )

    @property
    def instructions(self) -> str | None:
        """The server's own description of itself, or ``None`` if it gave one
        neither at ``initialize`` nor at ``server/discover``."""
        return self._instructions

    @property
    def credential_identity(self) -> str | None:
        """The pooled identity of this connection's current credential, or
        ``None`` when it has none.

        Read by ``McpPool.get_or_connect`` right after ``connect()`` returns,
        to detect an OAuth token acquired during era negotiation or the
        legacy handshake: that changes this from whatever was resolved
        before connecting (``None``, for a server with no static
        credential), and the pool key has to move with it (DESIGN.md
        §10.4; see this module's docstring, "The pool key moves when a
        token is acquired"). A refresh or a step-up afterwards never change
        this value.
        """
        if self._credential is not None:
            return self._credential.identity
        if self._oauth_credential_identity is not None:
            return self._oauth_credential_identity
        if isinstance(self._sdk_auth, _SdkAuth) and self._sdk_auth.credential is not None:
            return self._sdk_auth.credential.identity
        return None

    # -- lifecycle ------------------------------------------------------

    async def connect(self) -> None:
        """Open the official SDK client, negotiate, and populate the catalogue.

        Raises:
            McpServerUnreachable: the server could not be reached at all.
            McpProtocolError: the server responded with something that is
                not a usable MCP handshake, or a typed subclass for a
                recognised MCP error code.
        """
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._sdk_task = asyncio.create_task(self._run_sdk(ready))
        await ready
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def close(self) -> None:
        self._closed = True
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
        if self._sdk_task is not None and not self._sdk_task.done():
            future = asyncio.get_running_loop().create_future()
            await self._sdk_commands.put(_SdkCommand("close", future))
            await future
        if self._sdk_task is not None:
            await self._sdk_task

    async def _run_sdk(  # noqa: PLR0912, PLR0915 - lifecycle state machine
        self, ready: asyncio.Future[None]
    ) -> None:
        """Own the SDK context on one task and serialize calls through it.

        The SDK uses task-bound AnyIO cancel scopes. A pool may be closed by a
        different Worker task than the one that opened it, so connection
        setup, requests, and teardown are deliberately kept on this runner.
        """
        listener: asyncio.Task[None] | None = None
        watcher: asyncio.Task[None] | None = None
        pending: _SdkCommand | None = None
        try:
            official_auth: tuple[httpx2.Auth, str] | None = None
            if self._oauth is not None and self._credential is None:
                official_auth = await self._oauth.sdk_auth(
                    self._scope,
                    resource=self._server.url,
                    identity=self._oauth_identity,
                    grant=self._oauth_grant,
                    issuer=(self._server.oauth.issuer if self._server.oauth is not None else None),
                )
            if official_auth is not None:
                self._sdk_auth, self._oauth_credential_identity = official_auth
            else:
                self._sdk_auth = _SdkAuth(
                    scope=self._scope,
                    server=self._server,
                    credential=self._credential,
                    oauth=self._oauth,
                    oauth_identity=self._oauth_identity,
                    oauth_grant=self._oauth_grant,
                )

            if self._server.transport == "sse":

                def client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx2.Timeout | None = None,
                    auth: httpx2.Auth | None = None,
                ) -> httpx2.AsyncClient:
                    return self._transport.protocol_client(
                        scope=self._scope,
                        headers=headers,
                        timeout=timeout
                        or httpx2.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
                        auth=auth,
                    )

                sdk_transport = sse_client(
                    self._server.url,
                    auth=self._sdk_auth,
                    httpx_client_factory=client_factory,
                )
            else:
                http_client = self._transport.protocol_client(
                    scope=self._scope,
                    auth=self._sdk_auth,
                )
                sdk_transport = streamable_http_client(self._server.url, http_client=http_client)

            async with contextlib.AsyncExitStack() as stack:
                if self._server.transport != "sse":
                    await stack.enter_async_context(http_client)
                client = await stack.enter_async_context(
                    SdkClient(
                        sdk_transport,
                        raise_exceptions=True,
                        client_info=Implementation(name=_CLIENT_NAME, version=_CLIENT_VERSION),
                        message_handler=self._sdk_message_handler,
                        cache=None,
                    )
                )
                version = client.protocol_version or ""
                self._era = "modern" if version == _MODERN_PROTOCOL_VERSION else "legacy"
                self._instructions = client.instructions
                await self._sdk_refresh_catalogue(client)
                if isinstance(self._sdk_auth, _SdkAuth):
                    self._credential = self._sdk_auth.credential
                if not ready.done():
                    ready.set_result(None)
                listener = asyncio.create_task(self._sdk_listen(client))
                watcher = asyncio.create_task(self._sdk_watch_catalogue_changes())

                while True:
                    pending = await self._sdk_commands.get()
                    if pending.kind == "close":
                        if not pending.future.done():
                            pending.future.set_result(None)
                        pending = None
                        break
                    try:
                        value: Any
                        if pending.kind == "list":
                            value = await self._sdk_refresh_catalogue(client)
                        else:
                            assert pending.name is not None
                            value = await self._sdk_call_tool(client, pending)
                        if not pending.future.done():
                            pending.future.set_result(value)
                    except BaseException as err:
                        mapped = _sdk_exception(self._server.name, err)
                        if not pending.future.done():
                            pending.future.set_exception(mapped)
                    finally:
                        pending = None
        except BaseException as err:
            mapped = _sdk_exception(self._server.name, err)
            if not ready.done():
                ready.set_exception(mapped)
            elif pending is not None and not pending.future.done():
                pending.future.set_exception(mapped)
        finally:
            if listener is not None:
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            while not self._sdk_commands.empty():
                command = self._sdk_commands.get_nowait()
                if not command.future.done():
                    command.future.set_exception(
                        McpServerUnreachable(self._server.name, "connection closed")
                    )

    async def _sdk_call_tool(self, client: SdkClient, command: _SdkCommand) -> McpToolResult:
        assert command.name is not None
        result = await client.session.call_tool(
            command.name,
            command.arguments or {},
            allow_input_required=True,
        )
        dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
        if isinstance(result, InputRequiredResult):
            raise McpInputRequiredError(self._server.name, dumped)
        return McpToolResult(
            content=_render_tool_result(dumped),
            is_error=bool(getattr(result, "is_error", False)),
        )

    async def _sdk_refresh_catalogue(self, client: SdkClient) -> tuple[ToolDefinition, ...]:
        tools: list[ToolDefinition] = []
        cursor: str | None = None
        ttl_ms: int | None = None
        cache_scope: Literal["public", "private"] | None = None
        while True:
            listed = await client.list_tools(cursor=cursor, cache_mode="refresh")
            tools.extend(
                _tool_definition_from_mcp(
                    tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                )
                for tool in listed.tools
            )
            if self._era == "modern":
                ttl_ms = listed.ttl_ms
                cache_scope = listed.cache_scope
            cursor = listed.next_cursor
            if cursor is None:
                break
        return self._install_catalogue(tuple(tools), ttl_ms, cache_scope)

    def _install_catalogue(
        self,
        tools: tuple[ToolDefinition, ...],
        ttl_ms: int | None,
        cache_scope: Literal["public", "private"] | None,
    ) -> tuple[ToolDefinition, ...]:
        etag = _catalogue_etag(tools)
        now = time.monotonic()
        ttl_override = _coerce_ttl_seconds(ttl_ms)
        if self._catalogue is not None and self._catalogue.etag == etag:
            self._catalogue = replace(
                self._catalogue,
                fetched_at=now,
                ttl_override_seconds=ttl_override,
                cache_scope=cache_scope,
            )
        else:
            self._catalogue = _Catalogue(
                tools=tools,
                etag=etag,
                fetched_at=now,
                ttl_override_seconds=ttl_override,
                cache_scope=cache_scope,
            )
        return self._catalogue.tools

    async def _sdk_listen(self, client: SdkClient) -> None:
        if self._era != "modern":
            return
        delay = 0.1
        while not self._closed:
            try:
                async with client.listen(tools_list_changed=True) as subscription:
                    delay = 0.1
                    iterator = subscription.__aiter__()
                    while True:
                        try:
                            event = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        if isinstance(event, ToolsListChanged):
                            self._catalogue_changed.set()
                self._catalogue_changed.set()
            except asyncio.CancelledError:
                raise
            except ListenNotSupportedError:
                return
            except MCPError as err:
                if err.code in {-32601, -32602}:
                    return
                self._catalogue_changed.set()
            except (SubscriptionLost, httpx2.HTTPError, TimeoutError, OSError):
                self._catalogue_changed.set()
            except Exception:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

    async def _sdk_message_handler(self, message: Any) -> None:
        if isinstance(message, ToolListChangedNotification):
            self._catalogue_changed.set()
        await asyncio.sleep(0)

    async def _sdk_watch_catalogue_changes(self) -> None:
        while True:
            await self._catalogue_changed.wait()
            self._catalogue_changed.clear()
            with contextlib.suppress(McpServerUnreachable, McpProtocolError):
                await self._refresh_catalogue()

    async def _sdk_command(
        self,
        kind: Literal["list", "call"],
        *,
        name: str | None = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._sdk_task is None or self._sdk_task.done():
            raise McpServerUnreachable(self._server.name, "connection is closed")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume_future_exception)
        await self._sdk_commands.put(
            _SdkCommand(kind, future, name=name, arguments=dict(arguments or {}))
        )
        return await asyncio.shield(future)

    # -- catalogue --------------------------------------------------------

    async def list_tools(self, *, force: bool = False) -> tuple[ToolDefinition, ...]:
        """The server's current tool catalogue, refreshing if stale.

        DESIGN.md §10.3: cached with a TTL, refreshed on connect (by
        ``connect``, before this is ever callable), on a background sweep at
        TTL, on demand (``force=True`` here), and on
        ``notifications/tools/list_changed``. A server's own ``ttlMs`` on the
        ``tools/list`` result, when present, overrides the configured
        default for the freshness check below, never the other way round.
        """
        if not force and self._catalogue is not None:
            effective_ttl = (
                self._catalogue.ttl_override_seconds
                if self._catalogue.ttl_override_seconds is not None
                else self._ttl
            )
            age = time.monotonic() - self._catalogue.fetched_at
            if age < effective_ttl:
                return self._catalogue.tools
        return await self._refresh_catalogue()

    async def _refresh_catalogue(self) -> tuple[ToolDefinition, ...]:
        async with self._catalogue_lock:
            result = await self._sdk_command("list")
            assert isinstance(result, tuple)
            return result

    async def _sweep_loop(self) -> None:
        """Background TTL refresh, so a catalogue nobody happens to poll
        still goes stale-and-recovers rather than just stale."""
        while True:
            await asyncio.sleep(self._ttl)
            if self._closed:
                return
            # Best-effort: a network hiccup here skips one sweep rather
            # than tearing down the connection. The next sweep, the next
            # on-demand call, or a list_changed notification will catch
            # up. This is refresh cadence, not the log, so there is
            # nothing to corrupt by trying again later.
            with contextlib.suppress(McpServerUnreachable, McpProtocolError):
                await self._refresh_catalogue()

    # -- calling ------------------------------------------------------------

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> McpToolResult:
        result = await self._sdk_command("call", name=name, arguments=arguments)
        assert isinstance(result, McpToolResult)
        return result

    async def call_tool_text(self, name: str, arguments: Mapping[str, Any]) -> str:
        """``call_tool``, with a failure raised and a success flattened to text.

        What the runtime calls. The model reads a tool result as text either
        way, and the object form leaked into the log: with the in-memory store
        it round-tripped as a Python repr and with a real store as a JSON
        object, so the same Spec showed the model two different things
        depending on the adapter (DESIGN.md §23 item 8).
        """
        result = await self.call_tool(name, arguments)
        if result.is_error:
            raise McpToolError(self._server.name, name, result.content)
        return result.content


def _sdk_exception(  # noqa: PLR0911 - ordered boundary translation
    server: str, error: BaseException
) -> PsychError:
    """Translate SDK, validation, and transport failures into Psych's API."""
    if isinstance(error, PsychError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            if not isinstance(nested, asyncio.CancelledError):
                return _sdk_exception(server, nested)
        return McpServerUnreachable(server, "connection was cancelled")
    if isinstance(error, MCPError):
        if error.code == -32000 and any(
            marker in error.message.lower()
            for marker in ("stream ended", "connection closed", "transport")
        ):
            return McpServerUnreachable(server, error.message)
        return _error_to_exception(
            server,
            {"code": error.code, "message": error.message, "data": error.data},
        )
    if isinstance(error, (OAuthFlowError, OAuthRegistrationError, OAuthTokenError)):
        return McpServerUnreachable(server, f"OAuth authorization failed: {error}")
    if isinstance(error, ValidationError):
        return McpProtocolError(f"MCP server {server!r} returned an invalid response: {error}")
    if isinstance(error, (httpx.HTTPError, httpx2.HTTPError, TimeoutError, OSError)):
        return McpServerUnreachable(server, str(error))
    return McpProtocolError(f"MCP server {server!r} failed protocol handling: {error}")


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Observe a queued result when its original waiter was cancelled."""
    if not future.cancelled():
        future.exception()


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _map_annotations(raw: Mapping[str, Any] | None) -> frozenset[str]:
    """MCP's ``readOnlyHint``/``destructiveHint`` object to Psych's annotation
    set. An unannotated tool is ``write`` (DESIGN.md §10.9): the safer
    default, since an approval selector that only catches annotated
    destructive tools would silently wave through everything a server never
    bothered to annotate."""
    if not raw:
        return frozenset({"write"})
    if raw.get("readOnlyHint") is True:
        return frozenset({"read-only"})
    # MCP's own default for destructiveHint on a present annotations object is
    # true, so a server that annotates a tool at all ("title": "Delete user")
    # without saying otherwise is describing a destructive tool. Reading a
    # missing hint as merely `write` meant a consumer who narrowed their
    # selectors to @destructive -- the natural choice, to avoid approving every
    # write -- waved through exactly the calls the annotation exists to catch.
    if raw.get("destructiveHint", True) is not False:
        return frozenset({"write", "destructive"})
    return frozenset({"write"})


def _tool_definition_from_mcp(raw: Mapping[str, Any]) -> ToolDefinition:
    return ToolDefinition(
        name=raw["name"],
        description=raw.get("description") or "",
        input_schema=raw.get("inputSchema") or {},
        annotations=_map_annotations(raw.get("annotations")),
    )


def _flatten_content(items: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(f"[unsupported MCP content type: {item.get('type')!r}]")
    return "\n".join(parts)


def _render_tool_result(result: Mapping[str, Any]) -> str:
    content = _flatten_content(result.get("content") or [])
    structured = result.get("structuredContent")
    if structured is None:
        return content
    structured_text = json.dumps(structured, sort_keys=True, separators=(",", ":"))
    if not content:
        return structured_text
    if content.strip() == structured_text:
        return content
    return f"{content}\n\nStructured content:\n{structured_text}"


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


class McpPool:
    """Pools ``McpConnection``s by ``McpPoolKey``.

    One pool per process is the intended shape: every Run, for every tenant,
    shares it, and isolation comes entirely from the key, not from having a
    separate pool per tenant (which would just move the one-line mistake
    DESIGN.md §10.4 warns about to whoever wires up per-tenant pools).

    ``oauth`` is optional. Absent, a server behind OAuth still fails with
    ``McpServerUnreachable`` on its first 401, the same as it always has,
    except the message now says the server wants OAuth rather than just
    naming the status code. Present, this pool's connections acquire,
    refresh, and step up tokens against it automatically; see this module's
    docstring for the full flow and how the pool key follows an acquired
    token.

    ``oauth_identity`` and ``oauth_grant`` are the *default* client identity
    and grant kind, used only for a server whose Spec sets no
    ``McpServer.oauth``. A server that does declare one is
    connected with that identity and grant instead, so one pool serves any
    number of servers with different OAuth client identities and different
    grant kinds in the same Run -- see this module's docstring, "One pool,
    many OAuth identities and grants".
    """

    def __init__(
        self,
        *,
        transport: McpTransport,
        secrets: SecretResolver,
        catalogue_ttl_seconds: float = DEFAULT_CATALOGUE_TTL_SECONDS,
        oauth: OAuthClient | None = None,
        oauth_identity: ClientIdentityConfig | None = None,
        oauth_grant: GrantKind = _DEFAULT_OAUTH_GRANT,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self._ttl = catalogue_ttl_seconds
        self._oauth = oauth
        self._default_oauth_identity = (
            oauth_identity if oauth_identity is not None else ClientIdentityConfig()
        )
        self._default_oauth_grant = oauth_grant
        self._connections: dict[McpPoolKey, McpConnection] = {}
        self._redirects: dict[McpPoolKey, McpPoolKey] = {}
        """Pre-auth key -> post-auth key, for a connection whose credential
        identity changed during ``connect()`` (an OAuth token acquired where
        none was resolved beforehand). See this module's docstring, "The
        pool key moves when a token is acquired"."""
        self._locks: dict[McpPoolKey, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get_or_connect(self, scope: Scope, server: McpServer) -> McpConnection:
        """Return the pooled connection for ``(scope, server, credential)``,
        connecting one if none exists yet.

        The lock this method takes is always on the *pre-auth* key: the one
        computable before ``connect()`` runs, from whatever
        ``SecretResolver`` resolves (or ``None``, for a server with no
        static credential). If ``connect()`` then acquires an OAuth token,
        the connection's credential identity, and so its true pool key,
        changes; this method notices, stores the connection under that new
        key instead, and records the redirect so a second caller for the
        same pre-auth key finds the same connection rather than repeating
        the OAuth exchange. See this module's docstring for why the
        pre-auth key must never be the one a connection actually lives
        under.

        Raises:
            CredentialNotFound: ``server.credential`` names a credential this
                Scope has no value for.
            McpServerUnreachable: no connection exists yet and the server
                could not be reached, including a server that requires OAuth
                this pool has no ``OAuthClient`` configured to satisfy.
            McpProtocolError: no connection exists yet and the server's
                handshake was invalid.
        """
        credential = await self._resolve_credential(scope, server)
        oauth_identity, oauth_grant = await self._oauth_config_for(scope, server)
        pre_auth_key = McpPoolKey.from_scope(
            scope=scope,
            server=server,
            credential_identity=credential.identity if credential is not None else None,
        )
        lock = await self._lock_for(pre_auth_key)
        async with lock:
            lookup_key = self._redirects.get(pre_auth_key, pre_auth_key)
            existing = self._connections.get(lookup_key)
            if existing is not None:
                return existing
            connection = McpConnection(
                scope=scope,
                server=server,
                transport=self._transport,
                credential=credential,
                catalogue_ttl_seconds=self._ttl,
                oauth=self._oauth,
                oauth_identity=oauth_identity,
                oauth_grant=oauth_grant,
            )
            await connection.connect()
            post_auth_key = replace(
                pre_auth_key, credential_identity=connection.credential_identity
            )
            self._connections[post_auth_key] = connection
            if post_auth_key != pre_auth_key:
                self._redirects[pre_auth_key] = post_auth_key
            return connection

    async def _resolve_credential(
        self, scope: Scope, server: McpServer
    ) -> ResolvedCredential | None:
        if server.credential is None:
            return None
        credential = await self._secrets.resolve(scope, server.credential)
        if credential is None:
            raise CredentialNotFound(scope, server.credential)
        return credential

    async def _oauth_config_for(
        self, scope: Scope, server: McpServer
    ) -> tuple[ClientIdentityConfig, GrantKind]:
        """The OAuth client identity and grant kind to connect ``server``
        with: the server's own ``McpServer.oauth``, translated into
        the runtime ``ClientIdentityConfig`` shape ``OAuthClient`` expects, or
        this pool's default when the Spec sets none.

        The one thing a Spec's ``McpOAuth`` cannot carry is the confidential
        client secret itself (see that model's docstring): when
        ``client_secret_credential`` names one, this is where that name
        becomes a value, resolved through the same ``SecretResolver`` port
        ``_resolve_credential`` already uses right beside this call, and
        raising the same ``CredentialNotFound`` a missing static credential
        would.
        """
        if server.oauth is None:
            return self._default_oauth_identity, self._default_oauth_grant
        client_secret: SecretStr | None = None
        if server.oauth.client_secret_credential is not None:
            resolved = await self._secrets.resolve(scope, server.oauth.client_secret_credential)
            if resolved is None:
                raise CredentialNotFound(scope, server.oauth.client_secret_credential)
            client_secret = resolved.secret
        identity = ClientIdentityConfig(
            preregistered_client_id=server.oauth.preregistered_client_id,
            preregistered_client_secret=client_secret,
            cimd_url=server.oauth.cimd_url,
            allow_dynamic_registration=server.oauth.allow_dynamic_registration,
            application_type=server.oauth.application_type,
            client_name=server.oauth.client_name,
            redirect_uris=server.oauth.redirect_uris,
        )
        return identity, server.oauth.grant

    async def _lock_for(self, key: McpPoolKey) -> asyncio.Lock:
        # A lock per key, not one global lock, so connecting server A for
        # tenant X does not block connecting server B for tenant Y. The
        # guard lock is held only long enough to look up or create the
        # per-key lock, never across a connect.
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def peek(self, scope: Scope, server: McpServer) -> McpConnection | None:
        """A pooled connection for this Scope and server, without connecting.

        For reading something about a server that is true whoever asked: its
        protocol era, its catalogue, what it says it is for. Returns ``None``
        when nothing is pooled yet, which is an answer rather than a reason to
        open a connection. A caller that needs a connection calls
        ``get_or_connect``; this one is for a caller that would rather have
        nothing than a network round trip.

        Matched on tenant, principal, URL and transport, and deliberately
        **not** on credential identity. Two connections differing only by
        credential both belong to the same Scope, and the things this is used
        to read are properties of the server rather than of whoever
        authenticated. That reasoning does not extend to *calling* a tool,
        which is why ``McpTools.call`` resolves through ``get_or_connect`` and
        its full key instead (DESIGN.md §10.4).
        """
        tenant, principal = scope.pool_key
        for key, connection in self._connections.items():
            if (
                key.tenant == tenant
                and key.principal == principal
                and key.server_url == server.url
                and key.transport == server.transport
            ):
                return connection
        return None

    async def evict(self, key: McpPoolKey) -> None:
        """Close and forget one pooled connection, e.g. after a credential
        rotation the consumer wants to take effect immediately rather than
        waiting for the connection to next fail.

        ``key`` may be either a pre-auth or a post-auth key: any redirect
        naming or pointing at it is dropped too, so a future caller reruns
        the OAuth exchange instead of redirecting to a connection this call
        just closed.
        """
        connection = self._connections.pop(key, None)
        if connection is not None:
            await connection.close()
        for pre_auth_key, post_auth_key in list(self._redirects.items()):
            if key in (pre_auth_key, post_auth_key):
                self._redirects.pop(pre_auth_key, None)

    def connections(self) -> tuple[McpConnectionStatus, ...]:
        """Every pooled connection's status, tenant by tenant.

        Read-only and synchronous: nothing here connects, refreshes or blocks.
        A consumer's operations page shows this; a consumer's *user* should not
        see another tenant's row, and this returns every tenant's, so filter by
        ``tenant`` before rendering.
        """
        return tuple(connection.status() for connection in self._connections.values())

    async def evict_server(self, scope: Scope, server: McpServer) -> None:
        """Forget everything pooled for ``(scope, server)``: the connection and
        the stored authorization.

        ``evict`` already does this given a key, but a caller cannot build one.
        The key depends on the *resolved* credential identity, which only
        ``get_or_connect`` knows, and for an OAuth server it changes again once
        a token is acquired. This resolves the pre-auth key the same way,
        follows the redirect to whatever the connection actually lives under,
        and drops both.

        The authorization is forgotten too, and that is the half worth being
        deliberate about: closing the connection alone would reconnect on the
        same cached token, which is not what "connect again" means to somebody
        whose token is the thing that is wrong -- a scope granted since, a
        credential rotated at the provider. Forgetting the session makes the
        next connect run the exchange again.

        A server that was never connected is a no-op rather than an error, so
        a caller can always ask.
        """
        credential = await self._resolve_credential(scope, server)
        pre_auth_key = McpPoolKey.from_scope(
            scope=scope,
            server=server,
            credential_identity=credential.identity if credential is not None else None,
        )
        live_key = self._redirects.get(pre_auth_key, pre_auth_key)
        await self.evict(live_key)
        # `evict` is given only the key the connection lived under; a stale
        # pre-auth entry pointing at it has to go too.
        self._connections.pop(pre_auth_key, None)
        self._redirects.pop(pre_auth_key, None)
        if self._oauth is not None:
            self._oauth.evict(scope, resource=server.url)

    async def probe(self, scope: Scope, server: McpServer, *, force: bool = False) -> McpProbe:
        """Connect to ``server`` for real and report what it offers.

        The same path a Run takes -- credential resolution, OAuth, protocol
        negotiation, the pool key -- so a probe that passes means a Run for
        this same Scope will connect, and the connection it establishes is the
        one that Run will use. A consumer's own version of this could only
        call ``tools_for`` and catch ``Exception``, which loses the error's
        type and, if it probes under a different Scope than the Run will use,
        proves nothing about the Run.

        Args:
            scope: the Scope a Run against this server would carry.
            server: the server to connect to.
            force: drop the pooled connection and the stored authorization
                first, so this establishes a new connection on a new token
                instead of reporting the one already open. Off by default
                because the ordinary question is "what would a Run get", and
                tearing down a working connection to answer it would make
                asking expensive -- and would re-run the OAuth exchange, which
                for an ``authorization_code`` grant means a person signing in
                again.
        """
        if force:
            await self.evict_server(scope, server)
        try:
            connection = await self.get_or_connect(scope, server)
            tools = await connection.list_tools()
        except (CredentialNotFound, McpServerUnreachable, McpProtocolError) as err:
            return McpProbe(
                ok=False,
                server=server.name,
                url=server.url,
                detail=str(err),
                error_type=type(err).__name__,
            )
        return McpProbe(
            ok=True,
            server=server.name,
            url=server.url,
            tools=tuple(tools),
            detail=f"connected; {len(tools)} tool{'' if len(tools) == 1 else 's'} offered",
            era=connection.era,
        )

    async def close_all(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        self._redirects.clear()
        for connection in connections:
            await connection.close()


# ---------------------------------------------------------------------------
# Per-turn resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpServerResolution:
    """One server's contribution to one turn's tool set.

    Attributes:
        server: the Spec-local ``McpServer.name``.
        reachable: whether the server answered. False only for an
            ``optional`` server; a required one that cannot be reached raises
            instead of producing one of these (DESIGN.md §10.7).
        tools: the narrowed tool set this Spec may see from this server right
            now. Empty when unreachable.
        unavailable_reason: why ``reachable`` is False, for the model or the
            log to say something better than silence.
    """

    server: str
    reachable: bool
    tools: tuple[ToolDefinition, ...]
    unavailable_reason: str | None = None


async def resolve_mcp_server(pool: McpPool, scope: Scope, server: McpServer) -> McpServerResolution:
    """Connect to one MCP server and narrow its catalogue for this Spec.

    Fresh on every call, by design (DESIGN.md §10.2): the connection and its
    catalogue are what ``McpPool``/``McpConnection`` cache, never which tools
    this particular Spec is allowed to see, so a Spec's ``allow`` list is
    re-applied against the live catalogue every time this is called rather
    than pinned at the connection's first use.

    Only the Spec-grants plane of DESIGN.md §10.5's narrowing chain is applied
    here (``server.allow``). The tenant-permits plane depends on the
    consumer's ``Policy`` port, which is the per-turn ``ToolResolver``'s job,
    not this module's.

    Raises:
        McpServerUnreachable: ``server.optional`` is False and the server
            could not be reached.
        CredentialNotFound: ``server.credential`` names a credential this
            Scope has no value for, regardless of ``optional``: a missing
            credential is a configuration problem, not a transient outage.
    """
    try:
        connection = await pool.get_or_connect(scope, server)
    except McpServerUnreachable as err:
        if server.optional:
            return McpServerResolution(
                server=server.name, reachable=False, tools=(), unavailable_reason=str(err)
            )
        raise
    offered = await connection.list_tools()
    selected = set(narrow([tool.name for tool in offered], spec_grants=server.allow))
    tools = tuple(tool for tool in offered if tool.name in selected)
    return McpServerResolution(server=server.name, reachable=True, tools=tools)


async def resolve_mcp_servers(
    pool: McpPool, scope: Scope, servers: Sequence[McpServer]
) -> tuple[McpServerResolution, ...]:
    """``resolve_mcp_server`` for every server a Spec declares, in order."""
    return tuple([await resolve_mcp_server(pool, scope, server) for server in servers])


class McpTools:
    """The seam between a pool of MCP connections and a running agent.

    Two halves, because the resolver and the executor need different things
    and both must narrow the same way (DESIGN.md §10.5):

    - ``tools_for`` satisfies ``psych_runtime.tools.resolver.McpCatalog``, so a turn's
      tool set includes what each granted server currently offers.
    - ``call`` runs one of those tools.

    They live on one object rather than being wired separately because they
    share the inputs that decide access: the pool, which carries tenancy in its
    key, and the tenant policy. Wiring them from two places is how the
    resolver's view and the executor's view drift apart, and a drift in this
    direction is a model calling a tool the Spec excluded.

    ## Why ``call`` narrows again

    The resolver already filtered the tool set the model was shown. That is not
    a control, because the model chooses the name it sends: a caller that
    looked a name up across every connected server and invoked whatever
    answered would let a model reach a tool the Spec's ``allow`` list excluded
    simply by naming it. So this applies the same ``narrow`` the resolver
    applies, against the same three planes, and a name that does not survive it
    is refused however the model got hold of it.

    Tenancy rides on ``scope``: a name is resolved only against connections
    keyed to the calling Scope, so tenant A's Run cannot reach tenant B's
    server even if both grant a tool of the same name.
    """

    def __init__(
        self,
        pool: McpPool,
        *,
        tenant_policy: Any = None,
        describe_server: Callable[[Scope, McpServer], str | None] | None = None,
    ) -> None:
        """
        Args:
            pool: the connection pool. Keyed by ``(tenant, principal, server,
                transport, credential identity)``, which is what makes the
                lookups below tenant-safe rather than merely tenant-shaped.
            tenant_policy: the middle narrowing plane, matching
                ``psych_runtime.tools.resolver.TenantToolPolicy``. Structural rather
                than imported so this module does not depend on the resolver it
                is consumed by. ``None`` means the tenant permits everything the
                server offers, which is that protocol's own empty-means-all rule.
            describe_server: what this consumer says each server is for, looked
                up per ``(Scope, server)``. Wins over the server's own
                ``instructions``, because whoever wired the connection knows
                their deployment and the server's author does not.

                A callable rather than a mapping so a description edited in a
                console takes effect on the next turn without rebuilding this
                object. Called at every turn boundary, so it must be a cheap
                in-memory lookup: it is deliberately not ``async``, which is
                this signature's way of saying "do not put IO here".

                It takes the ``Scope`` because a description is per connection
                and a connection is per tenant. Two tenants may configure the
                same URL and mean different things by it, so a hook keyed on the
                server alone would put one tenant's words in the other's prompt.
                Every other consumer hook here is scope-keyed for the same
                reason (``SecretResolver.resolve``, ``tenant_policy``, the
                egress seam); this one was not, which made per-tenant
                descriptions impossible rather than merely awkward.
        """
        self._pool = pool
        self._tenant_policy = tenant_policy
        self._describe_server = describe_server

    async def describe(self, scope: Scope, server: McpServer) -> str | None:
        """What ``server`` is for, in one or two sentences, or ``None``.

        The consumer's own description first, the server's own ``instructions``
        second, nothing third. Never raises and never connects: a description
        is decoration on a prompt, and failing a turn over one would be absurd.
        A server that has not been reached yet simply has no ``instructions``
        to offer, and the consumer's description does not need a connection.

        Deliberately **not** a field on ``McpServer``. That model is part of
        ``AgentSpec``, so a description there would join the Version hash, and
        editing what a server is for would republish every agent that names it.
        A connection's description changes for reasons that have nothing to do
        with the agent; the agent did not change and its Version must not.
        """
        if self._describe_server is not None:
            described = self._describe_server(scope, server)
            if described and described.strip():
                return described
        connection = self._pool.peek(scope, server)
        return connection.instructions if connection is not None else None

    async def tools_for(self, scope: Scope, server: McpServer) -> Sequence[ToolDefinition]:
        """Every tool ``server`` offers ``scope`` right now.

        Raises rather than returning an empty list when the server cannot be
        reached, because the resolver distinguishes the two: an empty list is a
        server that answered and offers nothing, and DESIGN.md §10.7 wants a
        Run to fail on a missing *required* server rather than quietly proceed
        without its tools.
        """
        connection = await self._pool.get_or_connect(scope, server)
        return await connection.list_tools()

    async def call(
        self, spec: AgentSpec, scope: Scope, name: str, arguments: dict[str, Any]
    ) -> Any:
        """Run ``name`` on the one granted server identified by the resolved name.

        Raises:
            AccessDenied: no granted server offers a tool of that name that
                survives narrowing.
            Exception: the server offering it could not be reached. Propagated
                rather than folded into ``AccessDenied`` so the model is told
                the server is down, not that the tool does not exist. Those
                call for different next moves, and the second is a lie.
        """
        unreachable: Exception | None = None
        raw_matches: list[tuple[str, McpConnection, str]] = []
        qualified_matches: list[tuple[str, McpConnection, str]] = []

        for server in spec.mcp_servers:
            try:
                connection = await self._pool.get_or_connect(scope, server)
                offered = await connection.list_tools()
            except Exception as err:
                # Keep looking: another granted server may offer this name and
                # be perfectly healthy. Remember the failure in case none does.
                if unreachable is None:
                    unreachable = err
                continue

            callable_here = narrow(
                [definition.name for definition in offered],
                await self._tenant_allows(scope, server.name),
                list(server.allow),
            )
            for raw_name in callable_here:
                if name == raw_name:
                    raw_matches.append((server.name, connection, raw_name))
                if name == mcp_tool_name(server.name, raw_name):
                    qualified_matches.append((server.name, connection, raw_name))

        if len(qualified_matches) == 1:
            _, connection, raw_name = qualified_matches[0]
            return await connection.call_tool_text(raw_name, arguments)
        if len(qualified_matches) > 1:
            raise AccessDenied(
                f"tool {name!r}",
                "its qualified name matches more than one MCP server; rename one server alias",
            )
        if len(raw_matches) == 1:
            _, connection, raw_name = raw_matches[0]
            return await connection.call_tool_text(raw_name, arguments)
        if len(raw_matches) > 1:
            choices = ", ".join(
                mcp_tool_name(server_name, raw_name) for server_name, _, raw_name in raw_matches
            )
            raise AccessDenied(
                f"tool {name!r}",
                f"more than one MCP server offers it; call a server-qualified name ({choices})",
            )

        if unreachable is not None:
            raise unreachable
        raise AccessDenied(
            f"tool {name!r}",
            "no MCP server granted to this Run offers a callable tool of that name",
        )

    async def list_for(
        self, spec: AgentSpec, scope: Scope, server_name: str
    ) -> list[ToolDefinition]:
        """Every tool ``server_name`` offers this Run that it may actually call.

        What ``list_tools`` answers for a deferred server. Narrowed, not raw:
        discovery is not access, and showing a model a tool the Spec excluded
        only teaches it to ask for something that will be refused.

        Raises:
            AccessDenied: this Spec grants no server of that name. The model
                chose the name, so this is the same check ``call`` makes.
        """
        server = next((s for s in spec.mcp_servers if s.name == server_name), None)
        if server is None:
            raise AccessDenied(
                f"server {server_name!r}",
                "this run is not connected to a server of that name. Connected: "
                + (", ".join(repr(s.name) for s in spec.mcp_servers) or "none"),
            )
        connection = await self._pool.get_or_connect(scope, server)
        offered = await connection.list_tools()
        callable_here = set(
            narrow(
                [definition.name for definition in offered],
                await self._tenant_allows(scope, server.name),
                list(server.allow),
            )
        )
        return [definition for definition in offered if definition.name in callable_here]

    async def _tenant_allows(self, scope: Scope, server: str) -> list[str]:
        if self._tenant_policy is None:
            return []
        permitted = await self._tenant_policy.permitted_tools(scope, server)
        return list(permitted)

    def connections(self) -> tuple[McpConnectionStatus, ...]:
        """Every pooled connection's status. See ``McpPool.connections``."""
        return self._pool.connections()

    async def probe(self, scope: Scope, server: McpServer, *, force: bool = False) -> McpProbe:
        """Connect to one server and report what it offers. See
        ``McpPool.probe``."""
        return await self._pool.probe(scope, server, force=force)

    async def disconnect(self, scope: Scope, server: McpServer) -> None:
        """Close this Scope's connection to one server and forget its stored
        authorization. See ``McpPool.evict_server``."""
        await self._pool.evict_server(scope, server)

    def caller(self, spec: AgentSpec, scope: Scope) -> Callable[..., Awaitable[Any]]:
        """``call`` bound to one Run, in the shape ``ToolExecutor`` expects.

        The executor's ``mcp_caller`` takes only a name and arguments, so the
        Spec and Scope have to be closed over here. That is the right place for
        them: both are fixed for the life of a Run, and binding them per Run is
        what stops one Run's executor being usable to reach another's servers.
        """

        async def call(name: str, arguments: dict[str, Any]) -> Any:
            return await self.call(spec, scope, name, arguments)

        return call
