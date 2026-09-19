"""The out-of-the-box catalogue: the providers, connectors, agents and
workflows a fresh playground account starts with.

This module is data and pure functions. It performs no I/O, holds no state and
imports nothing from ``app.main``, so it can be read as a list of facts and
tested without a running application. ``app.catalogue_seed`` is the half that
writes any of it into an account.

## What is in here, and what deliberately is not

The product goal is that somebody runs the container, signs up, pastes one
provider key, and already has something to press. That needs three lists that
are *true*:

``PROVIDERS``
    OpenAI-compatible chat-completions base URLs. Every one of them is an
    endpoint the vendor documents as OpenAI-compatible, so one client shape
    reaches all of them and Psych needs no per-vendor adapter. The
    ``default_model`` on each entry is a **suggestion**, pinned at the time
    this file was written, and model ids move faster than a source file does.
    The settings page lists ``GET /v1/models`` live once a key is pasted, and
    that listing is the authority; this constant only decides what is
    pre-filled before anybody has a key.

``CONNECTORS``
    Remote MCP servers **run by the vendor whose data they expose**. Nothing
    here is a proxy, a community re-host or a container this image would have
    to start. That rule is what makes it safe to ship the list: the person
    signs in to GitHub at GitHub, and no credential of theirs ever passes
    through anything this project wrote. It is also why the list is shorter
    than the MCP ecosystem -- a vendor without its own hosted server is simply
    absent.

    **Google Workspace (Gmail, Drive, Calendar, Docs) is absent for exactly
    that reason.** Google does not run a first-party remote MCP server for
    Workspace. Every "Google MCP" in circulation is a third party asking for a
    Google OAuth client -- or worse, a service-account key -- and shipping one
    would mean shipping either somebody else's endpoint or an OAuth client
    secret in this image. Neither is acceptable, so the catalogue says nothing
    about Google rather than saying something convenient.

``AGENTS`` and ``WORKFLOWS``
    One specialist per connector, plus ``psych``, the orchestrator that
    delegates to them; and use-case workflows composed from those agents.
    Both are factories rather than constants because both depend on the
    account: an agent needs the model id that account's active provider names,
    and a workflow needs the ids the specialists were actually published
    under.

## No secrets, ever

Nothing in this file is a credential and nothing in this file creates one. An
API-key connector carries a ``credential_hint`` -- the *name* the secret
should be stored under and a sentence about where to get it -- and seeding
registers that name on the preset without writing a value. An OAuth connector
carries whether the server supports dynamic client registration, because a
server that does not needs the person to bring a pre-registered client or a
personal token, and a UI that cannot say so leaves them guessing at a failed
Connect button.

## Why ``auth`` is one flat model rather than a union

Every endpoint in ``CONNECTORS`` was probed live (an unauthenticated MCP
``initialize``) rather than read off a blog post, and the answers did not fall
into two clean buckets. Several OAuth servers also accept a bearer token;
HubSpot documents an API key and *also* publishes protected-resource metadata;
Hugging Face answers an unauthenticated ``initialize`` outright, so a
credential only raises its limits. A discriminated union would have forced
each of those into an arm it half fits, and a frontend reading it would have
to special-case its way back out. One model with four honest booleans says
more with less.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas import (
    AgentStepIn,
    BranchCaseIn,
    BranchStepIn,
    ConditionIn,
    CreateAgentRequest,
    CreateWorkflowRequest,
    HumanStepIn,
    LiteralValueIn,
    MapStepIn,
    McpOAuthIn,
    McpServerIn,
    ParallelStepIn,
    SleepStepIn,
    SpawnIn,
    SubagentRefIn,
    ValuePathIn,
    ValueRefIn,
)
from psych_runtime.model.pricing import ModelPrice

__all__ = [
    "AGENTS",
    "CF_ACCOUNT_ID_ENV",
    "CONNECTORS",
    "PROVIDERS",
    "PSYCH_AGENT_ID",
    "WORKFLOWS",
    "CatalogueAgent",
    "CatalogueConnector",
    "CatalogueModel",
    "CatalogueProvider",
    "CatalogueWorkflow",
    "ConnectorAuth",
    "catalogue_prices",
    "connector_by_name",
    "provider_by_id",
    "resolve_base_url",
]

CF_ACCOUNT_ID_ENV = "PSYCH_PLAYGROUND_CF_ACCOUNT_ID"
"""Where Cloudflare's account id is read from when the operator set one.

Cloudflare is the one provider whose base URL is not the same string for
everybody: the account id is a path segment. Reading it from the environment
lets an image built for one account seed a working provider rather than one
with a ``{account_id}`` placeholder the person has to notice and fix.
"""

PSYCH_AGENT_ID = "psych"
"""The catalogue id of the orchestrator. Named here rather than spelled into
three modules, because the frontend pins it and the seeder publishes it last."""


class CatalogueModel(BaseModel):
    """One model a provider serves, with its published list price.

    Rates are USD per million tokens at the standard tier, read from the
    vendor's own pricing page on the date in ``PRICES_CHECKED``. ``None`` means
    the vendor publishes no per-token price (a model that runs on your own
    machine, or one billed per request), and the console then shows nothing
    rather than a zero: DESIGN.md §13.2, an unknown cost is unknown.

    These are the prices the console shows beside a model id before anybody
    has chosen it, and the rates a Run is costed at when the account has not
    entered its own (see ``catalogue_prices``). They are a snapshot, and
    vendors change them; the account's Prices page overrides them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    input: Decimal | None = None
    output: Decimal | None = None
    note: str = ""
    """One short phrase when the id alone does not say enough: "reasoning",
    "fastest", "open weights"."""

    @property
    def priced(self) -> bool:
        return self.input is not None and self.output is not None


def m(
    model_id: str, inp: str | None = None, out: str | None = None, note: str = ""
) -> CatalogueModel:
    """``CatalogueModel`` from string rates, so the table below reads as prices
    do on a pricing page and never passes through a float."""
    return CatalogueModel(
        id=model_id,
        input=Decimal(inp) if inp is not None else None,
        output=Decimal(out) if out is not None else None,
        note=note,
    )


class CatalogueProvider(BaseModel):
    """One OpenAI-compatible model backend this playground knows how to reach.

    Frozen: this is a fact about a vendor, not a setting. What an account does
    with it -- a key, a chosen model, whether it is active -- lives in
    ``ProviderConfig``, and the two are joined by ``id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """Stable and human-readable, and reused verbatim as ``ProviderConfig.id``.

    Deliberately not a random id. Seeding has to be idempotent, and
    ``PUT /api/settings/providers`` rewrites the whole list from a request
    body that carries no catalogue field of its own -- so any provenance
    marker stored on the config would be dropped the first time somebody saved
    the settings page, and the next seed would duplicate every provider. A
    deterministic id has nothing to round-trip and so nothing to lose.
    """
    label: str
    base_url: str
    """The chat-completions base URL, possibly carrying ``{name}`` placeholders
    for each entry in ``requires``."""
    default_model: str
    """A suggestion, not a promise. See the module docstring: the live
    ``GET /api/models`` listing wins over this the moment a key exists."""
    models: tuple[CatalogueModel, ...] = ()
    """What the vendor serves today, with list prices, newest first. Offered
    before a key makes the live list reachable, and beside the live list once
    it is, because ``/models`` returns ids and never prices. Same caveat as
    ``default_model``: a snapshot, and the vendor's own listing wins."""
    key_url: str
    """Where a person goes to get a key. The single most useful link on the
    page, and the one a catalogue that only listed base URLs would omit."""
    docs_url: str
    requires: tuple[str, ...] = ()
    """Names that must be substituted into ``base_url`` before it can be
    called, as ``{name}``: Cloudflare's account id, Azure's resource name,
    Bedrock's region. The console asks for each one in the key dialog."""
    local: bool = False
    """Runs on the person's own machine and needs no key. Ollama only. The
    console shows it differently: there is no key to paste and no signup to
    link to, and a "get a key" button next to it would be nonsense."""
    key_header: str = ""
    """Set when the vendor does not read ``Authorization: Bearer``. Empty for
    every provider that does, which is all of them today; kept as a field so
    the fact is recorded where the URL is rather than in a comment."""

    @property
    def suggested_models(self) -> tuple[str, ...]:
        """The model ids alone, in catalogue order."""
        return tuple(model.id for model in self.models)

    def price_for(self, model_id: str) -> CatalogueModel | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None


class ConnectorAuth(BaseModel):
    """How one connector expects to be authenticated.

    One model rather than a union, for the reasons the module docstring gives.
    Every field was settled by probing the live endpoint, not by reading
    documentation about it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["oauth", "api_key"]
    """What the *primary* path is. ``oauth`` means the server answers an
    unauthenticated request with a bearer challenge carrying
    ``resource_metadata``, so the whole flow is discoverable and the Connect
    button can simply start it."""
    dynamic_registration: bool = True
    """Whether the server's authorization server accepts RFC 7591 dynamic
    client registration. ``False`` means this playground cannot register
    itself and the person must bring a pre-registered client id -- or, where
    ``token_alternative`` is set, a token instead. Copied onto the seeded
    preset as ``McpOAuthPreset.allow_dynamic_registration``."""
    token_alternative: bool = False
    """A bearer token or API key also works, whatever ``kind`` says. True for
    the servers whose challenge accepts one directly (GitHub, Atlassian,
    Stripe, PostHog, HubSpot, Hugging Face) and for the API-key-only ones."""
    credential_hint: str = ""
    """One sentence naming what the token is and where it comes from, shown
    beside the field a person pastes it into. Never a value."""
    optional: bool = False
    """Connecting succeeds with no credential at all. Hugging Face only: its
    endpoint answers an unauthenticated ``initialize`` and a token raises
    limits rather than unlocking the server."""


class CatalogueConnector(BaseModel):
    """One vendor-run remote MCP server, and the specialist agent it deserves.

    The agent text lives here rather than in ``AGENTS`` because the two are
    one decision: what a specialist is for is what its connector reaches, and
    splitting them across two constants is how they drift apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The slug. Also the seeded ``McpServerPreset.name`` and the specialist
    agent's ``name``, byte for byte -- a console joins on it, and three
    spellings of one connector would be three bugs."""
    label: str
    category: Literal["code", "work", "chat", "payments", "infra", "data", "design", "automation"]
    description: str
    """One plain sentence, for somebody scanning a list of twenty-five."""
    url: str
    transport: Literal["http", "sse"] = "http"
    auth: ConnectorAuth
    docs_url: str
    read_only_default: bool = False
    """Whether the specialist should be published with only this server's read
    tools. ``False`` everywhere today: narrowing needs the server's actual tool
    names, which are only known after a connection, and a guessed allow-list
    that silently drops a tool is worse than none. The field exists because the
    *policy* question is real -- see ``agent_request``, which points approvals
    at writes and destructive calls instead."""
    agent_focus: str
    """What this specialist is for, in a sentence or two, spliced into its
    instructions. See ``agent_request`` for why only this part varies."""
    workflows: tuple[str, ...] = ()
    """The catalogue workflow ids this connector takes part in."""

    @property
    def agent_description(self) -> str:
        """What a person browsing the agent list -- and the orchestrator's own
        roster entry -- reads. Comfortably over ``SubagentRefIn``'s twenty
        character floor for every connector, because ``agent_focus`` is."""
        return f"{self.label} specialist. {self.agent_focus}"

    @property
    def agent_instructions(self) -> str:
        """The specialist's prompt.

        Composed from one template plus ``agent_focus`` on purpose. The three
        things that make a connector agent safe -- read before you write,
        confirm before you destroy, report what you actually did -- are not
        opinions about GitHub or about Stripe, and writing twenty-five slightly
        different paragraphs saying them would guarantee that some of the
        twenty-five said them worse. What genuinely differs per connector is
        what the tools are *for*, and that is the part this file writes out by
        hand.
        """
        return (
            f"You are the {self.label} specialist. {self.agent_focus} You reach "
            f"{self.label} through its own MCP server, so the tools you are offered are "
            "the ones that account is actually entitled to -- if a tool you expect is "
            "missing, say so rather than guessing at a workaround.\n\n"
            "Read before you write. Look up the thing you are about to change and quote "
            "what you found, so the person can tell a stale request from a current one. "
            "Before anything that creates, edits, deletes, sends, merges, closes or "
            "charges, state exactly what you are about to do and to which object, and "
            "wait for the person to agree; if their request is ambiguous about which "
            "object they mean, ask rather than picking the likeliest.\n\n"
            "Report clearly. Give the answer first in a sentence, then the specifics -- "
            "ids, links, names, counts -- so it can be checked. Never invent an id, a "
            "URL or a number: if you did not read it from a tool result, say you do not "
            "have it."
        )

    def mcp_server(self) -> McpServerIn:
        """This connector as the ``POST /api/agents`` MCP entry.

        ``optional=True`` is the field that makes the whole catalogue work on a
        fresh account. A required server that nobody has connected yet fails
        the agent at its first turn, which would mean twenty-five agents that
        cannot run until twenty-five OAuth flows are done. Optional, the
        specialist publishes and runs immediately, says it has no connection
        when asked to do something needing one, and starts working the moment
        the person presses Connect -- no republish.

        ``preload=False`` because these catalogues are large. GitHub's alone is
        big enough that inlining its schemas costs more prompt than the rest of
        the agent put together; ``psych_runtime.tools.deferred`` discloses them
        on demand instead.
        """
        return McpServerIn(
            name=self.name,
            url=self.url,
            transport=self.transport,
            optional=True,
            preload=False,
            # Only where a credential is the *primary* path. `McpPool`
            # resolves `McpServer.credential` before it ever starts an OAuth
            # flow and raises `CredentialNotFound` when the named secret is
            # absent, so naming one on an OAuth connector would turn Connect
            # into "no credential" on every server that merely *also* accepts
            # a token. The token remains an option -- `auth.token_alternative`
            # and `credential_hint` say so, and the settings page can set the
            # credential -- it is just not presumed.
            credential=self.credential_name if self.auth.kind == "api_key" else None,
            oauth=(
                McpOAuthIn(
                    # Browser redirect, not client credentials. Every OAuth
                    # connector here is a person authorising their own account,
                    # and the preset's own default (`client_credentials`) would
                    # fail at connect time with a message about a client secret
                    # nobody was ever asked for.
                    grant="authorization_code",
                    allow_dynamic_registration=self.auth.dynamic_registration,
                    client_name="psych-playground",
                )
                if self.auth.kind == "oauth"
                else None
            ),
        )

    @property
    def credential_name(self) -> str:
        """The secret name this connector's token is looked up under.

        Derived rather than stored so it cannot disagree with ``name``. The
        secret itself is never created by seeding: the preset names it, the
        person fills it in, and until they do the resolver reports a missing
        credential -- which is the honest state, and readable in the UI.
        """
        return f"{self.name.replace('-', '_')}_token"

    def agent_request(self, model: str) -> CreateAgentRequest:
        """This specialist as the exact body ``POST /api/agents`` takes.

        A real ``CreateAgentRequest`` rather than a dict, so a malformed
        catalogue entry is a type error at check time instead of a 422 raised
        inside somebody's signup.
        """
        return CreateAgentRequest(
            name=self.name,
            description=self.agent_description,
            instructions=self.agent_instructions,
            model=model,
            mcp=[self.mcp_server()],
            may_ask_questions=True,
            tasks_enabled=True,
            components_enabled=True,
            # Anything that changes the vendor's data suspends for a person.
            # The specialists exist to touch real accounts; an unattended write
            # to somebody's production Stripe or their issue tracker is the one
            # outcome this catalogue must not ship by default.
            approval_selectors=["@write", "@destructive"],
        )


class CatalogueAgent(BaseModel):
    """One agent the catalogue publishes.

    The specialists are generated from ``CONNECTORS``; this model exists for
    the orchestrator, which has no connector of its own, and to give both a
    single shape the seeder and ``GET /api/catalogue`` can iterate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalogue_id: str
    name: str
    description: str
    connector: str | None = None
    """The connector this specialist is built around, or ``None`` for
    ``psych``."""
    subagents: tuple[str, ...] = ()
    """Catalogue ids of the specialists this agent delegates to. Only
    ``psych`` has any."""


def _specialist_agents() -> tuple[CatalogueAgent, ...]:
    return tuple(
        CatalogueAgent(
            catalogue_id=connector.name,
            name=connector.name,
            description=connector.agent_description,
            connector=connector.name,
        )
        for connector in CONNECTORS
    )


PSYCH_INSTRUCTIONS = """\
You are Psych, the agent a person talks to first. You do not do specialist work \
yourself; you understand what somebody wants and get it done by the specialists \
you have, each of which is connected to one real system -- a code host, an issue \
tracker, a chat workspace, a payment processor, a database, a design tool.

Start by working out what would actually answer the request, and which systems \
hold the facts that would. Say it back in one line when the request could mean \
two things, and ask rather than guessing when the answer changes which system \
you touch. Then delegate: hand each specialist a self-contained instruction -- \
what to find or do, which repository, project, channel or account, and over what \
period -- because a sub-agent sees your instruction and not this conversation.

When two pieces of work do not depend on each other, start them together rather \
than one after the other; a report drawing on commits and on incidents should ask \
for both at once. When one genuinely needs another's output, say so and wait.

Some specialists have not been connected yet. One that says it cannot reach its \
system is not broken: tell the person which connector to connect and what it \
would have got them, and carry on with what you can.

Report back in your own voice. Lead with the answer, then the specifics -- ids, \
links, counts, dates -- attributed to the specialist that found them. Never pass \
on a number or an id you were not given, and never let a sub-agent's write happen \
silently: when a specialist is about to change something in a real account, say \
what will change before it does.

You can also build. When somebody wants an agent for a job none of your \
specialists covers, or a repeatable pipeline rather than a one-off answer, use \
`list_agents` and `list_workflows` to see what exists, then `create_agent` or \
`create_workflow` to make it -- naming it plainly, writing instructions a \
stranger could follow, and saying in one line what you made and how to open it. \
Run an existing pipeline with `run_workflow` when the request is one it was \
built for. Each of those is confirmed by the person before it takes effect, so \
describe what you are about to create rather than asking whether you may.\
"""


def psych_agent_request(
    model: str, specialists: Mapping[str, str], *, tools: Sequence[str] = ()
) -> CreateAgentRequest:
    """The orchestrator, given the agent ids its specialists were published under.

    Args:
        model: the model id, from the account's active provider.
        specialists: catalogue id to published ``agent_id``, for every
            specialist that exists in this account. Missing ones are simply
            absent from the roster, so a partial seed still publishes a working
            orchestrator rather than failing on the first id it cannot resolve.
        tools: the registered code tools to offer alongside the roster.

    The roster is ``subagents`` -- an author-written list, embedded whole at
    each child's current Version, so one hash pins the tree (DESIGN.md §17) --
    *and* ``spawn`` is enabled, which is a different thing: an envelope
    permitting children the model composes itself, for the work no specialist
    covers. Both, because a roster without spawn cannot improvise and spawn
    without a roster has nothing good to start from.
    """
    roster = [
        SubagentRefIn(
            name=agent.name,
            description=agent.description,
            agent_id=specialists[agent.catalogue_id],
        )
        for agent in AGENTS
        if agent.connector is not None and agent.catalogue_id in specialists
    ]
    return CreateAgentRequest(
        name=PSYCH_AGENT_ID,
        description=(
            "The agent to talk to first. Understands what you want, delegates to the "
            "connector specialists that can get it, and reports back."
        ),
        instructions=PSYCH_INSTRUCTIONS,
        model=model,
        tools=list(tools),
        subagents=roster,
        subagents_enabled=True,
        spawn=SpawnIn(max_depth=2, max_alive=4, may_message=True),
        may_ask_questions=True,
        tasks_enabled=True,
        components_enabled=True,
        approval_selectors=["@write", "@destructive"],
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

PRICES_CHECKED = "2026-09-19"
"""The date every rate below was read from its vendor's pricing page. Shown
nowhere yet; kept so the next person refreshing the table knows how stale it
is without reading git history."""

PROVIDERS: tuple[CatalogueProvider, ...] = (
    CatalogueProvider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5.6-terra",
        models=(
            m("gpt-6-astra", "10", "50", "most capable"),
            m("gpt-5.6-sol", "4", "20"),
            m("gpt-5.6-terra", "2", "12", "capable, mid-price"),
            m("gpt-5.6-luna", "0.20", "1.20", "fast and cheap"),
            m("gpt-5.5", "5", "30"),
            m("gpt-5.5-pro", "30", "180"),
            m("gpt-5.4", "2.50", "15"),
            m("gpt-5.4-mini", "0.75", "4.50"),
            m("gpt-5.4-nano", "0.20", "1.25"),
            m("gpt-5.4-pro", "30", "180"),
            m("gpt-5.2", "1.75", "14"),
            m("gpt-5.2-pro", "21", "168"),
            m("gpt-5.1", "1.25", "10"),
            m("gpt-5", "1.25", "10"),
            m("gpt-5-mini", "0.25", "2"),
            m("gpt-5-nano", "0.05", "0.40"),
            m("gpt-5-pro", "15", "120"),
            m("gpt-4.1", "2", "8"),
            m("gpt-4.1-mini", "0.40", "1.60"),
            m("gpt-4.1-nano", "0.10", "0.40"),
            m("gpt-4o", "2.50", "10"),
            m("gpt-4o-mini", "0.15", "0.60"),
            m("o3", "2", "8", "reasoning"),
            m("o4-mini", "1.10", "4.40", "reasoning"),
        ),
        key_url="https://platform.openai.com/api-keys",
        docs_url="https://developers.openai.com/api/docs/pricing",
    ),
    CatalogueProvider(
        id="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-5",
        models=(
            m("claude-fable-5-1", "10", "50", "most capable"),
            m("claude-opus-5", "5", "25"),
            m("claude-sonnet-5", "2", "10", "capable, mid-price"),
            m("claude-haiku-4-5", "1", "5", "fastest"),
            m("claude-fable-5", "10", "50"),
            m("claude-opus-4-8", "5", "25"),
            m("claude-opus-4-7", "5", "25"),
            m("claude-opus-4-6", "5", "25"),
            m("claude-sonnet-4-6", "3", "15"),
            m("claude-opus-4-5", "5", "25"),
            m("claude-sonnet-4-5", "3", "15"),
        ),
        key_url="https://console.anthropic.com/settings/keys",
        docs_url="https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    CatalogueProvider(
        id="azure_openai",
        label="Azure OpenAI",
        base_url="https://{resource}.openai.azure.com/openai/v1",
        # The model field names your *deployment*, not an OpenAI model id.
        # Microsoft's own samples deploy under the model's name, which is why
        # the ids below are OpenAI's; rename them if your deployments differ.
        default_model="gpt-5.6-terra",
        models=(
            m("gpt-6-astra", note="your deployment name"),
            m("gpt-5.6-sol", note="your deployment name"),
            m("gpt-5.6-terra", note="your deployment name"),
            m("gpt-5.6-luna", note="your deployment name"),
            m("gpt-5.4", note="your deployment name"),
            m("gpt-5.4-mini", note="your deployment name"),
            m("gpt-4.1", note="your deployment name"),
            m("gpt-4o", note="your deployment name"),
            m("gpt-4o-mini", note="your deployment name"),
        ),
        key_url="https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI",
        docs_url="https://learn.microsoft.com/en-us/azure/ai-foundry/openai/supported-languages",
        requires=("resource",),
    ),
    CatalogueProvider(
        id="bedrock",
        label="AWS Bedrock",
        base_url="https://bedrock-runtime.{region}.amazonaws.com/openai/v1",
        # Bedrock's Chat Completions endpoint serves the OpenAI open-weight
        # models. Claude, Nova and the rest answer only on Bedrock's own
        # Converse and Messages APIs, which are not OpenAI-compatible, so
        # they are not listed here. The key is a Bedrock API key, sent as a
        # bearer token. Prices are us-east-1 on-demand.
        default_model="openai.gpt-oss-120b-1:0",
        models=(
            m("openai.gpt-oss-120b-1:0", "0.15", "0.60", "open weights"),
            m("openai.gpt-oss-20b-1:0", "0.07", "0.20", "open weights, smaller"),
        ),
        key_url="https://console.aws.amazon.com/bedrock/home#/api-keys",
        docs_url="https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html",
        requires=("region",),
    ),
    CatalogueProvider(
        id="cloudflare",
        label="Cloudflare Workers AI",
        base_url="https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        default_model="@cf/openai/gpt-oss-120b",
        models=(
            m("@cf/openai/gpt-oss-120b", "0.35", "0.75", "open weights"),
            m("@cf/openai/gpt-oss-20b", "0.20", "0.30"),
            m("@cf/zai-org/glm-5.3", "1.40", "4.40"),
            m("@cf/zai-org/glm-5.3-flash", "0.15", "0.50"),
            m("@cf/zai-org/glm-5.2", "1.40", "4.40"),
            m("@cf/zai-org/glm-4.7-flash", "0.06", "0.40"),
            m("@cf/moonshotai/kimi-k2.7-code", "0.95", "4"),
            m("@cf/moonshotai/kimi-k2.6", "0.95", "4"),
            m("@cf/moonshotai/kimi-k2.5", "0.60", "3"),
            m("@cf/deepseek-ai/deepseek-v4-pro-0813", "1.32", "3.96"),
            m("@cf/deepseek-ai/deepseek-v4-flash-0731", "0.44", "1.32"),
            m("@cf/nvidia/nemotron-3-120b-a12b", "0.50", "1.50"),
            m("@cf/qwen/qwen3.8-27b", "0.45", "3.20"),
            m("@cf/qwen/qwen3-30b-a3b-fp8", "0.051", "0.335"),
            m("@cf/google/gemma-4-26b-a4b-it", "0.10", "0.30"),
            m("@cf/google/gemma-3-12b-it", "0.345", "0.556"),
            m("@cf/meta/llama-4-scout-17b-16e-instruct", "0.27", "0.85"),
            m("@cf/meta/llama-3.3-70b-instruct-fp8-fast", "0.293", "2.253"),
            m("@cf/meta/llama-3.1-8b-instruct-fp8-fast", "0.045", "0.384"),
            m("@cf/mistralai/mistral-small-3.1-24b-instruct", "0.351", "0.555"),
            m("@cf/ibm-granite/granite-4.0-h-micro", "0.017", "0.112"),
        ),
        key_url="https://dash.cloudflare.com/profile/api-tokens",
        docs_url="https://developers.cloudflare.com/workers-ai/platform/pricing/",
        requires=("account_id",),
    ),
    CatalogueProvider(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-3.8-flash",
        models=(
            m("gemini-3.8-flash", "0.75", "3.75", "capable, mid-price"),
            m("gemini-3.7-flash", "0.75", "3.75"),
            m("gemini-3.6-flash", "0.75", "3.75"),
            m("gemini-3.5-flash", "1.50", "9"),
            m("gemini-3.5-flash-lite", "0.30", "2.50", "fast and cheap"),
            m("gemini-3.1-pro-preview", "2", "12", "up to 200k context; more above"),
            m("gemini-3.1-flash-lite", "0.25", "1.50"),
            m("gemini-3-flash-preview", "0.50", "3"),
            m("gemini-2.5-pro", "1.25", "10", "up to 200k context; more above"),
            m("gemini-2.5-flash", "0.30", "2.50"),
            m("gemini-2.5-flash-lite", "0.10", "0.40"),
        ),
        key_url="https://aistudio.google.com/apikey",
        docs_url="https://ai.google.dev/gemini-api/docs/pricing",
    ),
    CatalogueProvider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        default_model="openai/gpt-oss-120b",
        models=(
            m("openai/gpt-oss-120b", "0.15", "0.60", "open weights"),
            m("openai/gpt-oss-20b", "0.075", "0.30"),
            m("llama-3.3-70b-versatile", note="enterprise pricing"),
            m("llama-3.1-8b-instant", note="enterprise pricing"),
            m("groq/compound", note="agentic system, priced per request"),
            m("groq/compound-mini", note="agentic system, priced per request"),
        ),
        key_url="https://console.groq.com/keys",
        docs_url="https://console.groq.com/docs/models",
    ),
    CatalogueProvider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-large-2512",
        models=(
            m("mistral-large-2512", "0.50", "1.50", "Mistral Large 3"),
            m("mistral-small-2603", "0.15", "0.60", "Mistral Small 4"),
            m("ministral-3-14b-2512", "0.20", "0.20"),
            m("ministral-3-8b-2512", "0.15", "0.15"),
            m("ministral-3-3b-2512", "0.10", "0.10"),
            m("codestral-latest", "0.30", "0.90", "code"),
        ),
        key_url="https://console.mistral.ai/api-keys",
        docs_url="https://mistral.ai/pricing/api",
    ),
    CatalogueProvider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-5",
        models=(
            m("anthropic/claude-fable-5.1", "10", "50"),
            m("anthropic/claude-opus-5", "5", "25"),
            m("anthropic/claude-sonnet-5", "2", "10", "capable, mid-price"),
            m("anthropic/claude-haiku-4.5", "1", "5"),
            m("openai/gpt-6-astra", "10", "50"),
            m("openai/gpt-5.6-sol", "2", "10"),
            m("openai/gpt-5.6-terra", "2", "12"),
            m("openai/gpt-5.6-luna", "0.20", "1.20"),
            m("openai/gpt-oss-120b", "0.15", "0.60"),
            m("google/gemini-3.8-flash", "0.75", "3.75"),
            m("google/gemini-3.1-pro-preview", "2", "12"),
            m("x-ai/grok-4.6", "2", "6"),
            m("qwen/qwen3.8-max-0902", "2", "6"),
            m("z-ai/glm-5.3", "0.91", "2.86"),
            m("z-ai/glm-5.3-flash", "0.09", "0.30"),
            m("moonshotai/kimi-k3", "1.70", "8.50"),
            m("deepseek/deepseek-v4.1-flash", "0.15", "0.60"),
            m("minimax/minimax-m3", "0.30", "1.20"),
            m("meta/muse-spark-1.3", "1.25", "4.25"),
        ),
        key_url="https://openrouter.ai/keys",
        docs_url="https://openrouter.ai/models",
    ),
    CatalogueProvider(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        default_model="zai-org/GLM-5.3",
        models=(
            m("zai-org/GLM-5.3", "1.40", "4.40", "capable, mid-price"),
            m("zai-org/GLM-5.3-Flash", "0.15", "0.50"),
            m("zai-org/GLM-5.2", "1.40", "4.40"),
            m("moonshotai/Kimi-K3", "3", "15"),
            m("Qwen/Qwen3.8-2.4T-A95B", "2", "6"),
            m("Qwen/Qwen3.8-Flash", "0.15", "0.47"),
            m("Qwen/Qwen3.7-Max", "2.50", "7.50"),
            m("Qwen/Qwen3.7-Plus", "0.32", "1.28"),
            m("Qwen/Qwen3.6-Plus", "0.50", "3"),
            m("Qwen/Qwen3.5-9B", "0.17", "0.25"),
            m("deepseek-ai/DeepSeek-V4-Pro-0813", "1.32", "3.96"),
            m("deepseek-ai/DeepSeek-V4.1-Flash", "0.30", "1.20"),
            m("deepseek-ai/DeepSeek-V4-Flash-0731", "0.14", "0.28"),
            m("MiniMaxAI/MiniMax-M3", "0.30", "1.20"),
            m("openai/gpt-oss-120b", "0.15", "0.60"),
            m("meta-models/Muse-Glimmer-30B", "0.35", "1.50"),
            m("thinkingmachines/Inkling", "1", "4.05"),
            m("meta-llama/Llama-3.3-70B-Instruct-Turbo", "1.04", "1.04"),
        ),
        key_url="https://api.together.ai/settings/api-keys",
        docs_url="https://www.together.ai/pricing",
    ),
    CatalogueProvider(
        id="xai",
        label="xAI",
        base_url="https://api.x.ai/v1",
        default_model="grok-4.6",
        models=(
            m("grok-4.6", "2", "6", "up to 200k context; more above"),
            m("grok-4.5", "2", "6"),
            m("grok-4.3", "1.25", "2.50"),
            m("grok-4.20-0309-reasoning", "1.25", "2.50", "reasoning"),
            m("grok-4.20-0309-non-reasoning", "1.25", "2.50"),
            m("grok-4.20-multi-agent-0309", "1.25", "2.50"),
            m("grok-build-0.1", "1", "2", "code"),
        ),
        key_url="https://console.x.ai/",
        docs_url="https://docs.x.ai/docs/models",
    ),
    CatalogueProvider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-v4-pro",
        models=(
            m("deepseek-v4-pro", "1.32", "3.96", "peak rate; half off-peak"),
            m("deepseek-flash", "0.30", "1.20", "peak rate; half off-peak"),
        ),
        key_url="https://platform.deepseek.com/api_keys",
        docs_url="https://api-docs.deepseek.com/quick_start/pricing",
    ),
    CatalogueProvider(
        id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        default_model="qwen3.6",
        models=(
            m("qwen3.6", note="most pulled"),
            m("qwen3.8"),
            m("glm-5.3"),
            m("glm-5.3-flash"),
            m("deepseek-v4.1-flash"),
            m("deepseek-v4-flash"),
            m("minimax-m3"),
            m("nemotron3"),
            m("kimi-k2.7-code", note="code"),
            m("mistral-medium-3.5"),
            m("granite4.2"),
            m("gemma3"),
            m("llama3.2"),
            m("llama3.1"),
            m("qwen3"),
        ),
        key_url="https://ollama.com/download",
        docs_url="https://docs.ollama.com/openai",
        local=True,
    ),
)


def provider_by_id(provider_id: str) -> CatalogueProvider | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


def catalogue_prices() -> dict[str, ModelPrice]:
    """Every priced model in the catalogue, keyed by id, as the runtime's
    ``ModelPrice``.

    Sits between the account's own rates and the library's bundled snapshot in
    ``prices_for``: the snapshot is broad and dated, and knows nothing of the
    ids Groq, Mistral, xAI or Cloudflare use natively, so a Run on one of
    those recorded an unknown cost. Ids are vendor-specific (``gpt-4o-mini``
    and ``openai/gpt-4o-mini`` are different strings), so a later provider in
    the table never overwrites an earlier one by accident; where two vendors
    really share an id -- Azure deployments named after OpenAI models -- the
    prices agree, and the account's own entry wins over both regardless.
    """
    prices: dict[str, ModelPrice] = {}
    for provider in PROVIDERS:
        for model in provider.models:
            if model.input is None or model.output is None or model.id in prices:
                continue
            prices[model.id] = ModelPrice(
                input=model.input,
                output=model.output,
                cache_read=Decimal(0),
                cache_write=Decimal(0),
            )
    return prices


def resolve_base_url(provider: CatalogueProvider, env: Mapping[str, str] | None = None) -> str:
    """``provider.base_url`` with whatever placeholders the environment can fill.

    Only Cloudflare's account id is read from the environment. Substituting it
    here means an operator who set ``PSYCH_PLAYGROUND_CF_ACCOUNT_ID`` gets a
    provider that works as soon as a key is pasted, while everybody else gets
    the literal ``{account_id}`` placeholder -- which the settings page shows
    and asks for, and which fails loudly rather than quietly calling the wrong
    URL. Azure's ``{resource}`` and Bedrock's ``{region}`` are always asked
    for in the console: neither has a value an image could sensibly ship.
    """
    source = os.environ if env is None else env
    if "account_id" in provider.requires:
        account_id = source.get(CF_ACCOUNT_ID_ENV, "")
        if account_id:
            return provider.base_url.replace("{account_id}", account_id)
    return provider.base_url


# ---------------------------------------------------------------------------
# Connectors
#
# Every URL below was checked against the live endpoint with an
# unauthenticated MCP `initialize`, and the `auth` fields record what came
# back rather than what documentation claimed. Three shapes turned up:
#
#   * a 401 whose `WWW-Authenticate` carries `resource_metadata` -- the whole
#     OAuth flow is discoverable, so `kind="oauth"` and Connect just works;
#   * a 401 bearer challenge with no metadata -- a token, or a client somebody
#     registered by hand, so `token_alternative=True` and the hint matters;
#   * a 200 -- Hugging Face, which needs no credential at all.
# ---------------------------------------------------------------------------

CONNECTORS: tuple[CatalogueConnector, ...] = (
    CatalogueConnector(
        name="github",
        label="GitHub",
        category="code",
        description="Repositories, issues, pull requests, actions and code search on GitHub.",
        url="https://api.githubcopilot.com/mcp/",
        auth=ConnectorAuth(
            kind="oauth",
            # No protected-resource metadata on the challenge, so this
            # playground cannot register itself: the person brings a token, or
            # an OAuth app they registered.
            dynamic_registration=False,
            token_alternative=True,
            credential_hint=(
                "A GitHub personal access token (fine-grained, with the repositories and "
                "scopes you want the agent to reach) from github.com/settings/tokens, sent "
                "as a bearer token."
            ),
        ),
        docs_url="https://docs.github.com/en/copilot/how-tos/context/use-the-github-mcp-server",
        agent_focus=(
            "You look up repositories, commits, branches, pull requests, issues, releases "
            "and workflow runs, and you open, comment on and label them when asked."
        ),
        workflows=("github-activity-report", "release-notes", "incident-summary"),
    ),
    CatalogueConnector(
        name="notion",
        label="Notion",
        category="work",
        description="Pages, databases and comments in a Notion workspace.",
        url="https://mcp.notion.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developers.notion.com/docs/mcp",
        agent_focus=(
            "You search a Notion workspace, read pages and database rows, and create or "
            "update them -- including publishing a written-up report to a page."
        ),
        workflows=("github-activity-report",),
    ),
    CatalogueConnector(
        name="atlassian",
        label="Atlassian (Jira & Confluence)",
        category="work",
        description="Jira issues and sprints, and Confluence pages, in one Atlassian site.",
        url="https://mcp.atlassian.com/v2/mcp",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=False,
            token_alternative=True,
            credential_hint=(
                "An Atlassian API token from id.atlassian.com/manage-profile/security/api-tokens, "
                "used where a pre-registered OAuth client is not available."
            ),
        ),
        docs_url="https://support.atlassian.com/rovo/docs/getting-started-with-the-atlassian-remote-mcp-server/",
        agent_focus=(
            "You search and read Jira issues, sprints and boards and Confluence pages, and "
            "you create, transition and comment on issues when asked."
        ),
        workflows=("jira-sprint-digest",),
    ),
    CatalogueConnector(
        name="linear",
        label="Linear",
        category="work",
        description="Issues, projects and cycles in Linear.",
        url="https://mcp.linear.app/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://linear.app/docs/mcp",
        agent_focus=(
            "You search, read, create and update Linear issues, projects and cycles, and "
            "you triage an inbox of them into the right team, label and priority."
        ),
        workflows=("inbox-and-issues-triage",),
    ),
    CatalogueConnector(
        name="slack",
        label="Slack",
        category="chat",
        description="Channels, messages and search in a Slack workspace.",
        url="https://mcp.slack.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://docs.slack.dev/mcp/",
        agent_focus=(
            "You search and read Slack channels and threads, and you post messages -- "
            "always naming the exact channel before you send anything."
        ),
        workflows=("github-activity-report", "inbox-and-issues-triage"),
    ),
    CatalogueConnector(
        name="sentry",
        label="Sentry",
        category="infra",
        description="Errors, issues and releases tracked in Sentry.",
        url="https://mcp.sentry.dev/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://docs.sentry.io/product/sentry-mcp/",
        agent_focus=(
            "You find and read Sentry issues, their stack traces, their frequency and the "
            "releases they appeared in, and you resolve or assign them when asked."
        ),
        workflows=("incident-summary",),
    ),
    CatalogueConnector(
        name="stripe",
        label="Stripe",
        category="payments",
        description="Customers, payments, subscriptions and invoices in Stripe.",
        url="https://mcp.stripe.com/",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=True,
            token_alternative=True,
            credential_hint=(
                "A Stripe restricted API key from dashboard.stripe.com/apikeys. Start in "
                "test mode and grant read-only permissions unless you mean otherwise."
            ),
        ),
        docs_url="https://docs.stripe.com/mcp",
        agent_focus=(
            "You look up Stripe customers, payments, subscriptions, invoices and disputes. "
            "Money moves when you write here, so every create or refund is confirmed first."
        ),
    ),
    CatalogueConnector(
        name="vercel",
        label="Vercel",
        category="infra",
        description="Projects, deployments and logs on Vercel.",
        url="https://mcp.vercel.com/",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://vercel.com/docs/mcp/vercel-mcp",
        agent_focus=(
            "You inspect Vercel projects, deployments, build logs and environment "
            "configuration, and you explain why a deployment failed."
        ),
    ),
    CatalogueConnector(
        name="cloudflare",
        label="Cloudflare",
        category="infra",
        description="Workers, DNS, analytics and account configuration on Cloudflare.",
        url="https://mcp.cloudflare.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/",
        agent_focus=(
            "You read Cloudflare accounts, zones, DNS records, Workers and their analytics, "
            "and you look up documentation for the platform."
        ),
    ),
    CatalogueConnector(
        name="supabase",
        label="Supabase",
        category="data",
        description="Postgres schemas, tables, edge functions and logs in a Supabase project.",
        url="https://mcp.supabase.com/mcp",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=False,
            token_alternative=True,
            credential_hint=(
                "A Supabase personal access token from supabase.com/dashboard/account/tokens."
            ),
        ),
        docs_url="https://supabase.com/docs/guides/getting-started/mcp",
        agent_focus=(
            "You inspect a Supabase project's schema, run read queries against it, and "
            "read its logs. Any migration or data change is described and confirmed first."
        ),
    ),
    CatalogueConnector(
        name="neon",
        label="Neon",
        category="data",
        description="Serverless Postgres branches, databases and queries on Neon.",
        url="https://mcp.neon.tech/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://neon.com/docs/ai/neon-mcp-server",
        agent_focus=(
            "You list and inspect Neon projects, branches and databases, run read queries, "
            "and create or reset a branch when the person asks for one."
        ),
    ),
    CatalogueConnector(
        name="netlify",
        label="Netlify",
        category="infra",
        description="Sites, deploys and build logs on Netlify.",
        url="https://netlify-mcp.netlify.app/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://docs.netlify.com/build/build-with-ai/netlify-mcp-server/",
        agent_focus=(
            "You inspect Netlify sites, deploys, build logs and environment variables, and "
            "you explain a failed build from what the log actually says."
        ),
    ),
    CatalogueConnector(
        name="prisma",
        label="Prisma Postgres",
        category="data",
        description="Prisma Postgres databases, schemas and migrations.",
        url="https://mcp.prisma.io/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://www.prisma.io/docs/postgres/integrations/mcp-server",
        agent_focus=(
            "You create and inspect Prisma Postgres databases, read their schema, and help "
            "write and check a migration before it is applied."
        ),
    ),
    CatalogueConnector(
        name="paypal",
        label="PayPal",
        category="payments",
        description="Orders, invoices, subscriptions and disputes in PayPal.",
        url="https://mcp.paypal.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://www.paypal.ai/docs/tools/mcp-quickstart",
        agent_focus=(
            "You look up PayPal orders, invoices, subscriptions, transactions and disputes. "
            "Money moves when you write here, so every create or refund is confirmed first."
        ),
    ),
    CatalogueConnector(
        name="asana",
        label="Asana",
        category="work",
        description="Tasks, projects and portfolios in Asana.",
        url="https://mcp.asana.com/sse",
        transport="sse",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developers.asana.com/docs/mcp-server",
        agent_focus=(
            "You search, read, create and update Asana tasks, projects and portfolios, and "
            "you summarise what a project's tasks say is actually outstanding."
        ),
    ),
    CatalogueConnector(
        name="airtable",
        label="Airtable",
        category="data",
        description="Bases, tables and records in Airtable.",
        url="https://mcp.airtable.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://support.airtable.com/docs/airtable-mcp-server",
        agent_focus=(
            "You list Airtable bases and tables, read and filter records, and create or "
            "update rows -- naming the base and table every time before you write."
        ),
    ),
    CatalogueConnector(
        name="canva",
        label="Canva",
        category="design",
        description="Designs, folders and assets in Canva.",
        url="https://mcp.canva.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://www.canva.dev/docs/apps/mcp-server/",
        agent_focus=(
            "You find Canva designs and folders, create designs from a brief or a template, "
            "and export or share them when asked."
        ),
    ),
    CatalogueConnector(
        name="webflow",
        label="Webflow",
        category="design",
        description="Sites, pages, CMS collections and items in Webflow.",
        url="https://mcp.webflow.com/sse",
        transport="sse",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developers.webflow.com/data/docs/ai-tools",
        agent_focus=(
            "You read Webflow sites, pages and CMS collections, and you create or update "
            "collection items. Publishing a site is always confirmed first."
        ),
    ),
    CatalogueConnector(
        name="intercom",
        label="Intercom",
        category="chat",
        description="Conversations, contacts and help-centre articles in Intercom.",
        url="https://mcp.intercom.com/mcp",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developers.intercom.com/docs/guides/mcp",
        agent_focus=(
            "You search and read Intercom conversations, contacts and articles, and you "
            "summarise what customers are actually asking about over a period."
        ),
    ),
    CatalogueConnector(
        name="square",
        label="Square",
        category="payments",
        description="Payments, catalogue, orders and customers in Square.",
        url="https://mcp.squareup.com/sse",
        transport="sse",
        auth=ConnectorAuth(kind="oauth", dynamic_registration=True),
        docs_url="https://developer.squareup.com/docs/mcp",
        agent_focus=(
            "You look up Square payments, orders, catalogue items and customers. Money "
            "moves when you write here, so every create or refund is confirmed first."
        ),
    ),
    CatalogueConnector(
        name="box",
        label="Box",
        category="work",
        description="Files, folders and metadata in Box.",
        url="https://mcp.box.com",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=False,
            credential_hint=(
                'Box challenges with realm="Service" and publishes no registration '
                "metadata, so it needs a Box app you registered and its client id."
            ),
        ),
        docs_url="https://developer.box.com/guides/box-mcp/remote/",
        agent_focus=(
            "You search Box for files and folders, read their contents and metadata, and "
            "answer questions from what the documents themselves say."
        ),
    ),
    CatalogueConnector(
        name="posthog",
        label="PostHog",
        category="data",
        description="Product analytics, insights, feature flags and session data in PostHog.",
        url="https://mcp.posthog.com/mcp",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=True,
            token_alternative=True,
            credential_hint=(
                "A PostHog personal API key from your project settings; the server's own "
                "401 offers this as an alternative to the OAuth flow."
            ),
        ),
        docs_url="https://posthog.com/docs/model-context-protocol",
        agent_focus=(
            "You query PostHog insights, events, funnels and feature flags, and you answer "
            "product questions with the numbers the queries actually returned."
        ),
    ),
    CatalogueConnector(
        name="huggingface",
        label="Hugging Face",
        category="data",
        description="Models, datasets, Spaces and papers on the Hugging Face Hub.",
        url="https://huggingface.co/mcp",
        auth=ConnectorAuth(
            kind="oauth",
            dynamic_registration=True,
            token_alternative=True,
            optional=True,
            credential_hint=(
                "Optional. The endpoint answers without one; a Hugging Face access token "
                "from huggingface.co/settings/tokens raises limits and reaches private "
                "repositories."
            ),
        ),
        docs_url="https://huggingface.co/docs/hub/en/mcp",
        agent_focus=(
            "You search the Hugging Face Hub for models, datasets, Spaces and papers, and "
            "you read their cards to say what something is actually trained on or for."
        ),
    ),
    CatalogueConnector(
        name="zapier",
        label="Zapier",
        category="automation",
        description="Thousands of app actions exposed through a Zapier MCP endpoint.",
        url="https://mcp.zapier.com/api/mcp/mcp",
        auth=ConnectorAuth(
            kind="api_key",
            dynamic_registration=False,
            token_alternative=True,
            credential_hint=(
                "The API key from your own Zapier MCP endpoint at mcp.zapier.com -- it "
                "encodes which actions you exposed, so generate it after choosing them."
            ),
        ),
        docs_url="https://help.zapier.com/hc/en-us/articles/36265392843917",
        agent_focus=(
            "You run the specific app actions this workspace exposed through Zapier. The "
            "tool list is whatever was configured there, so check it before promising one."
        ),
    ),
    CatalogueConnector(
        name="hubspot",
        label="HubSpot",
        category="work",
        description="Contacts, companies, deals and engagements in a HubSpot CRM.",
        url="https://app.hubspot.com/mcp/v1/http",
        auth=ConnectorAuth(
            kind="api_key",
            # HubSpot also publishes protected-resource metadata, so the OAuth
            # flow works too; the API key is the path that needs no app.
            dynamic_registration=True,
            token_alternative=True,
            credential_hint=(
                "A HubSpot private app access token, with the CRM scopes you want the "
                "agent to reach, from your portal's private apps settings."
            ),
        ),
        docs_url="https://developers.hubspot.com/mcp",
        agent_focus=(
            "You search and read HubSpot contacts, companies, deals and engagements, and "
            "you create or update records and log activity when asked."
        ),
    ),
)


def connector_by_name(name: str) -> CatalogueConnector | None:
    return next((c for c in CONNECTORS if c.name == name), None)


AGENTS: tuple[CatalogueAgent, ...] = (
    *_specialist_agents(),
    CatalogueAgent(
        catalogue_id=PSYCH_AGENT_ID,
        name=PSYCH_AGENT_ID,
        description=(
            "The agent to talk to first. Understands what you want, delegates to the "
            "connector specialists that can get it, and reports back."
        ),
        connector=None,
        subagents=tuple(c.name for c in CONNECTORS),
    ),
)


# ---------------------------------------------------------------------------
# Workflows
#
# Each is a `CreateWorkflowRequest` factory taking the agent ids the
# specialists were published under, because a workflow's agent steps are
# resolved by id and a catalogue constant cannot know them. A workflow whose
# agents are not all present is simply not seeded -- see
# `app.catalogue_seed` -- rather than published with a step pointing nowhere.
# ---------------------------------------------------------------------------


def _path(path: str) -> ValueRefIn:
    return ValuePathIn(path=path)


def _literal(value: object) -> ValueRefIn:
    return LiteralValueIn(value=value)


def _ask(agent_id: str, name: str, message: str, *, description: str = "") -> AgentStepIn:
    """One agent step whose whole input is a written instruction.

    ``message`` is the field ``AgentStep`` treats as the first user message, so
    a literal here is exactly "say this to that agent". Steps that need a value
    from the Run's input interpolate it through a second mapped field instead
    of string-formatting it in, because the mapping is what the Run records.
    """
    return AgentStepIn(
        name=name,
        description=description,
        agent_id=agent_id,
        input={"message": _literal(message)},
    )


class CatalogueWorkflow(BaseModel):
    """One use-case workflow, and what it needs before it can be seeded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalogue_id: str
    name: str
    description: str
    connectors: tuple[str, ...]
    """The connectors whose specialists this workflow's steps run. Shown in the
    UI so somebody can see what to connect before running it."""
    agents: tuple[str, ...]
    """Catalogue agent ids every step names. All of them must have been seeded
    before this workflow can be published."""


def _approval(name: str, prompt: str) -> HumanStepIn:
    return HumanStepIn(name=name, prompt=prompt)


def github_activity_report(agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """Commits, pull requests and issues since a date, written up and optionally posted.

    The shape every other workflow here is a variation on: a specialist
    gathers, the orchestrator writes, a person approves, and only then does
    anything leave the building. The posting step sits behind a ``branch`` on
    ``input.post_to`` so the same workflow serves "just show me" and "put it in
    the channel" without two workflows to keep in step.
    """
    return CreateWorkflowRequest(
        name="github-activity-report",
        description=(
            "Gather GitHub activity since a date, write it up, get it approved, and "
            "optionally post it to Slack or Notion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "since": {"type": "string"},
                "post_to": {"type": "string", "enum": ["none", "slack", "notion"]},
            },
            "required": ["repository", "since"],
        },
        initial_state={"post_to": "none"},
        steps=[
            AgentStepIn(
                name="gather",
                description="Ask the GitHub specialist what happened in the repository.",
                agent_id=agents["github"],
                input={
                    "message": _literal(
                        "List what happened in this repository since the given date: merged "
                        "pull requests with their titles, numbers and authors; issues opened "
                        "and closed; and the notable commits on the default branch. Give "
                        "counts as well as the list, and say plainly if a period is empty."
                    ),
                    "repository": _path("input.repository"),
                    "since": _path("input.since"),
                },
            ),
            AgentStepIn(
                name="write",
                description="Turn the raw activity into a report somebody would read.",
                agent_id=agents[PSYCH_AGENT_ID],
                input={
                    "message": _literal(
                        "Write a short activity report from the findings below. Lead with "
                        "the one-paragraph summary, then themes, then a list of the merged "
                        "pull requests with numbers. Do not invent anything that is not in "
                        "the findings."
                    ),
                    "findings": _path("steps.gather.output"),
                    "since": _path("input.since"),
                },
            ),
            _approval(
                "approve",
                "Here is the activity report. Approve it before it is posted anywhere.",
            ),
            BranchStepIn(
                name="publish",
                description="Post the approved report, if the request asked for that.",
                cases=[
                    BranchCaseIn(
                        name="to_slack",
                        when=ConditionIn(path="input.post_to", op="eq", value="slack"),
                        step=AgentStepIn(
                            name="post_slack",
                            agent_id=agents["slack"],
                            input={
                                "message": _literal(
                                    "Post this approved activity report to the Slack channel "
                                    "the person names. Confirm the exact channel first."
                                ),
                                "report": _path("steps.write.output"),
                            },
                        ),
                    ),
                    BranchCaseIn(
                        name="to_notion",
                        when=ConditionIn(path="input.post_to", op="eq", value="notion"),
                        step=AgentStepIn(
                            name="post_notion",
                            agent_id=agents["notion"],
                            input={
                                "message": _literal(
                                    "Create a Notion page holding this approved activity "
                                    "report, under the parent page the person names."
                                ),
                                "report": _path("steps.write.output"),
                            },
                        ),
                    ),
                ],
            ),
            MapStepIn(
                name="result",
                output={
                    "report": _path("steps.write.output"),
                    "repository": _path("input.repository"),
                    "since": _path("input.since"),
                },
            ),
        ],
        output={"report": _path("steps.result.output.report")},
    )


def jira_sprint_digest(agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """What the current sprint actually did, and what is stuck.

    The ``sleep`` step is deliberate and short: Jira's search is eventually
    consistent right after a board transition, and a digest run from a chat
    message seconds after somebody moved the last ticket otherwise reports the
    board as it was.
    """
    return CreateWorkflowRequest(
        name="jira-sprint-digest",
        description="Summarise a Jira sprint: what shipped, what slipped, and what is blocked.",
        input_schema={
            "type": "object",
            "properties": {"project": {"type": "string"}, "sprint": {"type": "string"}},
            "required": ["project"],
        },
        steps=[
            SleepStepIn(
                name="settle",
                description="Let a just-finished board transition become visible to search.",
                seconds=5.0,
            ),
            AgentStepIn(
                name="gather",
                agent_id=agents["atlassian"],
                input={
                    "message": _literal(
                        "For the named Jira project and sprint, list the issues by status: "
                        "done, in progress, blocked and not started. Give each issue's key, "
                        "summary, assignee and how long it has been in its current status."
                    ),
                    "project": _path("input.project"),
                    "sprint": _path("input.sprint"),
                },
            ),
            AgentStepIn(
                name="write",
                agent_id=agents[PSYCH_AGENT_ID],
                input={
                    "message": _literal(
                        "Write the sprint digest from the issue list below: what shipped, "
                        "what slipped and why, and what is blocked on whom. Name issue keys. "
                        "Be direct about the blocked items -- that is the part people read."
                    ),
                    "issues": _path("steps.gather.output"),
                },
            ),
            _approval("approve", "Here is the sprint digest. Approve or send it back."),
        ],
        output={"digest": _path("steps.write.output")},
    )


def inbox_and_issues_triage(agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """Read the chat, read the tracker, and propose one triage plan for both.

    The two gathers run in a ``parallel`` step because neither needs the
    other: this is the shape the orchestrator is told to use, written down
    once so a person can see what "run them together" means.
    """
    return CreateWorkflowRequest(
        name="inbox-and-issues-triage",
        description=(
            "Read recent Slack messages and open Linear issues together, and propose what "
            "to do about each."
        ),
        input_schema={
            "type": "object",
            "properties": {"channel": {"type": "string"}, "team": {"type": "string"}},
            "required": ["channel", "team"],
        },
        steps=[
            ParallelStepIn(
                name="gather",
                description="Chat and tracker are independent, so both are read at once.",
                on_branch_failure="wait_all",
                branches=[
                    AgentStepIn(
                        name="slack",
                        agent_id=agents["slack"],
                        input={
                            "message": _literal(
                                "Read the recent messages in the named channel and list the "
                                "ones that are asking for something: who asked, what they "
                                "want, and whether anybody has answered."
                            ),
                            "channel": _path("input.channel"),
                        },
                    ),
                    AgentStepIn(
                        name="linear",
                        agent_id=agents["linear"],
                        input={
                            "message": _literal(
                                "List the open, untriaged issues for the named team: "
                                "identifier, title, age, and whether they have a priority "
                                "and an assignee."
                            ),
                            "team": _path("input.team"),
                        },
                    ),
                ],
            ),
            AgentStepIn(
                name="plan",
                agent_id=agents[PSYCH_AGENT_ID],
                input={
                    "message": _literal(
                        "Propose a triage plan from the two lists below. For each item say "
                        "what it is, what should happen to it, and who should own it. Group "
                        "the requests that are really the same thing. Recommend nothing "
                        "that is not backed by an item in the lists."
                    ),
                    "messages": _path("steps.gather.slack.output"),
                    "issues": _path("steps.gather.linear.output"),
                },
            ),
            _approval(
                "approve",
                "Here is the proposed triage. Approve it before anything is changed.",
            ),
        ],
        output={"plan": _path("steps.plan.output")},
    )


def release_notes(agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """Merged pull requests since a tag, turned into notes a human approves."""
    return CreateWorkflowRequest(
        name="release-notes",
        description="Draft release notes from the pull requests merged since a tag.",
        input_schema={
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "tag": {"type": "string"},
                "version": {"type": "string"},
            },
            "required": ["repository", "tag"],
        },
        steps=[
            AgentStepIn(
                name="gather",
                agent_id=agents["github"],
                input={
                    "message": _literal(
                        "List every pull request merged into the default branch since the "
                        "named tag: number, title, author and labels. Note which ones are "
                        "breaking changes or fixes, going by their labels and titles."
                    ),
                    "repository": _path("input.repository"),
                    "tag": _path("input.tag"),
                },
            ),
            AgentStepIn(
                name="write",
                agent_id=agents[PSYCH_AGENT_ID],
                input={
                    "message": _literal(
                        "Write release notes from the merged pull requests below, grouped "
                        "into Breaking changes, Added, Fixed and Internal. One line each, "
                        "written for somebody upgrading, with the pull request number in "
                        "brackets. Leave out a group that has nothing in it."
                    ),
                    "pull_requests": _path("steps.gather.output"),
                    "version": _path("input.version"),
                },
            ),
            _approval(
                "approve",
                "Here are the draft release notes. Approve them before they go anywhere.",
            ),
        ],
        output={"notes": _path("steps.write.output")},
    )


def incident_summary(agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """What broke, since when, and what shipped just before it.

    Sentry and GitHub are read in parallel for the same reason as the triage
    workflow, and the write-up is explicitly told to treat the correlation as a
    suspicion rather than a cause -- a deploy landing before an error spike is
    evidence, not a verdict.
    """
    return CreateWorkflowRequest(
        name="incident-summary",
        description=(
            "Pull a Sentry issue together with the code that shipped around it, and "
            "summarise the incident."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "repository": {"type": "string"},
                "since": {"type": "string"},
            },
            "required": ["issue", "repository", "since"],
        },
        steps=[
            ParallelStepIn(
                name="gather",
                on_branch_failure="wait_all",
                branches=[
                    AgentStepIn(
                        name="sentry",
                        agent_id=agents["sentry"],
                        input={
                            "message": _literal(
                                "Read the named Sentry issue: the error, its stack trace, "
                                "when it started, how often it fires, how many users it "
                                "touched, and which release it first appeared in."
                            ),
                            "issue": _path("input.issue"),
                        },
                    ),
                    AgentStepIn(
                        name="github",
                        agent_id=agents["github"],
                        input={
                            "message": _literal(
                                "List what merged into the named repository since the given "
                                "time: pull request numbers, titles and the files each one "
                                "touched."
                            ),
                            "repository": _path("input.repository"),
                            "since": _path("input.since"),
                        },
                    ),
                ],
            ),
            AgentStepIn(
                name="write",
                agent_id=agents[PSYCH_AGENT_ID],
                input={
                    "message": _literal(
                        "Write the incident summary from the error report and the merge "
                        "list below: what is failing, since when, how bad it is, and which "
                        "changes landed close enough in time to be worth looking at. Say "
                        "plainly that a correlation in time is a suspicion and not a cause, "
                        "and end with the next thing somebody should check."
                    ),
                    "error": _path("steps.gather.sentry.output"),
                    "changes": _path("steps.gather.github.output"),
                },
            ),
            _approval("approve", "Here is the incident summary. Approve or amend it."),
        ],
        output={"summary": _path("steps.write.output")},
    )


WORKFLOWS: tuple[CatalogueWorkflow, ...] = (
    CatalogueWorkflow(
        catalogue_id="github-activity-report",
        name="github-activity-report",
        description=(
            "Gather GitHub activity since a date, write it up, get it approved, and "
            "optionally post it to Slack or Notion."
        ),
        connectors=("github", "slack", "notion"),
        agents=("github", "slack", "notion", PSYCH_AGENT_ID),
    ),
    CatalogueWorkflow(
        catalogue_id="jira-sprint-digest",
        name="jira-sprint-digest",
        description="Summarise a Jira sprint: what shipped, what slipped, and what is blocked.",
        connectors=("atlassian",),
        agents=("atlassian", PSYCH_AGENT_ID),
    ),
    CatalogueWorkflow(
        catalogue_id="inbox-and-issues-triage",
        name="inbox-and-issues-triage",
        description=(
            "Read recent Slack messages and open Linear issues together, and propose what "
            "to do about each."
        ),
        connectors=("slack", "linear"),
        agents=("slack", "linear", PSYCH_AGENT_ID),
    ),
    CatalogueWorkflow(
        catalogue_id="release-notes",
        name="release-notes",
        description="Draft release notes from the pull requests merged since a tag.",
        connectors=("github",),
        agents=("github", PSYCH_AGENT_ID),
    ),
    CatalogueWorkflow(
        catalogue_id="incident-summary",
        name="incident-summary",
        description=(
            "Pull a Sentry issue together with the code that shipped around it, and "
            "summarise the incident."
        ),
        connectors=("sentry", "github"),
        agents=("sentry", "github", PSYCH_AGENT_ID),
    ),
)


_WorkflowBuilder = Callable[[Mapping[str, str]], CreateWorkflowRequest]
"""What every workflow factory above is: agent ids in, a request body out."""

_BUILDERS: Mapping[str, _WorkflowBuilder] = {
    "github-activity-report": github_activity_report,
    "jira-sprint-digest": jira_sprint_digest,
    "inbox-and-issues-triage": inbox_and_issues_triage,
    "release-notes": release_notes,
    "incident-summary": incident_summary,
}


def workflow_request(catalogue_id: str, agents: Mapping[str, str]) -> CreateWorkflowRequest:
    """The request body for one catalogue workflow.

    Args:
        catalogue_id: one of ``WORKFLOWS``.
        agents: catalogue agent id to published ``agent_id``. Every id in that
            workflow's ``agents`` must be present; the seeder checks first and
            skips a workflow whose agents are not all there, so a ``KeyError``
            here would mean a catalogue entry disagrees with its own factory.
    """
    return _BUILDERS[catalogue_id](agents)
