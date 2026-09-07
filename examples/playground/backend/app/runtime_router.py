"""Builds each Run a ``Runtime`` carrying its own account's model and approvals.

``psych_runtime.runtime.execute.Runtime`` is explicit that ``approval_selectors`` is a
process-wide policy: "Assembled once at boot by the consumer and handed to
every Worker in the process. Holds no per-Run state." That is the right
default for a real deployment, where one host enforces one approval policy for
every agent it runs.

This playground asks for two things that default cannot give:

- ``approval_selectors`` per agent, from ``POST /api/agents``. Widening Psych's
  ``Runtime`` to take a per-Spec override would put an approval policy inside a
  Spec's Version hash, a real design change this example has no business making
  unilaterally.
- A **model client per account**. Each account configures its own provider, its
  own base URL and its own API key, and a Run must reach the provider its owner
  configured. One process-wide client would bill one person's key for another
  person's Run, which is worse than a wrong answer.

So a ``Runtime`` is assembled per Attempt from the Run's own ``RunHeader``.

## Per Attempt, not cached, and that is the point

An earlier version cached one ``Runtime`` per selector tuple and mutated its
``model`` field in place when the active provider changed. With one global
provider that was safe. With a provider per account it is a race with a
credential in it: two Attempts for two accounts sharing one ``Runtime`` object
would each set ``model`` for the other, and the loser sends its prompt to the
other tenant's endpoint with the other tenant's key.

Building fresh removes the question rather than answering it carefully.
``Runtime`` is a dataclass whose ``__post_init__`` constructs a ``ToolResolver``
and a ``ToolExecutor`` and touches no IO; ``OpenAICompatibleClient`` is five
field assignments over the ``HttpTransport`` this router already shares. Both
are free beside an Attempt, which is a whole agent loop of network calls.

It also deletes the invalidation problem: a provider edited in Settings takes
effect on the next Attempt because the next Attempt reads it, with nothing to
notify and nothing to expire.

## The egress scope is now the Run's own

The model client is constructed with the Run's ``Scope`` rather than a shared
placeholder, so a consumer-supplied egress policy sees which tenant each
outbound model call belongs to. That is the property DESIGN.md §14 wants and
one this backend could not previously offer for the model path.
"""

from __future__ import annotations

from app.ports import AccountToolPolicy, SandboxProvision
from app.settings_store import PlaygroundState, ProviderConfig, SettingsStore
from app.store_index import PlaygroundIndex
from app.tools import current_scope
from psych_runtime.core.ids import VersionHash
from psych_runtime.core.scope import Scope
from psych_runtime.memory.port import MemoryStore
from psych_runtime.model.egress import HttpTransport
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import ModelClient
from psych_runtime.model.pricing import DEFAULT_PRICES, ModelPrice, StaticPriceTable
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.blob import BlobStore
from psych_runtime.store.port import RunHeader, Store
from psych_runtime.telemetry.port import Telemetry
from psych_runtime.tools.a2a import A2ATools
from psych_runtime.tools.http import HttpToolExecutor
from psych_runtime.tools.mcp import McpTools
from psych_runtime.tools.registry import ToolRegistry


def prices_for(state: PlaygroundState) -> StaticPriceTable:
    """The shipped table, amended with whatever this account has entered.

    `DEFAULT_PRICES` is documented as incomplete and known to go stale, and a
    model it has never heard of records `cost=None` rather than a zero
    (DESIGN.md §13.2). That default is right and stays: an unknown rate must
    say so loudly rather than be guessed at.

    What was missing is the other half. `PriceResolver` is a port so a consumer
    can supply the rates they actually pay, and this console never offered
    anywhere to put them, so somebody running a proxy that knows every rate
    still saw "cost unknown" on every Run. These entries override the shipped
    table for the models they name and leave every other model alone, so
    configuring one price does not silently make another model look free.
    """
    if not state.model_prices:
        return DEFAULT_PRICES
    return DEFAULT_PRICES.with_overrides(
        {
            entry.model: ModelPrice(
                input=entry.input,
                output=entry.output,
                cache_read=entry.cache_read,
                cache_write=entry.cache_write,
                currency=entry.currency,
            )
            for entry in state.model_prices
        }
    )


def active_provider(state: PlaygroundState) -> ProviderConfig | None:
    """The account's active provider, or ``None``.

    ``None`` when nothing is configured and when ``active_provider_id`` names a
    provider that has since been removed. Never picks a replacement on its own:
    an account that deleted its active provider gets an explicit "nothing is
    active" rather than a silent switch to whichever one happens to be first.
    """
    if state.active_provider_id is None:
        return None
    return next((p for p in state.providers if p.id == state.active_provider_id), None)


class AccountRoutedRuntime:
    """The ``AttemptRunner`` handed to the playground's ``Worker``.

    Structurally satisfies ``psych_runtime.runtime.worker.AttemptRunner`` -- a callable
    taking ``(Journal, RunHeader, AbortSignal)`` -- the same shape a bare
    ``Runtime`` instance already satisfies via ``Runtime.__call__``.

    A consumer with one approval policy and one model endpoint for every agent
    they run needs none of this and should construct one ``Runtime`` directly,
    the way ``docs/api.md`` does.
    """

    def __init__(
        self,
        *,
        store: Store,
        registry: ToolRegistry,
        mcp: McpTools,
        index: PlaygroundIndex,
        settings: SettingsStore,
        transport: HttpTransport,
        memory: MemoryStore,
        fallback_base_url: str,
        fallback_api_key: str | None,
        default_approval_selectors: tuple[str, ...],
        telemetry: Telemetry,
        http: HttpToolExecutor,
        blob: BlobStore,
        a2a: A2ATools,
        sandbox: SandboxProvision,
        policy: AccountToolPolicy,
    ) -> None:
        self._store = store
        self._registry = registry
        self._mcp = mcp
        self._http = http
        self._blob = blob
        self._a2a = a2a
        self._sandbox = sandbox
        self._policy = policy
        self._index = index
        self._settings = settings
        self._transport = transport
        self._memory = memory
        self._fallback_base_url = fallback_base_url
        self._fallback_api_key = fallback_api_key
        self._default_selectors = default_approval_selectors
        self._telemetry = telemetry
        """Where this process's spans go. Built once at boot by
        ``app.observability`` and handed to every ``Runtime``: a Run's spans
        are the same spans whichever account dispatched it, and building one
        per Run would create a tracer per Run for nothing."""

    def _client_for(self, scope: Scope, state: PlaygroundState) -> ModelClient:
        """The model client this account's Runs should use.

        Falls back to the process's boot configuration when the account has no
        active provider, so a fresh account can send its first message against
        whatever the operator started this backend with, and the console's
        Settings page is an improvement rather than a prerequisite.
        """
        provider = active_provider(state)
        return OpenAICompatibleClient(
            base_url=provider.base_url if provider is not None else self._fallback_base_url,
            transport=self._transport,
            scope=scope,
            api_key=provider.api_key if provider is not None else self._fallback_api_key,
        )

    async def model_for(self, scope: Scope) -> ModelClient:
        """As `_client_for`, reading the workspace itself. For callers outside
        an Attempt, such as the provider-test route."""
        return self._client_for(scope, await self._settings.load(scope.tenant))

    async def _selectors_for(self, header: RunHeader) -> tuple[str, ...]:
        # From the Run, not from the agent: a Version hash is content, so
        # republishing one Spec with different selectors overwrote the agent
        # entry and silently changed the policy of every Run of that Version,
        # in-flight ones included, while deleting the agent dropped them to the
        # process default. The Run's own entry records what it was admitted
        # under.
        run = await self._index.get_run(header.run_id)
        if run is not None and run.approval_selectors:
            return run.approval_selectors
        agent = await self._index.any_version(VersionHash(header.version_hash))
        return agent.approval_selectors if agent is not None else self._default_selectors

    async def __call__(self, journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
        # One read of the workspace for both the client and the rates. They are
        # two halves of the same question -- which provider is this account
        # using, and what does it charge -- and reading twice would let an edit
        # between the two produce a Run billed at one model's rate against
        # another model's endpoint.
        state = await self._settings.load(header.scope.tenant)
        runtime = Runtime(
            store=self._store,
            model=self._client_for(header.scope, state),
            registry=self._registry,
            mcp=self._mcp,
            memory=self._memory,
            telemetry=self._telemetry,
            # Not set here on purpose. `Runtime.end_user_id` is process-wide, so
            # setting it would point every account at one bucket of facts. Whose
            # memories a Run reads comes from that Run's own input, which
            # `app.main` fills in at dispatch -- see `app.memory_store` for why
            # that is passed explicitly rather than left to fall through to
            # `Scope.principal`.
            prices=prices_for(state),
            approval_selectors=await self._selectors_for(header),
            # The rest of what `Runtime` takes, each one an account's own
            # choice read off the same workspace snapshot (`RuntimeSettings`).
            # `http`, `blob` and `a2a` are process-wide objects whose
            # per-account behaviour comes from the Scope each call carries;
            # the sandbox is rebuilt per Attempt because its limits are the
            # account's and the adapter takes them at construction.
            cost_policy=state.runtime.cost_policy,
            blob=self._blob,
            blob_offload_bytes=state.runtime.blob_offload_bytes,
            catalogue_budget_chars=state.runtime.catalogue_budget_chars,
            http=self._http,
            a2a=self._a2a,
            sandbox=self._sandbox.build(state.runtime),
            policy=self._policy,
        )
        # Visible to every tool this Attempt calls, on this task and its
        # children only. See `app.tools` for what reads it and why a context
        # variable rather than a tool argument.
        token = current_scope.set(header.scope)
        try:
            await runtime(journal, header, abort)
        finally:
            current_scope.reset(token)
