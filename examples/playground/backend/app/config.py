"""Environment configuration for the playground backend.

Every variable read here, and nothing else, decides how this process wires
Psych up. The README documents the same list for a human; this module is
what actually runs, so trust this over the prose if the two ever disagree.

A ``.env`` file next to ``examples/playground/backend/`` is loaded if present
(``python-dotenv``), so a person can keep credentials out of their shell
history without exporting them by hand every time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_FILE)

_DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / ".playground-state.json"
"""Where settings persist by default -- providers, MCP presets, secrets. Holds
real API keys, so it is gitignored (see the repo root ``.gitignore``) and
``PSYCH_PLAYGROUND_STATE_FILE`` exists to point it somewhere else entirely."""

_DEFAULT_MEMORY_FILE = Path(__file__).resolve().parent.parent / ".playground-memory.json"

_DEFAULT_INDEX_FILE = Path(__file__).resolve().parent.parent / ".playground-index.json"
"""Where the agents-published and runs-dispatched lists persist by default.
Separate from the settings file above because it holds no credentials and is
rewritten on a different rhythm -- every publish and dispatch, rather than
every settings change. Gitignored all the same: it is local bookkeeping, not
something to share. See ``app.store_index``."""

DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
"""Matches ``scripts/live-provider-check.py``'s own default shape: a small,
fast Workers AI model good enough to drive a tool-calling loop by hand."""

_CLOUDFLARE_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"


@dataclass(frozen=True)
class McpServerHint:
    """One entry of ``PSYCH_PLAYGROUND_MCP_SERVERS``.

    Purely informational: ``GET /api/config`` lists these so a person filling
    in ``POST /api/agents``'s ``mcp`` array has real values to start from.
    Nothing here connects to anything by itself -- a server is only ever
    reached because some agent's own published Spec names it in ``mcp``.
    """

    name: str
    url: str
    transport: str = "http"
    oauth_grant: str | None = None


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    postgres_dsn: str | None
    base_url: str
    api_key: str | None
    model: str
    secrets: dict[str, str]
    mcp_server_hints: tuple[McpServerHint, ...]
    default_approval_selectors: tuple[str, ...]
    """Applied to an agent created with no ``approval_selectors`` of its own.
    See ``app.runtime_router`` for why this is a per-agent choice at all
    rather than the single process-wide setting Psych's own ``Runtime``
    models it as."""
    state_file: Path
    """Where ``app.settings_store.SettingsStore`` persists providers, MCP
    presets and secrets. These env-derived values (``base_url``, ``api_key``,
    ``model``, ``secrets``, ``mcp_server_hints``) seed that file's first write,
    the one time it does not exist yet; every boot after that reads the file
    and these variables are no longer consulted for what it already holds."""
    oauth_callback_url: str
    """Where an ``authorization_code`` grant sends the browser back: this
    process's own ``GET /api/oauth/callback``, derived from ``host``/``port``.
    Supplied by the backend rather than typed into a preset, since the
    playground knows its own address and a redirect URI that disagrees with
    the server serving it is rejected mid-consent, the worst possible moment.
    Override with ``PSYCH_PLAYGROUND_OAUTH_CALLBACK_URL`` to match whatever is
    registered against your OAuth client."""
    index_file: Path
    """Where ``app.store_index.PlaygroundIndex`` persists the agents and runs
    ``GET /api/agents`` and ``GET /api/runs`` list. Reconciled against the
    ``Store`` at boot, so entries it can no longer resolve are dropped rather
    than listed as though they were still there."""
    memory_file: Path
    """Where `app.memory_store.FileMemoryStore` keeps durable facts. Its own
    file rather than the settings one: that holds credentials and is rewritten
    when somebody changes a setting, this holds what agents remembered and is
    rewritten whenever one does."""
    allowed_origins: tuple[str, ...]
    """Browser origins allowed to call this API with a session cookie.

    An explicit list rather than ``*``, and not a matter of taste: a session
    cookie needs ``Access-Control-Allow-Credentials``, and the CORS
    specification forbids pairing that with a wildcard origin. So the wildcard
    this backend used before accounts is no longer merely loose, it does not
    work. The defaults cover the console's own dev and preview ports; override
    with ``PSYCH_PLAYGROUND_ALLOWED_ORIGINS`` (comma-separated) to serve it
    from anywhere else."""
    otlp_endpoint: str | None
    """Where to send spans, or ``None`` to collect none.

    ``PSYCH_PLAYGROUND_OTLP_ENDPOINT``, an OTLP/HTTP base such as
    ``http://localhost:4318``. Unset means the no-op ``Telemetry`` Psych
    defaults to, which is what keeps ``docker run`` a single command: nobody
    should have to stand up a collector to look at a chat window.

    Spans exist either way. ``Runtime`` opens them through the ``Telemetry``
    port and the port's default discards them; this only decides whether
    anything is listening. See ``app.observability``."""
    otlp_service_name: str
    """What this process calls itself in a trace. ``service.name`` is the
    attribute every OTel backend groups by, and a trace UI showing
    ``unknown_service`` is a trace UI nobody can navigate."""
    log_level: str
    """The level Psych's own loggers are set to.

    Psych attaches a ``NullHandler`` and configures nothing else, which is what
    a library should do. That leaves the choice here, and unmade it means the
    lease, supervisor and notification lines the library exists to emit are
    never seen. ``PSYCH_PLAYGROUND_LOG_LEVEL``; ``INFO`` by default, because
    those lines are the ones an operator wants and there are few of them."""
    cookie_secure: bool
    """Whether the session cookie is marked ``Secure``.

    Off by default because the default deployment is ``http://127.0.0.1`` and a
    ``Secure`` cookie is silently dropped over plain HTTP, which presents as
    "sign-in succeeds and then I am signed out". Set
    ``PSYCH_PLAYGROUND_COOKIE_SECURE=1`` behind TLS, where it should be on."""


def _load_secrets() -> dict[str, str]:
    """``PSYCH_PLAYGROUND_SECRETS``: a JSON object of credential name to value,
    loaded into the ``InMemorySecretResolver`` at boot. Names here are what
    ``McpServer.credential``, ``McpOAuth.client_secret_credential`` and
    ``HttpTool.credential`` in a Spec refer to -- never a literal secret in
    the Spec itself (DESIGN.md §10.4)."""
    raw = os.environ.get("PSYCH_PLAYGROUND_SECRETS")
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("PSYCH_PLAYGROUND_SECRETS must be a JSON object of name -> value")
    return {str(key): str(value) for key, value in parsed.items()}


def _load_mcp_hints() -> tuple[McpServerHint, ...]:
    raw = os.environ.get("PSYCH_PLAYGROUND_MCP_SERVERS")
    if not raw:
        return ()
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise TypeError("PSYCH_PLAYGROUND_MCP_SERVERS must be a JSON array")
    hints = []
    for item in parsed:
        hints.append(
            McpServerHint(
                name=item["name"],
                url=item["url"],
                transport=item.get("transport", "http"),
                oauth_grant=item.get("oauth_grant"),
            )
        )
    return tuple(hints)


_DEFAULT_ORIGINS: Final = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3010",
    "http://127.0.0.1:3010",
)
"""Where the console runs during development.

Both spellings of loopback, because a browser treats ``localhost`` and
``127.0.0.1`` as different origins and somebody will type the other one.
"""


def load_allowed_origins() -> tuple[str, ...]:
    raw = os.environ.get("PSYCH_PLAYGROUND_ALLOWED_ORIGINS")
    if not raw:
        return _DEFAULT_ORIGINS
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


def load_settings() -> Settings:
    account_id = os.environ.get("PSYCH_PLAYGROUND_CF_ACCOUNT_ID")
    default_base = _CLOUDFLARE_BASE_URL.format(account_id=account_id) if account_id else ""
    # Empty when nothing is configured, and that is a state this backend runs
    # in rather than refuses to boot in.
    #
    # It used to raise here. Every document describing the first run --
    # `docker-compose.yml`'s own header, `examples/playground/README.md`, the
    # playground page on psychruntime.com -- promises that the console "treats
    # having no provider as a designed state rather than an error", and neither
    # the compose file nor the Dockerfile sets either variable. So the one
    # command those documents tell a newcomer to run exited on startup with
    # this message, and the entrypoint dutifully reported that the backend died
    # before it started listening.
    #
    # The rest of the process was already written for this: `active_provider()`
    # returns None, `GET /api/config` reports `provider_label=None`, and the
    # Settings page exists precisely so a provider can be added at run time.
    # These variables only ever seeded the state file's first write (see
    # `Settings.state_file`), so an absent one means "seed nothing", not "stop".
    base_url = os.environ.get("PSYCH_PLAYGROUND_BASE_URL", default_base)
    selectors_raw = os.environ.get("PSYCH_PLAYGROUND_DEFAULT_APPROVAL_SELECTORS", "@destructive")
    host = os.environ.get("PSYCH_PLAYGROUND_HOST", "127.0.0.1")
    # 8080 rather than the usual 8000: this repo's own scripts/dev-services.sh
    # puts DynamoDB Local on 8000, so anyone who followed the project's setup
    # instructions and then started this backend got "address already in use"
    # as their first experience of it.
    port = int(os.environ.get("PSYCH_PLAYGROUND_PORT", "8080"))
    return Settings(
        host=host,
        port=port,
        postgres_dsn=os.environ.get("PSYCH_PLAYGROUND_POSTGRES_DSN"),
        base_url=base_url,
        api_key=os.environ.get("PSYCH_PLAYGROUND_API_KEY"),
        model=os.environ.get("PSYCH_PLAYGROUND_MODEL", DEFAULT_MODEL),
        secrets=_load_secrets(),
        mcp_server_hints=_load_mcp_hints(),
        default_approval_selectors=tuple(s for s in selectors_raw.split(",") if s),
        state_file=Path(os.environ["PSYCH_PLAYGROUND_STATE_FILE"])
        if os.environ.get("PSYCH_PLAYGROUND_STATE_FILE")
        else _DEFAULT_STATE_FILE,
        oauth_callback_url=os.environ.get(
            "PSYCH_PLAYGROUND_OAUTH_CALLBACK_URL", f"http://{host}:{port}/api/oauth/callback"
        ),
        index_file=Path(os.environ["PSYCH_PLAYGROUND_INDEX_FILE"])
        if os.environ.get("PSYCH_PLAYGROUND_INDEX_FILE")
        else _DEFAULT_INDEX_FILE,
        memory_file=Path(os.environ["PSYCH_PLAYGROUND_MEMORY_FILE"])
        if os.environ.get("PSYCH_PLAYGROUND_MEMORY_FILE")
        else _DEFAULT_MEMORY_FILE,
        otlp_endpoint=os.environ.get("PSYCH_PLAYGROUND_OTLP_ENDPOINT") or None,
        otlp_service_name=os.environ.get("PSYCH_PLAYGROUND_SERVICE_NAME", "psych-playground"),
        log_level=os.environ.get("PSYCH_PLAYGROUND_LOG_LEVEL", "INFO").upper(),
        allowed_origins=load_allowed_origins(),
        cookie_secure=os.environ.get("PSYCH_PLAYGROUND_COOKIE_SECURE", "")
        not in ("", "0", "false"),
    )
