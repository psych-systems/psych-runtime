"""The playground backend: routes, lifespan, and nothing Psych should own.

This is the "platform" DESIGN.md §1 refuses to be: an HTTP server, CORS for a
frontend served from a different origin, and the bookkeeping (``PlaygroundIndex``)
Psych's own ``Store`` deliberately has no query for. Everything that touches a
Run or a Spec goes through ``psych``'s public API (``psych_runtime.publish``,
``psych_runtime.dispatch``, ``psych_runtime.stream``, ...) exactly as a real consumer would call
it; nothing here reaches into the reducer or the log directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

import psych_runtime
from app.a2a import (
    A2ADeps,
    A2AService,
    CardFactory,
    PushConfigStore,
    PushSender,
    build_router,
    load_signing_key,
)
from app.accounts import Account, Session, hash_session_token, new_session_token
from app.auth import (
    PUBLIC_PATHS,
    account_from_request,
    account_summary,
    clear_session_cookie,
    is_first_account,
    require_account,
    scope_for,
    set_session_cookie,
    sign_in,
    sign_out,
)
from app.auth import create_account as register_account
from app.config import Settings, load_allowed_origins, load_settings
from app.errors import ApiProblem, from_pydantic, from_spec_validation
from app.memory_store import FileMemoryStore
from app.oauth_redirect import BrowserAuthorizationRedirect
from app.observability import Observability, build_telemetry, configure_logging
from app.ports import AccountEgressPolicy, AccountToolPolicy, SandboxProvision, same_uid_opt_in
from app.runtime_router import AccountRoutedRuntime, active_provider
from app.scenarios import SCENARIOS
from app.scenarios.base import Assertion, ScenarioContext, ScenarioModule, ScenarioResult
from app.schemas import (
    A2APeerPresetOut,
    A2ATokenRequest,
    A2ATokenResponse,
    AccountResponse,
    AgentSummary,
    AgentVersionSummary,
    AnswerResponse,
    BranchRequest,
    CompactionIn,
    ConfigResponse,
    CreateAgentRequest,
    CreateAgentResponse,
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    DispatchRequest,
    DispatchResponse,
    ForkRequest,
    HealthResponse,
    HttpToolIn,
    InterruptRequest,
    LocalPeerRequest,
    McpConnectionOut,
    McpLiveConnectionOut,
    McpOAuthIn,
    McpServerHintOut,
    McpServerPresetIn,
    McpServerPresetOut,
    McpTestResponse,
    MemoriesResponse,
    MemoryOut,
    MessageOut,
    ModelOptionsIn,
    ModelPriceOut,
    ModelsResponse,
    OkResponse,
    PendingAuthorizationOut,
    ProblemResponse,
    ProviderOut,
    ProviderTestResponse,
    ResumeRequest,
    RunSummary,
    RuntimeSettingsIn,
    RuntimeSettingsOut,
    SandboxLimitsIn,
    ScenarioAssertionOut,
    ScenarioOut,
    ScenarioProgressEvent,
    ScenarioRunResultOut,
    SecretsResponse,
    SendRequest,
    SendResponse,
    SettingsResponse,
    SignInRequest,
    SignUpRequest,
    SkillOut,
    SpawnIn,
    SubagentMessageRequest,
    SubagentNodeOut,
    SubagentRefOut,
    SubagentRetryResponse,
    SubagentTreeResponse,
    SuspensionIn,
    ThreadMessageOut,
    ThreadReportResponse,
    ThreadResponse,
    ThreadTotalsOut,
    ToolCallOut,
    ToolOut,
    TracingOut,
    UpdateA2APeersRequest,
    UpdateMcpServersRequest,
    UpdateModelPricesRequest,
    UpdateProvidersRequest,
    UpdateSecretsRequest,
    UpdateSkillsRequest,
    WorkflowSummary,
    WorkTurnOut,
)
from app.secrets import AccountSecretResolver
from app.serialization import settled_at
from app.settings_store import (
    A2APeerPreset,
    McpConnectionRecord,
    McpOAuthPreset,
    McpServerPreset,
    ModelPriceEntry,
    PlaygroundState,
    ProviderConfig,
    RuntimeSettings,
    SandboxLimitsEntry,
    SettingsStore,
    SkillPreset,
    StateFile,
    new_provider_id,
)
from app.spec_builder import build_agent_spec
from app.store_index import (
    AgentEntry,
    AgentPointer,
    CompactionEntry,
    HttpToolEntry,
    PlaygroundIndex,
    RunEntry,
    SkillEntry,
    SubagentRefEntry,
    WorkflowEntry,
    new_agent_id,
    new_branch_id,
    new_conversation_id,
)
from app.tools import install_workflow_publisher, registry
from app.workflows import (
    ToolWorkflowPublisher,
    WorkflowDefinition,
    WorkflowRefused,
    WorkflowService,
    summary_of,
)
from psych_runtime.core.corruption import CorruptLog
from psych_runtime.core.errors import (
    AccessDenied,
    PsychError,
    RunAlreadySettled,
    RunEndedWithoutAnswer,
    RunNotFound,
    RunNotSuspended,
    SpecValidationError,
    SuspensionExpired,
    TransientError,
)
from psych_runtime.core.ids import RunId, VersionHash
from psych_runtime.core.messages import UserMessage
from psych_runtime.core.records import QueueKind, RunAdmitted
from psych_runtime.core.spec import CodeTool, HttpTool
from psych_runtime.core.status import SETTLED_LIFECYCLE
from psych_runtime.core.thread_view import message_views
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.core.validation import ValidationContext
from psych_runtime.model.egress import HttpTransport
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import ModelRequest, StreamDone
from psych_runtime.report.model import RunReport, SubagentReport
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.blob_fs import FilesystemBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.tools.a2a import A2APool, A2ATools
from psych_runtime.tools.http import HttpToolExecutor
from psych_runtime.tools.mcp import McpConnectionStatus, McpPool, McpTools
from psych_runtime.tools.oauth import OAuthClient
from psych_runtime.tools.oauth.redirect import AuthorizationCallback

_LOG = logging.getLogger("psych.playground")

_SSE_KEEPALIVE_SECONDS = 15.0
"""How long an idle stream waits before sending a comment line. Short enough
for the proxies that drop a silent connection at 30 or 60 seconds."""

# There is no process-wide tenant any more. Every Run, every connection test
# and every model call is admitted under the Scope of the account that asked
# for it (`app.auth.scope_for`), which is what makes the isolation real rather
# than merely shaped: the MCP pool keys by tenant, so a connection made under
# any other Scope is not the connection that account's Run would get.
#
# `DEFAULT_TENANT` used to live here and `POST /api/runs` used to let a caller
# override it from the request body, so anyone could read anyone's Runs. See
# `app.auth`.


class AppState:
    """Everything the routes need, assembled once in ``lifespan`` and torn
    down there. One instance per process, hung off ``app.state.playground``."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        index: PlaygroundIndex,
        transport: HttpTransport,
        worker: Worker,
        worker_task: asyncio.Task[None],
        settings_store: SettingsStore,
        secrets: AccountSecretResolver,
        runner: AccountRoutedRuntime,
        mcp: McpTools,
        redirect: BrowserAuthorizationRedirect,
        descriptions: dict[tuple[str, str], str],
        memory: FileMemoryStore,
        a2a: A2ADeps,
        observability: Observability,
        sandbox: SandboxProvision,
        workflows: WorkflowService,
        a2a_base_url: str,
    ) -> None:
        self.sandbox = sandbox
        self.workflows = workflows
        self.a2a_base_url = a2a_base_url
        self.settings = settings
        self.store = store
        self.index = index
        self.transport = transport
        self.worker = worker
        self.worker_task = worker_task
        self.settings_store = settings_store
        self.observability = observability
        """Where this process's spans go, and whether anything is collecting
        them. Read by ``GET /api/config`` so the console can say so rather than
        showing a trace screen that is empty for an unexplained reason."""
        self.a2a = a2a
        """Everything the A2A routes need (`app.a2a`). Assembled here beside
        the rest so a peer agent talks to the same store, the same index and
        the same Worker the console does: an A2A task is a Run like any other
        and shows up in the console's own history."""
        self.memory = memory
        """Durable facts the agent has remembered, per account. Held so the
        memory routes can read and erase what a Run wrote, through the same
        adapter the Runtime writes with."""
        self.secrets = secrets
        self.descriptions = descriptions
        """``(tenant, url)`` to description, read live by the runtime.
        Rewritten whenever an account's connection list changes, so an edited
        description reaches the next turn without a restart.

        Keyed by tenant as well as URL because a description is a fact about a
        *connection* and a connection belongs to one account. Two accounts can
        configure the same URL and mean different things by it, and a map keyed
        on the URL alone would put one account's words into the other's system
        prompt."""
        self.redirect = redirect
        """Where a browser lands for an authorization_code grant. Held so
        the callback and pending-list routes can reach the same instance the
        OAuthClient inside the pool is waiting on."""
        self.mcp = mcp
        """The same ``McpTools`` (and so the same pool) the runtime uses, so
        testing a preset exercises the real connection path -- credential
        resolution, OAuth, protocol negotiation, allow-list narrowing --
        rather than a simplified probe that could pass where a Run would
        fail."""
        self.runner = runner
        """Kept beyond what ``Worker`` needs so the provider-test route can
        borrow ``runner.model_for(scope)`` and exercise the very client a Run
        would use, rather than a second one built to look like it."""


def _build_store(settings: Settings) -> Store:
    if settings.postgres_dsn is not None:
        return PostgresStore(dsn=settings.postgres_dsn)
    return InMemoryStore()


def _hint_transport(value: str) -> Literal["http", "sse"]:
    """``McpServerHint.transport`` is a plain ``str`` (``app.config`` accepts
    whatever ``PSYCH_PLAYGROUND_MCP_SERVERS`` names); ``McpServerPreset``'s is
    the narrower ``Literal`` an actual ``McpServer`` field allows. Anything
    else in the env var normalises to the same default ``McpServer.transport``
    itself defaults to, rather than failing boot over a typo in a value that
    was only ever informational before this."""
    return "sse" if value == "sse" else "http"


def _hint_grant(value: str) -> Literal["authorization_code", "client_credentials"]:
    return "authorization_code" if value == "authorization_code" else "client_credentials"


def _seed_state(settings: Settings) -> PlaygroundState:
    """The first ``PlaygroundState`` a fresh state file gets: one provider
    built from the env vars this process already booted with, active, plus
    whatever MCP hints and secrets those same env vars named. Never called
    once the file holds a provider of its own -- see the call site in
    ``lifespan``.

    With no provider in the environment there is no provider to seed, and the
    workspace starts empty with ``active_provider_id=None``. That is the state
    ``active_provider()`` is written to return None for and the Settings page
    exists to fill in. Seeding a ``ProviderConfig`` with an empty ``base_url``
    instead would be worse than seeding nothing: the console would list a
    provider that looks configured, and the first Run against it would fail at
    the transport with a URL error rather than saying no provider is set.
    """
    provider = (
        ProviderConfig(
            id="default",
            label="default",
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
        )
        if settings.base_url
        else None
    )
    mcp_servers = [
        McpServerPreset(
            name=hint.name,
            url=hint.url,
            transport=_hint_transport(hint.transport),
            oauth=McpOAuthPreset(grant=_hint_grant(hint.oauth_grant))
            if hint.oauth_grant is not None
            else None,
        )
        for hint in settings.mcp_server_hints
    ]
    return PlaygroundState(
        providers=[provider] if provider is not None else [],
        mcp_servers=mcp_servers,
        secrets=dict(settings.secrets),
        active_provider_id=provider.id if provider is not None else None,
    )


def _refresh_descriptions(
    target: dict[tuple[str, str], str], tenant: str, state: PlaygroundState
) -> None:
    """Rebuild one account's half of the ``(tenant, url)``-to-description map.

    Rewritten in place rather than replaced so the closure handed to
    ``McpTools`` at boot keeps seeing the current answer. Only this tenant's
    entries are cleared, so refreshing one account's connections never blanks
    another's descriptions; within the tenant everything is cleared first, so a
    description removed from a connection stops reaching the prompt rather than
    lingering until a restart.
    """
    for key in [k for k in target if k[0] == tenant]:
        del target[key]
    for preset in state.mcp_servers:
        if preset.description.strip():
            target[(tenant, preset.url)] = preset.description


async def _refresh_all_descriptions(
    target: dict[tuple[str, str], str], settings_store: SettingsStore
) -> None:
    """Load every account's descriptions at boot.

    The map is read synchronously per turn by design (``McpTools``'s
    ``describe_server`` is deliberately not ``async``: it must not do IO), so
    it has to be populated before any Run executes rather than filled in
    lazily on first use.
    """
    file = await settings_store.load_file()
    for account in file.accounts.accounts:
        _refresh_descriptions(target, account.id, file.workspace(account.id))


def _provider_out(provider: ProviderConfig) -> ProviderOut:
    return ProviderOut(
        id=provider.id,
        label=provider.label,
        base_url=provider.base_url,
        model=provider.model,
        has_api_key=bool(provider.api_key),
    )


def _mcp_preset_out(
    preset: McpServerPreset, live: Mapping[str, McpConnectionStatus] | None = None
) -> McpServerPresetOut:
    status = (live or {}).get(preset.name)
    return McpServerPresetOut(
        name=preset.name,
        url=preset.url,
        description=preset.description,
        transport=preset.transport,
        credential=preset.credential,
        allow=list(preset.allow),
        optional=preset.optional,
        preload=preset.preload,
        oauth=McpOAuthIn(**preset.oauth.model_dump()) if preset.oauth is not None else None,
        last_connection=(
            McpConnectionOut(
                ok=preset.last_connection.ok,
                detail=preset.last_connection.detail,
                checked_at=preset.last_connection.checked_at.isoformat(),
                tools=list(preset.last_connection.tools),
                error_type=preset.last_connection.error_type,
            )
            if preset.last_connection is not None
            else None
        ),
        live=(
            McpLiveConnectionOut(
                tenant=status.tenant,
                server=status.server,
                url=status.url,
                transport=status.transport,
                era=status.era,
                tool_count=status.tool_count,
                catalogue_age_seconds=status.catalogue_age_seconds,
                catalogue_ttl_seconds=status.catalogue_ttl_seconds,
            )
            if status is not None
            else None
        ),
    )


def _server_from_preset(preset: McpServerPreset, callback_url: str) -> psych_runtime.McpServer:
    """One place a stored preset becomes the ``McpServer`` a connection uses.

    Shared by the connect endpoint and by nothing else today, but it exists as
    a function because the version of this that lived inline in the endpoint
    drifted from ``app.spec_builder``'s: the endpoint supplied
    ``redirect_uris`` and the publisher did not, so an ``authorization_code``
    server passed its test in settings and then failed on every Run.
    """
    return psych_runtime.McpServer(
        name=preset.name,
        url=preset.url,
        transport=preset.transport,
        credential=preset.credential,
        allow=tuple(preset.allow),
        optional=preset.optional,
        preload=preset.preload,
        oauth=(
            psych_runtime.McpOAuth(
                grant=preset.oauth.grant,
                preregistered_client_id=preset.oauth.preregistered_client_id,
                client_secret_credential=preset.oauth.client_secret_credential,
                issuer=preset.oauth.issuer,
                cimd_url=preset.oauth.cimd_url,
                allow_dynamic_registration=preset.oauth.allow_dynamic_registration,
                application_type=preset.oauth.application_type,
                client_name=preset.oauth.client_name,
                redirect_uris=(
                    (callback_url,) if preset.oauth.grant == "authorization_code" else ()
                ),
            )
            if preset.oauth is not None
            else None
        ),
    )


def _carried_connection(
    stored: McpServerPreset | None, incoming: McpServerPresetIn
) -> McpConnectionRecord | None:
    if stored is None or stored.last_connection is None:
        return None
    unchanged = (
        stored.url == incoming.url
        and stored.transport == incoming.transport
        and stored.credential == incoming.credential
        and (stored.oauth.model_dump() if stored.oauth else None)
        == (incoming.oauth.model_dump() if incoming.oauth else None)
    )
    return stored.last_connection if unchanged else None


def _a2a_preset_out(peer: A2APeerPreset) -> A2APeerPresetOut:
    """One saved peer, outward. Carries the credential's *name*, which is what
    a Spec carries too, and never a secret value."""
    return A2APeerPresetOut(
        name=peer.name,
        url=peer.url,
        description=peer.description,
        credential=peer.credential,
        scheme=peer.scheme,
        tenant=peer.tenant,
        allow=list(peer.allow),
        optional=peer.optional,
        extensions=list(peer.extensions),
    )


def _runtime_out(state: PlaygroundState, sandbox: SandboxProvision | None) -> RuntimeSettingsOut:
    runtime = state.runtime
    return RuntimeSettingsOut(
        cost_policy=runtime.cost_policy,
        blob_offload_bytes=runtime.blob_offload_bytes,
        catalogue_budget_chars=runtime.catalogue_budget_chars,
        sandbox_enabled=runtime.sandbox_enabled,
        sandbox_limits=SandboxLimitsIn(**runtime.sandbox_limits.model_dump()),
        egress_allow=list(runtime.egress_allow),
        denied_tools=list(runtime.denied_tools),
        sandbox_available=sandbox.available if sandbox is not None else False,
        sandbox_unavailable_reason=sandbox.reason if sandbox is not None else None,
    )


def _settings_response(
    state: PlaygroundState,
    live: Mapping[str, McpConnectionStatus] | None = None,
    sandbox: SandboxProvision | None = None,
) -> SettingsResponse:
    return SettingsResponse(
        runtime=_runtime_out(state, sandbox),
        providers=[_provider_out(p) for p in state.providers],
        mcp_servers=[_mcp_preset_out(m, live) for m in state.mcp_servers],
        secrets=sorted(state.secrets),
        active_provider_id=state.active_provider_id,
        model_prices=[
            ModelPriceOut(
                model=entry.model,
                input=entry.input,
                output=entry.output,
                cache_read=entry.cache_read,
                cache_write=entry.cache_write,
                currency=entry.currency,
            )
            for entry in state.model_prices
        ],
        a2a_peers=[_a2a_preset_out(peer) for peer in state.a2a_peers],
        skills=[
            SkillOut(name=skill.name, description=skill.description, body=skill.body)
            for skill in state.skills
        ],
    )


def _merge_provider_api_key(
    prior: ProviderConfig | None, incoming_api_key: str | None
) -> str | None:
    """``PUT /api/settings/providers``'s own rule: omitted keeps what is
    stored, ``""`` clears it, anything else replaces it."""
    if incoming_api_key is None:
        return prior.api_key if prior is not None else None
    if incoming_api_key == "":
        return None
    return incoming_api_key


def _reconcile_active_provider_id(
    providers: list[ProviderConfig], active_id: str | None
) -> str | None:
    """Which provider is active after a full replace of the provider list.

    Three cases, and the difference between the last two is the whole point.

    ``active_id`` still names one of ``providers``: it stays active.

    ``active_id`` named one that is no longer in the list: nothing is active.
    An operator who removed their active provider gets an explicit "nothing is
    active" rather than a silent switch to whichever provider happens to be
    listed first, because that switch would send the next Run to an endpoint
    they did not choose, on a key they did not choose.

    ``active_id`` was already ``None``: the first provider in the list becomes
    active. There is no prior choice to respect here, so adopting one is not
    overriding anybody -- and refusing to was a bug in the documented first
    run. A console that boots with no provider (which is the state the compose
    file and the README describe) sends a person to Settings, they add their
    provider, and every screen still said none was configured because adding
    one never made it active. Nothing in the UI or the API told them to call
    ``activate`` as a second step.
    """
    if active_id is not None:
        return active_id if any(p.id == active_id for p in providers) else None
    return providers[0].id if providers else None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: PLR0915 - one boot, in order
    settings = load_settings()
    # First, so everything below can be seen. Psych attaches a `NullHandler`
    # and configures nothing else, which is what a library should do and which
    # means an application that configures nothing hears none of it.
    configure_logging(settings)
    observability = build_telemetry(settings)

    store = _build_store(settings)
    if settings.postgres_dsn is not None:
        migrate = getattr(store, "migrate", None)
        if migrate is not None:
            await migrate()
            _LOG.info("applied Postgres migrations at %s", settings.postgres_dsn)

    settings_store = SettingsStore(settings.state_file)
    # One egress seam for every outbound call this process makes -- the model,
    # MCP, HTTP tools, A2A peers and push notifications alike -- with each
    # account's own allow-list behind it (DESIGN.md §14). See `app.ports`.
    transport = HttpTransport(policy=AccountEgressPolicy(settings_store))

    # First boot against this state file: seed the workspace the first account
    # will adopt, from the environment, so a brand-new sign-up lands on a
    # console with a provider already configured rather than an empty shell
    # they must fill in before anything works. Every boot after this reads the
    # file as written and these env vars are no longer consulted for what it
    # already holds (`app.config.Settings`'s own docstring on `state_file`).
    await settings_store.update_file(
        lambda file: (
            file
            if file.accounts.accounts or file.legacy is not None
            else file.model_copy(update={"legacy": _seed_state(settings)})
        )
    )

    # Reads through to whichever account is asking, on every resolve. See
    # `app.secrets` for why answering the same name for every Scope is the
    # same bug as pooling MCP connections by URL.
    secrets = AccountSecretResolver(settings_store)
    # The redirect port is what makes `authorization_code` usable at all: the
    # grant is fully implemented in psych, but `OAuthClient` refuses it
    # without somewhere for a browser to land, which is the consumer's to
    # provide (DESIGN.md §1). See `app.oauth_redirect`.
    redirect = BrowserAuthorizationRedirect()
    pool = McpPool(
        transport=transport,
        secrets=secrets,
        oauth=OAuthClient(transport=transport, redirect=redirect),
    )
    # What each connected system is for, keyed by URL, read by Psych at every
    # turn boundary through `describe_server`. A mutable dict rather than a
    # read of the settings file because the lookup is synchronous by design
    # (`McpTools.describe_server`'s own docstring: it runs per turn, so it must
    # not do IO), and because a description edited in the console has to take
    # effect on the next turn without rebuilding the pool.
    descriptions: dict[tuple[str, str], str] = {}

    def describe_server(scope: psych_runtime.Scope, server: psych_runtime.McpServer) -> str | None:
        return descriptions.get((scope.tenant, server.url)) or None

    mcp = McpTools(pool, describe_server=describe_server)

    # Durable across restarts, unlike `psych_runtime.memory.store_backed`, which keeps
    # facts on the instance. A memory that empties when the process does would
    # demonstrate the API and disprove the feature. See `app.memory_store`.
    memory = FileMemoryStore(settings.memory_file)

    index = PlaygroundIndex(settings.index_file)
    # Reconciled against the Store rather than trusted wholesale: with the
    # in-memory store nothing outlives this process, so a saved index would
    # otherwise list agents that cannot be dispatched. See the method.
    await index.load(store)
    # The outbound half of A2A (`psych_runtime.tools.a2a`): pooled by
    # (scope, peer, credential) exactly as MCP is, over the same transport and
    # the same per-account secrets. Without it an agent's `a2a_peers` publish
    # and validate and are never offered to the model.
    a2a_tools = A2ATools(A2APool(transport=transport, secrets=secrets))
    workflows = WorkflowService(store=store, index=index, registry=registry)
    # The `create_workflow` tool's way in (`app.tools`): a Run's agent can
    # publish a workflow for the account that dispatched it.
    install_workflow_publisher(ToolWorkflowPublisher(workflows))
    sandbox = SandboxProvision(allow_same_uid=same_uid_opt_in())
    if not sandbox.available:
        _LOG.warning("run_code is unavailable on this host: %s", sandbox.reason)
    runner = AccountRoutedRuntime(
        store=store,
        registry=registry,
        mcp=mcp,
        index=index,
        settings=settings_store,
        transport=transport,
        memory=memory,
        fallback_base_url=settings.base_url,
        fallback_api_key=settings.api_key,
        default_approval_selectors=settings.default_approval_selectors,
        telemetry=observability.telemetry,
        http=HttpToolExecutor(transport, secrets),
        # Beside the other state files: a large tool result outlives the log
        # record that points at it, and a blob store that emptied with the
        # process would hand `read_tool_output` a dangling handle.
        blob=FilesystemBlobStore(settings.state_file.parent / "blobs"),
        a2a=a2a_tools,
        sandbox=sandbox,
        policy=AccountToolPolicy(settings_store),
    )
    worker = Worker(store, runner, poll_interval=0.2)
    worker_task = asyncio.create_task(worker.run())

    await _refresh_all_descriptions(descriptions, settings_store)

    # A2A (`app.a2a`): the transport half of the protocol, which DESIGN.md §1
    # refuses to put in Psych. Its own state files sit beside the others so a
    # registered webhook and a card signature both survive a restart.
    push_configs = PushConfigStore(settings.state_file.with_name("a2a-push-configs.json"))
    a2a_service = A2AService(store=store, index=index, push_configs=push_configs)
    a2a_push = PushSender(transport=transport, service=a2a_service)
    # What the cards advertise, and what a local peer preset points at. A
    # bind address of 0.0.0.0 is where this process listens, not somewhere a
    # client can connect, so the loopback spelling is used for the default.
    listen_host = "127.0.0.1" if settings.host in {"", "0.0.0.0", "::"} else settings.host
    a2a_base_url = os.environ.get(
        "PSYCH_PLAYGROUND_A2A_BASE_URL", f"http://{listen_host}:{settings.port}"
    ).rstrip("/")
    a2a_deps = A2ADeps(
        service=a2a_service,
        cards=CardFactory(
            base_url=a2a_base_url,
            signer=load_signing_key(settings.state_file.parent),
        ),
        index=index,
        settings=settings_store,
        push=a2a_push,
    )

    app.state.playground = AppState(
        settings=settings,
        store=store,
        index=index,
        transport=transport,
        worker=worker,
        worker_task=worker_task,
        settings_store=settings_store,
        secrets=secrets,
        runner=runner,
        mcp=mcp,
        redirect=redirect,
        descriptions=descriptions,
        memory=memory,
        a2a=a2a_deps,
        observability=observability,
        sandbox=sandbox,
        workflows=workflows,
        a2a_base_url=a2a_base_url,
    )
    accounts = await settings_store.load_file()
    _LOG.info(
        "playground backend up: store=%s accounts=%d fallback_model=%s fallback_base_url=%s "
        "state_file=%s index_file=%s",
        "postgres" if settings.postgres_dsn else "memory",
        len(accounts.accounts.accounts),
        settings.model,
        settings.base_url,
        settings.state_file,
        settings.index_file,
    )
    if not accounts.accounts.accounts:
        _LOG.info("no accounts yet: the console's first screen will offer to create one")

    try:
        yield
    finally:
        worker.stop()
        await a2a_push.aclose()
        await asyncio.wait_for(worker_task, timeout=30)
        # After the Worker has stopped, so the spans of whatever it was
        # finishing are in the batch this flushes. Spans are exported in
        # batches, and a process that exits without flushing loses the last few
        # seconds of them -- which is exactly the Run somebody just watched and
        # then went looking for.
        observability.shutdown()
        await transport.aclose()
        close = getattr(store, "close", None)
        if close is not None:
            await close()


app = FastAPI(
    title="Psych playground backend",
    lifespan=lifespan,
)


# A2A lives outside `/api`, so the session middleware below leaves it alone: a
# peer agent authenticates with a bearer token on every request and has no
# cookie to send. See `app.a2a.router`.
app.include_router(build_router(lambda request: _state(request).a2a))


@app.middleware("http")
async def _require_session(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuse an unauthenticated request before anything parses its body.

    The per-handler ``_account(request)`` call is how a route *gets* the
    account; this is what guarantees no route can answer without one. The
    difference showed up as a 422: FastAPI validates a request body before the
    handler runs, so an anonymous POST with a malformed body reached Pydantic
    and got a schema-shaped error back. Nothing catastrophic, and still the
    wrong order -- an unauthenticated caller should not be exercising this
    process's validators or learning its request shapes.

    Doing it here also means a route added later is guarded by default. The
    handler check would have to be remembered; this one has to be deliberately
    opted out of by adding a path to ``PUBLIC_PATHS``, which is a line in a
    diff somebody reviews.

    ``OPTIONS`` passes through because a CORS preflight carries no cookie by
    design; the real request behind it does not get the same pass.
    """
    if (
        request.method == "OPTIONS"
        or request.url.path in PUBLIC_PATHS
        or not request.url.path.startswith("/api/")
    ):
        return await call_next(request)
    state: AppState | None = getattr(request.app.state, "playground", None)
    if state is not None and await account_from_request(request, state.settings_store) is None:
        return JSONResponse(status_code=401, content={"detail": "Sign in to continue."})
    return await call_next(request)


# Registered after `_require_session` on purpose, which makes it the *outer*
# layer: Starlette's `add_middleware` inserts at position 0, so the last one
# registered runs first. Ordered the other way round, the 401 above returned
# before any CORS header was attached, and a browser refused to read a
# perfectly correct response -- which presents as "cannot reach the backend"
# while the server log cheerfully shows the request arriving and being
# answered. See `TestTheFrontDoor` for the assertion that keeps this order.
#
# An explicit origin list with credentials, not a wildcard: the CORS
# specification forbids `Access-Control-Allow-Credentials` alongside
# `Allow-Origin: *`, so the wildcard this backend used before accounts would
# now stop the session cookie being sent at all.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(load_allowed_origins()),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiProblem)
async def _handle_api_problem(_request: Request, exc: ApiProblem) -> JSONResponse:
    body = ProblemResponse(detail=exc.detail, issues=exc.issues)
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


def _state(request: Request) -> AppState:
    playground: AppState = request.app.state.playground
    return playground


async def _require_run(state: AppState, run_id: str, account: Account) -> RunHeader:
    """One Run, if it belongs to ``account``.

    The check is against ``RunHeader.scope`` in Psych's own Store rather than
    against this backend's ``PlaygroundIndex``. The header is the authoritative
    record of whose Run it is -- Psych writes it at admission and filters every
    store query by it -- while the index is a convenience this example keeps
    for listing and could, in principle, drift from it. Authorization must read
    the thing that cannot drift.

    A run id is not a secret. It appears in a URL the console navigates to, in
    a log line, in a shared link. So every route that names one comes through
    here, and "not yours" is answered with the same 404 as "does not exist":
    telling an unauthorized caller that a run id is real is itself a fact they
    did not have.
    """
    header = await state.store.get_run(RunId(run_id))
    if header is None or header.scope.tenant != account.id:
        raise ApiProblem(404, f"no run {run_id!r}")
    return header


class _Tree:
    """One account's Runs as the tree they actually are.

    A conversation is a chain of Runs, and branching makes the chain
    a tree: two Runs may continue the same predecessor, because somebody asked
    the same question a second way. The three facts a walk over that tree needs
    -- who continues whom, which branch each Run is on, and which conversation
    each belongs to -- are here, gathered in one pass, because every caller
    below needs them and reading every header three times is the kind of cost
    that only shows up once somebody has history.

    Branch and conversation are different things and the difference is the
    point. A conversation is the tree; a branch is one line through it. Several
    branches share a conversation and are paged between rather than listed
    separately, which is what the ``1/2`` version arrows do. A
    fork is the other operation: it copies the history up to a message into a
    conversation of its own, which then survives its original being deleted.

    The parent links come from ``RunHeader`` in Psych's own Store, which is the
    authoritative record and the one thing this backend cannot drift from. The
    branch ids come from the index beside it, because a branch is the
    consumer's bookkeeping and not the runtime's: DESIGN.md §7 gives ``Store``
    no parent-to-children query on purpose, and enumerating branches is exactly
    the job that omission leaves to the platform. See ``app.store_index``.

    Built per request and thrown away. It is a fold over one account's Runs,
    never a cache: a stale answer here would attribute a message to the wrong
    branch, and this example would rather pay the reads.
    """

    def __init__(
        self,
        children: dict[RunId, list[RunId]],
        branches: dict[RunId, str],
        conversations: dict[RunId, str],
        parents: dict[RunId, RunId],
        listed: set[RunId],
    ) -> None:
        self.children = children
        self.branches = branches
        self.conversations = conversations
        self.parents = parents
        self.listed = listed

    def ancestors(self, run_id: RunId) -> set[RunId]:
        """``run_id`` and everything it continues, back to the root.

        The set of Runs one conversation reads as its history, which is what
        reachability means here: a Run in this set is one some branch still
        needs, whatever branch stamped it.
        """
        seen: set[RunId] = set()
        current: RunId | None = run_id
        while current is not None and current not in seen:
            seen.add(current)
            current = self.parents.get(current)
        return seen

    def reachable_from_listed(self, *, excluding: str = "") -> set[RunId]:
        """Every Run some other listed conversation reads.

        Git's reachability, with each listed Run as a ref. Deleting a
        conversation removes what this set does not contain and unlists what it
        does, which is the whole rule and the reason a fork survives the
        conversation it was taken from.
        """
        reachable: set[RunId] = set()
        for run_id in self.listed:
            if excluding and self.conversation_of(run_id) == excluding:
                continue
            reachable |= self.ancestors(run_id)
        return reachable

    def on_branch(self, branch_id: str) -> set[RunId]:
        """Every Run stamped with this branch, listed or not."""
        return {run for run, branch in self.branches.items() if branch == branch_id}

    def in_conversation(self, conversation_id: str) -> set[RunId]:
        """Every Run in this conversation, on any branch, listed or not."""
        return {run for run in self.conversations if self.conversation_of(run) == conversation_id}

    def branch_of(self, run_id: RunId) -> str:
        return self.branches.get(run_id, "")

    def conversation_of(self, run_id: RunId) -> str:
        """Which conversation a Run belongs to.

        A Run recorded before conversations had ids falls back to the root of
        its own chain. That is what a conversation was before forking existed,
        so reading those Runs that way returns exactly the grouping they were
        recorded under rather than inventing one.
        """
        stamped = self.conversations.get(run_id, "")
        if stamped:
            return stamped
        current = run_id
        seen = {current}
        while True:
            parent = self.parents.get(current)
            if parent is None or parent in seen:
                return str(current)
            seen.add(parent)
            current = parent

    def newest_on_branch(self, run_id: RunId) -> RunId:
        """The last Run on ``run_id``'s own branch, following continuations.

        Only children sharing ``run_id``'s branch are followed. That is the
        whole fix: a Run before a fork point has two children and the walk used
        to take whichever it met first, so the thread report, the conversation
        list and opening a chat from history all silently followed an arbitrary
        one of the two futures. Branch ids are stamped so that each Run has at
        most one child on its own branch, and a person who forked to ask two
        things gets both back rather than one at random.

        ``max`` decides between children that should not both exist -- a
        continuation dispatched at the same parent by two racing callers, which
        this backend cannot prevent from outside the console. Arbitrary but
        deterministic, so at least the same conversation opens every time.
        Stops on a cycle it cannot have rather than looping forever.
        """
        branch = self.branch_of(run_id)
        seen = {run_id}
        current = run_id
        while True:
            same = [
                child for child in self.children.get(current, []) if self.branch_of(child) == branch
            ]
            if not same:
                return current
            following = max(same)
            if following in seen:
                return current
            seen.add(following)
            current = following

    def branch_for_continuation(self, continues: RunId | None) -> str:
        """The branch a Run dispatched now would join.

        Three cases and one rule. A conversation with no predecessor opens its
        own branch. A predecessor whose branch has nobody continuing it yet is
        extended, so an ordinary second message stays on one branch. A
        predecessor that already has a Run continuing it on its branch is being
        *branched*, and the new Run mints a fresh branch.

        Nothing has to declare that it is branching, then: what makes a
        dispatch a branch is that the message it continues from has already
        been answered onward, which is a fact about the tree rather than a
        claim in a request body. A direct API caller that continues a Run
        mid-thread gets a branch for the same reason, and the walk above keeps
        working instead of quietly picking one of two children.
        """
        if continues is None:
            return new_branch_id()
        branch = self.branch_of(continues)
        taken = any(self.branch_of(child) == branch for child in self.children.get(continues, []))
        return new_branch_id() if taken else branch


async def _read_tree(state: AppState, account: Account) -> _Tree:
    """Fold this account's Runs into a ``_Tree``.

    Filtered by account for the usual reason: this decides which Runs get read
    and walked, and a tree built from every tenant's Runs would let one
    conversation's walk step into another's.
    """
    children: dict[RunId, list[RunId]] = {}
    branches: dict[RunId, str] = {}
    parents: dict[RunId, RunId] = {}
    conversations: dict[RunId, str] = {}
    listed: set[RunId] = set()
    # Unlisted Runs included on purpose: they are history a live fork reads,
    # so a tree without them would break the chain at every fork point whose
    # other side has been deleted.
    for entry in await state.index.list_runs(account.id, include_unlisted=True):
        branches[entry.run_id] = entry.branch_id
        conversations[entry.run_id] = entry.conversation_id
        if entry.listed:
            listed.add(entry.run_id)
        header = await state.store.get_run(entry.run_id)
        if header is not None and header.continues_run_id is not None:
            children.setdefault(header.continues_run_id, []).append(entry.run_id)
            parents[entry.run_id] = header.continues_run_id
    return _Tree(children, branches, conversations, parents, listed)


async def _newest_in_thread(state: AppState, account: Account, run_id: RunId) -> RunId:
    """The last Run on ``run_id``'s branch. See ``_Tree.newest_on_branch``."""
    return (await _read_tree(state, account)).newest_on_branch(run_id)


def _thread_wall_clock(run_ids: Sequence[RunId], reports: Sequence[RunReport]) -> float:
    """Seconds from the conversation's first message to its last settlement.

    Falls back to the sum of the Runs' own wall clocks when the timestamps
    needed for the span are not all there -- a conversation whose newest turn
    is still running has no last settlement yet, and a partial number beats an
    empty one on a page whose job is to say how long this took.
    """
    starts = [report.admitted_at for report in reports]
    ends = [report.settled_at for report in reports if report.settled_at is not None]
    if starts and len(ends) == len(run_ids):
        return max((max(ends) - min(starts)).total_seconds(), 0.0)
    return sum(report.totals.latency.wall_clock_seconds for report in reports)


async def _account(request: Request) -> Account:
    """The signed-in account, or a 401. The dependency every route below takes.

    Deliberately a plain function rather than a `Depends` default on each
    route's signature: FastAPI would then also advertise it in the OpenAPI
    schema as a parameter, and more importantly a `Depends` default is easy to
    *omit* on a new route without anything failing. Written out at the top of
    each handler, a missing call is a missing name and the route cannot use an
    account it never fetched.
    """
    return await require_account(request, _state(request).settings_store)


# ---------------------------------------------------------------------------
# Health and identity
# ---------------------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Is this backend up, and does anyone have an account yet.

    The one route that answers before sign-in, and it answers with nothing
    about anybody: a boolean for whether the console should offer "create your
    account" or "sign in", and nothing else. `GET /api/config` used to serve
    this purpose and cannot any more, because what it reports (the active
    model, the connected servers) is per account.
    """
    state = _state(request)
    return HealthResponse(
        ok=True,
        store="postgres" if state.settings.postgres_dsn else "memory",
        needs_first_account=await is_first_account(state.settings_store),
    )


@app.post("/api/auth/signup", status_code=201, response_model=AccountResponse)
async def signup(body: SignUpRequest, request: Request, response: Response) -> AccountResponse:
    state = _state(request)
    account, token = await register_account(
        state.settings_store, body.email, body.password, body.display_name
    )
    # The first account inherits whatever was configured before accounts
    # existed: a provider seeded from the environment on first boot, or a real
    # workspace left by an older build. Whoever signs up on this machine is the
    # person who configured it; handing it to them beats making them retype a
    # provider key that is already on disk. Both halves move together, so the
    # agents in the index and the settings that published them stay one thing.
    await state.settings_store.adopt_legacy(account.id)
    await state.index.adopt_legacy(account.id)
    await _refresh_all_descriptions(state.descriptions, state.settings_store)
    set_session_cookie(response, token, secure=state.settings.cookie_secure)
    _LOG.info("account created: %s", account.id)
    return AccountResponse(**account_summary(account))


@app.post("/api/auth/signin", response_model=AccountResponse)
async def signin(body: SignInRequest, request: Request, response: Response) -> AccountResponse:
    state = _state(request)
    account, token = await sign_in(state.settings_store, body.email, body.password)
    set_session_cookie(response, token, secure=state.settings.cookie_secure)
    return AccountResponse(**account_summary(account))


@app.post("/api/auth/signout", response_model=OkResponse)
async def signout(request: Request, response: Response) -> OkResponse:
    """Idempotent: signing out twice, or with no session at all, is not an
    error. A sign-out that can fail is a sign-out people stop trusting."""
    state = _state(request)
    await sign_out(state.settings_store, request)
    clear_session_cookie(response, secure=state.settings.cookie_secure)
    return OkResponse(ok=True)


@app.get("/api/auth/me", response_model=AccountResponse)
async def whoami(request: Request) -> AccountResponse:
    return AccountResponse(**account_summary(await _account(request)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@app.get("/api/config", response_model=ConfigResponse)
async def get_config(request: Request) -> ConfigResponse:
    """What this backend is configured with, for the account asking.

    Read from that account's active provider, not from the environment this
    process booted with. Providers are per account and editable at runtime, so
    the env-derived answer was stale the moment anybody changed one -- and this
    is what the agent builder offers as the default model.

    The connected servers listed are the account's own presets rather than the
    process's ``PSYCH_PLAYGROUND_MCP_SERVERS`` hints. Those hints seed the first
    account's workspace and are not a description of what anyone has connected
    since; serving them to every account showed one person's connections to
    everybody.
    """
    state = _state(request)
    account = await _account(request)
    settings = state.settings
    workspace = await state.settings_store.load(account.id)
    active = active_provider(workspace)
    return ConfigResponse(
        model=active.model if active is not None else settings.model,
        store="postgres" if settings.postgres_dsn else "memory",
        mcp_servers=[
            McpServerHintOut(
                name=preset.name,
                url=preset.url,
                transport=preset.transport,
                oauth_grant=preset.oauth.grant if preset.oauth is not None else None,
            )
            for preset in workspace.mcp_servers
        ],
        has_api_key=bool(active.api_key) if active is not None else bool(settings.api_key),
        provider_label=active.label if active is not None else None,
        account_id=account.id,
        a2a_base_url=state.a2a_base_url,
        tracing=TracingOut(
            enabled=state.observability.enabled,
            endpoint=settings.otlp_endpoint,
            service_name=settings.otlp_service_name,
        ),
    )


@app.get("/api/models", response_model=ModelsResponse)
async def list_models(request: Request) -> ModelsResponse:
    """Model ids the account's active provider will accept.

    A separate route rather than a field on ``GET /api/config`` because it
    costs a real call to the provider and ``/api/config`` is polled.

    An empty list means "the provider would not say", never "nothing is
    valid" -- that is ``known_models``' own contract (DESIGN.md §19), and a
    proxy that does not expose ``/models`` is the ordinary case rather than a
    misconfiguration. The agent form therefore has to keep the field typeable:
    a picker that can only offer an empty list is a form nobody can complete.

    A provider that cannot be reached is reported as no models plus the
    reason, not as a failed request. Somebody editing an agent while their
    provider is down should still be able to publish.
    """
    state = _state(request)
    account = await _account(request)
    workspace = await state.settings_store.load(account.id)
    provider = active_provider(workspace)
    if provider is None:
        return ModelsResponse(
            models=[], detail="No provider is active. Add one in Settings.", provider_label=None
        )
    client = OpenAICompatibleClient(
        base_url=provider.base_url,
        transport=state.transport,
        scope=scope_for(account),
        api_key=provider.api_key,
    )
    try:
        models = list(await client.known_models())
    except Exception as err:
        return ModelsResponse(
            models=[],
            detail=f"Could not reach {provider.label}: {err}",
            provider_label=provider.label,
        )
    detail = (
        f"{provider.label} offers {len(models)} model{'' if len(models) == 1 else 's'}."
        if models
        else f"{provider.label} does not list its models, so type the id you want."
    )
    return ModelsResponse(models=models, detail=detail, provider_label=provider.label)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


@app.post("/api/agents", status_code=201, response_model=CreateAgentResponse)
async def create_agent(body: CreateAgentRequest, request: Request) -> CreateAgentResponse:
    state = _state(request)
    account = await _account(request)

    # Resolved before anything is published, so an edit naming an agent that is
    # not this account's fails without leaving a Version behind that nothing
    # points at.
    if body.agent_id is None:
        agent_id = new_agent_id()
    elif await state.index.get_agent(account.id, body.agent_id) is None:
        raise ApiProblem(404, f"no agent {body.agent_id!r}")
    else:
        agent_id = body.agent_id

    # Each child is this account's own agent, copied in at its *current*
    # Version. Resolved before anything is published so a roster naming an
    # agent that is not this account's fails without a Version left behind.
    subagent_specs: dict[str, psych_runtime.AgentSpec] = {}
    child_hashes: dict[str, VersionHash] = {}
    for ref in body.subagents:
        if ref.agent_id == agent_id:
            raise ApiProblem(400, f"subagent {ref.name!r} cannot be the agent being published")
        found = await state.index.get_agent(account.id, ref.agent_id)
        if found is None:
            raise ApiProblem(404, f"subagent {ref.name!r} names no agent {ref.agent_id!r}")
        child_version = await state.store.get_version(found[0].version_hash)
        if child_version is None or not isinstance(child_version.spec, psych_runtime.AgentSpec):
            raise ApiProblem(409, f"agent {ref.agent_id!r} has no published agent Version")
        subagent_specs[ref.agent_id] = child_version.spec
        child_hashes[ref.agent_id] = child_version.hash

    try:
        spec = build_agent_spec(
            body,
            oauth_redirect_uri=state.settings.oauth_callback_url,
            subagent_specs=subagent_specs,
        )
    except ValidationError as err:
        raise from_pydantic("the agent's fields did not validate", err) from err

    try:
        version = await psych_runtime.publish(
            state.store, spec, context=ValidationContext(registered_tools=registry.names)
        )
    except SpecValidationError as err:
        raise from_spec_validation(err) from err

    approval_selectors = (
        tuple(body.approval_selectors)
        if body.approval_selectors is not None
        else state.settings.default_approval_selectors
    )
    # `psych_runtime.publish` is not scoped: a Version is an immutable content-hashed
    # publication, so two accounts publishing the same Spec correctly share one
    # Version in the Store. Ownership and identity are this index's dimensions,
    # not Psych's, which is why an entry is keyed by
    # `(owner, agent_id, version_hash)`.
    created = await state.index.record_publish(
        AgentEntry(
            version_hash=version.hash,
            agent_id=agent_id,
            owner=account.id,
            name=spec.name,
            description=spec.description,
            instructions=spec.instructions,
            model=spec.model.model,
            model_options=spec.model.model_dump(exclude={"model", "temperature"}),
            tools=tuple(tool.name for tool in spec.tools if isinstance(tool, CodeTool)),
            http_tools=tuple(
                HttpToolEntry(**tool.model_dump(exclude={"kind"}))
                for tool in spec.tools
                if isinstance(tool, HttpTool)
            ),
            subagents=tuple(
                SubagentRefEntry(
                    name=ref.name,
                    description=ref.description,
                    agent_id=entry.agent_id,
                    version_hash=str(child_hashes[entry.agent_id]),
                )
                for ref, entry in zip(spec.subagents, body.subagents, strict=True)
            ),
            spawn=spec.spawn.model_dump() if spec.spawn is not None else None,
            suspension=spec.suspension.model_dump(exclude={"may_ask_questions"}),
            limits=spec.limits.model_dump(),
            # From the Spec, not from the request body: the Spec is what was
            # actually published, and its validator normalises and sorts. An
            # entry built from the request would disagree with the Version the
            # moment either of those did anything.
            skills=tuple(
                SkillEntry(name=skill.name, description=skill.description, body=skill.body)
                for skill in spec.skills
            ),
            mcp_servers=tuple(server.name for server in spec.mcp_servers),
            a2a_peers=tuple(peer.name for peer in spec.a2a_peers),
            answer_style=spec.answer_style,
            may_ask_questions=spec.suspension.may_ask_questions,
            tasks_enabled=spec.tasks_enabled,
            components_enabled=spec.components_enabled,
            subagents_enabled=spec.spawn is not None,
            compaction=(
                CompactionEntry(
                    trigger_tokens=spec.compaction.trigger_tokens,
                    keep_recent_turns=spec.compaction.keep_recent_turns,
                    model=spec.compaction.model,
                    max_summary_tokens=spec.compaction.max_summary_tokens,
                    summary_instructions=spec.compaction.summary_instructions,
                )
                if spec.compaction is not None
                else None
            ),
            published_at=version.published_at,
            approval_selectors=approval_selectors,
        ),
        now=datetime.now(UTC),
    )
    return CreateAgentResponse(
        agent_id=agent_id, version_hash=version.hash, name=spec.name, created=created
    )


def _agent_summary(pointer: AgentPointer, entry: AgentEntry) -> AgentSummary:
    """One agent, described by the Version it currently points at."""
    return AgentSummary(
        agent_id=pointer.agent_id,
        version_hash=entry.version_hash,
        name=entry.name,
        description=entry.description,
        instructions=entry.instructions,
        model=entry.model,
        model_options=ModelOptionsIn(**entry.model_options) if entry.model_options else None,
        tools=list(entry.tools),
        http_tools=[HttpToolIn(**tool.model_dump()) for tool in entry.http_tools],
        skills=[
            SkillOut(name=skill.name, description=skill.description, body=skill.body)
            for skill in entry.skills
        ],
        mcp_servers=list(entry.mcp_servers),
        subagents=[SubagentRefOut(**ref.model_dump()) for ref in entry.subagents],
        spawn=SpawnIn(**entry.spawn) if entry.spawn is not None else None,
        suspension=SuspensionIn(**entry.suspension) if entry.suspension else None,
        limits=dict(entry.limits),
        approval_selectors=list(entry.approval_selectors),
        a2a_peers=list(entry.a2a_peers),
        answer_style=entry.answer_style,
        may_ask_questions=entry.may_ask_questions,
        tasks_enabled=entry.tasks_enabled,
        components_enabled=entry.components_enabled,
        subagents_enabled=entry.subagents_enabled,
        compaction=(
            CompactionIn(
                trigger_tokens=entry.compaction.trigger_tokens,
                keep_recent_turns=entry.compaction.keep_recent_turns,
                model=entry.compaction.model,
                max_summary_tokens=entry.compaction.max_summary_tokens,
                summary_instructions=entry.compaction.summary_instructions,
            )
            if entry.compaction is not None
            else None
        ),
        published_at=entry.published_at.isoformat(),
        created_at=pointer.created_at.isoformat(),
        updated_at=pointer.updated_at.isoformat(),
        version_count=len(pointer.history),
    )


@app.get("/api/agents", response_model=list[AgentSummary])
async def list_agents(request: Request) -> list[AgentSummary]:
    """This account's agents, one row each -- not one row per Version.

    An agent edited eleven times is one thing a person recognises. Listing
    Versions instead is what made every flag flip look like a second agent.
    """
    account = await _account(request)
    pairs = await _state(request).index.list_agents(account.id)
    return [_agent_summary(pointer, entry) for pointer, entry in pairs]


@app.get("/api/agents/{agent_id}", response_model=AgentSummary)
async def get_agent(agent_id: str, request: Request) -> AgentSummary:
    account = await _account(request)
    found = await _state(request).index.get_agent(account.id, agent_id)
    if found is None:
        raise ApiProblem(404, f"no agent {agent_id!r}")
    return _agent_summary(*found)


@app.get("/api/agents/{agent_id}/versions", response_model=list[AgentVersionSummary])
async def list_agent_versions(agent_id: str, request: Request) -> list[AgentVersionSummary]:
    """What this agent has been, newest first.

    The audit trail people assume immutability is for, and the reason the
    pointer costs nothing: editing freely and knowing exactly what ran are not
    in tension once an agent and a Version are two different things.
    """
    account = await _account(request)
    found = await _state(request).index.get_agent(account.id, agent_id)
    if found is None:
        raise ApiProblem(404, f"no agent {agent_id!r}")
    pointer, _ = found
    entries = await _state(request).index.list_versions(account.id, agent_id)
    return [
        AgentVersionSummary(
            version_hash=entry.version_hash,
            name=entry.name,
            model=entry.model,
            published_at=entry.published_at.isoformat(),
            current=entry.version_hash == pointer.version_hash,
        )
        for entry in entries
    ]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def _inherited_target(
    state: AppState, account: Account, run_id: str
) -> tuple[VersionHash, str] | None:
    """The Version and agent of a Run already in this conversation.

    ``None`` when that Run is not this account's, or is old enough to have no
    agent recorded. Both are reasons to fall back to what the request named
    rather than to fail: an unknown predecessor is refused a moment later by
    ``psych_runtime.dispatch(continues=...)`` itself, which is the check that cannot be
    forgotten.
    """
    prior = await state.index.get_run(RunId(run_id))
    if prior is None or prior.tenant != account.id:
        return None
    if prior.agent_id == "" and prior.workflow_id == "":
        return None
    return prior.version_hash, prior.agent_id


async def _named_target(
    state: AppState, account: Account, agent_id: str | None, version_hash: str | None
) -> tuple[VersionHash, str]:
    """Which Version a request pins, and which agent it belongs to.

    Both, together, because neither answers the other. A Version is content, so
    two agents of one account built from the same Spec share a hash and the
    agent cannot be recovered from it afterwards -- which is why the Run
    records it rather than being attributed by hash later.

    Either way in, the answer is checked against *this account's* index rather
    than against the Store alone. A Version hash is guessable by anyone holding
    the same Spec, so the Store would happily resolve one an account was never
    shown.
    """
    if (agent_id is None) == (version_hash is None):
        raise ApiProblem(400, "name exactly one of agent_id or version_hash")
    if agent_id is not None:
        found = await state.index.get_agent(account.id, agent_id)
        if found is None:
            raise ApiProblem(404, f"no agent {agent_id!r}")
        return found[0].version_hash, found[0].agent_id
    pinned = VersionHash(str(version_hash))
    entry = await state.index.find_version(account.id, pinned)
    if entry is None:
        if await state.index.find_workflow_version(account.id, pinned) is not None:
            return pinned, ""
        raise ApiProblem(404, f"no published agent with version_hash {pinned!r}")
    return pinned, entry.agent_id


async def _resolve_dispatch_target(
    state: AppState, account: Account, body: DispatchRequest
) -> tuple[VersionHash, str]:
    """What ``POST /api/runs`` runs.

    Continuing a conversation settles both fields from the Run being continued,
    ahead of anything the body names: that pins the Version the thread opened
    on, and keeps the thread with the agent it started with. Changing either
    halfway through a conversation is what pinning exists to prevent, so the
    body does not get to ask for it here. Forking is the deliberate way to
    run a different agent, and it starts a *different* conversation to do it.
    """
    if body.continues_run_id is not None:
        inherited = await _inherited_target(state, account, body.continues_run_id)
        if inherited is not None:
            return inherited
    if body.workflow_id is not None:
        if body.agent_id is not None or body.version_hash is not None:
            raise ApiProblem(400, "name exactly one of agent_id, version_hash or workflow_id")
        workflow = await state.index.get_workflow(account.id, body.workflow_id)
        if workflow is None:
            raise ApiProblem(404, f"no workflow {body.workflow_id!r}")
        return workflow.version_hash, ""
    return await _named_target(state, account, body.agent_id, body.version_hash)


async def _start_run(
    state: AppState,
    account: Account,
    *,
    message: str,
    continues: RunId | None,
    version_hash: VersionHash,
    agent_id: str,
    branch_id: str,
    conversation_id: str,
) -> DispatchResponse:
    """Admit a Run and record it in this backend's own index.

    Shared by ``POST /api/runs`` and the branch and fork routes below, which
    differ only in what they resolve before getting here. Branching and forking
    are both an ordinary dispatch that continues an *earlier* Run of the
    conversation; all that separates them is whether the new Run keeps the
    conversation id or takes a new one. If that were not so, each would need
    its own execution path through the runtime.
    """
    version = await state.store.get_version(version_hash)
    if version is None:
        raise ApiProblem(404, f"no published agent with version_hash {version_hash!r}")

    scope = scope_for(account)
    try:
        dispatched = await psych_runtime.dispatch(
            state.store,
            version_hash,
            scope,
            # `end_user_id` is a decision, not a fallthrough. Without it
            # `Runtime` would reach `Scope.principal`, which is this account id
            # anyway, and nobody reading the code later could tell the default
            # from the choice. Passing it here also puts it on `RunAdmitted`,
            # so a trace says whose memories a Run was reading. A consumer
            # whose agent serves *their* customers passes the customer's id in
            # exactly this position. See `app.memory_store`.
            input={"message": message, "end_user_id": account.id},
            continues=continues,
        )
    except RunNotFound as err:
        raise ApiProblem(404, f"no run {continues!r} to continue") from err
    except AccessDenied as err:
        raise ApiProblem(403, str(err)) from err
    agent = await state.index.find_version(account.id, version_hash)
    workflow = (
        await state.index.find_workflow_version(account.id, version_hash)
        if agent_id == ""
        else None
    )
    await state.index.put_run(
        RunEntry(
            run_id=dispatched.run_id,
            agent_id=agent_id,
            workflow_id=workflow.workflow_id if workflow is not None else "",
            branch_id=branch_id,
            conversation_id=conversation_id,
            name=version.spec.name,
            tenant=scope.tenant,
            started_at=datetime.now(UTC),
            version_hash=version_hash,
            # Copied at admission, not read back at execution time: see
            # RunEntry.approval_selectors.
            approval_selectors=(
                agent.approval_selectors
                if agent is not None
                else state.settings.default_approval_selectors
            ),
            message=message,
        )
    )
    return DispatchResponse(run_id=dispatched.run_id)


@app.post("/api/runs", status_code=201, response_model=DispatchResponse)
async def create_run(body: DispatchRequest, request: Request) -> DispatchResponse:
    """Dispatch a Run -- a fresh conversation, or, when ``continues_run_id`` is
    set, a second message in one already underway. Continuing is a
    dispatch like any other; the history a continued Run's model call actually
    sees comes from the log via ``psych_runtime.dispatch(continues=...)``, never from
    anything this backend assembles itself.
    """
    state = _state(request)
    account = await _account(request)
    # The pointer is read exactly here, once, and the hash it yields is what
    # the Run pins. Editing the agent a second later moves the pointer and
    # leaves this Run running what it started on -- which is the property that
    # lets an agent be editable at all, since `psych_runtime.runtime.execute` reloads
    # the pinned Version at the top of every Attempt including a reclaiming
    # Worker's.
    version_hash, agent_id = await _resolve_dispatch_target(state, account, body)
    continues = RunId(body.continues_run_id) if body.continues_run_id else None
    tree = await _read_tree(state, account)
    return await _start_run(
        state,
        account,
        message=body.message,
        continues=continues,
        version_hash=version_hash,
        agent_id=agent_id,
        branch_id=tree.branch_for_continuation(continues),
        conversation_id=(
            new_conversation_id() if continues is None else tree.conversation_of(continues)
        ),
    )


@app.post("/api/runs/{run_id}/branch", status_code=201, response_model=DispatchResponse)
async def branch_run(run_id: str, body: BranchRequest, request: Request) -> DispatchResponse:
    """Ask a message of this conversation again, differently, keeping both.

    This is branching, and it stays *inside* the conversation. ``run_id`` names
    the Run whose message is being re-asked; the new Run continues that Run's
    predecessor, so everything said before that message is shared history and
    everything from it on is a second future. Both futures carry the same
    ``conversation_id``, the history list shows one chat, and the console pages
    between them as message versions.

    The agent does not change. Re-asking a question of the same conversation is
    a different wording, not a different correspondent, and letting it switch
    agents halfway would produce a thread whose two halves were answered by
    different Specs with nothing in the chat saying so. Putting one question to
    two agents is what ``/fork`` is for.

    At Run boundaries only, never mid-Run. In a chat thread every user message
    opens a Run, so "branch from this question" and "branch from that
    answer" both already land on a boundary and a prefix bound into the log
    would buy nothing. It would cost something: a Run truncated partway is a
    Run whose tool calls may have no results, which is a log Psych's own
    reducer would be right to call corrupt.

    The new Run is on a new branch, which is what stops the console following
    an arbitrary one of the two futures afterwards. It gets that branch without
    saying so: the message it continues from already has a Run continuing it.
    See ``_Tree.branch_for_continuation``.

    Branching from the conversation's first message has no predecessor to
    continue, so the new Run starts with no history. It is still the same
    conversation and still a second branch of it, which is what the arrows in
    the console will offer on that first message.

    A merge is not the other half of this and is not coming. Two divergent
    branches over an append-only log have nothing to reconcile.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)

    inherited = await _inherited_target(state, account, run_id)
    if inherited is None:
        raise ApiProblem(400, f"run {run_id!r} has no agent recorded; it cannot be branched")
    version_hash, agent_id = inherited

    continues = header.continues_run_id
    tree = await _read_tree(state, account)
    return await _start_run(
        state,
        account,
        message=body.message,
        continues=continues,
        version_hash=version_hash,
        agent_id=agent_id,
        branch_id=tree.branch_for_continuation(continues),
        conversation_id=tree.conversation_of(RunId(run_id)),
    )


@app.post("/api/runs/{run_id}/fork", status_code=201, response_model=DispatchResponse)
async def fork_run(run_id: str, body: ForkRequest, request: Request) -> DispatchResponse:
    """Take this conversation somewhere else, from ``run_id``, as a new chat.

    Forking is the other operation, and the difference from ``/branch`` is one
    field. The new Run diverges at exactly the same place -- it continues the
    named Run's predecessor, so the history up to that message comes with it --
    but it takes a fresh ``conversation_id``, so it is a chat of its own from
    the moment it is dispatched. It gets its own row in the history list, it is
    deleted on its own, and deleting the conversation it came from leaves it
    standing.

    That last part is why the shared history cannot simply be deleted with its
    original. Those Runs belong to the old conversation and are replayed by the
    new one, which is Git's situation exactly, and ``DELETE /api/runs`` gives
    Git's answer: a Run a surviving fork still reaches is kept and unlisted
    rather than removed. Nothing is copied to achieve that. The forked chat
    reads the very Runs it forked from, so its history is the history rather
    than a snapshot of it that could drift.

    The agent *may* change here, and that is the point of forking rather than
    branching: the same question put to two agents, side by side, in two chats
    a person can hold open at once. Continuing or branching a conversation must
    not switch agents halfway through. Starting a new one may.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)

    if body.agent_id is not None:
        version_hash, agent_id = await _named_target(state, account, body.agent_id, None)
    else:
        inherited = await _inherited_target(state, account, run_id)
        if inherited is None:
            raise ApiProblem(400, f"run {run_id!r} has no agent recorded; name one to fork onto")
        version_hash, agent_id = inherited

    return await _start_run(
        state,
        account,
        message=body.message,
        continues=header.continues_run_id,
        version_hash=version_hash,
        agent_id=agent_id,
        # A new conversation, so a new branch too: the fork's Runs must never
        # be walked as a continuation of the branch it left.
        branch_id=new_branch_id(),
        conversation_id=new_conversation_id(),
    )


@app.get("/api/runs", response_model=list[RunSummary])
async def list_runs(request: Request) -> list[RunSummary]:
    state = _state(request)
    account = await _account(request)
    entries = await state.index.list_runs(account.id)
    # One fold, for the conversation ids: a Run recorded before conversations
    # had them is read as the root of its own chain, which needs the tree.
    tree = await _read_tree(state, account)
    summaries: list[RunSummary] = []
    for entry in entries:
        header = await state.store.get_run(entry.run_id)
        run_state = header.state.value if header is not None else "unknown"
        run_settled_at: str | None = entry.settled_at.isoformat() if entry.settled_at else None
        if run_settled_at is None and header is not None and header.state is RunState.SETTLED:
            # One read, the first time a settled Run is listed, for the one
            # timestamp `RunHeader` does not carry. Cached on the entry after
            # that: this list is polled every few seconds while anything is
            # running, and reading every settled Run's whole log each time is
            # the kind of cost that only shows up once someone has history.
            log = await state.store.read(entry.run_id)
            run_settled_at = settled_at(log)
            if run_settled_at is not None:
                await state.index.mark_settled(entry.run_id, datetime.fromisoformat(run_settled_at))
        summaries.append(
            RunSummary(
                run_id=entry.run_id,
                agent_id=entry.agent_id,
                workflow_id=entry.workflow_id,
                kind="workflow" if entry.workflow_id else "agent",
                conversation_id=tree.conversation_of(entry.run_id),
                branch_id=entry.branch_id,
                name=entry.name,
                tenant=entry.tenant,
                state=run_state,
                started_at=entry.started_at.isoformat(),
                version_hash=entry.version_hash,
                message=entry.message,
                settled_at=run_settled_at,
                continues_run_id=header.continues_run_id if header is not None else None,
            )
        )
    return summaries


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    state = _state(request)
    await _require_run(state, run_id, await _account(request))

    async def event_source() -> AsyncIterator[bytes]:
        # The keep-alive runs beside the stream, not around it. Wrapping
        # `__anext__` in `wait_for` cancels the generator on timeout: the
        # cancellation is thrown into its own frame, it finalises, and the
        # next `__anext__` raises StopAsyncIteration -- so this endpoint sent
        # `event: done` on a Run that had merely been quiet for fifteen
        # seconds. A suspended approval, a slow model or an OAuth wait all
        # look like that, and the client treats `done` as terminal, so the
        # page froze until a reload.
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for record in psych_runtime.stream(state.store, RunId(run_id), after=after):
                    await queue.put(f"data: {json.dumps(jsonable_encoder(record))}\n\n".encode())
            except Exception:
                _LOG.exception("streaming run %s failed", run_id)
            finally:
                await queue.put(None)

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except TimeoutError:
                    # A comment line, not an event: keeps an idle connection
                    # open through a proxy that drops silent ones, without the
                    # client mistaking it for a Record.
                    yield b": keep-alive\n\n"
                    continue
                if frame is None:
                    break
                yield frame
            yield b"event: done\ndata: {}\n\n"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        # Both matter behind a proxy: without them nginx buffers the whole
        # response and the "stream" arrives at once, when the Run ends.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/report")
async def get_report(run_id: str, request: Request) -> JSONResponse:
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    rpt = await psych_runtime.report(state.store, RunId(run_id))
    return JSONResponse(jsonable_encoder(rpt))


@app.get("/api/runs/{run_id}/status")
async def get_run_status(run_id: str, request: Request) -> JSONResponse:
    """What the Run is doing right now, from ``psych_runtime.status``.

    Replaces this backend's own projection of the reducer's working dataclass:
    the library ships one now (``psych_runtime.RunStatus``), it serialises directly,
    and it carries the two things the hand-rolled version could not -- the
    exact call awaiting approval with its arguments, and a distinct lifecycle
    for a Run that has been told to stop and has not settled yet.
    """
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    status = await psych_runtime.status(state.store, RunId(run_id))
    return JSONResponse(jsonable_encoder(status))


@app.get("/api/runs/{run_id}/messages", response_model=list[MessageOut])
async def get_run_messages(run_id: str, request: Request) -> list[MessageOut]:
    """The conversation, projected for a chat UI to render a past run without
    interpreting raw Records itself.

    Built from ``psych_runtime.core.conversation.build_conversation`` -- Psych's own
    conversation projection, the same one a Worker replays to build a
    byte-identical prompt after a crash. See
    ``app.serialization.build_message_views`` for exactly how ``seq``/``at``
    are attached to what that function returns.
    """
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    return [
        MessageOut(
            role=view.role,
            content=view.content,
            tool_name=view.tool_name,
            seq=view.seq,
            at=view.at.isoformat(),
        )
        for view in message_views(await psych_runtime.records(state.store, RunId(run_id)))
    ]


@app.delete("/api/agents/{agent_id}", response_model=OkResponse)
async def delete_agent(agent_id: str, request: Request) -> OkResponse:
    """Stop offering this agent, with every Version it has had. No Version is
    destroyed.

    ``Store`` has no delete and should not: a Version is an immutable
    content-hashed publication, and every Run that pinned it still reads that
    Spec to know what it ran (DESIGN.md §4). What a platform owns is its own
    catalogue, so this removes the agent from ``GET /api/agents`` and leaves
    the Runs it already produced intact and readable.
    """
    state = _state(request)
    account = await _account(request)
    if not await state.index.remove_agent(account.id, agent_id):
        raise ApiProblem(404, f"no agent {agent_id!r}")
    return OkResponse()


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


def _workflow_summary(entry: WorkflowEntry) -> WorkflowSummary:
    return WorkflowSummary(**summary_of(entry))


@app.post("/api/workflows", status_code=201, response_model=CreateWorkflowResponse)
async def create_workflow(body: CreateWorkflowRequest, request: Request) -> CreateWorkflowResponse:
    """Publish a workflow (DESIGN.md §5). Validated at publish like an agent,
    run by the same Worker under the same log, memoised step by step."""
    state = _state(request)
    account = await _account(request)
    try:
        entry, created = await state.workflows.publish(
            account.id,
            WorkflowDefinition(
                name=body.name,
                description=body.description,
                steps=list(body.steps),
                limits=body.limits,
            ),
            workflow_id=body.workflow_id,
        )
    except WorkflowRefused as err:
        raise ApiProblem(err.status, str(err)) from err
    return CreateWorkflowResponse(
        workflow_id=entry.workflow_id,
        version_hash=str(entry.version_hash),
        name=entry.name,
        created=created,
    )


@app.get("/api/workflows", response_model=list[WorkflowSummary])
async def list_workflows(request: Request) -> list[WorkflowSummary]:
    state = _state(request)
    account = await _account(request)
    return [_workflow_summary(e) for e in await state.index.list_workflows(account.id)]


@app.get("/api/workflows/{workflow_id}", response_model=WorkflowSummary)
async def get_workflow(workflow_id: str, request: Request) -> WorkflowSummary:
    state = _state(request)
    entry = await state.index.get_workflow((await _account(request)).id, workflow_id)
    if entry is None:
        raise ApiProblem(404, f"no workflow {workflow_id!r}")
    return _workflow_summary(entry)


@app.delete("/api/workflows/{workflow_id}", response_model=OkResponse)
async def delete_workflow(workflow_id: str, request: Request) -> OkResponse:
    """As ``DELETE /api/agents/{agent_id}``: out of the catalogue, never out of
    the Store, so every Run that pinned it still reads what it ran."""
    state = _state(request)
    if not await state.index.remove_workflow((await _account(request)).id, workflow_id):
        raise ApiProblem(404, f"no workflow {workflow_id!r}")
    return OkResponse()


@app.delete("/api/runs/{run_id}", response_model=OkResponse)
async def delete_run(run_id: str, request: Request, thread: bool = False) -> OkResponse:
    """Remove a chat from the history list.

    With ``thread=true`` the conversation goes rather than the single turn,
    which is what deleting a chat means to a person: one conversation is a
    chain of Runs and removing only the newest would leave its
    predecessors as a stump.

    ## The whole tree, and only this tree

    Deleting takes the whole conversation, every branch of it. Branches are not
    separate chats: they are the versions of a message a person pages between,
    so deleting one of them individually is not an operation anyone asked for,
    and deleting the chat you are looking at plainly means all of it.

    A **fork** is the case that needs care, because a fork is a different
    conversation that replays some of this one's Runs as its history. Those
    Runs are commits two refs reach, so this takes Git's answer:

    - A Run of this conversation that **no surviving conversation reads** is
      removed from the catalogue.
    - A Run of this conversation that a surviving fork **does** read is kept
      and unlisted. It stops being a chat and stays available as that fork's
      history, because the fork replays it.
    - Every other conversation is untouched, including the one this was forked
      from and the ones forked off it.

    Then a collection pass, for the same reason Git has one: a Run kept only
    because some fork reached it, whose last reader has since been deleted, is
    unreachable and goes. Without it the catalogue would accumulate fragments
    nobody can open.

    Like ``DELETE /api/agents``, all of this is catalogue removal. The Runs and
    their Records stay in the Store, reachable by id, because ``Store`` has no
    delete (DESIGN.md §7) and a Run that pinned a Version must stay readable.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)

    if not thread:
        # One turn, named directly. No branch is being deleted, so nothing is
        # reachable-from-elsewhere in a way this has to reason about.
        await state.index.remove_run(RunId(run_id))
        return OkResponse()

    _ = header
    tree = await _read_tree(state, account)
    conversation_id = tree.conversation_of(RunId(run_id))
    reachable = tree.reachable_from_listed(excluding=conversation_id)
    for target in tree.in_conversation(conversation_id):
        if target in reachable:
            await state.index.unlist_run(target)
        else:
            await state.index.remove_run(target)

    # Collect what the deletion made unreachable: a Run kept earlier for a fork
    # that has since gone is nobody's history now.
    after = await _read_tree(state, account)
    still_reachable = after.reachable_from_listed()
    for orphan in set(after.branches) - still_reachable:
        if orphan not in after.listed:
            await state.index.remove_run(orphan)
    return OkResponse()


@app.get("/api/runs/{run_id}/answer", response_model=AnswerResponse)
async def get_run_answer(run_id: str, request: Request) -> AnswerResponse:
    """What the run concluded, and the work behind it.

    ``psych_runtime.answer()`` served straight through. The split is derived from the
    log rather than decided by the model: the answer is the turn the loop
    itself finished on, and the work is everything before it. A console shows
    the answer and collapses the rest.
    """
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    view = await psych_runtime.answer(state.store, RunId(run_id))
    return AnswerResponse(
        run_id=view.run_id,
        text=view.text,
        finished=view.finished,
        summary=view.summary(),
        tool_call_count=view.tool_call_count,
        work=[
            WorkTurnOut(
                turn=turn.turn,
                text=turn.text,
                failure_message=turn.failure_message,
                at=turn.at.isoformat(),
                tool_calls=[
                    ToolCallOut(
                        call_id=call.call_id,
                        tool=call.tool,
                        arguments=call.arguments,
                        outcome=call.outcome.value if call.outcome is not None else None,
                        result=call.result,
                        failure_message=call.failure_message,
                        duration_seconds=call.duration_seconds,
                        started_at=call.started_at.isoformat(),
                        finished_at=(
                            call.finished_at.isoformat() if call.finished_at is not None else None
                        ),
                    )
                    for call in turn.tool_calls
                ],
            )
            for turn in view.work
        ],
    )


@app.get("/api/threads/{run_id}/report", response_model=ThreadReportResponse)
async def get_thread_report(run_id: str, request: Request) -> ThreadReportResponse:
    """A whole conversation's report: the totals, and every Run's own.

    ``run_id`` may name any Run in the chain, not only the first.
    ``psych_runtime.thread`` walks to the root and forward from there, so a link from
    the newest message and a link from the oldest land on the same page.

    Reports are read per Run because that is what ``psych_runtime.report`` projects,
    and a conversation is a handful of Runs rather than a page of them. The
    totals are summed **here**, once, rather than in the console: two places
    adding money is two places to get it wrong differently.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)
    # `psych_runtime.thread` walks *backwards* to the root, which is all it can do:
    # `Store` has no query from a Run to the Runs continuing it, deliberately
    # (DESIGN.md §7), so entering from the first message would otherwise report
    # a one-message conversation. Walking forward is this backend's job, which
    # is what `PlaygroundIndex` is for. Resolve the newest Run in the chain
    # first, then let the library walk back over the whole thing.
    newest = await _newest_in_thread(state, account, RunId(run_id))
    try:
        view = await psych_runtime.thread(state.store, newest, scope=header.scope)
    except AccessDenied as err:
        raise ApiProblem(403, str(err)) from err

    reports = [await psych_runtime.report(state.store, RunId(rid)) for rid in view.run_ids]

    usage = Usage()
    cost: Cost | None = None
    unpriced = unreported_usage = model_calls = failed_calls = tool_calls = provider_priced = 0
    compaction_calls = 0
    model_seconds = tool_seconds = 0.0
    for report in reports:
        totals = report.totals
        usage = usage + totals.usage
        # Only priced calls contribute, and `Cost.__add__` refuses to mix
        # currencies rather than inventing a rate. A conversation that somehow
        # spanned two currencies should fail loudly here rather than report a
        # number that is the sum of neither.
        if totals.cost is not None:
            cost = totals.cost if cost is None else cost + totals.cost
        unpriced += totals.unpriced_model_calls
        unreported_usage += totals.unreported_usage_calls
        provider_priced += totals.provider_reported_costs
        model_calls += totals.model_calls
        failed_calls += totals.failed_model_calls
        tool_calls += totals.tool_calls
        compaction_calls += totals.compaction_calls
        model_seconds += totals.latency.model_seconds
        tool_seconds += totals.latency.tool_seconds

    # Measured end to end, not summed: the gaps between messages are a person
    # reading and typing, and dropping them would report a two minute exchange
    # as eight seconds. See `ThreadTotalsOut.wall_clock_seconds`.
    wall_clock = _thread_wall_clock(view.run_ids, reports)

    return ThreadReportResponse(
        run_ids=[str(rid) for rid in view.run_ids],
        reports=[jsonable_encoder(report) for report in reports],
        messages=[
            ThreadMessageOut(
                run_id=message.run_id,
                role=message.role,
                content=message.content,
                tool_name=message.tool_name,
                seq=message.seq,
                at=message.at.isoformat(),
            )
            for message in view.messages
        ],
        totals=ThreadTotalsOut(
            messages=len(view.run_ids),
            model_calls=model_calls,
            failed_model_calls=failed_calls,
            tool_calls=tool_calls,
            compaction_calls=compaction_calls,
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            reasoning_tokens=usage.reasoning,
            total_tokens=usage.input + usage.output + usage.cache_read + usage.cache_write,
            usage_source=(
                "unknown"
                if model_calls + compaction_calls == 0
                or unreported_usage == model_calls + compaction_calls
                else "partial"
                if unreported_usage > 0
                else "provider"
            ),
            unreported_usage_calls=unreported_usage,
            cost_amount=str(cost.amount) if cost is not None else None,
            cost_currency=cost.currency if cost is not None else None,
            cost_source=cost.source if cost is not None else None,
            provider_reported_costs=provider_priced,
            cost_input_amount=(
                str(cost.input_amount)
                if cost is not None and cost.input_amount is not None
                else None
            ),
            cost_output_amount=(
                str(cost.output_amount)
                if cost is not None and cost.output_amount is not None
                else None
            ),
            cost_cache_read_amount=(
                str(cost.cache_read_amount)
                if cost is not None and cost.cache_read_amount is not None
                else None
            ),
            cost_cache_write_amount=(
                str(cost.cache_write_amount)
                if cost is not None and cost.cache_write_amount is not None
                else None
            ),
            unpriced_model_calls=unpriced,
            cost_is_incomplete=unpriced > 0,
            wall_clock_seconds=wall_clock,
            model_seconds=model_seconds,
            tool_seconds=tool_seconds,
        ),
    )


@app.get("/api/runs/{run_id}/thread", response_model=ThreadResponse)
async def get_run_thread(run_id: str, request: Request) -> ThreadResponse:
    """One conversation, across every Run in its chain, oldest message first.

    ``psych_runtime.thread`` does the walk and the projection: this endpoint exists to
    turn its result into JSON and to map the tenancy refusal onto a status
    code. The version that lived here walked ``continues_run_id`` itself and
    re-derived each message's position by re-implementing the library's own
    conversation projection beside it -- with an assertion guarding against the
    two drifting, which is what said plainly that the library should provide it.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)
    newest = await _newest_in_thread(state, account, RunId(run_id))
    try:
        view = await psych_runtime.thread(state.store, newest, scope=header.scope)
    except AccessDenied as err:
        raise ApiProblem(403, str(err)) from err
    return ThreadResponse(
        run_ids=list(view.run_ids),
        messages=[
            ThreadMessageOut(
                run_id=message.run_id,
                role=message.role,
                content=message.content,
                tool_name=message.tool_name,
                seq=message.seq,
                at=message.at.isoformat(),
            )
            for message in view.messages
        ],
    )


@app.post("/api/runs/{run_id}/resume", response_model=OkResponse)
async def resume_run(run_id: str, body: ResumeRequest, request: Request) -> OkResponse:
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    try:
        await psych_runtime.resume(
            state.store,
            RunId(run_id),
            approved=body.approved,
            payload=body.payload,
            by=body.by,
        )
    except RunNotSuspended as err:
        raise ApiProblem(409, str(err)) from err
    except SuspensionExpired as err:
        # 410: the decision arrived after the suspension's own expiry, so the
        # Run was settled abandoned rather than executing a call decided
        # against a world that has moved on.
        raise ApiProblem(410, str(err)) from err
    return OkResponse()


@app.post("/api/runs/{run_id}/interrupt", response_model=OkResponse)
async def interrupt_run(run_id: str, body: InterruptRequest, request: Request) -> OkResponse:
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    await psych_runtime.interrupt(state.store, RunId(run_id), reason=body.reason)
    return OkResponse()


@app.post("/api/runs/{run_id}/send", response_model=SendResponse)
async def send_to_run(run_id: str, body: SendRequest, request: Request) -> SendResponse:
    """A message into a Run that is still executing (DESIGN.md §9).

    Three queues, chosen by the caller, because "stop mid-answer and say
    something else" is three different intentions: correct the turn running
    now (``steer``), add something once it finishes (``follow_up``), or leave a
    note for the next Run in this conversation (``next_run``). The library
    refuses the first two after an interrupt, and that refusal is surfaced as
    a 409 rather than smoothed over, since the queue the client picked is the
    thing that would otherwise be silently wrong.
    """
    state = _state(request)
    await _require_run(state, run_id, await _account(request))
    try:
        entry_id = await psych_runtime.send(
            state.store, RunId(run_id), message=body.message, queue=QueueKind(body.queue)
        )
    except RunAlreadySettled as err:
        raise ApiProblem(
            409,
            f"{err} A message for a settled Run is a new one: POST /api/runs with "
            "continues_run_id.",
        ) from err
    except (ValueError, CorruptLog) as err:
        # A steer or follow-up after an abort: the reducer refuses it before
        # it is written (DESIGN.md §9), and the client wanted `next_run`.
        raise ApiProblem(409, str(err)) from err
    return SendResponse(entry_id=entry_id, queue=body.queue)


@app.get("/api/runs/{run_id}/state")
async def get_run_state(run_id: str, request: Request) -> JSONResponse:
    """``psych_runtime.state()``: the reducer's working object, whole.

    Everything ``status()`` derives its screen-shaped answer from -- open tool
    calls, the three queues, failure streaks, repeat counts, compaction
    boundaries, children, result handles. For the debugging view, where the
    question is "what does the runtime believe right now", and deliberately
    not for a chat window.
    """
    state = _state(request)
    header = await _require_run(state, run_id, await _account(request))
    view = await psych_runtime.state(state.store, RunId(run_id), scope=header.scope)
    return JSONResponse(jsonable_encoder(view))


@app.get("/api/runs/{run_id}/records")
async def get_run_records(
    run_id: str, request: Request, after: int = 0, limit: int | None = None
) -> JSONResponse:
    """``psych_runtime.records()``: one page of the raw log, for a consumer
    building their own projection or a client that wants the whole log at
    once rather than as a stream."""
    state = _state(request)
    header = await _require_run(state, run_id, await _account(request))
    log = await psych_runtime.records(
        state.store, RunId(run_id), after=after, limit=limit, scope=header.scope
    )
    return JSONResponse(jsonable_encoder(list(log)))


@app.get("/api/runs/{run_id}/text")
async def stream_run_text(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
    """``psych_runtime.stream_text()`` as server-sent events: only the assistant's
    words, for a client that renders nothing else.

    The full record stream stays the truth and ``/stream`` serves it; this is
    the projection over it that every chat client otherwise writes itself and
    gets wrong the same three ways (text arrives on ``model_call_finished``,
    an abort is a Record, the iterator ends at settlement). A Run that ends
    without an answer is an ``event: error`` naming why, never a silent close.
    """
    state = _state(request)
    header = await _require_run(state, run_id, await _account(request))

    async def event_source() -> AsyncIterator[bytes]:
        try:
            async for delta in psych_runtime.stream_text(
                state.store, RunId(run_id), after=after, scope=header.scope
            ):
                yield f"data: {json.dumps(delta)}\n\n".encode()
        except RunEndedWithoutAnswer as err:
            yield f"event: error\ndata: {json.dumps(str(err))}\n\n".encode()
            return
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def _subagent_node(child: SubagentReport, parent_run_id: str, state: str) -> SubagentNodeOut:
    """One node of the tree, from the parent's log and the child's own report."""
    branch = child.subtree
    nested = child.report.subagents if child.report is not None else ()
    return SubagentNodeOut(
        name=child.name,
        run_id=child.child_run_id,
        parent_run_id=parent_run_id,
        state=state,
        terminal_state=child.terminal_state.value if child.terminal_state else None,
        purpose=child.purpose,
        task=child.task,
        deliverable=child.deliverable,
        tools=list(child.tools),
        model=child.model,
        depth=child.delegation_depth,
        spawned_at=child.spawned_at.isoformat(),
        finished_at=child.finished_at.isoformat() if child.finished_at else None,
        messages_sent=child.messages_sent,
        latest=_child_latest(child),
        error=child.failure.message if child.failure is not None else None,
        input_tokens=child.usage.input + child.usage.cache_read,
        output_tokens=child.usage.output,
        cost=str(child.cost.amount) if child.cost is not None else None,
        subtree_input_tokens=(
            branch.usage.input + branch.usage.cache_read if branch else child.usage.input
        ),
        subtree_output_tokens=branch.usage.output if branch else child.usage.output,
        subtree_cost=str(branch.cost.amount) if branch and branch.cost else None,
        subtree_runs=branch.runs if branch else 1,
        complete=branch.complete if branch else child.terminal_state is not None,
        children=[
            # The state of a grandchild is read from its own report rather than
            # from another store round trip: `child_depth` already walked it.
            _subagent_node(
                grandchild,
                child.child_run_id,
                _lifecycle_of(grandchild),
            )
            for grandchild in nested
        ],
    )


def _lifecycle_of(child: SubagentReport) -> str:
    """A grandchild's state, from what its own report already says.

    The tree endpoint reads a top-level child's lifecycle from `psych_runtime.status`,
    which is exact. Deeper down it uses this, which is the same answer derived
    from the walked report: worth having rather than one status call per node in
    a wide tree, and honest about being a projection of a projection.
    """
    if child.terminal_state is None:
        return "running"
    return SETTLED_LIFECYCLE[child.terminal_state].value


def _child_latest(child: SubagentReport) -> str:
    """The last thing a child said, for the tree's one-line preview."""
    if child.output is not None:
        text = child.output.get("text")
        if isinstance(text, str) and text.strip():
            return text
    if child.report is not None:
        for call in reversed(child.report.model_calls):
            if call.text.strip():
                return call.text
    return ""


@app.get("/api/runs/{run_id}/subagents", response_model=SubagentTreeResponse)
async def get_subagents(run_id: str, request: Request, depth: int = 2) -> SubagentTreeResponse:
    """The subagent tree under one Run, with tokens and cost rolled up per branch.

    A projection over the log and nothing else (DESIGN.md §6): the parent's own
    records say what it spawned, what it steered and how each child ended, and
    `psych_runtime.report(child_depth=...)` walks into each child's log for the rest.
    There is no tree stored beside the log for this to read, which is why a tree
    watched live and a tree read back a year later cannot disagree.

    Each top-level child's ``state`` comes from `psych_runtime.status` on the child
    itself rather than from whether the parent has heard back. The two differ
    while a notification is in flight, and that is exactly the moment somebody
    is watching.
    """
    state = _state(request)
    account = await _account(request)
    header = await _require_run(state, run_id, account)

    report = await psych_runtime.report(
        state.store, RunId(run_id), child_depth=depth, scope=header.scope
    )
    nodes: list[SubagentNodeOut] = []
    for child in report.subagents:
        status = await psych_runtime.status(state.store, child.child_run_id, scope=header.scope)
        nodes.append(_subagent_node(child, run_id, status.lifecycle.value))

    subtree = report.subtree
    return SubagentTreeResponse(
        run_id=run_id,
        state=(
            await psych_runtime.status(state.store, RunId(run_id), scope=header.scope)
        ).lifecycle.value,
        children=nodes,
        total_input_tokens=(
            subtree.usage.input + subtree.usage.cache_read
            if subtree
            else report.totals.usage.input + report.totals.usage.cache_read
        ),
        total_output_tokens=(subtree.usage.output if subtree else report.totals.usage.output),
        total_cost=str(subtree.cost.amount) if subtree and subtree.cost else None,
        complete=subtree.complete if subtree else True,
    )


async def _require_child(
    state: AppState, run_id: str, child_run_id: str, account: Account
) -> RunHeader:
    """A child Run, if it belongs to ``account`` and to this parent.

    Both halves matter. The scope check is what stops one account reaching
    another's Run (`_require_run`), and the parentage check is what stops a
    console control aimed at a tree acting on an unrelated Run of the same
    account's that happens to be open in another tab.
    """
    await _require_run(state, run_id, account)
    child = await _require_run(state, child_run_id, account)
    if child.parent_run_id != RunId(run_id):
        raise ApiProblem(404, f"no subagent {child_run_id!r} under run {run_id!r}")
    return child


@app.post("/api/runs/{run_id}/subagents/{child_run_id}/message", response_model=OkResponse)
async def message_subagent(
    run_id: str, child_run_id: str, body: SubagentMessageRequest, request: Request
) -> OkResponse:
    """Send a message to a running subagent, from the console.

    The same steering queue the parent agent's own `message_subagent` tool uses
    (DESIGN.md §9), so a person and an agent steering the same child do it one
    way, and both arrivals are in the child's log in the order they happened.
    """
    state = _state(request)
    await _require_child(state, run_id, child_run_id, await _account(request))
    try:
        await psych_runtime.send(state.store, RunId(child_run_id), message=body.message)
    except RunAlreadySettled as err:
        raise ApiProblem(409, str(err)) from err
    return OkResponse()


@app.post("/api/runs/{run_id}/subagents/{child_run_id}/interrupt", response_model=OkResponse)
async def interrupt_subagent(
    run_id: str, child_run_id: str, body: InterruptRequest, request: Request
) -> OkResponse:
    """Stop one subagent without stopping its parent.

    An abort is a Record with a sequence number, so "what arrived after the
    stop" stays a question the log answers rather than a race (DESIGN.md §9).
    The parent is told the child stopped through the same notification a clean
    ending uses: from its side, a stopped child is a child that finished.
    """
    state = _state(request)
    await _require_child(state, run_id, child_run_id, await _account(request))
    await psych_runtime.interrupt(state.store, RunId(child_run_id), reason=body.reason)
    return OkResponse()


@app.post(
    "/api/runs/{run_id}/subagents/{child_run_id}/retry",
    status_code=201,
    response_model=SubagentRetryResponse,
)
async def retry_subagent(run_id: str, child_run_id: str, request: Request) -> SubagentRetryResponse:
    """Run a failed subagent's Spec again, as a new Run.

    Not a second attempt at the old one. A Run is admitted once and its log is
    append-only, so "try that again" is a new log pinning the same Version with
    the same input -- which is also what makes it honest: the failed Run stays
    in the tree, failed, next to the one that worked.

    The new Run stands outside its old tree. Its parent's log was written by an
    Attempt that finished long ago, and a spawn record may only be appended by
    the Attempt that holds the lease; inventing one now would put a record in
    that log that no Attempt wrote.
    """
    state = _state(request)
    account = await _account(request)
    child = await _require_child(state, run_id, child_run_id, account)

    records = await psych_runtime.records(state.store, RunId(child_run_id), scope=child.scope)
    admitted = next((r for r in records if isinstance(r, RunAdmitted)), None)
    if admitted is None:
        raise ApiProblem(404, f"no run {child_run_id!r}")

    dispatched = await psych_runtime.dispatch(
        state.store, child.version_hash, child.scope, input=dict(admitted.input)
    )
    # Listed like any other Run this account owns, so a retry is visible in
    # the console's own history rather than only reachable from the tree it
    # came out of.
    await state.index.put_run(
        RunEntry(
            run_id=dispatched.run_id,
            name=f"retry of {child_run_id}",
            tenant=child.scope.tenant,
            started_at=datetime.now(UTC),
            version_hash=child.version_hash,
            message=_input_message(admitted.input),
        )
    )
    return SubagentRetryResponse(
        run_id=dispatched.run_id,
        retried_from=child_run_id,
        version_hash=child.version_hash,
    )


def _input_message(payload: dict[str, Any]) -> str:
    value = payload.get("message")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@app.get("/api/tools", response_model=list[ToolOut])
async def list_tools(request: Request) -> list[ToolOut]:
    """Every tool a Spec can name, read off the live ``ToolRegistry``
    (``app.tools.registry``) rather than any list kept beside it -- the two
    cannot drift because there is only one.

    The same answer for every account, since these are the process's own code
    tools rather than anything anyone configured. Still behind a session: the
    only reader is the agent builder, and a catalogue of what this deployment
    can do is not something to hand out to whoever asks.
    """
    await _account(request)
    return [
        ToolOut(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            annotations=sorted(tool.annotations),
            interruptible=tool.interruptible,
            safe_to_retry=tool.safe_to_retry,
        )
        for tool in registry
    ]


# ---------------------------------------------------------------------------
# Capabilities scenarios
#
# DESIGN.md §23, made runnable. Each module under app.scenarios/ drives the
# real runtime end to end and reports what actually happened; nothing here
# is pre-recorded or simulated. See app.scenarios.base for the contract every
# module satisfies and app.scenarios.__init__ for the list.
# ---------------------------------------------------------------------------


async def _scenario_context(state: AppState, account: Account) -> ScenarioContext:
    workspace = await state.settings_store.load(account.id)
    return ScenarioContext(transport=state.transport, active_provider=active_provider(workspace))


def _find_scenario(scenario_id: str) -> ScenarioModule:
    for scenario in SCENARIOS:
        if scenario.INFO.id == scenario_id:
            return scenario
    raise ApiProblem(404, f"no scenario {scenario_id!r}")


def _assertion_out(assertion: Assertion) -> ScenarioAssertionOut:
    return ScenarioAssertionOut(claim=assertion.claim, held=assertion.held, detail=assertion.detail)


def _result_out(result: ScenarioResult) -> ScenarioRunResultOut:
    return ScenarioRunResultOut(
        passed=result.passed,
        summary=result.summary,
        assertions=[_assertion_out(a) for a in result.assertions],
        run_ids=result.run_ids,
        report=result.report,
    )


@app.get("/api/scenarios", response_model=list[ScenarioOut])
async def list_scenarios(request: Request) -> list[ScenarioOut]:
    ctx = await _scenario_context(_state(request), await _account(request))
    out: list[ScenarioOut] = []
    for scenario in SCENARIOS:
        reason = await scenario.check_availability(ctx)
        out.append(
            ScenarioOut(
                id=scenario.INFO.id,
                title=scenario.INFO.title,
                proves=scenario.INFO.proves,
                design_ref=scenario.INFO.design_ref,
                requires=list(scenario.INFO.requires),
                available=reason is None,
                unavailable_reason=reason,
            )
        )
    return out


@app.post("/api/scenarios/{scenario_id}/run")
async def run_scenario(scenario_id: str, request: Request) -> StreamingResponse:
    """SSE: a ``{step, detail, at}`` event per ``emit()`` call the scenario
    makes, then one final ``{"result": {...}}`` event.

    A scenario whose ``check_availability`` names a reason is not run at
    all -- it reports that reason as a failed result rather than a silent
    skip or a vacuous pass, matching every other "cannot run here" path in
    this set. An unhandled exception from the scenario itself is caught the
    same way: reported as a failed result naming what went wrong, rather
    than breaking the stream the caller is reading.
    """
    state = _state(request)
    scenario = _find_scenario(scenario_id)
    ctx = await _scenario_context(state, await _account(request))

    async def event_source() -> AsyncIterator[bytes]:
        reason = await scenario.check_availability(ctx)
        if reason is not None:
            unavailable = ScenarioResult(passed=False, summary=f"unavailable: {reason}")
            payload = json.dumps({"result": _result_out(unavailable).model_dump(mode="json")})
            yield f"data: {payload}\n\n".encode()
            yield b"event: done\ndata: {}\n\n"
            return

        events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        async def emit(step: str, detail: str) -> None:
            await events.put((step, detail))

        run_task = asyncio.create_task(scenario.run(ctx, emit))
        try:
            while not run_task.done():
                try:
                    step, detail = await asyncio.wait_for(events.get(), timeout=1.0)
                except TimeoutError:
                    continue
                progress = ScenarioProgressEvent(
                    step=step, detail=detail, at=datetime.now(UTC).isoformat()
                )
                yield f"data: {progress.model_dump_json()}\n\n".encode()
            while not events.empty():
                step, detail = events.get_nowait()
                progress = ScenarioProgressEvent(
                    step=step, detail=detail, at=datetime.now(UTC).isoformat()
                )
                yield f"data: {progress.model_dump_json()}\n\n".encode()
            result = await run_task
        except Exception as err:
            _LOG.exception("scenario %r raised", scenario_id)
            result = ScenarioResult(passed=False, summary=f"scenario raised: {err!r}")

        payload = json.dumps({"result": _result_out(result).model_dump(mode="json")})
        yield f"data: {payload}\n\n".encode()
        yield b"event: done\ndata: {}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Memory: durable facts across conversations (DESIGN.md §15)
#
# Psych writes these from the `remember` tool during a Run; these routes are
# how a person reads and deletes them. Both halves matter. A memory that works
# and cannot be seen is indistinguishable from one that does not work, and
# erasure is the operation §15 exists for -- a consumer's own customers will
# ask them to delete their data, and this is what answering that looks like.
# ---------------------------------------------------------------------------


@app.get("/api/memories", response_model=MemoriesResponse)
async def list_memories(request: Request) -> MemoriesResponse:
    state = _state(request)
    account = await _account(request)
    facts = await state.memory.recall(scope_for(account), account.id)
    return MemoriesResponse(
        memories=[
            MemoryOut(id=fact.id, content=fact.content, created_at=fact.created_at.isoformat())
            for fact in facts
        ],
        end_user_id=account.id,
    )


@app.delete("/api/memories/{memory_id}", response_model=OkResponse)
async def forget_memory(memory_id: str, request: Request) -> OkResponse:
    """Forget one fact.

    Looked up under this account's own key, so a fact id copied from somewhere
    else finds nothing rather than deleting somebody's memory. A 404 for an id
    that is not there: forgetting is idempotent inside the port, but a route
    that always says "done" would hide a UI listing facts that no longer exist.
    """
    state = _state(request)
    account = await _account(request)
    if not await state.memory.forget(scope_for(account), account.id, memory_id):
        raise ApiProblem(404, f"no memory {memory_id!r}")
    return OkResponse()


@app.delete("/api/memories", response_model=OkResponse)
async def erase_memories(request: Request) -> OkResponse:
    """Erase everything this account's agents remembered.

    Complete and immediate, not a soft delete. This is the operation DESIGN.md
    §15 requires, and the reason it is a first-class button rather than a loop
    over `forget`: a consumer asked to delete an end user's data needs one
    call that finishes the job, not one that half-finishes if it is interrupted.
    """
    state = _state(request)
    account = await _account(request)
    erased = await state.memory.erase(scope_for(account), account.id)
    _LOG.info("erased %d memories for %s", erased, account.id)
    return OkResponse()


# ---------------------------------------------------------------------------
# Settings
#
# Persisted to app.settings_store.SettingsStore (a JSON file, path from
# PSYCH_PLAYGROUND_STATE_FILE) rather than kept in process memory the way
# app.store_index.PlaygroundIndex is: a person configuring a provider or an
# MCP preset by hand expects it to survive the backend restarting, the same
# way an agent they published or a Run they dispatched already does inside
# Psych's own Store.
#
# None of these change what any *published* Spec runs with. A provider swap
# changes which ModelClient the process-wide Worker calls next; the Spec
# a Run pinned at admission never changes underneath it (DESIGN.md §4). An
# MCP entry here is a preset POST /api/agents can be filled in from, never a
# live connection: the Spec still carries its own copy of whatever an agent
# was actually built with.
# ---------------------------------------------------------------------------


def _live_connections(state: AppState, account: Account) -> dict[str, McpConnectionStatus]:
    """The pooled connections this process holds, keyed by server name.

    Read from ``psych_runtime.McpPool.connections()``, which is a look at what exists
    rather than a second connection attempt. Keyed by name because that is what
    a preset is identified by here; two presets pointing at one URL under two
    names would each see their own row, which is right.
    """
    # Filtered by tenant. The pool is process-wide and holds every account's
    # connections; keyed by server name alone this returned another account's
    # live connection under the same preset name, so a settings page showed one
    # person's server as connected because somebody else had connected theirs.
    return {
        status.server: status for status in state.mcp.connections() if status.tenant == account.id
    }


@app.get("/api/settings", response_model=SettingsResponse)
async def get_settings(request: Request) -> SettingsResponse:
    state = _state(request)
    account = await _account(request)
    workspace = await state.settings_store.load(account.id)
    return _settings_response(workspace, _live_connections(state, account), state.sandbox)


@app.put("/api/settings/providers", response_model=SettingsResponse)
async def update_providers(body: UpdateProvidersRequest, request: Request) -> SettingsResponse:
    """Replaces the whole provider list. A provider named by ``id`` in the
    body is merged with what is already stored for that ``id`` (see
    ``ProviderIn``'s own docstring for the ``api_key`` merge rule); anything
    else is created fresh. A provider not named in the body is dropped.
    """
    state = _state(request)
    account = await _account(request)

    def merge(current: PlaygroundState) -> PlaygroundState:
        existing_by_id = {p.id: p for p in current.providers}
        merged: list[ProviderConfig] = []
        for item in body.providers:
            prior = existing_by_id.get(item.id) if item.id is not None else None
            merged.append(
                ProviderConfig(
                    id=item.id if item.id is not None else new_provider_id(),
                    label=item.label,
                    base_url=item.base_url,
                    model=item.model,
                    api_key=_merge_provider_api_key(prior, item.api_key),
                )
            )
        return current.model_copy(
            update={
                "providers": merged,
                "active_provider_id": _reconcile_active_provider_id(
                    merged, current.active_provider_id
                ),
            }
        )

    updated = await state.settings_store.update(account.id, merge)
    return _settings_response(updated)


@app.post("/api/settings/providers/{provider_id}/activate", response_model=OkResponse)
async def activate_provider(provider_id: str, request: Request) -> OkResponse:
    """Switch this account's active provider.

    Takes effect on the next Attempt with nothing to rebuild or invalidate:
    ``AccountRoutedRuntime`` reads the active provider when it assembles a
    ``Runtime``, so the next Run reads this write. A Run already in flight
    finishes on the provider it started with, which is the behaviour worth
    having anyway.
    """
    state = _state(request)
    account = await _account(request)

    def activate(current: PlaygroundState) -> PlaygroundState:
        if not any(p.id == provider_id for p in current.providers):
            raise ApiProblem(404, f"no provider {provider_id!r}")
        return current.model_copy(update={"active_provider_id": provider_id})

    await state.settings_store.update(account.id, activate)
    return OkResponse()


@app.post("/api/settings/providers/{provider_id}/test", response_model=ProviderTestResponse)
async def check_provider(provider_id: str, request: Request) -> ProviderTestResponse:
    """One real call to the provider, so a person can tell a bad key from a
    bad URL before publishing an agent against it. Tests whichever provider
    ``provider_id`` names, active or not -- a provider is worth testing
    before it is ever activated.

    Tries ``known_models()`` first. Its own contract
    (``psych_runtime.model.port.ModelClient.known_models``) is that an empty result
    means "I cannot tell you", not "nothing is valid" -- true of a proxy that
    never exposes ``/models`` and does not by itself mean the provider is
    unreachable. So an empty result falls back to the next-cheapest real
    call, a one-token completion against the provider's configured model,
    which unlike ``known_models()`` raises on exactly the failures a person
    wants distinguished: a bad key, a bad URL, a model id the provider has
    never heard of.
    """
    state = _state(request)
    account = await _account(request)
    workspace = await state.settings_store.load(account.id)
    provider = next((p for p in workspace.providers if p.id == provider_id), None)
    if provider is None:
        raise ApiProblem(404, f"no provider {provider_id!r}")

    # Under the account's own Scope, so this test call goes through the egress
    # seam exactly as one of their Runs would rather than under a placeholder
    # identity a policy could not tell apart from anyone else's.
    client = OpenAICompatibleClient(
        base_url=provider.base_url,
        transport=state.transport,
        scope=scope_for(account),
        api_key=provider.api_key,
    )
    models = await client.known_models()
    if models:
        return ProviderTestResponse(
            ok=True,
            detail=f"reachable: {len(models)} model(s) known at {provider.base_url}",
            models=list(models),
        )

    probe = ModelRequest(
        model=provider.model,
        messages=(UserMessage(content="ping"),),
        max_output_tokens=1,
    )
    try:
        async for event in client.stream(probe):
            if isinstance(event, StreamDone):
                break
    except (TransientError, PsychError) as err:
        return ProviderTestResponse(ok=False, detail=str(err))
    return ProviderTestResponse(
        ok=True, detail=f"model {provider.model!r} responded to a 1-token completion"
    )


@app.put("/api/settings/mcp", response_model=SettingsResponse)
async def update_mcp_settings(body: UpdateMcpServersRequest, request: Request) -> SettingsResponse:
    """Replaces the whole MCP preset list. These are presets the agent
    builder offers, not live connections -- see this section's module
    docstring."""
    state = _state(request)
    account = await _account(request)

    seen: set[str] = set()
    for item in body.mcp_servers:
        if item.name in seen:
            raise ApiProblem(
                400,
                f"two MCP servers are both called {item.name!r}. A preset is addressed by "
                "name -- connecting one, deleting one, naming one in an agent -- so two "
                "with the same name means every one of those reaches an arbitrary one.",
            )
        seen.add(item.name)

    def merge(current: PlaygroundState) -> PlaygroundState:
        stored = {preset.name: preset for preset in current.mcp_servers}
        presets = [
            McpServerPreset(
                name=item.name,
                url=item.url,
                description=item.description,
                transport=item.transport,
                credential=item.credential,
                allow=tuple(item.allow),
                optional=item.optional,
                preload=item.preload,
                oauth=McpOAuthPreset(**item.oauth.model_dump()) if item.oauth is not None else None,
                # Kept across an edit, and only while the connection details
                # are unchanged: a person who fixes a typo in the allow-list
                # has not invalidated what the last connection found, and one
                # who repoints the URL has.
                last_connection=_carried_connection(stored.get(item.name), item),
            )
            for item in body.mcp_servers
        ]
        return current.model_copy(update={"mcp_servers": presets})

    updated = await state.settings_store.update(account.id, merge)
    # A description edited here reaches the very next turn, with no restart:
    # the runtime reads this map through the closure built at boot. Only this
    # account's entries are touched.
    _refresh_descriptions(state.descriptions, account.id, updated)
    return _settings_response(updated, _live_connections(state, account))


@app.get("/api/oauth/pending", response_model=list[PendingAuthorizationOut])
async def list_pending_authorizations(request: Request) -> list[PendingAuthorizationOut]:
    """Authorizations waiting for someone to open a browser.

    ``authorize()`` is called server-side, inside a Run or a preset test, so
    the authorization URL has nowhere to surface by itself. The UI polls this
    and offers the link. See ``app.oauth_redirect`` for why the wait is
    modelled this way rather than opening a browser from the process.
    """
    app_state = _state(request)
    account = await _account(request)
    # This account's own, not the process's. A pending authorization carries an
    # authorization URL that starts a consent flow against somebody's OAuth
    # client; listing every tenant's handed one account a link that would bind
    # another account's connection.
    return [
        PendingAuthorizationOut(
            state=pending.state,
            authorization_url=pending.authorization_url,
            tenant=pending.tenant,
            started_at=pending.started_at.isoformat(),
        )
        for pending in app_state.redirect.pending()
        if pending.tenant == account.id
    ]


@app.get("/api/oauth/callback")
async def oauth_callback(
    request: Request,
    *,
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Where the authorization server sends the browser back.

    Hands the query parameters, verbatim, to whichever ``authorize()`` call is
    waiting on this ``state``. Nothing is validated here on purpose:
    ``OAuthClient`` performs the ``state`` comparison and the RFC 9207 issuer
    check itself, against values this process must not second-guess. Doing any
    of it twice, in two places, is how the two drift and one of them stops
    being the real check.

    Answers HTML rather than JSON because the reader is a person looking at a
    browser tab, not a program.

    The one authenticated-looking route that takes no session, deliberately.
    The browser arriving here has been round-tripped through an authorization
    server and may not carry this console's cookie, and requiring one would
    break the flow at its last step. It is not unprotected: ``state`` is the
    unguessable value ``OAuthClient`` minted and is the only thing that
    resolves a waiting ``authorize()`` call, so a caller without it can do
    nothing here but read an error page.
    """
    app_state = _state(request)
    delivered = app_state.redirect.resolve(
        AuthorizationCallback(
            code=code, state=state, iss=iss, error=error, error_description=error_description
        )
    )
    if error is not None:
        body, status = f"Authorization failed: {error_description or error}", 400
    elif not delivered:
        # Unknown, expired or replayed `state`. Said plainly: the common cause
        # is a link opened long after the Run that produced it gave up.
        body, status = (
            "Nothing was waiting for this callback. The authorization may have expired. "
            "Start it again from the playground.",
            409,
        )
    else:
        body, status = "Authorized. You can close this tab and return to the playground.", 200
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8>"
        f"<body style='font:15px system-ui;padding:3rem;max-width:34rem'>{body}</body>",
        status_code=status,
    )


@app.post("/api/settings/mcp/{name}/test", response_model=McpTestResponse)
async def connect_mcp_server(
    name: str, request: Request, *, force: bool = False
) -> McpTestResponse:
    """Connect to a preset for real and report what it offers.

    Goes through the same ``McpPool`` the runtime uses, so this exercises the
    real path -- credential resolution, OAuth, protocol negotiation, the
    allow-list narrowing -- rather than a simplified probe that could pass
    where a Run would fail. A preset is only ever a starting point for
    ``POST /api/agents``, so nothing about testing it connects an agent to
    anything; the pooled connection is shared with any Run that later names
    the same server under the same credential, which is exactly the point.

    ``ok: false`` carries the real error text. The failure worth naming: an
    endpoint answering ``text/html`` is almost always a redirect page (an
    ``http://`` URL the server 301s to ``https://``), a login form, or a proxy
    block -- and a person cannot tell those from a bad credential unless the
    message says so.
    """
    state = _state(request)
    account = await _account(request)
    workspace = await state.settings_store.load(account.id)
    preset = next((p for p in workspace.mcp_servers if p.name == name), None)
    if preset is None:
        raise ApiProblem(404, f"no MCP preset {name!r}")

    server = _server_from_preset(preset, state.settings.oauth_callback_url)
    # Under the Scope this account's Runs will actually use, which the caller
    # no longer gets to name. The pool keys by (tenant, principal, url,
    # transport, credential), so connecting under any other Scope proves
    # nothing about the connection a Run would get -- and for an
    # authorization_code grant it would mean authorising twice, once here and
    # again inside the Run.
    # `force` drops the pooled connection and the stored token first, so this
    # reconnects on a new one rather than reporting the one already open. Not
    # the default: the ordinary question is what a Run would get, and for an
    # authorization_code grant a forced reconnect means signing in again.
    probe = await state.mcp.probe(scope_for(account), server, force=force)

    record = McpConnectionRecord(
        ok=probe.ok,
        detail=probe.detail,
        checked_at=datetime.now(UTC),
        tools=tuple(tool.name for tool in probe.tools),
        error_type=probe.error_type,
    )

    def remember(current: PlaygroundState) -> PlaygroundState:
        updated = [
            p.model_copy(update={"last_connection": record}) if p.name == name else p
            for p in current.mcp_servers
        ]
        return current.model_copy(update={"mcp_servers": updated})

    await state.settings_store.update(account.id, remember)

    return McpTestResponse(
        ok=probe.ok,
        detail=probe.detail,
        tool_count=len(probe.tools) if probe.ok else None,
        tools=[tool.name for tool in probe.tools] if probe.ok else None,
        error_type=probe.error_type,
    )


@app.post("/api/settings/mcp/{name}/disconnect", response_model=OkResponse)
async def disconnect_mcp_server(name: str, request: Request) -> OkResponse:
    """Close this account's connection to a preset and forget its token.

    The counterpart to connecting, and missing until now: a connection could
    be opened and never deliberately closed, so a token stayed usable for as
    long as the process lived. Signing out of a server somebody connected by
    mistake, or revoking a grant before walking away from a shared machine,
    had no answer short of restarting the backend.

    Scoped to the caller's own account, like every other route here: this
    closes the connection keyed to their tenant and cannot reach another
    account's, even for the same server URL (DESIGN.md §10.4).

    Disconnecting a server that was never connected succeeds. There is nothing
    to report -- the caller's intent is "not connected", and that is already
    true.
    """
    state = _state(request)
    account = await _account(request)
    workspace = await state.settings_store.load(account.id)
    preset = next((p for p in workspace.mcp_servers if p.name == name), None)
    if preset is None:
        raise ApiProblem(404, f"no MCP preset {name!r}")

    server = _server_from_preset(preset, state.settings.oauth_callback_url)
    await state.mcp.disconnect(scope_for(account), server)

    def forget(current: PlaygroundState) -> PlaygroundState:
        # The stored result goes too. Leaving it would show "connected, 351
        # tools" against a server this account just disconnected from.
        updated = [
            p.model_copy(update={"last_connection": None}) if p.name == name else p
            for p in current.mcp_servers
        ]
        return current.model_copy(update={"mcp_servers": updated})

    await state.settings_store.update(account.id, forget)
    return OkResponse()


@app.put("/api/settings/prices", response_model=SettingsResponse)
async def update_model_prices(body: UpdateModelPricesRequest, request: Request) -> SettingsResponse:
    """Replaces this account's model rates.

    Psych ships a broad, dated rate snapshot and records ``cost=None`` for anything
    else, which is deliberate: a silent zero makes metering look correct and be
    wrong (DESIGN.md §13.2). What was missing is the seam on the other side.
    ``PriceResolver`` is a port so a consumer can supply the rates they pay,
    and this console had nowhere to put them -- so an operator running a proxy
    that knows every rate still saw "cost unknown" on every Run.

    These override the shipped table for the models they name and leave every
    other model alone, so entering one price never makes a different model look
    free. They apply to the **next** Run: a cost is computed per model call and
    written into the Record so it cannot change retroactively when somebody
    edits a rate, which is what makes an old Run's number still mean what it
    meant at the time.
    """
    state = _state(request)
    account = await _account(request)

    seen: set[str] = set()
    for entry in body.prices:
        if entry.model in seen:
            raise ApiProblem(
                400,
                f"two rates are both for {entry.model!r}. A model resolves to one rate, so "
                "two entries means the one that wins is whichever happened to be stored last.",
            )
        seen.add(entry.model)

    def merge(current: PlaygroundState) -> PlaygroundState:
        return current.model_copy(
            update={
                "model_prices": [
                    ModelPriceEntry(
                        model=entry.model,
                        input=entry.input,
                        output=entry.output,
                        cache_read=entry.cache_read,
                        cache_write=entry.cache_write,
                        currency=entry.currency,
                    )
                    for entry in body.prices
                ]
            }
        )

    updated = await state.settings_store.update(account.id, merge)
    return _settings_response(updated, _live_connections(state, account))


@app.put("/api/settings/a2a", response_model=SettingsResponse)
async def update_a2a_peers(body: UpdateA2APeersRequest, request: Request) -> SettingsResponse:
    """Replaces this account's saved A2A peers.

    Presets, in the same sense ``PUT /api/settings/mcp`` means it: read only to
    fill in a ``POST /api/agents`` request. Attaching one copies its URL,
    credential name and grants into the published Spec, where they join the
    Version hash, so editing here changes what the next publish gets and never
    what an already-published agent calls.

    Nothing connects on save. A peer is reached when a Run actually calls it,
    and its skills are read from its Agent Card at that point rather than
    declared here (DESIGN.md §10.7).
    """
    state = _state(request)
    account = await _account(request)

    seen: set[str] = set()
    for peer in body.a2a_peers:
        name = peer.name.strip()
        if name == "":
            raise ApiProblem(400, "a peer needs a name")
        if name in seen:
            raise ApiProblem(
                400,
                f"two peers are both called {name!r}. A Spec's peers are keyed by name, so "
                "attaching both would publish one and silently drop the other.",
            )
        seen.add(name)
        if not peer.url.strip():
            raise ApiProblem(400, f"peer {name!r} needs a URL")

    def merge(current: PlaygroundState) -> PlaygroundState:
        return current.model_copy(
            update={
                "a2a_peers": [
                    A2APeerPreset(
                        name=peer.name.strip(),
                        url=peer.url.strip(),
                        description=peer.description,
                        credential=peer.credential or None,
                        scheme=peer.scheme or "Bearer",
                        tenant=peer.tenant or None,
                        allow=tuple(peer.allow),
                        optional=peer.optional,
                        extensions=tuple(peer.extensions),
                    )
                    for peer in body.a2a_peers
                ]
            }
        )

    updated = await state.settings_store.update(account.id, merge)
    return _settings_response(updated, _live_connections(state, account))


async def _mint_a2a_token(
    state: AppState, account: Account, secret_name: str, ttl_days: int
) -> A2ATokenResponse:
    """A long-lived session token, saved as one of this account's secrets.

    The A2A door authenticates with a console session token presented as a
    bearer token (``app.a2a.router._authenticate``), so a peer that wants to
    call this account's agents needs one that outlives a browser session. Only
    the hash is stored, as for every session; the value goes into the secrets
    store under ``secret_name`` so a peer preset can name it, and is returned
    once so a person can hand it to a deployment elsewhere.
    """
    token = new_session_token()
    now = datetime.now(UTC)
    session = Session(
        token_hash=hash_session_token(token),
        account_id=account.id,
        created_at=now,
        expires_at=now + timedelta(days=ttl_days),
    )

    def add(file: StateFile) -> StateFile:
        accounts = file.accounts.model_copy(update={"sessions": [*file.accounts.sessions, session]})
        workspace = file.workspace(account.id)
        workspace = workspace.model_copy(
            update={"secrets": {**workspace.secrets, secret_name: token}}
        )
        return file.model_copy(update={"accounts": accounts}).with_workspace(account.id, workspace)

    await state.settings_store.update_file(add)
    return A2ATokenResponse(
        secret_name=secret_name, token=token, expires_at=session.expires_at.isoformat()
    )


@app.post("/api/settings/a2a/tokens", status_code=201, response_model=A2ATokenResponse)
async def create_a2a_token(body: A2ATokenRequest, request: Request) -> A2ATokenResponse:
    state = _state(request)
    return await _mint_a2a_token(state, await _account(request), body.secret_name, body.ttl_days)


@app.post("/api/settings/a2a/peers/local", response_model=SettingsResponse)
async def add_local_peer(body: LocalPeerRequest, request: Request) -> SettingsResponse:
    """One of this account's agents, as a peer its other agents can call.

    The preset points at this process's own card URL for that agent and names
    a token secret, minted here if the account has none by that name. From
    there it is an ordinary peer: attach it to an agent and the Run fetches
    the card, offers the skills on it as tools, and sends ``message/send``
    over loopback, exactly as it would to a deployment across the internet.
    """
    state = _state(request)
    account = await _account(request)
    found = await state.index.get_agent(account.id, body.agent_id)
    if found is None:
        raise ApiProblem(404, f"no agent {body.agent_id!r}")
    pointer, _ = found
    workspace = await state.settings_store.load(account.id)
    if body.secret_name not in workspace.secrets:
        await _mint_a2a_token(state, account, body.secret_name, 365)
    name = (body.name or pointer.name).strip()
    if any(peer.name == name for peer in workspace.a2a_peers):
        raise ApiProblem(409, f"a peer called {name!r} already exists")
    preset = A2APeerPreset(
        name=name,
        url=f"{state.a2a_base_url}/a2a/v1/agents/{pointer.agent_id}/agent-card.json",
        description=body.description or f"This workspace's own {pointer.name!r} agent, over A2A.",
        credential=body.secret_name,
        scheme="Bearer",
        tenant=pointer.agent_id,
    )
    updated = await state.settings_store.update(
        account.id, lambda cur: cur.model_copy(update={"a2a_peers": [*cur.a2a_peers, preset]})
    )
    return _settings_response(updated, _live_connections(state, account), state.sandbox)


@app.put("/api/settings/skills", response_model=SettingsResponse)
async def update_skills(body: UpdateSkillsRequest, request: Request) -> SettingsResponse:
    """Replaces this account's skill library.

    A skill here is a procedure written once and attachable to any agent this
    account builds. Attaching one **copies** its three fields into the
    published Spec, so it joins the Version hash and this route can never
    change what an agent already published is told.

    That is the opposite of what ``PUT /api/settings/mcp`` does with a server
    description, and the difference is the point. A description is a fact about
    somebody else's system. A skill body is instructions the model follows, so
    letting it change under a published Version would make two Runs of one
    Version behave differently -- exactly what DESIGN.md §23.1 asks not to
    happen. Editing here changes what the *next* publish gets; an existing
    agent picks it up when somebody republishes it, under a new hash.
    """
    state = _state(request)
    account = await _account(request)

    seen: set[str] = set()
    for skill in body.skills:
        name = skill.name.strip()
        if name == "":
            raise ApiProblem(400, "a library skill needs a name")
        if name in seen:
            raise ApiProblem(
                400,
                f"two skills are both called {name!r}. A Spec's skills are keyed by name, so "
                "attaching both would publish one of them and silently drop the other.",
            )
        seen.add(name)

    def merge(current: PlaygroundState) -> PlaygroundState:
        return current.model_copy(
            update={
                "skills": [
                    SkillPreset(
                        name=skill.name.strip(),
                        description=skill.description,
                        body=skill.body,
                    )
                    for skill in body.skills
                ]
            }
        )

    updated = await state.settings_store.update(account.id, merge)
    return _settings_response(updated, _live_connections(state, account))


@app.put("/api/settings/runtime", response_model=SettingsResponse)
async def update_runtime_settings(body: RuntimeSettingsIn, request: Request) -> SettingsResponse:
    """Replaces how this account's Runs are executed. See ``RuntimeSettings``.

    Applies to the next Attempt of every Run, in-flight ones included when
    they are reclaimed, because each Attempt reads the workspace afresh
    (``app.runtime_router``). Nothing here is part of any Spec, so no agent's
    Version hash moves.
    """
    state = _state(request)
    account = await _account(request)
    for pattern in body.egress_allow:
        if not pattern.strip():
            raise ApiProblem(400, "an egress entry needs a hostname or a pattern")

    def merge(current: PlaygroundState) -> PlaygroundState:
        return current.model_copy(
            update={
                "runtime": RuntimeSettings(
                    cost_policy=body.cost_policy,
                    blob_offload_bytes=body.blob_offload_bytes,
                    catalogue_budget_chars=body.catalogue_budget_chars,
                    sandbox_enabled=body.sandbox_enabled,
                    sandbox_limits=SandboxLimitsEntry(**body.sandbox_limits.model_dump()),
                    egress_allow=tuple(p.strip().lower() for p in body.egress_allow),
                    denied_tools=tuple(t.strip() for t in body.denied_tools if t.strip()),
                )
            }
        )

    updated = await state.settings_store.update(account.id, merge)
    return _settings_response(updated, _live_connections(state, account), state.sandbox)


@app.put("/api/settings/secrets", response_model=SecretsResponse)
async def update_secrets(body: UpdateSecretsRequest, request: Request) -> SecretsResponse:
    """Merges into this account's stored secrets.

    Usable by the very next MCP connect with no restart and nothing to notify:
    ``app.secrets.AccountSecretResolver`` reads the settings store on every
    resolve, for the tenant asking, which is what ``SecretResolver``'s own
    "resolve fresh, never cache" contract wants anyway.

    Never returns a value, only names: see ``SecretsResponse``.
    """
    state = _state(request)
    account = await _account(request)

    def merge(current: PlaygroundState) -> PlaygroundState:
        merged = {**current.secrets, **body.secrets}
        return current.model_copy(update={"secrets": merged})

    updated = await state.settings_store.update(account.id, merge)
    return SecretsResponse(secrets=sorted(updated.secrets))


@app.delete("/api/settings/secrets/{name}", response_model=OkResponse)
async def delete_secret(name: str, request: Request) -> OkResponse:
    state = _state(request)
    account = await _account(request)

    def remove(current: PlaygroundState) -> PlaygroundState:
        if name not in current.secrets:
            return current
        remaining = {k: v for k, v in current.secrets.items() if k != name}
        return current.model_copy(update={"secrets": remaining})

    await state.settings_store.update(account.id, remove)
    return OkResponse()
