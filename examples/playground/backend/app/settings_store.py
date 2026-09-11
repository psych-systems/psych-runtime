"""Persisted playground settings, per account: providers, MCP presets, secrets.

``POST /api/agents`` and the rest of the original playground contract are
configured once, from environment variables, at boot (``app.config``). The
settings a real UI needs to offer instead have to survive a restart while
someone iterates on them by hand -- add a provider, fix a pasted API key,
register an MCP preset -- which means they need a home that outlives the
process. ``PlaygroundState`` is that home: one JSON file, loaded at boot and
rewritten on every settings change, guarded by one lock so two concurrent
writes read-modify-write rather than clobber each other.

## One file, one workspace per account

``PlaygroundState`` used to be the whole file, which meant one set of
providers, one set of API keys, one set of MCP presets and one set of secrets
shared by everyone who opened the console. On a machine one person runs by
hand that was merely untidy. The moment a second person can sign in it is a
credential leak: whoever loads the page next inherits the OAuth client secret
the last person pasted in.

So ``PlaygroundState`` is now the *per-account* workspace and ``StateFile``
holds one of them per account id, beside the accounts themselves
(``app.accounts.AccountsFile``). Still one file and still one lock, because
that is the property that makes a read-modify-write correct; the dimension
added is the account, not the storage.

An account that has never had a setting written has no entry, and
``workspace()`` returns an empty ``PlaygroundState`` rather than raising.
Absent and empty mean the same thing to every caller, and a new account should
not have to be initialised before it can be read.

## This file holds real credentials, in plaintext, on purpose

``ProviderConfig.api_key`` and ``PlaygroundState.secrets`` are exactly what a
person typed into the settings UI: an API key, an MCP client secret. Nothing
here encrypts or wraps them, and account isolation is not encryption: it stops
one signed-in person reading another's key through the API, and does nothing
about anyone who can read the file itself. That is acceptable for what this is
-- a tool someone runs on their own machine, the same trust model
``app.secrets`` documents -- and unacceptable for a shared deployment, which
is why the state file's path is gitignored and the README says so. Password
hashes are the exception and are never stored raw at all; see
``app.accounts``.

``app.main`` never serves these models directly: every route narrows to
``schemas.SettingsResponse`` first, which is what actually keeps a key from
reaching a response body (see ``tests/playground`` -- that is asserted, not
just claimed).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.accounts import AccountsFile

__all__ = [
    "A2APeerPreset",
    "McpConnectionRecord",
    "McpOAuthPreset",
    "McpServerPreset",
    "ModelPriceEntry",
    "PlaygroundState",
    "ProviderConfig",
    "SettingsStore",
    "SkillPreset",
    "StateFile",
    "new_provider_id",
]


class McpOAuthPreset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant: Literal["authorization_code", "client_credentials"] = "client_credentials"
    preregistered_client_id: str | None = None
    client_secret_credential: str | None = None
    """A credential *name*, resolved later through ``SecretResolver`` -- never
    a raw secret, matching ``McpOAuth.client_secret_credential`` in
    ``psych_runtime.core.spec`` (DESIGN.md §10.4)."""
    issuer: str | None = None
    cimd_url: str | None = None
    allow_dynamic_registration: bool = True
    application_type: Literal["native", "web"] = "native"
    client_name: str = "psych"
    # Field for field with `psych_runtime.McpOAuth` minus `redirect_uris`, which is
    # this process's own callback route rather than anything about the server
    # being described, and is supplied wherever a Spec or a connection is
    # actually built. A preset that carried fewer fields than the request
    # shape did meant `PUT /api/settings/mcp` failed with a validation error
    # naming fields a person never typed.


class McpConnectionRecord(BaseModel):
    """What the last connection attempt to one server found.

    Persisted beside the preset so a person who connects a server, then
    refreshes the page, still sees it connected and still sees its tools.
    Before this the result lived in one React component's state and was lost on
    a tab change, so a server with 351 tools looked exactly like one nobody had
    ever tried.

    The tool names, not just the count: an allow-list is written against them,
    and an agent builder that cannot show them makes a person guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    detail: str
    checked_at: datetime
    tools: tuple[str, ...] = ()
    error_type: str | None = None
    """The exception class when ``ok`` is false, so the UI can tell "cannot
    reach it" from "no credential for it" from "it answered with nonsense"
    without matching on message text."""


class McpServerPreset(BaseModel):
    """One agent-builder preset for an MCP server.

    Deliberately *not* ``psych_runtime.core.spec.McpServer``: a Spec carries its own
    copy of whatever an agent is built with, since MCP config is part of the
    Version hash (DESIGN.md §4) and a preset changing later must never mutate
    an already-published agent. This is only ever read to pre-fill a
    ``POST /api/agents`` request; nothing connects to it directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    description: str = ""
    """What this system is for, in a sentence or two, injected into the system
    prompt of every agent that reaches it.

    Stored here rather than on the published Spec on purpose: a description
    changes for reasons that have nothing to do with any agent, and a field on
    ``psych_runtime.McpServer`` would join the Version hash and republish every agent
    that names the server every time somebody improved the wording. Psych
    takes it at run time through ``McpTools(describe_server=...)``.

    Empty falls back to whatever the server says about itself at the MCP
    handshake, and then to nothing."""
    transport: Literal["http", "sse"] = "http"
    credential: str | None = None
    allow: tuple[str, ...] = ()
    optional: bool = False
    preload: bool | None = None
    """Whether this server's tool schemas go in the prompt, or are reached
    through discovery. ``None`` lets ``psych_runtime.tools.deferred`` decide from the
    catalogue size, which is the only honest default: the count is a fact
    about the server, learned at connect time. Stored per preset because the
    answer is per server -- the one this playground is developed against
    offers 351 tools, and preloading those costs 2.0 MB of schemas per turn."""
    oauth: McpOAuthPreset | None = None
    last_connection: McpConnectionRecord | None = None
    """The last connection attempt, or ``None`` for a preset nobody has tried.
    Written by ``POST /api/settings/mcp/{name}/test``; read by the settings
    page and the agent builder."""


class A2APeerPreset(BaseModel):
    """One agent-builder preset for an A2A peer.

    ``McpServerPreset``'s sibling and deliberately not ``psych_runtime.A2APeer``, for
    the same reason: a Spec carries its own copy of whatever an agent is built
    with, since peers are part of the Version hash, and a preset changing later
    must never mutate an already-published agent. Only ever read to pre-fill a
    ``POST /api/agents`` request; nothing connects to it directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    url: str
    """The peer's A2A base URL, or its Agent Card URL. `psych_runtime.tools.a2a`
    resolves a base URL to the well-known card path itself."""
    description: str = ""
    """What this peer is for, in a person's words. Local to this console: the
    model learns what a peer does from its Agent Card, not from here."""
    credential: str | None = None
    """A credential *name*, resolved through ``SecretResolver`` at call time.
    Never a secret value."""
    scheme: str = "Bearer"
    tenant: str | None = None
    allow: tuple[str, ...] = ()
    optional: bool = False
    extensions: tuple[str, ...] = ()


class ProviderConfig(BaseModel):
    """One configured OpenAI-compatible model backend, persisted with its key.

    ``id`` is stable across edits: ``active_provider_id`` and a frontend's own
    remembered selection both refer to it, and neither should break because a
    label got typo-fixed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    base_url: str
    model: str
    api_key: str | None = None


class ModelPriceEntry(BaseModel):
    """What one model costs, in this account's own words.

    ``psych_runtime.model.pricing.DEFAULT_PRICES`` ships a broad, dated snapshot
    and is documented as incomplete and able to go stale, because provider
    rates change. A model it has never heard of records ``cost=None`` rather than a
    zero, which is deliberate: DESIGN.md §13.2 calls a silent zero the failure
    that makes metering look correct and be wrong.

    ``PriceResolver`` is a port precisely so a consumer can supply the rates
    they actually pay, and until now this console offered nowhere to put them.
    A person running a proxy in front of a dozen models -- which knows every
    rate, while Psych does not -- had no way to say so, and every Run reported
    an unknown cost.

    Rates are **per million tokens**, matching ``ModelPrice`` field for field
    so the mapping is a rename of nothing. Cached reads and writes are priced
    separately from input because providers charge for them separately, and
    folding them together is how a cache-heavy workload gets billed wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    """The id as the provider names it. Matched exactly first, then by longest
    prefix, so ``gpt-4o`` covers ``gpt-4o-2024-08-06`` without a second entry
    (see ``StaticPriceTable``)."""
    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    cache_read: Decimal = Field(default=Decimal(0), ge=0)
    cache_write: Decimal = Field(default=Decimal(0), ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class SkillPreset(BaseModel):
    """One skill in an account's library (DESIGN.md §16).

    ``McpServerPreset``'s sibling, and deliberately *not*
    ``psych_runtime.core.spec.Skill``: this is only ever read to fill in a
    ``POST /api/agents`` request. Attaching it copies the three fields into the
    published Spec, where they join the Version hash.

    ## Why the library is copied rather than read at run time

    An MCP server's description is injected at turn time instead
    (``McpTools(describe_server=...)``), and the two cases look alike
    enough to be worth separating. A description is a fact about an external
    system: it changes for reasons that have nothing to do with any agent, and
    injecting it late is right precisely because the agent did not change.

    A skill body is *instructions the model follows*, much closer to
    ``AgentSpec.instructions``. If editing this could change what an already
    published agent is told, two Runs of one Version would behave differently,
    and DESIGN.md §23.1 asks for the exact opposite. So "global" here means
    "available to every agent you build", never "reaching into every agent you
    have built". Editing a library skill changes what the next publish gets;
    an existing agent picks it up when somebody republishes it, under a new
    hash. The console says so on the page, because the word invites the other
    reading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    """One line, in the system prompt of every agent that attaches this."""
    body: str
    """The procedure, loaded on demand by ``load_skill``."""


class SandboxLimitsEntry(BaseModel):
    """``psych_runtime.SandboxLimits`` as this account has set them. The defaults
    are the subprocess adapter's own, which its module documents one by one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: float = 10.0
    address_space_bytes: int = 512 * 1024 * 1024
    file_size_bytes: int = 10 * 1024 * 1024
    process_count: int = 64
    wall_seconds: float = 30.0


class RuntimeSettings(BaseModel):
    """The ``Runtime`` knobs Psych leaves to the consumer, per account.

    Every field here is a constructor argument of ``psych_runtime.Runtime`` or
    of a port it takes, and none of them is part of a Spec: they describe how
    *this deployment* runs an agent, not what the agent is, so changing one
    changes the next Attempt of every agent and no Version hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_policy: Literal["prefer_provider", "computed", "provider_only"] = "prefer_provider"
    """Whose number a Run's cost is: the provider's when it reports one,
    Psych's own arithmetic, or the provider's only (unknown otherwise)."""
    blob_offload_bytes: int = 300_000
    """A tool result larger than this is stored as a blob rather than in the
    log record. Independent of ``Limits.large_result_bytes``, which decides
    what the *model* sees and is the Spec's to set."""
    catalogue_budget_chars: int = 20_000
    """How much of the prompt one MCP server's catalogue may take before its
    tools are disclosed on demand instead of listed."""
    sandbox_enabled: bool = True
    """Whether agents are offered ``run_code``. Off means no sandbox is wired,
    which is the library's own default."""
    sandbox_limits: SandboxLimitsEntry = Field(default_factory=SandboxLimitsEntry)
    egress_allow: tuple[str, ...] = ()
    """Hostnames, or ``*.example.com`` patterns, this account's Runs may reach.
    Empty allows everything. Applies to every outbound call Psych makes for
    this account: the model, MCP, HTTP tools and A2A peers alike."""
    denied_tools: tuple[str, ...] = ()
    """Tool names the ``Policy`` port refuses for this account, whatever any
    Spec grants. The refusal reaches the model as the tool's result."""


class PlaygroundState(BaseModel):
    """One account's workspace: everything ``GET /api/settings`` answers from."""

    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfig] = Field(default_factory=list)
    mcp_servers: list[McpServerPreset] = Field(default_factory=list)
    secrets: dict[str, str] = Field(default_factory=dict)
    active_provider_id: str | None = None
    model_prices: list[ModelPriceEntry] = Field(default_factory=list)
    """Rates this account supplies for models Psych's shipped table does not
    know. Empty leaves every Run reporting the honest unknown."""
    a2a_peers: list[A2APeerPreset] = Field(default_factory=list)
    """Other agents this account can point an agent at, over A2A. Copied into a
    Spec at publish; see ``A2APeerPreset``."""
    skills: list[SkillPreset] = Field(default_factory=list)
    """Skills written once and attachable to any agent this account builds.
    Copied into a Spec at publish; see ``SkillPreset``."""
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    """How this account's Runs are executed. See ``RuntimeSettings``."""


class StateFile(BaseModel):
    """The whole file: who can sign in, and what each of them has configured.

    ``accounts`` and ``workspaces`` are kept as separate fields rather than
    nesting a workspace inside each ``Account`` so that the model carrying
    password hashes stays small enough to audit on one screen, and so a
    workspace can be read without loading a credential that has nothing to do
    with it.

    ``legacy`` is the one-time upgrade path. A state file written before
    accounts existed had providers, MCP presets and secrets at the top level,
    with no owner. Discarding it would silently delete the provider key
    somebody had already configured, so it is parked here and adopted by the
    first account created (see ``SettingsStore.adopt_legacy``). After that it
    is cleared and never consulted again.
    """

    model_config = ConfigDict(extra="forbid")

    accounts: AccountsFile = Field(default_factory=AccountsFile)
    workspaces: dict[str, PlaygroundState] = Field(default_factory=dict)
    legacy: PlaygroundState | None = None

    def workspace(self, account_id: str) -> PlaygroundState:
        """This account's workspace, empty if it has never written one.

        Absent and empty mean the same thing to every caller, so a new account
        is readable before it is initialised.
        """
        return self.workspaces.get(account_id) or PlaygroundState()

    def with_workspace(self, account_id: str, state: PlaygroundState) -> StateFile:
        return self.model_copy(update={"workspaces": {**self.workspaces, account_id: state}})


def _read_state_file(raw: str) -> StateFile:
    """Parse the file, upgrading a pre-accounts one rather than failing on it.

    The old shape is a bare ``PlaygroundState`` at the top level. It is
    recognised by the absence of an ``accounts`` key rather than by trying the
    new model first and catching the error, because ``StateFile`` would happily
    validate ``{}`` and quietly throw away a populated old file.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("the playground state file must contain a JSON object")
    if "accounts" in parsed or "workspaces" in parsed:
        return StateFile.model_validate(parsed)
    return StateFile(legacy=PlaygroundState.model_validate(parsed))


def new_provider_id() -> str:
    """A short, unguessable id for a provider created with none of its own."""
    return uuid.uuid4().hex[:8]


class SettingsStore:
    """File-backed home for one ``StateFile``. One file, one process.

    Guarded by a single ``asyncio.Lock`` -- the same shape ``PlaygroundIndex``
    (``app.store_index``) already uses for its own in-memory bookkeeping, here
    protecting a file instead. A write goes to a sibling ``<path>.tmp`` and is
    atomically renamed into place, so a crash mid-write never leaves a
    truncated JSON file for the next boot to fail on.

    Every account-scoped method takes the account id explicitly. There is no
    "current account" on this object on purpose: a store that remembered whose
    settings it last read is one ``await`` away from answering the wrong
    person, and that is precisely the bug this whole change exists to remove.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _read(self) -> StateFile:
        if not self._path.exists():
            return StateFile()
        return _read_state_file(self._path.read_text())

    def _write(self, state: StateFile) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(self._path)

    async def load_file(self) -> StateFile:
        """The whole file. For the boot path and the auth routes; a route
        serving one account's settings wants ``load`` instead."""
        async with self._lock:
            return self._read()

    async def load(self, account_id: str) -> PlaygroundState:
        async with self._lock:
            return self._read().workspace(account_id)

    async def update_file(self, fn: Callable[[StateFile], StateFile]) -> StateFile:
        """Read, transform with ``fn``, and write back, atomically with
        respect to every other read or update on this store.

        ``fn`` may raise to refuse the change -- the file is left untouched
        when it does, since nothing is written until ``fn`` returns.
        """
        async with self._lock:
            current = self._read()
            updated = fn(current)
            self._write(updated)
            return updated

    async def update(
        self, account_id: str, fn: Callable[[PlaygroundState], PlaygroundState]
    ) -> PlaygroundState:
        """Transform one account's workspace, leaving every other untouched."""
        updated = await self.update_file(
            lambda file: file.with_workspace(account_id, fn(file.workspace(account_id)))
        )
        return updated.workspace(account_id)

    async def adopt_legacy(self, account_id: str) -> None:
        """Hand a pre-accounts workspace to its new owner, once.

        Called when an account is created and the file still carries a
        ``legacy`` workspace. The first account to exist gets it, which is the
        only defensible answer: on a machine where one person had already
        configured a provider and a connection, that person is the one signing
        up. A no-op when there is nothing to adopt or the account already has
        a workspace of its own, so it is safe to call unconditionally.
        """

        def adopt(file: StateFile) -> StateFile:
            if file.legacy is None or account_id in file.workspaces:
                return file
            return file.model_copy(
                update={
                    "workspaces": {**file.workspaces, account_id: file.legacy},
                    "legacy": None,
                }
            )

        await self.update_file(adopt)
