"""Writing ``app.catalogue`` into one account, once.

``app.catalogue`` is data. This is the half that touches state: it adds
provider presets, MCP server presets, published agents and published workflows
to an account that does not have them yet, and does nothing at all to one that
does.

## Idempotence, and where each half's key lives

Seeding runs on signup and again whenever somebody presses the button, so
"already there" has to be answerable for every kind. Two different answers,
because the two halves are stored differently:

*Providers and connectors* live in ``PlaygroundState``, which
``PUT /api/settings/providers`` and ``PUT /api/settings/mcp`` rewrite wholesale
from a request body. A provenance field on ``ProviderConfig`` would survive
exactly until the first time somebody saved the settings page -- the request
shape has nowhere to carry it -- and the next seed would then duplicate
everything. So the key is the identity itself: a catalogue provider is
``ProviderConfig.id == catalogue.id`` and a catalogue connector is
``McpServerPreset.name == connector.name``. Nothing to round-trip, nothing to
lose, and a person who deliberately deleted one gets it back on the next seed,
which is the behaviour a "restore the defaults" button wants anyway.

*Agents and workflows* live in ``PlaygroundIndex``, which nothing rewrites
wholesale, so they carry ``catalogue_id`` outright. That matters more than it
sounds: an agent is meant to be edited, including renamed, and matching on the
name would re-seed a second ``github`` the moment somebody called theirs
"GitHub (read-only)".

## Order, and why a partial seed is fine

Specialists are published before the orchestrator, because the orchestrator's
roster embeds each child's current Version (DESIGN.md §17) and a roster entry
naming an agent that does not exist is a 404 at publish. Workflows come last,
for the same reason applied to agent steps. Every stage tolerates the previous
one having been partial: the orchestrator's roster is built from the
specialists that *are* there, and a workflow whose agents are not all present
is skipped rather than published with a step pointing nowhere.

## What seeding never does

It never writes a secret. An API-key connector's preset names the credential
it wants (``McpServerPreset.credential``) and the secret itself stays absent
until a person pastes one, so the resolver reports a missing credential -- the
honest state, and one the settings page can show. It never writes an API key
for a provider either: a seeded provider is a base URL, a model and a label,
and the one thing the person has to do is the one thing nobody else can.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from app import catalogue
from app.agent_publish import publish_agent
from app.catalogue import PSYCH_AGENT_ID, CatalogueConnector, CatalogueProvider
from app.errors import ApiProblem
from app.ports import SandboxProvider
from app.settings_store import (
    McpOAuthPreset,
    McpServerPreset,
    PlaygroundState,
    ProviderConfig,
    SettingsStore,
)
from app.store_index import PlaygroundIndex
from app.tools import registry
from app.workflows import WorkflowDefinition, WorkflowRefused, WorkflowService
from psych_runtime.store.port import Store

SeedKind = str
"""One of ``providers``, ``connectors``, ``agents``, ``workflows``."""

ALL_KINDS: tuple[SeedKind, ...] = ("providers", "connectors", "agents", "workflows")


class _SeedSettings(Protocol):
    """The two fields of app.config.Settings seeding reads.

    Properties rather than attributes so the match is covariant: a Protocol
    attribute is read-write, and a concrete str field then fails to satisfy
    a str | None one for reasons that have nothing to do with this code.
    """

    @property
    def oauth_callback_url(self) -> str: ...

    @property
    def default_approval_selectors(self) -> tuple[str, ...]: ...


class SeedDeps(Protocol):
    """What seeding needs from the application.

    A Protocol rather than ``app.main.AppState`` because ``app.main`` imports
    this module (``signup`` calls it), so an import the other way would be a
    cycle. ``AppState`` satisfies it structurally, and a test can satisfy it
    with four attributes.
    """

    @property
    def store(self) -> Store: ...

    @property
    def index(self) -> PlaygroundIndex: ...

    @property
    def settings_store(self) -> SettingsStore: ...

    @property
    def sandbox(self) -> SandboxProvider: ...

    @property
    def workflows(self) -> WorkflowService: ...

    @property
    def settings(self) -> _SeedSettings: ...


class SeedResult:
    """What one seeding run added, per kind, by catalogue id.

    Empty lists on a second run: that is the assertion "idempotent" actually
    means, and returning it rather than a bare count is what lets the console
    say *which* things appeared.
    """

    __slots__ = ("agents", "connectors", "providers", "workflows")

    def __init__(self) -> None:
        self.providers: list[str] = []
        self.connectors: list[str] = []
        self.agents: list[str] = []
        self.workflows: list[str] = []

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "providers": list(self.providers),
            "connectors": list(self.connectors),
            "agents": list(self.agents),
            "workflows": list(self.workflows),
        }


def _provider_config(entry: CatalogueProvider) -> ProviderConfig:
    return ProviderConfig(
        id=entry.id,
        label=entry.label,
        base_url=catalogue.resolve_base_url(entry),
        model=entry.default_model,
        api_key=None,
    )


def _connector_preset(entry: CatalogueConnector) -> McpServerPreset:
    """One connector as the preset the settings page and agent builder read.

    ``optional=True`` and ``preload=False`` are the two that matter and are
    explained on ``CatalogueConnector.mcp_server``: an unconnected server must
    not stop its specialist publishing or running, and these catalogues are far
    too large to inline into a prompt.
    """
    return McpServerPreset(
        name=entry.name,
        url=entry.url,
        description=entry.description,
        transport=entry.transport,
        credential=entry.credential_name if entry.auth.kind == "api_key" else None,
        optional=True,
        preload=False,
        oauth=(
            McpOAuthPreset(
                grant="authorization_code",
                allow_dynamic_registration=entry.auth.dynamic_registration,
                client_name="psych-playground",
            )
            if entry.auth.kind == "oauth"
            else None
        ),
    )


def _seed_providers(state: PlaygroundState, result: SeedResult) -> PlaygroundState:
    have = {p.id for p in state.providers}
    added = [_provider_config(entry) for entry in catalogue.PROVIDERS if entry.id not in have]
    if not added:
        return state
    result.providers.extend(p.id for p in added)
    providers = [*state.providers, *added]
    # Activated only when nothing is. A person who already picked a provider
    # keeps it; a fresh account gets a selection rather than a dropdown reading
    # "none", which is the difference between a settings page that asks for a
    # key and one that asks what any of this is.
    active = state.active_provider_id or added[0].id
    return state.model_copy(update={"providers": providers, "active_provider_id": active})


def _seed_connectors(state: PlaygroundState, result: SeedResult) -> PlaygroundState:
    have = {s.name for s in state.mcp_servers}
    added = [_connector_preset(c) for c in catalogue.CONNECTORS if c.name not in have]
    if not added:
        return state
    result.connectors.extend(s.name for s in added)
    return state.model_copy(update={"mcp_servers": [*state.mcp_servers, *added]})


def _model_for(state: PlaygroundState) -> str:
    """The model id a seeded agent is published with.

    The account's active provider first, then whatever provider it has, then
    the catalogue's own first default. Never empty: a Spec with no model fails
    validation, and a signup that failed because nobody had pasted a key yet
    would defeat the whole point of seeding.
    """
    by_id = {p.id: p for p in state.providers}
    active = by_id.get(state.active_provider_id or "")
    if active is not None:
        return active.model
    if state.providers:
        return state.providers[0].model
    return catalogue.PROVIDERS[0].default_model


async def _seeded_agent_ids(index: PlaygroundIndex, account_id: str) -> dict[str, str]:
    """Catalogue id to published ``agent_id``, for this account's catalogue agents."""
    found: dict[str, str] = {}
    for pointer, entry in await index.list_agents(account_id):
        if entry.catalogue_id:
            found[entry.catalogue_id] = pointer.agent_id
    return found


PSYCH_TOOLS: tuple[str, ...] = (
    "current_time",
    "calculate",
    "list_agents",
    "create_agent",
    "update_agent",
    "list_workflows",
    "create_workflow",
    "run_workflow",
)


async def _seed_agents(
    deps: SeedDeps, account_id: str, state: PlaygroundState, result: SeedResult
) -> None:
    model = _model_for(state)
    profiles = deps.sandbox.profile_names(state.runtime)
    existing = await _seeded_agent_ids(deps.index, account_id)

    for connector in catalogue.CONNECTORS:
        if connector.name in existing:
            continue
        published = await publish_agent(
            connector.agent_request(model),
            owner=account_id,
            store=deps.store,
            index=deps.index,
            registry=registry,
            sandbox_profiles=profiles,
            oauth_redirect_uri=deps.settings.oauth_callback_url,
            default_approval_selectors=deps.settings.default_approval_selectors,
            catalogue_id=connector.name,
        )
        existing[connector.name] = published.agent_id
        result.agents.append(connector.name)

    specialists = {k: v for k, v in existing.items() if k != PSYCH_AGENT_ID}
    # Every host tool, the writing ones included. The orchestrator is the agent
    # a person asks to "make me an agent for X" or "run the sprint digest", so
    # it needs `create_agent`, `create_workflow` and `run_workflow`, and the
    # `@write`/`@destructive` selectors on its request make each of those a
    # suspension the person approves in the console before anything is filed.
    body = catalogue.psych_agent_request(model, specialists, tools=PSYCH_TOOLS)
    psych_id = existing.get(PSYCH_AGENT_ID)
    if psych_id is None:
        published = await publish_agent(
            body,
            owner=account_id,
            store=deps.store,
            index=deps.index,
            registry=registry,
            sandbox_profiles=profiles,
            oauth_redirect_uri=deps.settings.oauth_callback_url,
            default_approval_selectors=deps.settings.default_approval_selectors,
            catalogue_id=PSYCH_AGENT_ID,
        )
        result.agents.append(PSYCH_AGENT_ID)
    elif await _roster_is_stale(deps, account_id, psych_id, specialists):
        # Republished rather than left alone, and deliberately *not* counted as
        # added: a seed that added three new specialists has to put them on the
        # orchestrator's roster, because a roster embeds children at publish and
        # never updates itself afterwards. Nothing is "added" -- the same agent id gains
        # a new Version -- so a caller reading ``added`` still sees an idempotent
        # second run.
        body.agent_id = psych_id
        await publish_agent(
            body,
            owner=account_id,
            store=deps.store,
            index=deps.index,
            registry=registry,
            sandbox_profiles=profiles,
            oauth_redirect_uri=deps.settings.oauth_callback_url,
            default_approval_selectors=deps.settings.default_approval_selectors,
            catalogue_id=PSYCH_AGENT_ID,
        )


async def _roster_is_stale(
    deps: SeedDeps, account_id: str, psych_id: str, specialists: Mapping[str, str]
) -> bool:
    found = await deps.index.get_agent(account_id, psych_id)
    if found is None:
        return False
    return {ref.agent_id for ref in found[1].subagents} != set(specialists.values())


async def _seed_workflows(deps: SeedDeps, account_id: str, result: SeedResult) -> None:
    have = {
        entry.catalogue_id
        for entry in await deps.index.list_workflows(account_id)
        if entry.catalogue_id
    }
    agents = await _seeded_agent_ids(deps.index, account_id)
    for entry in catalogue.WORKFLOWS:
        if entry.catalogue_id in have:
            continue
        if not all(name in agents for name in entry.agents):
            # A workflow whose agents are not all seeded is left for the next
            # run rather than published with a step naming nothing. This is the
            # case a partial seed -- or a person who deleted a specialist --
            # actually produces.
            continue
        request = catalogue.workflow_request(entry.catalogue_id, agents)
        definition = WorkflowDefinition(
            name=request.name,
            description=request.description,
            steps=request.steps,
            limits=request.limits,
            input_schema=request.input_schema,
            initial_state=request.initial_state,
            output=request.output,
            retry=request.retry,
        )
        try:
            await deps.workflows.publish(account_id, definition, catalogue_id=entry.catalogue_id)
        except WorkflowRefused as err:
            raise ApiProblem(
                err.status, f"catalogue workflow {entry.catalogue_id!r}: {err}"
            ) from err
        result.workflows.append(entry.catalogue_id)


async def seed_catalogue(
    deps: SeedDeps, account_id: str, *, kinds: Sequence[SeedKind] = ALL_KINDS
) -> SeedResult:
    """Add whatever of the catalogue this account does not already have.

    Args:
        deps: the application's store, index, settings store, sandbox and
            workflow service.
        account_id: whose workspace to seed.
        kinds: which halves to run, in case a caller wants only one. Order is
            fixed by dependency regardless of the order given -- agents need
            connectors to attach and workflows need agents to run -- so a
            caller asking for ``("workflows", "agents")`` gets agents first.

    Returns:
        What was added, per kind, by catalogue id. Empty everywhere on a second
        run.
    """
    wanted = set(kinds)
    result = SeedResult()

    if {"providers", "connectors"} & wanted:

        def apply(state: PlaygroundState) -> PlaygroundState:
            if "providers" in wanted:
                state = _seed_providers(state, result)
            if "connectors" in wanted:
                state = _seed_connectors(state, result)
            return state

        await deps.settings_store.update(account_id, apply)

    state = await deps.settings_store.load(account_id)
    if "agents" in wanted:
        await _seed_agents(deps, account_id, state, result)
    if "workflows" in wanted:
        await _seed_workflows(deps, account_id, result)
    return result
