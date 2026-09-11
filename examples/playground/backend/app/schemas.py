"""Request and response models for the playground's REST contract.

These are the HTTP boundary's own models, distinct from the Spec models
``psych`` validates: a ``CreateAgentRequest`` is turned into a
``psych_runtime.AgentSpec`` by ``app.main``, not used as one directly, so a request
shape can stay convenient (plain strings, plain dicts) without loosening
DESIGN.md §4's own rule that a Spec is data with nothing left to interpret.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.tools.deferred import MAX_SERVER_DESCRIPTION_CHARS


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


class McpServerHintOut(_ApiModel):
    name: str
    url: str
    transport: str
    oauth_grant: str | None = None


class ModelsResponse(_ApiModel):
    """``GET /api/models``: what the active provider will accept.

    ``models`` empty is not an error. ``known_models`` returns nothing when a
    provider will not say what it serves (DESIGN.md §19), which is the ordinary
    case for a proxy, so ``detail`` carries the reason in words a person can
    act on and the form stays typeable either way.
    """

    models: list[str]
    detail: str
    provider_label: str | None = None


class HealthResponse(_ApiModel):
    """``GET /api/health``: the one route that answers before sign-in.

    Deliberately says nothing about anybody. ``GET /api/config`` used to double
    as the liveness check, and cannot any more: what it reports is per account,
    so answering it to an anonymous caller would leak one account's connected
    servers to whoever asked first.
    """

    ok: bool
    store: str
    needs_first_account: bool
    """True when nobody has signed up yet, so the console can open on "create
    your account" rather than on a sign-in form for accounts that do not
    exist."""


class AccountResponse(_ApiModel):
    """Who is signed in. Never the password hash, and never a session token."""

    id: str
    email: str
    display_name: str


class SignUpRequest(_ApiModel):
    email: str
    password: str
    display_name: str = ""


class SignInRequest(_ApiModel):
    email: str
    password: str


class ConfigResponse(_ApiModel):
    model: str
    """The active provider's model, not the one this process booted with."""
    store: str
    mcp_servers: list[McpServerHintOut]
    has_api_key: bool
    provider_label: str | None = None
    """Which provider is active, by the name a person gave it."""
    account_id: str
    a2a_base_url: str = ""
    """Where this deployment's A2A door is, for a card URL somebody copies.
    Matches what the served cards advertise."""
    """The tenant this backend dispatches the caller's Runs as, and connects
    their MCP servers under: their own account id. Named in the response so the
    console does not have to derive it and then disagree with the backend about
    which connection a Run will use."""
    tracing: TracingOut
    """Whether spans are being collected, and where they went."""


class TracingOut(_ApiModel):
    """What `GET /api/config` says about traces.

    Reported at all because "off" and "on but you are looking in the wrong
    place" are different problems and look identical from a console that says
    nothing. Psych opens spans on every Run either way; this is only about
    whether anything downstream is listening.
    """

    enabled: bool
    endpoint: str | None = None
    """The OTLP endpoint spans go to, or `None` when nothing collects them.
    Not a secret: it is an address inside the operator's own deployment, and a
    console that would not say it cannot help anyone find their trace."""
    service_name: str
    """What to look for in the trace backend. A UI grouped by `service.name` is
    unnavigable if you do not know which name is yours."""


# ---------------------------------------------------------------------------
# POST /api/agents, GET /api/agents
# ---------------------------------------------------------------------------


class McpOAuthIn(_ApiModel):
    grant: Literal["authorization_code", "client_credentials"] = "client_credentials"
    preregistered_client_id: str | None = None
    client_secret_credential: str | None = None
    issuer: str | None = None
    cimd_url: str | None = None
    allow_dynamic_registration: bool = True
    application_type: Literal["native", "web"] = "native"
    client_name: str = "psych"


class McpServerIn(_ApiModel):
    name: str
    url: str
    transport: Literal["http", "sse"] = "http"
    """Carried into the published Spec. Dropped before, so a preset declaring
    the legacy HTTP+SSE transport published an agent that connected over plain
    HTTP -- and transport is part of the pool key, so the connection the
    settings page tested was not the one a Run would make."""
    credential: str | None = None
    allow: list[str] = Field(default_factory=list)
    optional: bool = False
    preload: bool | None = None
    """Carried into the published Spec as ``McpServer.preload``. ``None``
    defers a catalogue larger than ``psych_runtime.tools.deferred``'s threshold and
    preloads anything smaller; ``True`` and ``False`` say so outright."""
    oauth: McpOAuthIn | None = None


class A2APeerIn(_ApiModel):
    """One A2A peer on ``POST /api/agents``.

    Mirrors ``psych_runtime.A2APeer`` field for field, and deliberately declares
    nothing about what the peer can *do*: its skills come from its Agent Card
    at run time, because what a remote system offers is a fact about that
    system rather than about this agent (DESIGN.md §10.7). ``allow`` narrows
    those skills; empty grants all of them.
    """

    name: str
    url: str
    """The peer's A2A base URL, or its Agent Card URL directly."""
    credential: str | None = None
    """A credential *name* resolved through ``SecretResolver``, never a secret
    value: a Spec is versioned and readable by anyone who can read a Version."""
    scheme: str = "Bearer"
    tenant: str | None = None
    """The peer's opaque routing id, when its card's interface declares one."""
    allow: list[str] = Field(default_factory=list)
    optional: bool = False
    extensions: list[str] = Field(default_factory=list)


class SkillIn(_ApiModel):
    """One skill on ``POST /api/agents`` (DESIGN.md §16).

    Deliberately mirrors ``psych_runtime.Skill`` rather than wrapping it: the field
    names are the Spec's, so a person reading the API and a person reading the
    Spec are reading the same three words.

    The split between ``description`` and ``body`` is the whole mechanism and
    is worth stating wherever it is offered. The description goes into every
    system prompt of every turn, so it is charged for constantly and must stay
    one line. The body is charged for only when the model calls ``load_skill``,
    so it can be as long as the procedure actually is. Writing the procedure
    into ``description`` gets the cost without the benefit.
    """

    name: str
    description: str
    """One line, in the system prompt on every turn. What this covers, and
    when to reach for it."""
    body: str
    """The full instructions, loaded on demand. May reference another skill
    with ``[[skill:name]]``; a link to a skill this agent does not have is
    refused at publish, naming the link."""


class CompactionIn(_ApiModel):
    """When an agent replaces its older conversation with a summary.

    Mirrors ``psych_runtime.CompactionPolicy`` field for field, bounds included, so a
    value this API accepts is a value the publish accepts and a person reading
    the two is reading the same four words.

    The same model is what ``GET /api/agents`` reports back, rather than a
    second read-only copy of the same four fields: the console fills its edit
    form from that list, and two shapes that had to agree would eventually
    stop agreeing on the field an edit then silently dropped.

    ``None`` on an agent means compaction is off, which is the default, and
    which is not the same as an object with small numbers in it.
    """

    trigger_tokens: int = Field(gt=0)
    """Compact once the provider's own input count for the last model call
    reaches this many tokens.

    Required, and with no default here either, for the reason the library
    gives: Psych has no context window to take a fraction of, so any number
    invented here would be a guess about somebody else's model. The console's
    form starts the field at a number and says so; this API does not.
    """
    keep_recent_turns: int = Field(default=3, gt=0, le=100)
    model: str | None = None
    """Which model writes the summary. ``None`` means the agent's own."""
    max_summary_tokens: int = Field(default=2_048, gt=0, le=32_000)
    summary_instructions: str | None = Field(default=None, max_length=4_000)
    """What this agent additionally needs kept, on top of what Psych keeps.

    Added to Psych's own summary rules rather than replacing them, which is the
    library's own wording and is worth repeating wherever this is offered: a
    terse "keep every order number" read as the whole brief would throw away
    the constraints and the outstanding work.
    """


class HttpToolIn(_ApiModel):
    """One ``psych_runtime.HttpTool`` on ``POST /api/agents``: a REST endpoint the
    agent may call without anyone writing a Python function for it.

    ``credential`` is a *name* the secret resolver looks up at call time, never
    a value, and ``headers`` may not carry ``Authorization``: a Spec is
    exported and reviewed in git, so a literal token in one is refused by the
    library itself.
    """

    name: str
    description: str = Field(min_length=1, max_length=4096)
    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    credential: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    interruptible: bool = True


class SubagentRefIn(_ApiModel):
    """One author-written subagent (DESIGN.md §17): an existing agent of this
    account, embedded into the parent's Spec under ``name`` so one Version hash
    pins the whole delegation tree. The parent is offered ``delegate`` for it.

    ``agent_id`` is resolved to that agent's *current* Version at publish and
    copied in. Editing the child later does not move this parent: a Version is
    content, and a parent that silently changed when a child did would break
    crash recovery, which replays the pinned hash.
    """

    name: str
    description: str = Field(min_length=20, max_length=2048)
    agent_id: str


class SpawnIn(_ApiModel):
    """The ``psych_runtime.SpawnEnvelope``: a permission, never a roster. What a
    child the *model* composes at run time may be given."""

    tools: list[str] = Field(default_factory=list)
    """Names, or trailing-``*`` patterns, out of the parent's own tools. Empty
    means everything the parent holds. A ceiling, never a grant."""
    models: list[str] = Field(default_factory=list)
    """Models a child may run on. Empty means the parent's own and no other."""
    max_depth: int = Field(default=2, ge=1, le=16)
    max_alive: int = Field(default=3, ge=1, le=32)
    may_message: bool = True


class SuspensionIn(_ApiModel):
    """``psych_runtime.SuspensionPolicy`` less ``may_ask_questions``, which the
    request carries at the top level for the sake of every client written
    before these four expiries were exposed."""

    approval_expires_seconds: float = Field(default=86_400.0, gt=0)
    question_expires_seconds: float = Field(default=86_400.0, gt=0)
    external_expires_seconds: float = Field(default=604_800.0, gt=0)
    children_expires_seconds: float = Field(default=3_600.0, gt=0)


class ModelOptionsIn(_ApiModel):
    """The rest of ``psych_runtime.ModelRef``: sampling and the fallback chain."""

    top_p: float | None = Field(default=None, gt=0, le=1)
    max_output_tokens: int | None = Field(default=None, gt=0)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    fallbacks: list[str] = Field(default_factory=list)
    """Tried in order on a *transient* failure of the model before them. Not a
    router: a model that answers badly is never swapped for another."""


class CreateAgentRequest(_ApiModel):
    agent_id: str | None = None
    """Which existing agent this publishes a new Version of.

    ``None`` creates an agent. Naming one edits it: the Spec is published as
    always -- a Version is still immutable and still content-hashed -- and the
    agent's pointer moves to it. Nothing about an already-running Run changes,
    because a Run pinned its hash at admission.
    """
    name: str
    description: str = Field(default="", max_length=4096)
    """What this agent is for, for a person browsing a list of them and for a
    peer reading its A2A card. Not in the prompt."""
    instructions: str
    model: str
    temperature: float | None = None
    model_options: ModelOptionsIn | None = None
    tools: list[str] = Field(default_factory=list)
    http_tools: list[HttpToolIn] = Field(default_factory=list)
    skills: list[SkillIn] = Field(default_factory=list)
    mcp: list[McpServerIn] = Field(default_factory=list)
    a2a: list[A2APeerIn] = Field(default_factory=list)
    subagents: list[SubagentRefIn] = Field(default_factory=list)
    """The delegation roster (DESIGN.md §17). Distinct from ``spawn`` below:
    these are children an author named, called through ``delegate`` and
    blocking; ``spawn`` permits children the model composes itself."""
    spawn: SpawnIn | None = None
    """The envelope published when ``subagents_enabled`` is on. ``None`` with
    the flag on publishes the library's defaults, which is what every client
    before this field existed gets."""
    suspension: SuspensionIn | None = None
    """Other agents this one may call over A2A. Part of the published Spec and
    so part of its Version hash: which agents an agent may delegate to is part
    of what it is."""
    limits: dict[str, Any] | None = None
    approval_selectors: list[str] | None = None
    may_ask_questions: bool = False
    """Whether this agent is offered ``ask_question``. Off by default: an agent
    that can park a conversation waiting for a person is a different agent from
    one that cannot, so it joins the Version hash rather than being a runtime
    convenience."""
    tasks_enabled: bool = False
    """Whether this agent keeps a plan with ``update_tasks``. Off by default;
    joins the Version hash, since an agent that keeps one writes a different
    prompt on every turn once it has."""
    components_enabled: bool = False
    """Whether this agent may answer with a card, a chart or a timeline as well
    as with prose. Off by default; joins the Version hash, since the agent is
    offered a different tool set on every turn once it is on."""
    subagents_enabled: bool = False
    """Whether this agent may write its own subagents and run them in the
    background (DESIGN.md §17).

    Off by default. On, it publishes a ``psych_runtime.SpawnEnvelope`` granting the
    agent its own tools and its own model for any child it composes -- an
    envelope, not a roster: what a child may be given, never which children
    exist. It joins the Version hash like every other permission, so an agent
    that can compose subagents is a different agent from one that cannot."""
    compaction: CompactionIn | None = None
    """Whether this agent summarises its older conversation once a prompt gets
    large, instead of letting it reach the context window.

    ``None``, the default, means it does not. On, it publishes a
    ``psych_runtime.CompactionPolicy``, which joins the Version hash like every other
    field here: an agent that is shown a summary in place of its own older
    turns is not being shown the same conversation as one that is not."""
    answer_style: Literal["concise"] | None = None
    """How the agent shapes its final answer. Part of the published Spec and so
    part of its Version hash, deliberately: it changes what the model is told.
    ``None`` adds nothing to the prompt."""


class ToolStepIn(_ApiModel):
    kind: Literal["tool"] = "tool"
    name: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentStepIn(_ApiModel):
    kind: Literal["agent"] = "agent"
    name: str
    agent_id: str


class NestedWorkflowStepIn(_ApiModel):
    kind: Literal["workflow"] = "workflow"
    name: str
    workflow_id: str


WorkflowStepIn = ToolStepIn | AgentStepIn | NestedWorkflowStepIn


class CreateWorkflowRequest(_ApiModel):
    """``POST /api/workflows``: DESIGN.md §5's fixed pipeline.

    Steps are tool calls with their arguments written down, agents named by
    id, or other workflows named by id; the latter two are copied in at their
    current Version (see ``app.workflows``). ``workflow_id`` names an existing
    workflow to publish a new Version of, exactly as ``agent_id`` does for an
    agent.
    """

    workflow_id: str | None = None
    name: str
    description: str = Field(default="", max_length=4096)
    steps: list[WorkflowStepIn] = Field(min_length=1)
    limits: dict[str, Any] | None = None


class WorkflowStepOut(_ApiModel):
    kind: Literal["tool", "agent", "workflow"]
    name: str
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    workflow_id: str | None = None
    version_hash: str | None = None
    """For an embedded agent or workflow: the child hash this Version pins,
    which may be older than what that agent or workflow runs today."""


class WorkflowSummary(_ApiModel):
    workflow_id: str
    version_hash: str
    name: str
    description: str = ""
    steps: list[WorkflowStepOut]
    tools: list[str]
    limits: dict[str, Any] = Field(default_factory=dict)
    published_at: str
    created_at: str
    updated_at: str
    version_count: int


class CreateWorkflowResponse(_ApiModel):
    workflow_id: str
    version_hash: str
    name: str
    created: bool


class CreateAgentResponse(_ApiModel):
    agent_id: str
    version_hash: str
    name: str
    created: bool
    """``True`` when this call created the agent, ``False`` when it published a
    new Version of one that already existed."""


class SkillOut(_ApiModel):
    """One skill as ``GET /api/agents`` reports it.

    Carries the body as well as the description, because the agent list is
    what the edit form is filled in from: an agent whose skills came back
    without their bodies could not be edited without silently emptying them.
    """

    name: str
    description: str
    body: str


class SubagentRefOut(_ApiModel):
    name: str
    description: str
    agent_id: str
    """The agent the child was copied from, or ``""`` when that agent no longer
    exists or the child was published without one."""
    version_hash: str
    """The child's own content hash, which is what the parent actually pins."""


class AgentSummary(_ApiModel):
    """One agent, described by the Version it currently runs.

    ``agent_id`` is the identity: stable across every edit, and what the
    console routes and dispatches by. ``version_hash`` is what it happens to
    run today and changes whenever somebody edits it.
    """

    agent_id: str
    version_hash: str
    name: str
    description: str = ""
    answer_style: Literal["concise"] | None = None
    instructions: str
    model: str
    model_options: ModelOptionsIn | None = None
    tools: list[str]
    http_tools: list[HttpToolIn] = Field(default_factory=list)
    skills: list[SkillOut]
    mcp_servers: list[str]
    subagents: list[SubagentRefOut] = Field(default_factory=list)
    spawn: SpawnIn | None = None
    suspension: SuspensionIn | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    """Every ``Limits`` field as published, so the edit form restores the
    numbers rather than this form's defaults."""
    approval_selectors: list[str] | None = None
    """What this agent's Runs are admitted under, or ``None`` for the
    installation default."""
    a2a_peers: list[str] = Field(default_factory=list)
    """By name only, like ``mcp_servers``. A published Spec carries its own
    copy of every peer's URL and credential name, and this catalogue reports
    neither."""
    may_ask_questions: bool = False
    tasks_enabled: bool = False
    components_enabled: bool = False
    subagents_enabled: bool = False
    """Reported back so the edit form can restore them. All three join the
    Version hash, so a form that could not read them published a different
    agent from the one somebody meant to edit."""
    compaction: CompactionIn | None = None
    """The agent's compaction policy, or ``None`` when it has none.

    Reported back whole rather than as a bare "on" flag, and for the same
    reason the skills carry their bodies: an edit form told only that
    compaction was on would have to invent the four numbers again, and would
    publish an agent that summarises on different terms from the one somebody
    opened."""
    published_at: str
    """When the *current* Version was first published. Not when the agent was
    created, and not when it was last edited: a Version republished after a
    round trip through an earlier configuration keeps its original timestamp,
    which is correct for a Version and misleading for an agent."""
    created_at: str
    updated_at: str
    """When the pointer last moved. This is the "last edited" a person means."""
    version_count: int
    """How many distinct configurations this agent has had, current included."""


class AgentVersionSummary(_ApiModel):
    """One entry in an agent's history.

    Deliberately thinner than ``AgentSummary``: this list exists to answer
    "what has this agent been", and a page rendering every superseded Version's
    full instructions and skill bodies would bury the one row that matters.
    """

    version_hash: str
    name: str
    model: str
    published_at: str
    current: bool


# ---------------------------------------------------------------------------
# POST /api/runs, GET /api/runs
# ---------------------------------------------------------------------------


class DispatchRequest(_ApiModel):
    """What a caller may say about a Run they are starting.

    There is no ``tenant`` field, and its absence is the point. This model used
    to carry one and ``POST /api/runs`` used to build its ``Scope`` from it, so
    any caller could name any tenant and read anybody's Runs. The tenant now
    comes from the verified session and from nowhere else. Validating a
    supplied tenant would have been an improvement; not accepting one is the
    fix, because a field that exists is a field somebody eventually trusts.
    """

    agent_id: str | None = None
    """The agent to talk to. Resolved to whatever Version it points at right
    now, once, and that hash is what the Run pins.

    The normal way in. ``version_hash`` is the other way, for pinning one
    deliberately -- replaying an older configuration, or continuing a thread on
    the Version it started on. Exactly one of the two.
    """
    version_hash: str | None = None
    workflow_id: str | None = None
    """A workflow to run instead of an agent. The third way in, exclusive with
    the other two: its current Version is what the Run pins, and the Worker
    drives the workflow engine rather than the agent loop because the Version
    says so, not because this route did anything different."""
    message: str
    continues_run_id: str | None = None
    """A prior Run this message continues, for a second turn in the same
    conversation, omitted for a thread's first message. Passed
    straight through to ``psych_runtime.dispatch(continues=...)``, which refuses a Run
    in a different Scope itself: naming somebody else's Run here fails inside
    Psych, before this backend's own check would have to."""


class BranchRequest(_ApiModel):
    """Re-ask one message of a conversation, keeping the old answer too.

    A route of its own rather than a fourth field on ``DispatchRequest``,
    because the thing being branched is a Run and a Run already has a URL. The
    alternative -- ``branch_from_run_id`` beside ``continues_run_id`` -- puts
    two fields on one body that mean incompatible things about the same chain,
    and nothing but a hand-written check would stop a caller setting both.

    ``message`` is the question to ask instead, and it is the only field. There
    is deliberately no ``agent_id``: a branch is another wording of a question
    inside one conversation, so the agent answering it does not change. Putting
    one question to two agents is ``ForkRequest``.
    """

    message: str


class ForkRequest(_ApiModel):
    """Take a conversation somewhere else from one of its messages, as a new chat.

    Same divergence point as ``BranchRequest`` and the same shared history. The
    difference is scope, and it is the whole difference: a fork is a separate
    conversation with its own row in the history list, deleted on its own, and
    left standing when the conversation it came from is deleted.

    ``agent_id`` is optional and is what branching does not offer: a fork is a
    new conversation, so it may run a *different* agent, which is how one
    question gets two answers side by side in two chats. Omitted, the fork
    stays on the agent and the Version the forked Run was running, the same way
    a continuation does.
    """

    message: str
    agent_id: str | None = None


class DispatchResponse(_ApiModel):
    run_id: str


class SubagentNodeOut(_ApiModel):
    """One subagent in a Run's tree, with its branch rolled up.

    Built from ``psych_runtime.report(child_depth=...)``, which reads it out of the
    parent's own log: every spawn, message and ending is a Record, so a console
    watching a tree is reading the same thing a report six months later will.

    Attributes:
        state: the child's own lifecycle -- ``running``, ``waiting``, ``done``,
            ``failed``, ``stopped`` -- read from the child's Run rather than
            inferred from whether its parent has heard back yet. The two differ
            for exactly as long as a notification is in flight, which is the
            window a person watching is most likely to be looking at.
        subtree_*: this child and its own descendants. A branch is what a person
            reading a tree means by "what did that cost".
        complete: whether the rollup counted everything. False while a branch is
            still running, or when the walk stopped before the bottom.
    """

    name: str
    run_id: str
    parent_run_id: str
    state: str
    terminal_state: str | None = None
    purpose: str
    task: str
    deliverable: str
    tools: list[str]
    model: str
    depth: int
    spawned_at: str
    finished_at: str | None = None
    messages_sent: int
    latest: str = ""
    """The child's answer, or the last thing it said on the way to one."""
    error: str | None = None
    input_tokens: int
    output_tokens: int
    cost: str | None = None
    """Decimal as a string. ``None`` means the models had no known price, never
    zero: a silent zero makes metering look correct and be wrong."""
    subtree_input_tokens: int
    subtree_output_tokens: int
    subtree_cost: str | None = None
    subtree_runs: int
    complete: bool
    children: list[SubagentNodeOut] = Field(default_factory=list)


class SubagentTreeResponse(_ApiModel):
    """A Run and everything it spawned, for the console's tree view."""

    run_id: str
    state: str
    children: list[SubagentNodeOut]
    total_input_tokens: int
    total_output_tokens: int
    total_cost: str | None = None
    """The whole tree, this Run included."""
    complete: bool


class SubagentMessageRequest(_ApiModel):
    """A message for a running child.

    Delivered at the start of its next turn, never into the tool call it is in
    the middle of: the side effect of that call has already happened, and
    cancelling it would only lose the record of whether it did.
    """

    message: str = Field(min_length=1, max_length=8192)


class SubagentRetryResponse(_ApiModel):
    """What retrying a subagent produced.

    A **new Run** of the same pinned Version with the same input, not a second
    attempt at the old one: a Run is admitted once and a Record log is
    append-only, so "run that again" is a new log rather than a rewritten one.
    It stands on its own, outside the original tree, because its parent's log
    was written by an Attempt that has long since finished and nothing may
    append a spawn to it now.
    """

    run_id: str
    retried_from: str
    version_hash: str


class RunSummary(_ApiModel):
    run_id: str
    agent_id: str = ""
    workflow_id: str = ""
    """Set instead of ``agent_id`` for a workflow's Run."""
    kind: Literal["agent", "workflow"] = "agent"
    """Which agent this Run belongs to, recorded at admission.

    Not derivable from ``version_hash``: a Version is content, so two agents
    built from the same Spec share one, and attributing by hash puts one
    agent's conversations under the other. Empty for a Run dispatched before
    agents had identities, which a caller can only fall back to matching by
    hash for."""
    conversation_id: str = ""
    """Which conversation this Run belongs to.

    The history list shows one row per conversation id, not one per branch. A
    conversation is the whole tree: every branch of it shares this, because
    re-asking a question is still the same chat. A fork takes a new one,
    because a fork is a new chat.

    Empty for a Run dispatched before conversations had ids, which a caller
    should read as the root of that Run's own chain."""
    branch_id: str = ""
    """Which branch of its conversation this Run is on.

    A conversation branched at a message has two futures from that point, and
    ``continues_run_id`` alone cannot say which one a Run is in: the branch
    point has two children and a walk forward would pick either. Runs sharing a
    branch id are one readable line through that tree, which is what a console
    pages between with the ``1/2`` arrows on a branched message.

    Empty for a Run dispatched before conversations could branch. Those cannot
    have branched, so grouping them by the root of their chain -- which is what
    an empty branch means to a caller -- is exactly right."""
    name: str
    tenant: str
    state: str
    started_at: str
    version_hash: str
    """The Version this Run pinned at admission, and still runs whatever its
    agent has been edited to since."""
    message: str
    """The dispatch input, verbatim (``DispatchRequest.message``), so a chat
    history list can render its first line without fetching every run's
    messages."""
    settled_at: str | None = None
    """``None`` while the Run has not settled. See ``app.serialization.settled_at``."""
    continues_run_id: str | None = None
    """The Run this one continues, or ``None`` when it opened its thread
    A conversation is a chain of Runs rather than one long-lived
    Run -- ``psych_runtime.runtime.thread``'s docstring says why -- so a history list
    that ignores this field shows every turn of one conversation as a separate
    entry. Read from ``RunHeader``, which ``GET /api/runs`` already fetches."""


class MessageOut(_ApiModel):
    """One entry of ``GET /api/runs/{run_id}/messages``.

    Built from ``psych_runtime.core.conversation.build_conversation`` -- see
    ``app.serialization.build_message_views`` for exactly how.
    """

    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None
    seq: int
    at: str


class ThreadMessageOut(MessageOut):
    """One entry of ``GET /api/runs/{run_id}/thread``.

    ``seq`` is only unique within one Run's log, so a thread flattening
    several Runs' messages needs the Run each came from to key them apart --
    that is the whole of what this adds over ``MessageOut``.
    """

    run_id: str


class ToolCallOut(_ApiModel):
    """One tool call inside a run's collapsed working section."""

    call_id: str
    tool: str
    arguments: dict[str, Any]
    outcome: str | None = None
    result: Any = None
    failure_message: str | None = None
    duration_seconds: float | None = None
    started_at: str
    finished_at: str | None = None


class WorkTurnOut(_ApiModel):
    turn: int
    text: str = ""
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    failure_message: str | None = None
    at: str


class AnswerResponse(_ApiModel):
    """``GET /api/runs/{run_id}/answer``: ``psych_runtime.answer()``, serialised.

    The other way to read a run. ``/thread`` is the conversation in order,
    which is what a transcript is; this is the answer with the work behind it,
    which is what someone who asked a question wants to see first.
    """

    run_id: str
    text: str = ""
    """Empty when the run has not reached an answer. ``finished`` says which,
    and the reason is on ``/status``."""
    work: list[WorkTurnOut] = Field(default_factory=list)
    finished: bool = False
    summary: str = ""
    """One line for the collapsed section, or empty when there is no work."""
    tool_call_count: int = 0


class ThreadResponse(_ApiModel):
    """``GET /api/runs/{run_id}/thread``: one conversation, across every Run
    in its chain."""

    run_ids: list[str]
    """Oldest first, ending at the Run that was asked for. What a UI needs to
    tell which history entries collapse into this one thread."""
    messages: list[ThreadMessageOut]
    """Every message in the thread, oldest first."""


# ---------------------------------------------------------------------------
# Resume / interrupt
# ---------------------------------------------------------------------------


class ResumeRequest(_ApiModel):
    """Answering a Run that stopped for a person.

    Two shapes, because a suspension has two kinds. An approval takes
    ``approved``; a question takes ``payload``. Neither is required, so a
    caller answering a question does not have to send a verdict on a call
    nobody was asked to approve, which would land a meaningless ``approved``
    on the ``resumed`` record.
    """

    approved: bool | None = None
    payload: dict[str, Any] | None = None
    """The person's answer, for a Run waiting on ``ask_question``. Passed
    straight to ``psych_runtime.resume(payload=...)``; the runtime reads ``answer`` or
    ``answers`` out of it (``psych_runtime.runtime.agent._settle_question``)."""
    by: str | None = None
    """Who decided. Recorded on the Run's ``resumed`` record: an approval of a
    destructive call whose log cannot say who approved it is not an audit
    trail. This playground has no login, so it is whatever the caller says;
    a real consumer fills it from their own identity system."""


class SendRequest(_ApiModel):
    """``POST /api/runs/{run_id}/send``: a message into a Run still executing.

    DESIGN.md §9's three queues. ``steer`` reaches the turn running now,
    ``follow_up`` is read once the current turn settles, and ``next_run`` is
    handed to whatever Run continues this one -- the only queue that is legal
    after an interrupt. A message for a Run that has already settled is not
    any of these; that is ``POST /api/runs`` with ``continues_run_id``.
    """

    message: str = Field(min_length=1, max_length=32_768)
    queue: Literal["steer", "follow_up", "next_run"] = "steer"


class SendResponse(_ApiModel):
    entry_id: str
    queue: Literal["steer", "follow_up", "next_run"]


class InterruptRequest(_ApiModel):
    reason: str = ""


class OkResponse(_ApiModel):
    ok: Literal[True] = True


# ---------------------------------------------------------------------------
# GET /api/tools
# ---------------------------------------------------------------------------


class ToolOut(_ApiModel):
    """One entry of ``GET /api/tools``, derived from a live ``RegisteredTool``
    (``psych_runtime.tools.registry``) -- never a hardcoded list, so this always
    matches what the running process would actually resolve a call against."""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: list[str]
    interruptible: bool
    safe_to_retry: bool
    source: Literal["code"] = "code"
    """Every tool this registry holds is a registered Python function
    (DESIGN.md §10.1); an MCP-discovered tool is never in this registry at
    all (``psych_runtime.tools.mcp`` resolves those separately, per-turn, against a
    live server catalogue), so ``"code"`` is not a guess -- it is the only
    value this endpoint can ever produce."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class ProviderIn(_ApiModel):
    """One entry of ``PUT /api/settings/providers``'s ``providers`` array.

    ``id`` omitted (or naming a provider that does not exist yet) creates a
    new provider. ``api_key`` omitted keeps whatever is already stored for
    that ``id``; ``api_key=""`` clears it; anything else replaces it. There is
    no way to *read* a stored key back through this API -- see ``ProviderOut``.
    """

    id: str | None = None
    label: str
    base_url: str
    model: str
    api_key: str | None = None


class ProviderOut(_ApiModel):
    id: str
    label: str
    base_url: str
    model: str
    has_api_key: bool
    """Never the key itself -- a test asserts this endpoint cannot leak one."""


class McpServerPresetIn(_ApiModel):
    """One entry of ``PUT /api/settings/mcp``'s ``mcp_servers`` array.

    A preset the agent builder offers, not a live connection and not part of
    any published Spec -- see ``app.main``'s route docstring for why a Spec
    still carries its own copy of whatever this produces.
    """

    name: str
    url: str
    description: str = Field(default="", max_length=MAX_SERVER_DESCRIPTION_CHARS)
    """What this system is for. Reaches the model at run time, and is
    deliberately not part of any published agent: see
    ``app.settings_store.McpServerPreset.description``.

    Bounded here as well as in the console's own form. Psych truncates
    anything longer before it reaches a prompt
    (``psych_runtime.tools.deferred.summarise_description``), so an oversized value was
    never a risk to the model. It was a risk to the person: they would type
    six hundred characters, the form would say four hundred, the API would
    accept all six, and the prompt would quietly carry the first four. Refusing
    it at the boundary means the stored value and the sentence the agent reads
    are the same sentence."""
    transport: Literal["http", "sse"] = "http"
    credential: str | None = None
    allow: list[str] = Field(default_factory=list)
    optional: bool = False
    preload: bool | None = None
    oauth: McpOAuthIn | None = None


class McpConnectionOut(_ApiModel):
    """The last connection attempt to one preset, as stored."""

    ok: bool
    detail: str
    checked_at: str
    tools: list[str] = Field(default_factory=list)
    error_type: str | None = None


class McpServerPresetOut(_ApiModel):
    name: str
    url: str
    description: str = ""
    transport: str
    credential: str | None = None
    allow: list[str]
    optional: bool
    preload: bool | None = None
    oauth: McpOAuthIn | None = None
    last_connection: McpConnectionOut | None = None
    """What happened the last time anyone connected to this server, so the UI
    shows a connected server as connected after a refresh instead of offering
    to connect it again."""
    live: McpLiveConnectionOut | None = None
    """The pooled connection this process is holding right now, if any. From
    ``psych_runtime.McpPool.connections()``: not a second attempt, a read of the one
    that exists."""


class McpLiveConnectionOut(_ApiModel):
    """One live pooled connection, from ``McpConnectionStatus``."""

    tenant: str
    server: str
    url: str
    transport: str
    era: str | None
    tool_count: int
    catalogue_age_seconds: float | None
    catalogue_ttl_seconds: float


class McpTestResponse(_ApiModel):
    """``POST /api/settings/mcp/{name}/test``: one real connection attempt.

    Deliberately shaped like ``ProviderTestResponse``: a person wants the same
    question answered -- does this actually work, and if not, why -- before
    publishing an agent against it.
    """

    ok: bool
    detail: str
    """The real error text when ``ok`` is false, never a generic 'connection
    failed'. An MCP server answering with ``text/html`` (a redirect page, a
    login form, a proxy block) is the common misconfiguration, and the only
    way a person can tell it apart from a bad credential is to be shown it."""
    tool_count: int | None = None
    """How many tools the server offered, once connected. ``None`` when the
    connection never got that far."""
    tools: list[str] | None = None
    """Their names, so a person can see whether the allow-list they are about
    to write matches what is really there."""
    error_type: str | None = None
    """The exception's class name when ``ok`` is false, from
    ``psych_runtime.McpProbe``. Lets the UI say "no credential stored for this server"
    rather than relaying an exception message, without matching on text."""


class PendingAuthorizationOut(_ApiModel):
    """One entry of ``GET /api/oauth/pending``: an ``authorization_code``
    grant waiting for a browser.

    Carries no token material and no ``code`` -- only the URL to send a person
    to. ``state`` is included because it is what the callback correlates on,
    and the UI needs it to tell one pending authorization from another.
    """

    state: str
    authorization_url: str
    tenant: str
    started_at: str


class UpdateProvidersRequest(_ApiModel):
    providers: list[ProviderIn]


class UpdateMcpServersRequest(_ApiModel):
    mcp_servers: list[McpServerPresetIn]


class UpdateSecretsRequest(_ApiModel):
    secrets: dict[str, str]


class SecretsResponse(_ApiModel):
    secrets: list[str]
    """Names only. See ``ProviderOut.has_api_key`` for the same rule applied
    to a provider's key -- a secret's value never appears in any response
    this API returns."""


class MemoryOut(_ApiModel):
    """One durable fact the agent remembered (DESIGN.md §15)."""

    id: str
    content: str
    created_at: str


class MemoriesResponse(_ApiModel):
    """``GET /api/memories``: what this account's agents still know.

    Oldest first, in full. Not a search result: DESIGN.md §15 draws the line at
    retrieval, and `recall` returns every fact rather than the relevant ones.
    """

    memories: list[MemoryOut]
    end_user_id: str
    """Whose facts these are. The account's own id in this console, and named
    in the response because a consumer serving real end users would see a
    different one here for each of their customers."""


class ThreadTotalsOut(_ApiModel):
    """One conversation's totals, summed across every Run in its chain.

    A conversation is many Runs: each message continues the previous
    Run rather than extending it. So every per-Run number the console used to
    lead with was one message's share of something bigger, which is the wrong
    figure to put at the top of a page about a conversation.

    ``wall_clock_seconds`` is deliberately **not** a sum. It is measured from
    the first message to the last settlement, so it includes the gaps where a
    person was reading and typing. Summing the Runs instead would report a two
    minute exchange as eight seconds of work, which is true of the machine and
    false of the conversation.
    """

    messages: int
    """Runs in the chain. One per message somebody sent."""
    model_calls: int
    failed_model_calls: int
    tool_calls: int
    compaction_calls: int = 0
    """Summaries written to keep the conversation under the context window.

    Not a turn and not counted as one: a compaction is a model call the agent
    did not ask for, so it sits beside ``model_calls`` rather than inside it.
    Its tokens and its cost are already in the figures above, which is why a
    conversation whose bill grew without its answers getting longer is only
    explicable with this number on the page."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    """Counted inside ``output_tokens`` by every provider that reports them, so
    never add this to the total: it is a breakdown, not a fifth bucket."""
    total_tokens: int
    usage_source: Literal["provider", "partial", "unknown"] = "provider"
    unreported_usage_calls: int = 0
    """Finished calls whose provider response omitted usage entirely."""
    cost_amount: str | None = None
    """A decimal string, never a float: this is money summed across calls.
    ``None`` when not one call in the conversation had a known price."""
    cost_currency: str | None = None
    cost_source: str | None = None
    """Where the figure came from: ``provider`` when the gateway reported it,
    ``computed`` when Psych derived it from a price table, ``mixed`` when the
    conversation used both. Named rather than blended, because reconciling
    against an invoice needs to know which rows came from where."""
    provider_reported_costs: int = 0
    cost_input_amount: str | None = None
    cost_output_amount: str | None = None
    cost_cache_read_amount: str | None = None
    cost_cache_write_amount: str | None = None
    """Per-category money, present only when every priced call supplied a
    breakdown. Provider totals commonly omit it; ``None`` is honest there."""
    unpriced_model_calls: int
    cost_is_incomplete: bool
    """True when at least one call had no known rate. Keeps "this cost $0.02"
    and "this cost $0.02 plus three calls I could not price" from reading the
    same (DESIGN.md §13.2)."""
    wall_clock_seconds: float
    model_seconds: float
    tool_seconds: float


class ThreadReportResponse(_ApiModel):
    """``GET /api/threads/{run_id}/report``: a whole conversation at once.

    Carries the totals *and* every Run's own report, so the console can show
    the conversation by default and one message on demand without a second
    round trip per message. A conversation is a handful of Runs, so this is one
    request rather than N.
    """

    run_ids: list[str]
    """Oldest first, root included."""
    totals: ThreadTotalsOut
    reports: list[Any]
    """Each Run's own ``psych_runtime.report()`` output, in the same order as
    ``run_ids``. Untyped here for the same reason ``GET /api/runs/{id}/report``
    is: the report is the library's model and restating its shape in this file
    would be two definitions to keep in step."""
    messages: list[ThreadMessageOut]
    """The conversation itself, oldest message first, from ``psych_runtime.thread()``.
    Lets the timeline mark where each message begins without the console
    stitching it together from two endpoints."""


class ModelPriceIn(_ApiModel):
    """One model's rates, per million tokens, on ``PUT /api/settings/prices``.

    Decimals rather than floats all the way down: a rate of ``0.075`` per
    million multiplied by a few million tokens is exactly the arithmetic
    binary floating point gets visibly wrong, and this number is money.
    """

    model: str
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_read: Decimal = Field(default=Decimal(0), ge=0)
    cache_write: Decimal = Field(default=Decimal(0), ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class ModelPriceOut(ModelPriceIn):
    """As stored. Same shape: a rate is not a secret, so unlike an API key
    there is nothing here to withhold on the way back out."""


class UpdateModelPricesRequest(_ApiModel):
    prices: list[ModelPriceIn]


class A2APeerPresetOut(_ApiModel):
    """One saved peer, as ``GET /api/settings`` reports it. Never a secret:
    ``credential`` is the name of one, which is what a Spec carries too."""

    name: str
    url: str
    description: str = ""
    credential: str | None = None
    scheme: str = "Bearer"
    tenant: str | None = None
    allow: list[str] = Field(default_factory=list)
    optional: bool = False
    extensions: list[str] = Field(default_factory=list)


class A2ATokenRequest(_ApiModel):
    """``POST /api/settings/a2a/tokens``: a bearer token another agent presents
    to call this account's agents over A2A, saved under ``secret_name`` so a
    peer preset can name it. The value is returned exactly once."""

    secret_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    ttl_days: int = Field(default=365, ge=1, le=3650)


class A2ATokenResponse(_ApiModel):
    secret_name: str
    token: str
    expires_at: str


class LocalPeerRequest(_ApiModel):
    """``POST /api/settings/a2a/peers/local``: one of this account's own agents,
    added as an A2A peer of this account's other agents.

    The playground calling itself over A2A is the honest way to show the
    protocol working end to end without a second deployment: the peer's card
    is fetched from this process, its skills are read from that card, and the
    call is a real ``message/send`` over loopback, authenticated with a token
    this account minted (``secret_name``; minted here when it does not exist).
    """

    agent_id: str
    name: str | None = None
    """The peer's name in a Spec. Defaults to the agent's own name."""
    secret_name: str = Field(default="a2a-self", pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = ""


class UpdateA2APeersRequest(_ApiModel):
    """Replace this account's A2A peer presets wholesale, like the MCP list."""

    a2a_peers: list[A2APeerPresetOut]


class SandboxLimitsIn(_ApiModel):
    cpu_seconds: float = Field(default=10.0, gt=0, le=3_600)
    address_space_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    file_size_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    process_count: int = Field(default=64, gt=0, le=4_096)
    wall_seconds: float = Field(default=30.0, gt=0, le=3_600)


class RuntimeSettingsIn(_ApiModel):
    """``PUT /api/settings/runtime``: the ``Runtime`` knobs this account sets.
    Mirrors ``app.settings_store.RuntimeSettings`` field for field."""

    cost_policy: Literal["prefer_provider", "computed", "provider_only"] = "prefer_provider"
    blob_offload_bytes: int = Field(default=300_000, gt=0)
    catalogue_budget_chars: int = Field(default=20_000, gt=0)
    sandbox_enabled: bool = True
    sandbox_limits: SandboxLimitsIn = Field(default_factory=SandboxLimitsIn)
    egress_allow: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)


class RuntimeSettingsOut(RuntimeSettingsIn):
    sandbox_available: bool = True
    """Whether this host can offer ``run_code`` at all. ``sandbox_enabled`` is
    the account's wish; this is the process's answer."""
    sandbox_unavailable_reason: str | None = None


class UpdateSkillsRequest(_ApiModel):
    """Replace this account's skill library wholesale.

    The same shape ``PUT /api/settings/mcp`` uses, for the same reason: the
    library is small, a person edits it as one list, and a merge protocol would
    need a delete verb nobody asked for.
    """

    skills: list[SkillIn]


class SettingsResponse(_ApiModel):
    providers: list[ProviderOut]
    mcp_servers: list[McpServerPresetOut]
    secrets: list[str]
    active_provider_id: str | None = None
    model_prices: list[ModelPriceOut] = Field(default_factory=list)
    """Rates this account supplies for models Psych's own table does not know.
    Empty leaves every Run reporting an honest unknown cost rather than a
    zero (DESIGN.md §13.2)."""
    a2a_peers: list[A2APeerPresetOut] = Field(default_factory=list)
    """Saved A2A peers, offered when building an agent. Presets only: a
    published Spec carries its own copy, so editing one here never changes an
    agent already published."""
    runtime: RuntimeSettingsOut = Field(default_factory=RuntimeSettingsOut)
    skills: list[SkillOut] = Field(default_factory=list)
    """This account's skill library: procedures written once and attached to as
    many agents as want them.

    Read only to fill in an agent form. Attaching one **copies it** into the
    published Spec, so it joins the Version hash and a later edit here changes
    nothing about an agent already published. That is the whole design, and it
    is the opposite of how an MCP server description works: a
    description is a fact about somebody else's system, while a skill body is
    instructions the model follows, and instructions that can change under a
    published Version make two Runs of that Version behave differently."""


class ProviderTestResponse(_ApiModel):
    ok: bool
    detail: str
    """A human-readable summary. On failure, the provider's own error text
    (``str()`` of whatever ``psych_runtime.model.openai_compat`` raised) -- not a
    generic "connection failed", so a bad key reads differently from a bad
    URL or a bad model id."""
    models: list[str] | None = None


# ---------------------------------------------------------------------------
# Capabilities scenarios: GET /api/scenarios, POST to run one by id
# ---------------------------------------------------------------------------


class ScenarioOut(_ApiModel):
    """One entry of ``GET /api/scenarios``, built from a scenario module's own
    ``INFO`` (``app.scenarios.base.ScenarioInfo``) plus whether it can
    actually run right now in this process."""

    id: str
    title: str
    proves: str
    design_ref: str
    requires: list[str]
    available: bool
    unavailable_reason: str | None = None


class ScenarioProgressEvent(_ApiModel):
    """One SSE ``progress`` event while ``POST /api/scenarios/{id}/run`` is
    still working -- the scenario's own ``emit(step, detail)`` calls, each
    turned into one of these."""

    step: str
    detail: str
    at: str


class ScenarioAssertionOut(_ApiModel):
    claim: str
    held: bool
    detail: str


class ScenarioRunResultOut(_ApiModel):
    """The SSE stream's final ``result`` event -- ``app.scenarios.base
    .ScenarioResult``, as JSON."""

    passed: bool
    summary: str
    assertions: list[ScenarioAssertionOut]
    run_ids: list[str]
    report: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidationProblem(_ApiModel):
    """One entry of ``SpecValidationError.issues``, or one pydantic error."""

    path: str
    message: str


class ProblemResponse(_ApiModel):
    """The body of every non-2xx response this API returns."""

    detail: str
    issues: list[ValidationProblem] = Field(default_factory=list)
