"""The Spec: the only artifact the runtime executes.

DESIGN.md §4. A Spec is a serialisable description of an agent or a workflow.
Data, never code. Four authoring forms converge here (chat, Python builder, file,
import) and Psych privileges none of them, so there is exactly one runtime input
and exactly one validator.

## The invariant every reviewer checks

**A Spec references tools by name and never holds a callable.** A builder may
accept a function and register it as a side effect, but what lands in the Spec is
the registered name. The moment a Spec holds a live object it stops being
serialisable, hashable and storable, and the runtime forks into two execution
models. ``_NoCallables`` below enforces this at construction rather than trusting
review to catch it.

## Normalisation happens here, not in the hasher

Two structurally identical Specs must produce the same Version hash (§23). The
cheap way to get that wrong is to leave normalisation to the canonical serialiser
and then argue about whether tool order is meaningful. Instead the models
normalise themselves at validation time: set-like collections sort, order-bearing
collections do not, and line endings normalise. By the time
``psych_runtime.core.version`` sees a Spec there is nothing left to decide.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    "MIN_SPAWN_DELIVERABLE",
    "MIN_SPAWN_PURPOSE",
    "MIN_SPAWN_TASK",
    "MIN_SUBAGENT_DESCRIPTION",
    "A2APeer",
    "AgentSpec",
    "AgentStep",
    "CodeTool",
    "CompactionPolicy",
    "HttpTool",
    "Limits",
    "McpOAuth",
    "McpServer",
    "ModelRef",
    "Skill",
    "SpawnEnvelope",
    "Spec",
    "SubagentRef",
    "SuspensionPolicy",
    "ToolStep",
    "WorkflowSpec",
    "WorkflowStep",
]

_NAME_PATTERN: Final = r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,127}$"
"""Tool, skill, step and subagent names. Restrictive on purpose: these names
reach a model as JSON schema property names and reach a log as identifiers, and
a name with a newline or a quote in it is a problem in both places."""

MIN_SUBAGENT_DESCRIPTION: Final = 20
"""DESIGN.md §17: the validator rejects a subagent whose description is missing
or under 20 characters. Vague descriptions are the root cause of bad routing, and
this is the cheapest possible guard against one."""


def _normalise_text(value: str) -> str:
    """Normalise line endings, and nothing else.

    A Spec authored in a YAML file on Windows and the same Spec authored through
    chat differ by ``\\r\\n`` against ``\\n``, and a human would call those the
    same agent. Line endings are a transport artifact, so they normalise.

    Trailing whitespace deliberately does not. It is content: a model reading an
    instruction block can be affected by it, so two Specs differing in trailing
    whitespace really are two different agents and should hash differently.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


class _SpecModel(BaseModel):
    """Base for everything in a Spec.

    Frozen so a Spec cannot be edited after it is validated, and ``extra`` is
    forbidden so a typo in a YAML file fails loudly at publish rather than being
    silently dropped and producing an agent that quietly ignores a setting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _holds_no_callables(self) -> Self:
        """Reject a Spec that holds a live object.

        Pydantic's typed fields already refuse a function where a ``str`` is
        declared, so on today's models this validator never fires. It exists for
        the change that has not been made yet: the moment someone adds a field
        typed ``Any`` or ``object``, this is what stops a callable landing in a
        Spec and forking the runtime into two execution models (DESIGN.md §4).
        """
        for name in type(self).model_fields:
            _reject_callables(getattr(self, name), f"{type(self).__name__}.{name}")
        return self


def _reject_callables(value: object, path: str) -> None:
    """Walk a validated value and raise if anything in it is callable."""
    if isinstance(value, _SpecModel):
        return  # Already checked by its own validator when it was constructed.
    if callable(value):
        # Classes count. A class in a Spec is as unserialisable as a function is,
        # and excluding types here would leave `{"default_factory": dict}` in a
        # JSON schema field looking fine right up until the Version is stored.
        #
        # ValueError rather than TypeError on purpose: pydantic converts a
        # ValueError raised inside a validator into a ValidationError carrying
        # the field path, and lets a TypeError escape raw.
        raise ValueError(  # noqa: TRY004
            f"{path} holds a callable ({value!r}). A Spec references tools by "
            "name and never holds a callable: register the function and put its "
            "registered name here (DESIGN.md §4)."
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_callables(item, f"{path}[{key!r}]")
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            _reject_callables(item, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class CodeTool(_SpecModel):
    """A Python function the consumer registered at boot, referenced by name.

    The Spec carries the name and nothing else. The schema is derived from the
    registered function's type hints at registration time, so it lives in the
    registry rather than here; duplicating it would let the two drift.
    """

    kind: Literal["code"] = "code"
    name: str = Field(pattern=_NAME_PATTERN)
    interruptible: bool = True
    """DESIGN.md §9: on abort an interruptible tool is cancelled and recorded as
    aborted, while a non-interruptible one is allowed to finish and its result
    recorded. A refund tool sets this False. It is the difference between a
    stopped agent and a half-issued refund."""


class HttpTool(_SpecModel):
    """A tool that is entirely data, so an end user can create one at runtime."""

    kind: Literal["http"] = "http"
    name: str = Field(pattern=_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=4096)
    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    """JSON Schema for the arguments, as handed to the model."""
    headers: dict[str, str] = Field(default_factory=dict)
    credential: str | None = Field(default=None, max_length=256)
    """A name resolved through the ``SecretResolver`` port at call time, keyed by
    Scope. Psych never stores a raw credential and an exported Version carries
    this name, never a secret (DESIGN.md §10.4)."""
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    interruptible: bool = True

    @field_validator("headers")
    @classmethod
    def _headers_carry_no_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        """A literal Authorization header in a Spec is a credential in a Spec.

        Specs are exported, reviewed in git and stored unencrypted. Anything
        secret goes through ``credential`` and the SecretResolver instead.
        """
        for header in value:
            if header.lower() in {"authorization", "proxy-authorization", "cookie"}:
                raise ValueError(
                    f"header {header!r} cannot be set literally in a Spec; use "
                    "`credential` so the value resolves through the SecretResolver "
                    "port and never lands in an exported Version"
                )
        return value


class McpOAuth(_SpecModel):
    """OAuth 2.1 configuration for one MCP server: which grant this
    server needs and which client identity requests it, carried per server
    rather than per ``McpPool`` so a Spec naming two servers behind two
    different authorization servers -- or one needing ``authorization_code``
    and another ``client_credentials`` -- never has to run two pools.

    ## Why this is Spec-carried rather than runtime configuration keyed by
    server name (the design question this ticket asked to settle first)

    The alternative was runtime configuration alongside the
    ``SecretResolver``, the same way a consumer wires up their store or
    sandbox: keep ``McpServer`` exactly as it was and let a wiring layer map
    server names to OAuth identities and grants outside the Spec. That
    keeps the Spec free of what looks like deployment detail, and it is a
    real, defensible position -- OAuth setup often *is* environment-specific
    plumbing.

    It loses to Spec-carried for the same reason ``McpServer.url``,
    ``McpServer.transport`` and ``McpServer.credential`` are already Spec
    fields rather than runtime lookups keyed by server name: which
    authorization server a tool set sits behind, and which grant it needs to
    reach it, is part of what the agent *is*, not a knob layered on top of an
    otherwise-generic agent. Publishing "the CRM agent" already means
    publishing an agent that talks to one specific CRM at one specific URL;
    the OAuth client identity that CRM's authorization server expects is the
    same kind of fact, not a different kind. A ``client_id`` under OAuth 2.1
    reinforces this: it is a public identifier of *which application* is
    calling (RFC 6749 draws the client-id/client-secret line at exactly
    public/confidential), the same register as a URL, not a secret --
    so pinning ``preregistered_client_id`` in the Spec puts it exactly where
    ``McpServer.url`` already lives, and it travels with the agent into
    review and into the exported Version the same way. Deploying the same
    agent against a differently-registered OAuth app is, in the same sense a
    different server URL already is, a different Spec, not the same Spec
    with different config bolted on beside it -- and DESIGN.md §4 already
    commits to a Spec being the full, self-contained description of what an
    agent connects to.

    What must never follow the client_id into the Spec, either way this
    decision went, is the one genuinely confidential half. A pre-registered
    confidential client's secret does not appear here as a value:
    ``client_secret_credential`` is a name, resolved through the same
    ``SecretResolver`` port ``McpServer.credential`` already resolves
    through, for the identical reason ``HttpTool`` refuses a literal
    ``Authorization`` header. Two Specs asking for the same
    ``client_secret_credential`` name may resolve to different values under
    different Scopes exactly as ``McpServer.credential`` already can, which
    is what lets one Spec, deployed for many tenants, use one shared
    application registration while each tenant's actual secret (if a
    per-tenant registration is what a consumer's authorization server
    requires) still resolves independently.

    ## A consequence worth being honest about: the Version hash moves

    ``psych_runtime.core.version.canonical_bytes`` serialises every field of every
    model, including one newly added with a default, so adding this field to
    ``McpServer`` changes the canonical bytes -- and therefore the hash -- of
    any Spec that already declares one or more ``mcp_servers``. That is true
    of adding *any* field here, not particular to OAuth or to this decision:
    there is no way to extend ``McpServer`` without moving the hash of a Spec
    that uses it, short of excluding unset fields from the hash, which
    ``psych_runtime.core.version`` deliberately does not do (its docstring: "explicit
    null" and "not provided" must hash the same, which excluding-when-None
    would break in the other direction). A Spec with an empty
    ``mcp_servers`` tuple is unaffected -- there is no ``McpServer`` object
    for the new field to appear on. See
    ``tests/unit/test_version.py::TestMcpOAuthHashImpact`` for both halves
    verified against running code rather than assumed.
    """

    grant: Literal["authorization_code", "client_credentials"] = "client_credentials"
    """``authorization_code`` needs a redirect listener Psych does not run
    (DESIGN.md §1); a consumer wanting it configures ``redirect_uris`` below
    and supplies an ``AuthorizationRedirectPort`` at the runtime layer.
    ``client_credentials`` needs nothing from the host application, so it is
    the default -- the same reasoning ``psych_runtime.tools.mcp``'s own
    ``_DEFAULT_OAUTH_GRANT`` documents."""
    preregistered_client_id: str | None = Field(default=None, max_length=512)
    """A public client identifier, not a secret: the OAuth 2.1 confidential/
    public client split puts this on the public side (RFC 6749 §2.2)."""
    client_secret_credential: str | None = Field(default=None, max_length=256)
    """A name the SecretResolver resolves at connect time, exactly like
    ``McpServer.credential``. Never a literal secret: only a pre-registered
    confidential client needs this, and even then this Spec carries its name,
    not its value."""
    issuer: str | None = Field(default=None, max_length=2048)
    """The authorization server issuer for a pre-registered client.

    Set this for ``client_credentials`` so the SDK will send the client
    secret only to metadata belonging to the expected issuer. It remains
    optional for compatibility with servers that discover the issuer from
    protected-resource metadata.
    """
    cimd_url: str | None = Field(default=None, max_length=2048)
    """An HTTPS URL, controlled by the consumer, hosting this client's own
    Client ID Metadata Document. Public by construction -- it is meant to be
    fetched by any authorization server that supports CIMD."""
    allow_dynamic_registration: bool = True
    application_type: Literal["native", "web"] = "native"
    client_name: str = Field(default="psych", max_length=256)
    redirect_uris: tuple[str, ...] = ()
    """Required (non-empty) for the ``authorization_code`` grant, ignored for
    ``client_credentials``. Order-bearing -- the first entry is the one used
    when a caller does not pass a more specific redirect URI -- so, like
    ``ModelRef.fallbacks``, this is never sorted."""

    @field_validator("preregistered_client_id", "client_secret_credential", "issuer", "cimd_url")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        return _normalise_text(value) if value is not None else None


class McpServer(_SpecModel):
    """An MCP connection, as data, creatable at runtime.

    The tools a server contributes are discovered from the server, not declared
    here. ``allow`` narrows what this Spec is granted out of whatever the server
    offers, which is the third link in the chain of DESIGN.md §10.5.
    """

    name: str = Field(pattern=_NAME_PATTERN)
    url: str = Field(min_length=1, max_length=2048)
    transport: Literal["http", "sse"] = "http"
    """``"http"`` is Streamable HTTP (MCP spec 2026-07-28) and is the
    documented default: ``psych_runtime.tools.mcp`` negotiates it, with a fallback to
    the ``initialize``-handshake dialect for a 2025-11-25 or 2025-06-18
    server, all through one connection.

    ``"sse"`` selects the pre-2025-03-26 HTTP+SSE transport. The official MCP
    client implements its separate event and message endpoints. MCP has
    deprecated this transport, so new Specs should use ``"http"`` unless they
    must connect to an older server.

    ``"stdio"`` was removed from this enum rather than kept alongside a real
    implementation. The MCP spec has not deprecated it, and it is the
    fully-supported local-subprocess transport most MCP servers people run
    on their own machine actually use. Psych simply never implemented it:
    there was no subprocess launcher, no line-delimited JSON-RPC framing
    over stdin/stdout, nothing. A Spec could ask for it, validation passed,
    and the connection could never work, which is exactly a placeholder on a
    shipped path. Removing the enum member is a
    breaking change to the public API; adding a real stdio transport back is
    open work, not a silent revival of the old member.
    """
    credential: str | None = Field(default=None, max_length=256)
    oauth: McpOAuth | None = None
    """This server's OAuth 2.1 configuration, or ``None`` for a server that
    is either unauthenticated or authenticated only by ``credential`` above.
    See ``McpOAuth`` for why this lives here rather than in runtime
    configuration keyed by server name, and for the Version-hash consequence
    of this field's addition."""
    allow: tuple[str, ...] = ()
    """Tool name patterns this Spec is granted. Empty means every tool the
    server offers, still subject to what the tenant permits. Patterns support a
    trailing ``*``; see ``psych_runtime.tools.narrowing``."""
    optional: bool = False
    """DESIGN.md §10.7: an unreachable server fails the Run by default. Marking
    it optional omits its tools and tells the model they are unavailable.
    Defaulting this to True would produce an agent that confidently tells a
    customer it cannot issue refunds today."""
    preload: bool | None = None
    """Whether this server's tools are listed in the prompt, or discovered.

    ``None``, the default, decides from the catalogue's size: a server whose
    tools would cost more than the Runtime's budget in **characters** is
    deferred and a smaller one is not (``psych_runtime.tools.deferred``, which explains
    why characters rather than tokens). That is the honest default because the
    size is a fact about the server, discovered at run time and liable to
    change, not something a Spec's author knows when they write it.

    ``False`` always defers: the model is told the server is there and reaches
    it through ``list_tools``/``get_tool_info``/``call_tool``. ``True`` always
    preloads, which is what a consumer sets when they would rather pay for the
    schemas than have the model spend a turn discovering them.

    Why this matters in bytes: a real server this project connects to offers
    351 tools whose schemas are 2.0 MB. Preloaded, that is sent on every turn
    of every Run -- past several providers' request limits outright, and billed
    on every turn where it is not."""

    @field_validator("allow")
    @classmethod
    def _allow_is_a_sorted_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Grant order carries no meaning, so it must not change the hash."""
        return tuple(sorted(set(value)))


class A2APeer(_SpecModel):
    """Another agent, reachable over A2A, that this agent may delegate to.

    The sibling of ``McpServer`` and shaped like it on purpose: a URL, a
    credential *name*, an allow list, and an ``optional`` flag. Everything a
    peer can actually do is discovered from its Agent Card at run time
    (``psych_runtime.tools.a2a``), never declared here, for the same reason an MCP
    server's tools are not declared here: what a remote system offers is a
    fact about that system, and a Spec that copied it would be advertising an
    ability that may have been withdrawn (DESIGN.md §10.7).

    ``allow`` narrows the peer's skills the same way ``McpServer.allow``
    narrows a server's tools, through the one narrowing function every plane
    of DESIGN.md §10.5 goes through. A peer with no allow list grants every
    skill its card declares.

    **Adding this field changes the Version hash of every Spec**, because
    ``psych_runtime.core.version`` hashes the validated model with every field
    present. That is the same trade-off ``McpOAuth`` documented when it was
    added, and the same answer: the alternative is keeping peers in runtime
    configuration keyed by name, where two deployments could run "the same"
    published agent against different peers and the Version could not tell
    them apart. Which agents this agent may call is part of what this agent
    *is*.
    """

    name: str = Field(pattern=_NAME_PATTERN)
    """The Spec-local alias. Tool names offered to the model are derived from
    it (``psych_runtime.tools.a2a``), so it is what the model sees, not the peer's own
    name -- two peers may legitimately both be called "research"."""
    url: str = Field(min_length=1, max_length=2048)
    """The peer's A2A base URL, or its Agent Card URL. ``psych_runtime.tools.a2a``
    resolves a base URL to ``/.well-known/agent-card.json`` (§8.2) and takes a
    URL that already ends in a card path as the card itself."""
    credential: str | None = Field(default=None, max_length=256)
    """The name of a credential the consumer's ``SecretResolver`` holds. Never
    a secret value: a Spec is exported, versioned and read by anyone who can
    read a Version (DESIGN.md §10.4)."""
    scheme: str = Field(default="Bearer", max_length=64)
    """The HTTP authentication scheme the credential is presented under, from
    the peer's declared ``HTTPAuthSecurityScheme`` (§4.5.3). ``Bearer`` covers
    OAuth 2 access tokens and most API keys presented as bearer tokens."""
    tenant: str | None = Field(default=None, max_length=256)
    """The peer's opaque routing identifier, when its card's chosen
    ``AgentInterface`` declares one (§4.4.6): "When set, clients MUST include
    this value in the ``tenant`` field of all request messages sent to this
    interface"."""
    allow: tuple[str, ...] = ()
    """Skill ids this Spec is granted. Empty means every skill the card
    declares, still subject to what the tenant permits."""
    optional: bool = False
    """An unreachable peer fails the Run by default, exactly as an MCP server
    does (DESIGN.md §10.7)."""
    extensions: tuple[str, ...] = ()
    """Extension URIs to opt into on every call (§4.6.1's ``A2A-Extensions``).
    Declared in the Spec because opting into an extension changes what the
    peer does, which is part of what this agent is."""

    @field_validator("allow", "extensions")
    @classmethod
    def _grants_are_a_sorted_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Grant order carries no meaning, so it must not change the hash."""
        return tuple(sorted(set(value)))


AnyTool = Annotated[CodeTool | HttpTool, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Model, limits, skills, suspension
# ---------------------------------------------------------------------------


class ModelRef(_SpecModel):
    """Which model, and how it is called.

    ``fallbacks`` is failover for a provider outage, not routing. DESIGN.md §19
    puts automatic model routing out of scope for v1 and says no partial
    implementation should be added, so this list is tried in order on transient
    failure and is never chosen between on cost or capability.
    """

    model: str = Field(min_length=1, max_length=256)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    fallbacks: tuple[str, ...] = ()
    """Order-bearing: tried in sequence, so it is not sorted."""


class Limits(_SpecModel):
    """Budgets. Every one of these has a default because every one of these
    prevents a cost incident, and an unset limit is an unbounded bill."""

    max_steps: int = Field(default=48, gt=0, le=10_000)
    max_turns: int = Field(default=32, gt=0, le=1_000)
    max_tool_calls_per_turn: int = Field(default=16, gt=0, le=256)
    deadline_seconds: float = Field(default=900.0, gt=0, le=86_400)
    """DESIGN.md §8.4: every Run has a deadline. There is no 'no deadline'."""
    transient_retry_budget: int = Field(default=8, ge=0, le=100)
    """Per Run, not per call. Bounding per call lets a Run retry forever by
    spreading failures across steps (DESIGN.md §8.6)."""
    max_delegation_depth: int = Field(default=3, ge=0, le=16)
    max_fanout_per_turn: int = Field(default=4, gt=0, le=64)
    """One turn spawning a tree is a cost incident (DESIGN.md §17)."""
    failure_streak_threshold: int = Field(default=3, gt=0, le=100)
    """Consecutive failures of one tool before the model is told to change
    approach (DESIGN.md §10.6)."""
    failure_streak_hard_stop: int = Field(default=6, gt=0, le=200)
    """Consecutive failures before the turn fails outright rather than burning
    the step budget on a loop."""
    repeat_call_threshold: int = Field(default=3, gt=0, le=100)
    """Identical calls -- same tool, same arguments, *same result* -- before
    the call is refused and the model told it already has the answer.

    Distinct from the failure-streak fields above, which count failures. This
    counts successes that taught the model nothing, which is the other way a
    Run burns its budget and the one nothing else here catches."""
    repeat_call_hard_stop: int = Field(default=6, gt=0, le=200)
    """Identical calls before the turn fails outright, for a model that was
    told and carried on anyway."""
    stream_idle_seconds: float = Field(default=300.0, ge=0, le=3_600)
    """DESIGN.md §8.5. Generous by default because reasoning models are
    legitimately silent for long periods, and ``0`` disables it."""
    large_result_bytes: int = Field(default=32_768, gt=0)
    """Above this a tool result is elided in the model's context and kept whole
    in the log (DESIGN.md §10.8)."""
    max_history_records: int = Field(default=300, ge=0, le=20_000)
    """Bounds how much of a continued conversation a turn replays
    (DESIGN.md §23.3). When a Run was admitted with ``continues=`` a
    predecessor, this is the budget ``psych_runtime.runtime.thread.load_thread_history``
    spends walking that chain back for ancestor Runs' records to fold into the
    conversation, on top of this Run's own.

    Replaying a long chat history into every turn is the dominant cost of a
    chat agent, so this exists to keep that bounded rather than growing
    without limit as a thread gets longer -- counted in raw Records, since
    that is what actually gets replayed onto the wire, not in turns or
    messages.

    The walk always includes the immediate predecessor Run in full, even one
    that alone exceeds this budget: a thread's very first continuation has to
    carry *something* forward, and a bound that could erase all context on
    the one message it exists to enable would defeat the feature. Every Run
    further back is included whole or omitted entirely, never truncated
    mid-Run -- splitting a Run's own log would separate a tool call from its
    result and corrupt ``psych_runtime.core.conversation.build_conversation``'s
    pairing.

    ``0`` disables replayed history entirely: a continuation still records
    ``continues_run_id`` in the log, so the thread link is never lost, but no
    ancestor's conversation reaches the model."""

    @model_validator(mode="after")
    def _hard_stop_is_above_the_advisory_threshold(self) -> Self:
        if self.failure_streak_hard_stop < self.failure_streak_threshold:
            raise ValueError(
                f"failure_streak_hard_stop ({self.failure_streak_hard_stop}) is below "
                f"failure_streak_threshold ({self.failure_streak_threshold}); the turn "
                "would fail before the model was ever told to change approach"
            )
        if self.repeat_call_hard_stop < self.repeat_call_threshold:
            raise ValueError(
                f"repeat_call_hard_stop ({self.repeat_call_hard_stop}) is below "
                f"repeat_call_threshold ({self.repeat_call_threshold}); the turn "
                "would fail before the model was ever told it already had the answer"
            )
        return self


class SuspensionPolicy(_SpecModel):
    """How long a suspended Run waits before it is given up on.

    DESIGN.md §11: suspensions expire, and a Run suspended past its expiry is
    settled as abandoned rather than waiting forever on a user who left.
    """

    approval_expires_seconds: float = Field(default=86_400.0, gt=0)
    question_expires_seconds: float = Field(default=86_400.0, gt=0)
    external_expires_seconds: float = Field(default=604_800.0, gt=0)
    children_expires_seconds: float = Field(default=3_600.0, gt=0)
    """How long a parent waits on subagents it spawned in the background before
    it is given up on (``SuspendReason.CHILDREN``).

    An hour by default, which is far shorter than the day an approval waits and
    the week an external event does, because it is waiting on something with its
    own deadline rather than on a person. Every child Run carries
    ``Limits.deadline_seconds`` and is force-settled past it (DESIGN.md §8.4),
    so a parent still waiting long after that is waiting on a notification that
    is never coming rather than on work still being done. Settling it abandoned
    is the honest answer, and it is the same mechanism every other suspension
    uses (DESIGN.md §11)."""
    may_ask_questions: bool = False
    """Whether this agent is offered the ``ask_question`` built-in.

    Off by default, and deliberately a Spec field rather than a ``Runtime``
    one. An agent that can stop and wait for a person is a different agent from
    one that cannot: it can park a Run for a day, and a consumer running work
    unattended needs that to be a decision somebody made rather than a
    capability every agent quietly has. Being on the Spec means it joins the
    Version hash, so what an agent was allowed to do is pinned with everything
    else about it (DESIGN.md §4).

    Unanswered questions expire on ``question_expires_seconds`` above, the same
    mechanism an approval uses.
    """


class Skill(_SpecModel):
    """An instruction pack loaded on demand.

    DESIGN.md §16: descriptions of every available skill sit in the system
    prompt; bodies load only when the model calls ``load_skill``. Skills are part
    of the Spec, so they version and pin with it.
    """

    name: str = Field(pattern=_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=1024)
    body: str = Field(min_length=1)

    @field_validator("body", "description")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_text(value)


MIN_SPAWN_PURPOSE: Final = 20
"""What a composed child is for, in at least this many characters.

The same reasoning as ``MIN_SUBAGENT_DESCRIPTION`` above, applied at the other
end of the same problem. A ``SubagentRef``'s description is written by an author
at publish and is refused when it is vague; a composed child's purpose is
written by the model at run time and is refused here for exactly the same
reason. Debugging bad routing after the fact costs far more than refusing a
one-line brief at the boundary."""

MIN_SPAWN_TASK: Final = 40
"""And the task itself, which the child sees instead of the conversation.

Longer than the purpose because it is doing more work: the child does not see
the parent's conversation (``psych_runtime.runtime.subagent.child_input``), so a task
that assumes context the child cannot reach produces a confident answer to the
wrong question. Forty characters is not a quality bar, it is a floor low enough
that no legitimate brief hits it and high enough to catch "do the research"."""

MIN_SPAWN_DELIVERABLE: Final = 15
"""And what it should hand back. Shortest of the three because "a JSON list of
URLs" is a complete answer at fifteen characters, while the parent having no
statement of what it asked for at all is what makes a returned blob unusable."""


class SpawnEnvelope(_SpecModel):
    """What a parent may compose at run time, rather than what it may call.

    DESIGN.md §17 describes a roster: a parent lists its subagents, each
    subagent's Spec is embedded in the parent's, and one Version hash pins the
    whole tree. That is load-bearing -- ``psych_runtime.runtime.execute`` reloads the
    pinned Version at the top of every Attempt including a reclaiming Worker's,
    so a tree that could change between a crash and a reclaim would resume as a
    different tree -- and it stays exactly as it was.

    A **dynamically composed** child is the other case: its instructions are
    written by the model at run time and so are not in the parent's Version at
    all. What pins them is the child's own Run. This model is the permission
    that makes that legal, and it is deliberately an envelope rather than a
    roster: it says whether this agent may compose children, out of which of
    its own tools, on which models, how deep and how many at once. Being on the
    Spec, it joins the Version hash like every other permission, so "this agent
    was allowed to write its own subagents" is pinned with everything else
    about it (DESIGN.md §4).

    The child Spec composed inside this envelope is published as a Version like
    any other and recorded by hash in the spawn record, so crash recovery is
    unchanged: the child Run pins a hash, and replaying it re-reads that Spec
    rather than re-composing one from a prompt the model would write
    differently the second time.

    Think of a ``SubagentRef`` as a pre-composed type and this as writing the
    brief fresh. Both paths exist; neither replaces the other.
    """

    tools: tuple[str, ...] = ()
    """Which of the parent's own tools a composed child may be given.

    Names or trailing-``*`` prefixes, matched by ``psych_runtime.tools.narrowing``.
    Empty means everything the parent holds, which is that module's rule
    everywhere else and would be dangerous to invert here alone: an empty list
    meaning "nothing" would make ``spawn`` silently useless rather than
    obviously wrong, and the fix people reach for is ``*``.

    This is a ceiling, not a grant. What a child actually gets is the
    intersection of what the model asked for, this list, and what the parent
    itself holds, computed by the same ``narrow`` the validator and the
    per-turn resolver call. A parent cannot write itself a child with more
    access than it has."""

    models: tuple[str, ...] = ()
    """Which models a composed child may name. Empty means the parent's own
    model and nothing else, which is the conservative reading: a model the
    author never mentioned is not a model they priced."""

    max_depth: int = Field(default=2, ge=1, le=16)
    """How deep composed spawning may go, counted the same way delegation depth
    is (DESIGN.md §17) and enforced by the same monotone ``resolve_child_depth``.
    Separate from ``Limits.max_delegation_depth`` because the two answer
    different questions: that one bounds a tree an author wrote and can read,
    this one bounds a tree nobody has read yet."""

    max_alive: int = Field(default=3, ge=1, le=32)
    """How many composed children may be **running at once**.

    Not per turn. ``Limits.max_fanout_per_turn`` counts delegations started in
    one turn and is right for a blocking delegation, which is finished by the
    time the turn ends. A background child outlives the turn that spawned it,
    so a per-turn cap would let a parent hold twenty children open by spawning
    four a turn for five turns. This counts the ones still alive, which is the
    number that actually costs money."""

    may_message: bool = True
    """Whether a parent may send a message into a running child. Off makes
    spawning fire-and-forget, which is the right shape for a fan-out of
    independent jobs and the wrong one for anything a person is watching."""

    @field_validator("tools", "models")
    @classmethod
    def _sorted_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Order carries no meaning to the runtime, so it must not change the
        hash: two authors writing the same envelope in a different order have
        written the same agent."""
        return tuple(sorted(set(value)))


class CompactionPolicy(_SpecModel):
    """When this agent replaces its older conversation with a summary.

    Present means compaction is on for this agent; ``AgentSpec.compaction`` is
    ``None`` by default, which means a conversation grows until the provider
    refuses it. On the Spec rather than on the Runtime because an agent that
    summarises its own history is a different agent: what the model is shown on
    a long Run is not the same conversation, so the difference belongs in the
    Version hash (DESIGN.md §4) rather than in a flag a Runtime could change
    under a published Version.

    The compaction itself is a write (``psych_runtime.runtime.compaction``); what it
    produces is a ``CompactionApplied`` Record, and the replaced records stay in
    the log. Compaction changes what the model sees next, never what happened.
    """

    trigger_tokens: int = Field(gt=0)
    """Compact once the provider's own input count for the last model call
    reaches this many tokens.

    Required, with no default, because Psych has no context window to compare
    against: the port speaks to a proxy that can reach models it has never been
    told about (``psych_runtime.model.port.ModelClient.known_models`` returns empty for
    exactly that reason), so any default here would be a guess about somebody
    else's model. The consumer knows which model they pointed this at and what
    fraction of its window they want to spend before summarising.

    Measured from ``Usage``, which the provider reports on every call, rather
    than estimated by a tokenizer. Psych takes no tokenizer dependency: one
    tokenizer per provider is a maintenance burden and an estimate that looks
    like a measurement is worse than no number at all (the reasoning
    ``psych_runtime.model.port.StreamDone`` already applies to usage). The number
    compared against this is ``input + cache_read + cache_write`` of the last
    finished call, which is the size of the prompt actually sent -- cached input
    still occupies the window. See ``RunStateView.last_prompt_tokens``.

    The consequence worth knowing: the trigger is read one call late. A Run
    compacts on the turn *after* the one that crossed the line, so this should
    sit far enough below the real window that one more turn fits. That is a
    property of measuring rather than estimating, and it is the trade this
    takes deliberately: a real number one turn late beats an invented one on
    time.
    """

    keep_recent_turns: int = Field(default=3, gt=0, le=100)
    """How many recent turns stay verbatim below the summary.

    A turn here is one model call and the tool results it produced, which is
    what the log's own ``turn_started`` boundaries mark. The cut always lands on
    one of those boundaries: cutting anywhere else could put an assistant
    message's tool calls above the line and their results below it, and a
    provider rejects a conversation whose tool calls have no answers
    (DESIGN.md §9).
    """

    model: str | None = None
    """Which model writes the summary. ``None`` means the agent's own.

    Separate because summarising is not the work: it is a bounded, mechanical
    read of a transcript, and a consumer paying for a frontier model on the
    agent itself will often want a cheap one here. Named on the Spec so it
    joins the Version hash, and checked at publish against the ModelClient's
    known models like the agent's own model is.
    """

    max_summary_tokens: int = Field(default=2_048, gt=0, le=32_000)
    """Cap on the summary's length, passed as ``max_output_tokens``.

    A summary that is allowed to run as long as the conversation it replaces
    saves nothing.
    """

    summary_instructions: str | None = Field(default=None, max_length=4_000)
    """What this agent additionally needs kept, on top of what Psych keeps.

    Psych's own instruction is general and always applies: keep what was asked
    and the constraints on it, facts established by tool results, decisions and
    why, what is done and what is outstanding; preserve identifiers, numbers and
    quoted text exactly; drop pleasantries and superseded attempts. That is the
    floor and this cannot lower it, which is the point -- an agent author knows
    what *their* domain cannot afford to lose, and nobody knows in advance what
    a summariser will decide was pleasantry.

    So this is added rather than substituted, and it is placed last, where a
    model weighs it most heavily. Say what must survive and what may go, in the
    vocabulary of the work: "always keep every order number and its status,
    even for orders already resolved", "the customer's stated budget is a
    constraint, never a preference". A summariser has no idea which of the
    numbers in a transcript is the one that matters.

    Joins the Version hash like everything else here. An agent that summarises
    by different rules produces a different conversation on a long Run, and
    that is a different agent.
    """


class SubagentRef(_SpecModel):
    """A nested agent this Spec may delegate to.

    The subagent's Spec is embedded rather than referenced by hash so that a
    Version is self-contained: one hash pins the whole delegation tree, and a
    trace read later cannot find a dangling child reference.
    """

    name: str = Field(pattern=_NAME_PATTERN)
    description: str = Field(min_length=MIN_SUBAGENT_DESCRIPTION, max_length=2048)
    """DESIGN.md §17 rejects a description under 20 characters. The parent's
    delegation tool describes each subagent by this text, and vague descriptions
    are the root cause of bad routing."""
    spec: AgentSpec

    @field_validator("description")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_text(value)


# ---------------------------------------------------------------------------
# Agent and workflow
# ---------------------------------------------------------------------------


class AgentSpec(_SpecModel):
    """A Step that loops Turns until a stop condition (DESIGN.md §5)."""

    kind: Literal["agent"] = "agent"
    name: str = Field(pattern=_NAME_PATTERN)
    description: str = Field(default="", max_length=4096)
    instructions: str = Field(default="")
    model: ModelRef
    tools: tuple[AnyTool, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    a2a_peers: tuple[A2APeer, ...] = ()
    """Other agents this one may call over A2A (``psych_runtime.tools.a2a``). Empty
    for the overwhelming majority of agents, and the same third narrowing
    plane as ``mcp_servers``: what the Spec grants."""
    skills: tuple[Skill, ...] = ()
    subagents: tuple[SubagentRef, ...] = ()
    spawn: SpawnEnvelope | None = None
    """Whether this agent may compose subagents at run time, and inside what.

    ``None``, the default, means it may not: an agent that can write its own
    children is a different agent from one that can only route to the ones its
    author wrote, and that difference belongs in the Version hash rather than in
    a Runtime flag. See ``SpawnEnvelope`` for why this is an envelope rather
    than a second roster.

    Independent of ``subagents`` above: an agent may have both, either or
    neither. A named subagent stays a blocking ``delegate`` call; a composed one
    runs in the background and reports back as a Record."""
    compaction: CompactionPolicy | None = None
    """Whether this agent summarises its older conversation instead of letting
    it reach the context window, and on what terms.

    ``None``, the default, means it does not: the conversation grows until the
    provider refuses the request. Opt-in for the same reason ``spawn`` is --
    it changes what the model is shown, so it belongs in the Version hash
    rather than in Runtime wiring -- and off by default because summarising is
    lossy and an agent whose Runs are short should never pay for it. See
    ``CompactionPolicy``, and ``psych_runtime.runtime.compaction`` for the write side.
    """
    limits: Limits = Field(default_factory=Limits)
    suspension: SuspensionPolicy = Field(default_factory=SuspensionPolicy)
    tasks_enabled: bool = False
    """Whether this agent is offered the ``update_tasks`` built-in.

    Off by default. A task list is closer to product opinion than to runtime
    mechanism -- see ``psych_runtime.core.tasks`` for the argument, and for why the
    line DESIGN.md §1 draws is between a capability a consumer chooses and one
    Psych imposes. On the Spec rather than the Runtime so it joins the Version
    hash: an agent that keeps a plan is a different agent from one that does
    not, and its prompt differs on every turn once it writes one.
    """
    components_enabled: bool = False
    """Whether this agent is offered the ``show_component`` built-in.

    Off by default, and for the same reason ``tasks_enabled`` is: it is a
    capability a consumer chooses rather than an opinion Psych imposes, and an
    agent that will only ever answer in a sentence should not spend prompt on
    a vocabulary it never uses. On the Spec so it joins the Version hash --
    an agent that can answer with a chart is a different agent, and its every
    turn is offered a different tool set.

    What it does *not* enable is any rendering: see ``psych_runtime.core.components``
    for why the payload stops at data and intent.
    """
    answer_style: Literal["concise"] | None = None
    """How this agent is asked to shape its final answer, or ``None`` for no
    instruction at all.

    ``"concise"`` asks for a short answer, and for bullets or a table when
    presenting more than a few facts. Not "always use a table", which would
    make a one-line answer absurd.

    **A Spec field, so it joins the Version hash, and that is deliberate.** It
    changes what the model is told, and a Version that does not capture what
    the model was told stops being an honest record of what the agent was
    (DESIGN.md §4). This is the opposite call from an MCP server's description
    (``psych_runtime.tools.mcp.McpTools.describe``), which is kept out of the Spec on
    purpose: a response style is part of what this agent *is* and changes only
    when someone changes the agent, while a server description is a fact about
    an external system that moves on its own schedule.

    ``None`` adds nothing to the prompt, so an existing Version assembles
    byte-identically and nothing already published changes behaviour."""

    @field_validator("instructions", "description")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_text(value)

    @field_validator("tools")
    @classmethod
    def _tools_are_a_sorted_set(cls, value: tuple[AnyTool, ...]) -> tuple[AnyTool, ...]:
        """Tool order carries no meaning to the runtime, so it must not change
        the hash. Sorting here rather than in the canonical serialiser keeps the
        serialiser dumb and makes the decision visible on the model that owns it.
        """
        _reject_duplicate_names(value, "tools")
        return tuple(sorted(value, key=lambda tool: tool.name))

    @field_validator("mcp_servers")
    @classmethod
    def _servers_are_a_sorted_set(cls, value: tuple[McpServer, ...]) -> tuple[McpServer, ...]:
        _reject_duplicate_names(value, "mcp_servers")
        return tuple(sorted(value, key=lambda server: server.name))

    @field_validator("a2a_peers")
    @classmethod
    def _peers_are_a_sorted_set(cls, value: tuple[A2APeer, ...]) -> tuple[A2APeer, ...]:
        _reject_duplicate_names(value, "a2a_peers")
        return tuple(sorted(value, key=lambda peer: peer.name))

    @field_validator("skills")
    @classmethod
    def _skills_are_a_sorted_set(cls, value: tuple[Skill, ...]) -> tuple[Skill, ...]:
        _reject_duplicate_names(value, "skills")
        return tuple(sorted(value, key=lambda skill: skill.name))

    @field_validator("subagents")
    @classmethod
    def _subagents_are_a_sorted_set(cls, value: tuple[SubagentRef, ...]) -> tuple[SubagentRef, ...]:
        _reject_duplicate_names(value, "subagents")
        return tuple(sorted(value, key=lambda sub: sub.name))

    @model_validator(mode="after")
    def _tool_names_do_not_collide_with_builtins(self) -> Self:
        clashes = sorted({tool.name for tool in self.tools} & RESERVED_TOOL_NAMES)
        if clashes:
            raise ValueError(
                f"tool name(s) {clashes} are reserved for Psych built-ins; the model "
                "would see two tools with one name and could not address either"
            )
        return self


class AgentStep(_SpecModel):
    """A workflow step that runs an agent."""

    kind: Literal["agent"] = "agent"
    name: str = Field(pattern=_NAME_PATTERN)
    spec: AgentSpec


class ToolStep(_SpecModel):
    """A workflow step that calls one tool with fixed arguments."""

    kind: Literal["tool"] = "tool"
    name: str = Field(pattern=_NAME_PATTERN)
    tool: str = Field(pattern=_NAME_PATTERN)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkflowStepRef(_SpecModel):
    """A workflow step that runs a nested workflow."""

    kind: Literal["workflow"] = "workflow"
    name: str = Field(pattern=_NAME_PATTERN)
    spec: WorkflowSpec


WorkflowStep = Annotated[AgentStep | ToolStep | WorkflowStepRef, Field(discriminator="kind")]


class WorkflowSpec(_SpecModel):
    """A Step that sequences other Steps deterministically (DESIGN.md §5).

    A workflow step may be an agent, and an agent's tool may be a workflow, so
    recursion falls out and there is one durability implementation rather than
    two.
    """

    kind: Literal["workflow"] = "workflow"
    name: str = Field(pattern=_NAME_PATTERN)
    description: str = Field(default="", max_length=4096)
    steps: tuple[WorkflowStep, ...] = Field(min_length=1)
    """Order-bearing by definition: this is what 'sequences deterministically'
    means. Never sorted."""
    tools: tuple[AnyTool, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    limits: Limits = Field(default_factory=Limits)
    suspension: SuspensionPolicy = Field(default_factory=SuspensionPolicy)

    @field_validator("description")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_text(value)

    @field_validator("steps")
    @classmethod
    def _step_names_are_unique(cls, value: tuple[WorkflowStep, ...]) -> tuple[WorkflowStep, ...]:
        """Step ids derive from position, and a duplicate name makes a report
        ambiguous about which step a record belongs to."""
        _reject_duplicate_names(value, "steps")
        return value

    @field_validator("tools")
    @classmethod
    def _tools_are_a_sorted_set(cls, value: tuple[AnyTool, ...]) -> tuple[AnyTool, ...]:
        _reject_duplicate_names(value, "tools")
        return tuple(sorted(value, key=lambda tool: tool.name))

    @field_validator("mcp_servers")
    @classmethod
    def _servers_are_a_sorted_set(cls, value: tuple[McpServer, ...]) -> tuple[McpServer, ...]:
        _reject_duplicate_names(value, "mcp_servers")
        return tuple(sorted(value, key=lambda server: server.name))


Spec = Annotated[AgentSpec | WorkflowSpec, Field(discriminator="kind")]
"""The only runtime input. Chat, builder, file and import all converge here."""


RESERVED_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "load_skill",  # §16
        "read_tool_output",  # §10.8
        "remember",  # §15
        "forget",  # §15
        "ask_question",  # §11, psych_runtime.tools.builtins
        "update_tasks",  # psych_runtime.core.tasks
        "show_component",  # psych_runtime.core.components
        "delegate",  # §17
        "spawn_subagent",  # §17, composed at run time
        "check_subagent",
        "message_subagent",
        "run_code",  # §18
        "list_tools",  # deferred disclosure, psych_runtime.tools.deferred
        "get_tool_info",
        "call_tool",
    }
)
"""Names Psych's own built-in tools occupy. A Spec claiming one would put two
tools with the same name in front of the model, which can then address neither.

Kept here rather than imported from the modules that define them, because
``psych_runtime.core`` imports nothing from the other packages and import-linter
enforces that. The duplication is the cost of the layering, so
``tests/unit/test_builtin_names.py`` asserts the two agree: every built-in's
own name constant has to be in this set, and a built-in added without a line
here fails that test rather than silently shadowing a consumer's tool.
"""


class _Named(BaseModel):
    name: str


def _reject_duplicate_names(items: tuple[Any, ...], field: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        name = getattr(item, "name", None)
        if not isinstance(name, str):
            continue
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise ValueError(
            f"{field} contains duplicate name(s) {sorted(duplicates)}; names address "
            "entries in the log and in the model's context, so they must be unique"
        )


# Resolve the forward references created by the recursive step and subagent
# types. Without this, AgentSpec.subagents and WorkflowStepRef.spec stay strings.
SpawnEnvelope.model_rebuild()
SubagentRef.model_rebuild()
WorkflowStepRef.model_rebuild()
AgentSpec.model_rebuild()
WorkflowSpec.model_rebuild()
