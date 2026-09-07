"""The A2A protocol data model, ported from the proto that defines it.

## Where these definitions come from

A2A specification v1.0 §1.4 is explicit: ``spec/a2a.proto`` in the
`a2aproject/A2A <https://github.com/a2aproject/A2A>`_ repository is "the single
authoritative normative definition of all protocol data objects and
request/response messages", and the generated ``spec/a2a.json`` is "a
non-normative build artifact". So every model in this file was written from
the proto (which is actually at ``specification/a2a.proto`` in the repository,
not at the ``spec/`` path §1.4 names -- the prose is one directory out of
date), and the prose specification was read only for the binding rules the
proto cannot express.

Two facts in this package were resolved against the proto and the binding
sections rather than against summaries of them, because the summaries
disagreed:

- **JSON-RPC method names are bare PascalCase**: ``"SendMessage"``,
  ``"GetTask"``, and so on. Not ``"a2a.SendMessage"``, and not the pre-1.0
  ``"message/send"``. §9.1 says "PascalCase method names matching gRPC
  conventions", §9.4.1's example request carries ``"method": "SendMessage"``,
  and §5.3's mapping table lists the same eleven strings for JSON-RPC as for
  gRPC. They are in ``psych_runtime.a2a.jsonrpc``.
- **The Agent Card well-known path is ``/.well-known/agent-card.json``**, not
  ``/.well-known/a2a-agent-card.json``. §8.2 and the IANA well-known URI
  registration template in §14 both give the former, and it is what this
  module exports as ``AGENT_CARD_WELL_KNOWN_PATH``.

## Why these models are not the proto's field names

§5.5 requires camelCase in every JSON serialisation of the data model, while
the proto declares snake_case. Rather than write both spellings out by hand,
every model here declares Python-natural snake_case fields and generates the
camelCase alias, so ``model_dump(by_alias=True)`` is wire-shaped and
``Model(context_id=...)`` is Python-shaped. Enum values are the proto's own
SCREAMING_SNAKE_CASE names, which is what ProtoJSON requires (§5.5) and is why
``TaskState.WORKING`` carries the value ``"TASK_STATE_WORKING"``.

## Why ``extra="ignore"``, against this repository's habit

Everything else in Psych sets ``extra="forbid"``: a Record or a Spec with an
unexpected key is a writer bug and should fail loudly. These models are the
opposite case. They parse what another organisation's agent sent, and §5.7
requires implementations to "ignore unrecognized fields ... allowing for
forward compatibility as the protocol evolves". Refusing a peer's 1.1 field
would turn a forward-compatible protocol into a brittle one, so the wire
models ignore what they do not know. What Psych itself *writes* is still
exactly these fields.

## The oneofs

``Part``, ``SecurityScheme``, ``OAuthFlows``, ``SendMessageResponse`` and
``StreamResponse`` are proto ``oneof``s. ProtoJSON renders a oneof as exactly
one present key, so each is modelled as a group of optional fields with a
validator that counts how many were *explicitly set* rather than how many are
non-``None``. Counting non-``None`` would make ``Part(text=None)`` and
``Part()`` indistinguishable from a ``Part`` carrying an explicit JSON
``null`` in ``data``, which is a legal value of ``google.protobuf.Value``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

__all__ = [
    "A2A_MEDIA_TYPE",
    "AGENT_CARD_WELL_KNOWN_PATH",
    "DEFAULT_PAGE_SIZE",
    "INTERRUPTED_STATES",
    "MAX_PAGE_SIZE",
    "TERMINAL_STATES",
    "APIKeySecurityScheme",
    "AgentCapabilities",
    "AgentCard",
    "AgentCardSignature",
    "AgentExtension",
    "AgentInterface",
    "AgentProvider",
    "AgentSkill",
    "Artifact",
    "AuthenticationInfo",
    "AuthorizationCodeOAuthFlow",
    "CancelTaskRequest",
    "ClientCredentialsOAuthFlow",
    "DeleteTaskPushNotificationConfigRequest",
    "DeviceCodeOAuthFlow",
    "GetExtendedAgentCardRequest",
    "GetTaskPushNotificationConfigRequest",
    "GetTaskRequest",
    "HTTPAuthSecurityScheme",
    "ListTaskPushNotificationConfigsRequest",
    "ListTaskPushNotificationConfigsResponse",
    "ListTasksRequest",
    "ListTasksResponse",
    "Message",
    "MutualTlsSecurityScheme",
    "OAuth2SecurityScheme",
    "OAuthFlows",
    "OpenIdConnectSecurityScheme",
    "Part",
    "ProtocolBinding",
    "Role",
    "SecurityRequirement",
    "SecurityScheme",
    "SendMessageConfiguration",
    "SendMessageRequest",
    "SendMessageResponse",
    "StreamResponse",
    "StringList",
    "SubscribeToTaskRequest",
    "Task",
    "TaskArtifactUpdateEvent",
    "TaskPushNotificationConfig",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "Timestamp",
    "wire_dict",
]

AGENT_CARD_WELL_KNOWN_PATH: Final = "/.well-known/agent-card.json"
"""§8.2 and the IANA well-known URI registration in §14.

Resolved from the specification itself rather than from a summary: the
alternative spelling ``/.well-known/a2a-agent-card.json`` appears nowhere in
the document.
"""

A2A_MEDIA_TYPE: Final = "application/a2a+json"
"""The REST binding's content type (§11.4). The JSON-RPC binding uses plain
``application/json`` instead (§9.1), which is why this is not one constant for
both."""


def _iso8601_z(value: datetime) -> str:
    """§5.6.1: UTC, ``Z`` suffix, millisecond precision, never an offset.

    ``datetime.isoformat()`` writes ``+00:00`` for UTC and microseconds for
    sub-second precision, and §5.6.1 forbids the first ("Timestamps MUST NOT
    include timezone offsets other than 'Z'") and asks for the second
    ("Millisecond precision SHOULD be used"). A naive datetime is read as UTC
    rather than rejected: it reached here from a caller who already decided
    this was a protocol timestamp, and guessing local time would be worse than
    assuming the timezone the protocol mandates.
    """
    moment = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


Timestamp = Annotated[datetime, PlainSerializer(_iso8601_z, return_type=str, when_used="json")]
"""A ``google.protobuf.Timestamp`` field, serialised per §5.6.1."""


class _A2AModel(BaseModel):
    """Wire shape for every protocol object: camelCase out, either spelling in.

    ``frozen`` for the same reason ``Scope`` is frozen: these objects cross a
    trust boundary, and one that can be mutated after it is validated can be
    mutated between the check and the use.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        alias_generator=to_camel,
    )


def wire_dict(model: BaseModel) -> dict[str, JsonValue]:
    """Serialise a protocol object with §5.7's field-presence rules.

    Neither of pydantic's two obvious settings is right on its own.
    ``exclude_none=True`` drops a field that was *explicitly* set to null,
    which §5.7 says must be kept and §8.4.1 says must be kept for a signature
    to reproduce. ``exclude_unset=True`` drops a required field left at its
    default, which §8.4.1 says must be present. So the rule applied here is
    the specification's own, stated once:

    - a field that was never set and is absent-shaped (``None``, or an empty
      repeated/map field, which is a proto default) is omitted;
    - everything else is emitted, including an explicit ``null`` and an
      explicitly empty list.

    The result is used for every JSON body Psych writes *and* as the input to
    Agent Card canonicalisation, deliberately: a card that serialised one way
    for the wire and another way for signing would produce signatures that
    verify against a document nobody ever receives.
    """
    result: dict[str, JsonValue] = {}
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if name not in model.model_fields_set and _is_absent(value):
            continue
        result[field.alias or to_camel(name)] = _wire_value(value)
    return result


def _is_absent(value: object) -> bool:
    """Proto default: unset scalar, or an empty repeated/map field."""
    return value is None or (isinstance(value, tuple | list | dict) and not value)


def _wire_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return wire_dict(value)
    if isinstance(value, tuple | list):
        return [_wire_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _wire_value(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return _iso8601_z(value)
    if isinstance(value, StrEnum):
        return str(value.value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    raise TypeError(f"{type(value).__name__} has no A2A JSON representation")


def _exactly_one_set(model: BaseModel, group: tuple[str, ...], what: str) -> None:
    """Enforce a proto ``oneof``: exactly one of ``group`` explicitly present.

    Explicit presence, not non-``None``: a ``google.protobuf.Value`` may
    legitimately hold JSON ``null``, so a ``Part`` whose ``data`` was
    explicitly set to ``None`` carries content while a ``Part`` with nothing
    set does not.
    """
    present = [name for name in group if name in model.model_fields_set]
    if len(present) != 1:
        raise ValueError(
            f"{what} is a oneof: exactly one of {', '.join(group)} must be set, "
            f"got {present or 'none'}"
        )


# ---------------------------------------------------------------------------
# Core objects (§4.1)
# ---------------------------------------------------------------------------


class Role(StrEnum):
    """Who sent a ``Message`` (proto ``Role``, §4.1.5)."""

    UNSPECIFIED = "ROLE_UNSPECIFIED"
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"


class TaskState(StrEnum):
    """A Task's lifecycle (proto ``TaskState``, §4.1.3).

    Nine values including ``UNSPECIFIED``. The proto's own comments split them
    three ways, and the split is load-bearing for ``SubscribeToTask``, which
    must refuse a terminal task (§3.1.6): terminal is ``COMPLETED``,
    ``FAILED``, ``CANCELED``, ``REJECTED``; interrupted is ``INPUT_REQUIRED``
    and ``AUTH_REQUIRED``; the rest are live. See ``TERMINAL_STATES`` and
    ``INTERRUPTED_STATES``, which are the sets the rest of this package tests
    against so that no caller re-lists them and gets one wrong.
    """

    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


TERMINAL_STATES: Final = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
)
"""The four states the proto calls terminal. A task in one of these refuses
``SubscribeToTask`` (§3.1.6) and refuses ``CancelTask`` (§5.4's
``TaskNotCancelableError``)."""

INTERRUPTED_STATES: Final = frozenset({TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED})
"""Not terminal, but not progressing either: the task is waiting on the
client. A ``SendMessage`` with ``return_immediately=false`` returns when a task
reaches one of these as well as when it reaches a terminal state (§3.2.2)."""


class Part(_A2AModel):
    """One section of content (proto ``Part``, §4.1.6).

    ``raw`` is ``bytes`` in the proto and a base64 string in JSON, which is
    what ProtoJSON does with a ``bytes`` field; it is typed ``str`` here
    because this model is only ever the JSON form.
    """

    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: JsonValue | None = None
    metadata: dict[str, JsonValue] | None = None
    filename: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def _one_content(self) -> Self:
        _exactly_one_set(self, ("text", "raw", "url", "data"), "Part.content")
        return self

    @classmethod
    def from_text(cls, text: str, *, media_type: str | None = None) -> Part:
        """The common case, without the caller having to know it is a oneof.

        ``media_type`` is passed only when given rather than always: §5.7 makes
        an explicitly-set ``null`` different from an omitted field, and a
        helper that set every optional field would put ``"mediaType": null``
        into every part Psych writes.
        """
        if media_type is None:
            return cls(text=text)
        return cls(text=text, media_type=media_type)


class Message(_A2AModel):
    """One unit of communication (proto ``Message``, §4.1.4)."""

    message_id: str = Field(min_length=1)
    context_id: str | None = None
    task_id: str | None = None
    role: Role
    parts: tuple[Part, ...] = Field(min_length=1)
    metadata: dict[str, JsonValue] | None = None
    extensions: tuple[str, ...] = ()
    reference_task_ids: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """Every text part joined, which is what a text-only agent reads.

        Non-text parts are skipped rather than rendered: a caller that cares
        about a file part should read ``parts`` and decide for itself, and
        interpolating a placeholder here would put the word "image" into a
        model's prompt as though the sender had typed it.
        """
        return "\n".join(part.text for part in self.parts if part.text is not None)


class Artifact(_A2AModel):
    """A task output (proto ``Artifact``, §4.1.7).

    §3.7 draws the line this type sits on: Messages are communication and
    Artifacts are output, and "Messages SHOULD NOT be used to deliver task
    outputs". Psych's answer projection is therefore an Artifact and never a
    Message -- see ``psych_runtime.a2a.mapping``.
    """

    artifact_id: str = Field(min_length=1)
    name: str | None = None
    description: str | None = None
    parts: tuple[Part, ...] = Field(min_length=1)
    metadata: dict[str, JsonValue] | None = None
    extensions: tuple[str, ...] = ()


class TaskStatus(_A2AModel):
    """A state, plus the message and the moment that go with it (§4.1.2)."""

    state: TaskState
    message: Message | None = None
    timestamp: Timestamp | None = None


class Task(_A2AModel):
    """The core unit of work (proto ``Task``, §4.1.1)."""

    id: str = Field(min_length=1)
    context_id: str | None = None
    status: TaskStatus
    artifacts: tuple[Artifact, ...] = ()
    history: tuple[Message, ...] = ()
    metadata: dict[str, JsonValue] | None = None


# ---------------------------------------------------------------------------
# Streaming events (§4.2)
# ---------------------------------------------------------------------------


class TaskStatusUpdateEvent(_A2AModel):
    """A task changed state (§4.2.1)."""

    task_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    status: TaskStatus
    metadata: dict[str, JsonValue] | None = None


class TaskArtifactUpdateEvent(_A2AModel):
    """A task produced or extended an artifact (§4.2.2)."""

    task_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    artifact: Artifact
    append: bool = False
    last_chunk: bool = False
    metadata: dict[str, JsonValue] | None = None


class StreamResponse(_A2AModel):
    """One frame of a stream, or one push-notification body (§3.2.3, §4.3.3).

    The same wrapper serves SSE and webhooks, which is why the push sender and
    the SSE endpoint in the playground both build one of these rather than two
    similar shapes that would drift.
    """

    task: Task | None = None
    message: Message | None = None
    status_update: TaskStatusUpdateEvent | None = None
    artifact_update: TaskArtifactUpdateEvent | None = None

    @model_validator(mode="after")
    def _one_payload(self) -> Self:
        _exactly_one_set(
            self,
            ("task", "message", "status_update", "artifact_update"),
            "StreamResponse.payload",
        )
        return self


class SendMessageResponse(_A2AModel):
    """What ``SendMessage`` returns: a Task, or a bare Message (§9.4.1)."""

    task: Task | None = None
    message: Message | None = None

    @model_validator(mode="after")
    def _one_payload(self) -> Self:
        _exactly_one_set(self, ("task", "message"), "SendMessageResponse.payload")
        return self


# ---------------------------------------------------------------------------
# Push notification objects (§4.3)
# ---------------------------------------------------------------------------


class AuthenticationInfo(_A2AModel):
    """How the agent authenticates itself to a webhook (§4.3.2).

    ``credentials`` is a secret in transit. §13.2 says to treat it as one,
    which is why the playground's read paths return the scheme and never the
    credential.
    """

    scheme: str = Field(min_length=1)
    credentials: str | None = None


class TaskPushNotificationConfig(_A2AModel):
    """One webhook registration for one task (§4.3.1)."""

    tenant: str | None = None
    id: str | None = None
    task_id: str | None = None
    url: str = Field(min_length=1)
    token: str | None = None
    authentication: AuthenticationInfo | None = None


# ---------------------------------------------------------------------------
# Security objects (§4.5)
# ---------------------------------------------------------------------------


class StringList(_A2AModel):
    """proto ``StringList``: the value half of a ``SecurityRequirement``."""

    list: tuple[str, ...] = ()


class SecurityRequirement(_A2AModel):
    """Scheme name to required scopes (§4.5)."""

    schemes: dict[str, StringList] = Field(default_factory=dict)


class APIKeySecurityScheme(_A2AModel):
    """§4.5.2. ``location`` is "query", "header" or "cookie"."""

    description: str | None = None
    location: str = Field(min_length=1)
    name: str = Field(min_length=1)


class HTTPAuthSecurityScheme(_A2AModel):
    """§4.5.3. ``scheme`` is an IANA HTTP auth scheme, e.g. ``Bearer``."""

    description: str | None = None
    scheme: str = Field(min_length=1)
    bearer_format: str | None = None


class AuthorizationCodeOAuthFlow(_A2AModel):
    """§4.5.8."""

    authorization_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    refresh_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)
    pkce_required: bool = False


class ClientCredentialsOAuthFlow(_A2AModel):
    """§4.5.9."""

    token_url: str = Field(min_length=1)
    refresh_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)


class DeviceCodeOAuthFlow(_A2AModel):
    """§4.5.10."""

    device_authorization_url: str = Field(min_length=1)
    token_url: str = Field(min_length=1)
    refresh_url: str | None = None
    scopes: dict[str, str] = Field(default_factory=dict)


class OAuthFlows(_A2AModel):
    """§4.5.7, minus the two flows the proto marks deprecated.

    ``implicit`` and ``password`` carry ``[deprecated = true]`` in the proto,
    with comments naming their replacements. Psych declares neither: a card
    this library writes should not offer a flow the protocol's own definition
    tells implementers to stop using. A card *read* from a peer that offers
    one still parses, because these models ignore fields they do not know.
    """

    authorization_code: AuthorizationCodeOAuthFlow | None = None
    client_credentials: ClientCredentialsOAuthFlow | None = None
    device_code: DeviceCodeOAuthFlow | None = None

    @model_validator(mode="after")
    def _one_flow(self) -> Self:
        _exactly_one_set(
            self,
            ("authorization_code", "client_credentials", "device_code"),
            "OAuthFlows.flow",
        )
        return self


class OAuth2SecurityScheme(_A2AModel):
    """§4.5.4."""

    description: str | None = None
    flows: OAuthFlows
    oauth2_metadata_url: str | None = None


class OpenIdConnectSecurityScheme(_A2AModel):
    """§4.5.5."""

    description: str | None = None
    open_id_connect_url: str = Field(min_length=1)


class MutualTlsSecurityScheme(_A2AModel):
    """§4.5.6. Carries only a description: everything else about mTLS is
    established below the protocol."""

    description: str | None = None


class SecurityScheme(_A2AModel):
    """The discriminated union of all five schemes (§4.5.1).

    All five are declarable, which is the point: an agent that can only say
    "bearer token" cannot be put behind a corporate mTLS boundary, and the
    declaration is the only part of authentication that A2A owns.
    """

    api_key_security_scheme: APIKeySecurityScheme | None = None
    http_auth_security_scheme: HTTPAuthSecurityScheme | None = None
    oauth2_security_scheme: OAuth2SecurityScheme | None = None
    open_id_connect_security_scheme: OpenIdConnectSecurityScheme | None = None
    mtls_security_scheme: MutualTlsSecurityScheme | None = None

    @model_validator(mode="after")
    def _one_scheme(self) -> Self:
        _exactly_one_set(
            self,
            (
                "api_key_security_scheme",
                "http_auth_security_scheme",
                "oauth2_security_scheme",
                "open_id_connect_security_scheme",
                "mtls_security_scheme",
            ),
            "SecurityScheme.scheme",
        )
        return self


# ---------------------------------------------------------------------------
# Agent discovery objects (§4.4)
# ---------------------------------------------------------------------------


class ProtocolBinding(StrEnum):
    """The bindings the proto names as officially supported
    (``AgentInterface.protocol_binding``).

    An open string in the proto, so a custom binding declares a URI instead
    (§5.8). These three are the ones with a normative binding section.
    """

    JSONRPC = "JSONRPC"
    GRPC = "GRPC"
    HTTP_JSON = "HTTP+JSON"


class AgentInterface(_A2AModel):
    """One URL, one binding, one protocol version (§4.4.6)."""

    url: str = Field(min_length=1)
    protocol_binding: str = Field(min_length=1)
    tenant: str | None = None
    protocol_version: str = Field(min_length=1)


class AgentProvider(_A2AModel):
    """§4.4.2."""

    url: str = Field(min_length=1)
    organization: str = Field(min_length=1)


class AgentExtension(_A2AModel):
    """§4.4.4. ``required=true`` obliges a client to opt in (§4.6.3)."""

    uri: str = Field(min_length=1)
    description: str | None = None
    required: bool = False
    params: dict[str, JsonValue] | None = None


class AgentCapabilities(_A2AModel):
    """§4.4.3.

    ``streaming``, ``push_notifications`` and ``extended_agent_card`` are
    ``optional bool`` in the proto and stay ``bool | None`` here, because
    §8.4.1's canonicalisation rules make "explicitly false" and "not set"
    different signed bytes. Collapsing them to ``False`` would produce a card
    whose signature the verifying peer cannot reproduce.
    """

    streaming: bool | None = None
    push_notifications: bool | None = None
    extensions: tuple[AgentExtension, ...] = ()
    extended_agent_card: bool | None = None


class AgentSkill(_A2AModel):
    """One capability of an agent (§4.4.5)."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ()
    output_modes: tuple[str, ...] = ()
    security_requirements: tuple[SecurityRequirement, ...] = ()


class AgentCardSignature(_A2AModel):
    """A JWS over the card (§4.4.7), assembled by ``psych_runtime.a2a.signing``."""

    protected: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    header: dict[str, JsonValue] | None = None


class AgentCard(_A2AModel):
    """The self-describing manifest (§4.4.1, §8).

    Field order follows the proto's field numbers rather than alphabetical or
    importance order, so a reader diffing this against ``a2a.proto`` can go
    top to bottom.
    """

    name: str = Field(min_length=1)
    description: str
    supported_interfaces: tuple[AgentInterface, ...] = Field(min_length=1)
    provider: AgentProvider | None = None
    version: str = Field(min_length=1)
    documentation_url: str | None = None
    capabilities: AgentCapabilities
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)
    security_requirements: tuple[SecurityRequirement, ...] = ()
    default_input_modes: tuple[str, ...] = Field(min_length=1)
    default_output_modes: tuple[str, ...] = Field(min_length=1)
    skills: tuple[AgentSkill, ...] = ()
    signatures: tuple[AgentCardSignature, ...] = ()
    icon_url: str | None = None


# ---------------------------------------------------------------------------
# Operation parameter objects (§3.2) and request/response messages
# ---------------------------------------------------------------------------


class SendMessageConfiguration(_A2AModel):
    """§3.2.2.

    ``return_immediately=False`` is the proto's default and means the call
    waits until the task is terminal or interrupted. Psych's outbound client
    sends it explicitly, so a peer's own default cannot change what a tool
    call means.
    """

    accepted_output_modes: tuple[str, ...] = ()
    task_push_notification_config: TaskPushNotificationConfig | None = None
    history_length: int | None = Field(default=None, ge=0)
    return_immediately: bool = False


class SendMessageRequest(_A2AModel):
    """§3.2.1."""

    tenant: str | None = None
    message: Message
    configuration: SendMessageConfiguration | None = None
    metadata: dict[str, JsonValue] | None = None


class GetTaskRequest(_A2AModel):
    """proto ``GetTaskRequest``."""

    tenant: str | None = None
    id: str = Field(min_length=1)
    history_length: int | None = Field(default=None, ge=0)


DEFAULT_PAGE_SIZE: Final = 50
"""proto ``ListTasksRequest.page_size``: "If unspecified, at most 50 tasks
will be returned"."""

MAX_PAGE_SIZE: Final = 100
"""proto ``ListTasksRequest.page_size``: "The maximum value is 100"."""


class ListTasksRequest(_A2AModel):
    """proto ``ListTasksRequest`` (§3.1.4).

    ``page_size`` is bounded 1..100 by the proto's own comment, and it is
    enforced here rather than in each binding so a caller cannot ask one
    binding for a page the other would refuse.
    """

    tenant: str | None = None
    context_id: str | None = None
    status: TaskState | None = None
    page_size: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    page_token: str | None = None
    history_length: int | None = Field(default=None, ge=0)
    status_timestamp_after: Timestamp | None = None
    include_artifacts: bool | None = None


class ListTasksResponse(_A2AModel):
    """proto ``ListTasksResponse``."""

    tasks: tuple[Task, ...] = ()
    next_page_token: str = ""
    page_size: int = 0
    total_size: int = 0


class CancelTaskRequest(_A2AModel):
    """proto ``CancelTaskRequest``."""

    tenant: str | None = None
    id: str = Field(min_length=1)
    metadata: dict[str, JsonValue] | None = None


class SubscribeToTaskRequest(_A2AModel):
    """proto ``SubscribeToTaskRequest``."""

    tenant: str | None = None
    id: str = Field(min_length=1)


class GetTaskPushNotificationConfigRequest(_A2AModel):
    """proto ``GetTaskPushNotificationConfigRequest``."""

    tenant: str | None = None
    task_id: str = Field(min_length=1)
    id: str = Field(min_length=1)


class DeleteTaskPushNotificationConfigRequest(_A2AModel):
    """proto ``DeleteTaskPushNotificationConfigRequest``."""

    tenant: str | None = None
    task_id: str = Field(min_length=1)
    id: str = Field(min_length=1)


class ListTaskPushNotificationConfigsRequest(_A2AModel):
    """proto ``ListTaskPushNotificationConfigsRequest``."""

    tenant: str | None = None
    task_id: str = Field(min_length=1)
    page_size: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    page_token: str | None = None


class ListTaskPushNotificationConfigsResponse(_A2AModel):
    """proto ``ListTaskPushNotificationConfigsResponse``."""

    configs: tuple[TaskPushNotificationConfig, ...] = ()
    next_page_token: str = ""


class GetExtendedAgentCardRequest(_A2AModel):
    """proto ``GetExtendedAgentCardRequest``. Carries only the routing
    identifier: the caller's identity comes from the transport's own
    authentication (§13.3)."""

    tenant: str | None = None
