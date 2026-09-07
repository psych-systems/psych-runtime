# Psych playground backend

A FastAPI app you run locally to exercise Psych by hand: publish an agent,
dispatch a Run against it, watch it stream, resume it past an approval,
interrupt it, and read back its report. This is the "platform" DESIGN.md §1
says Psych deliberately does not ship -- an HTTP server, a couple of small
in-memory indexes, and the handful of wiring decisions (which model backend,
which secrets, how approvals are gated) every real consumer makes for
themselves. None of it lives in `psych_runtime`; that is the entire point of this
being an example.

The frontend at `examples/playground/web/` talks to this over the REST
contract below. You can also drive it with `curl` -- every example in this
file is a real, copy-pasteable command.

## Accounts, and why they are here

Every route except five needs a session. Sign up, sign in, and everything you
publish, connect, configure or say is yours: another account on the same
backend cannot list it, cannot open it by naming its id, and cannot use your
provider key or your MCP credential.

That is not a feature of Psych, and it could not be. DESIGN.md §1 refuses
"authentication, identity, users, sessions-as-login" and "organizations, teams,
roles or permissions" by name. What Psych provides is `Scope(tenant,
principal)`, threaded through every entry point, stamped on every Record and
filtering every store query while knowing nothing about who anyone is. Turning
a person into a `Scope` is the consumer's job. `app/accounts.py` and
`app/auth.py` are this example doing that job, and they are the shortest
answer to "how do I put my own users in front of Psych".

Before this, `POST /api/runs` built its `Scope` from a `tenant` field in the
request body, so any caller could name any tenant and read anybody's Runs, and
one state file held one set of provider keys and MCP credentials for everyone
who opened the page. Both are fixed; the field is gone rather than validated.

`curl` examples below therefore need a cookie jar:

```sh
curl -sc jar.txt -X POST http://127.0.0.1:8080/api/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "password": "a long enough passphrase"}'

curl -sb jar.txt http://127.0.0.1:8080/api/config
```

**Same host on both sides.** The session cookie is `SameSite=Lax`, and a
browser treats `localhost` and `127.0.0.1` as different *sites*. Open the
console on whichever one the backend is on, or the cookie is never attached and
you get signed out immediately after signing in. The console corrects for this
when both are loopback; `PSYCH_PLAYGROUND_ALLOWED_ORIGINS` is what to set when
either is not.

What is deliberately absent: password reset, email verification, OAuth
sign-in, roles, invitations and shared workspaces. Each is real work with real
security corners, and a console that half-implements them is worse than one
that plainly does not have them.

## Run it

```sh
cd /path/to/psych
uv sync --extra playground          # fastapi, uvicorn, python-dotenv, asyncpg
export PSYCH_PLAYGROUND_CF_ACCOUNT_ID=<your Cloudflare account id>
export PSYCH_PLAYGROUND_API_KEY=<a Workers AI API token>
uv run python examples/playground/backend/run.py
```

That starts the server at `http://127.0.0.1:8080` against Cloudflare Workers
AI's OpenAI-compatible endpoint, using `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
by default. Point `GET /api/config` at it to sanity-check what it came up:

```sh
curl -s http://127.0.0.1:8080/api/config | python3 -m json.tool
```

You can put the same variables in a `.env` file next to this README instead
of exporting them (loaded automatically via `python-dotenv`); see
`.env.example`.

### Using a different OpenAI-compatible backend

Set `PSYCH_PLAYGROUND_BASE_URL` directly and skip the Cloudflare-specific
variable -- this points at any OpenAI-compatible `/chat/completions` and
`/models` endpoint, for example a local compatible gateway:

```sh
export PSYCH_PLAYGROUND_BASE_URL=http://127.0.0.1:4000/v1
export PSYCH_PLAYGROUND_API_KEY=sk-...          # omit if the proxy needs none
export PSYCH_PLAYGROUND_MODEL=gpt-4o-mini
uv run python examples/playground/backend/run.py
```

### Using Postgres instead of the in-memory store

```sh
scripts/dev-services.sh start       # from the repo root, if not already up
export PSYCH_PLAYGROUND_POSTGRES_DSN=postgresql://postgres@127.0.0.1:5432/psych_playground
uv run python examples/playground/backend/run.py
```

Migrations run automatically at startup (`PostgresStore.migrate()`), once,
which is safe to do here because a human starting a local playground process
*is* the moment a consumer decides their schema changes -- DESIGN.md §22 only
forbids running migrations implicitly on every `connect()` inside a library
that does not know when that moment is.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `PSYCH_PLAYGROUND_HOST` | `127.0.0.1` | Bind address. |
| `PSYCH_PLAYGROUND_PORT` | `8080` | Bind port. |
| `PSYCH_PLAYGROUND_POSTGRES_DSN` | unset | Set to use `PostgresStore` instead of `MemoryStore`. Migrated automatically at startup. |
| `PSYCH_PLAYGROUND_CF_ACCOUNT_ID` | unset | Your Cloudflare account id. Used only to build the default `PSYCH_PLAYGROUND_BASE_URL` for Workers AI; ignored if that variable is set explicitly. |
| `PSYCH_PLAYGROUND_BASE_URL` | derived from the account id above | The OpenAI-compatible API root. Required, one way or the other. |
| `PSYCH_PLAYGROUND_API_KEY` | unset | Sent as `Authorization: Bearer <key>`. A Cloudflare API token with Workers AI access, or whatever your proxy wants. Some proxies authenticate at the network layer and need none. |
| `PSYCH_PLAYGROUND_MODEL` | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | The default model id offered when creating an agent -- an agent can still name any model its `model` field asks for, since `ModelRef.model` is per-Spec, not fixed by this process. |
| `PSYCH_PLAYGROUND_SECRETS` | `{}` | A JSON object of credential name to value, e.g. `{"crm_client_secret": "abc123"}`. Loaded into a `SecretResolver` at boot; these are the names an `McpServer.credential`, `McpOAuth.client_secret_credential` or an `HttpTool.credential` in a Spec refers to. See "MCP behind client-credentials OAuth" below. |
| `PSYCH_PLAYGROUND_MCP_SERVERS` | `[]` | A JSON array of `{"name", "url", "transport", "oauth_grant"}`, informational only -- listed by `GET /api/config` so a person filling in `POST /api/agents`'s `mcp` array has real values to start from. Nothing connects until an agent actually names a server. |
| `PSYCH_PLAYGROUND_DEFAULT_APPROVAL_SELECTORS` | `@destructive` | Comma-separated. Applied to an agent created with no `approval_selectors` of its own. See "Approvals are per-agent, sort of" below. |
| `PSYCH_PLAYGROUND_ALLOWED_ORIGINS` | the console's dev ports on both `localhost` and `127.0.0.1` | Comma-separated browser origins allowed to call this API with a session cookie. Explicit rather than `*` because the CORS specification forbids `Access-Control-Allow-Credentials` alongside a wildcard origin, so the cookie would simply never be sent. |
| `PSYCH_PLAYGROUND_COOKIE_SECURE` | off | Marks the session cookie `Secure`. Off by default because the default deployment is plain HTTP on loopback, where a `Secure` cookie is silently dropped and sign-in presents as instantly signing out. Set it behind TLS. |
| `PSYCH_PLAYGROUND_STATE_FILE` | `.playground-state.json` next to this README | Where settings changed through `/api/settings/*` persist -- providers (with their API keys), MCP presets, and secrets. Holds real credentials in plaintext; the default path is gitignored. See "Settings persist to a JSON file" below. |
| `PSYCH_PLAYGROUND_INDEX_FILE` | `.playground-index.json` next to this README | Where the agents, their Version histories and the runs-dispatched list persist, so `GET /api/agents` and `GET /api/runs` survive a restart. No credentials in it. Reconciled against the `Store` at boot; see the last section. |

## MCP behind client-credentials OAuth

1. Put the confidential client's secret in `PSYCH_PLAYGROUND_SECRETS`, under
   whatever name you like:

   ```sh
   export PSYCH_PLAYGROUND_SECRETS='{"crm_client_secret": "s3cr3t-value"}'
   ```

2. Reference that name, not the value, when creating an agent:

   ```sh
   curl -s -X POST http://127.0.0.1:8080/api/agents \
     -H 'Content-Type: application/json' \
     -d '{
       "name": "crm-agent",
       "instructions": "Help with CRM lookups.",
       "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
       "tools": [],
       "mcp": [{
         "name": "crm",
         "url": "https://crm.example.com/mcp",
         "allow": [],
         "optional": false,
         "oauth": {
           "grant": "client_credentials",
           "preregistered_client_id": "psych-playground",
           "client_secret_credential": "crm_client_secret"
         }
       }]
     }'
   ```

The secret itself never appears in the Spec or in the published Version --
only `crm_client_secret`, the name, does (DESIGN.md §10.4). The backend
resolves it through `app.secrets.AccountSecretResolver` at connect time, and
the connection pool (`psych_runtime.tools.mcp.McpPool`) is keyed by the *resolved*
credential's identity, never by the name or the server URL alone, so this
never risks pooling one tenant's token onto another tenant's call.

`AccountSecretResolver` answers per account: `SecretResolver.resolve` takes the
`Scope`, so the same credential *name* resolves to your value for you and to
somebody else's for them. An earlier version answered the same name for every
Scope, which handed one account's connection another account's OAuth secret
while the pool key saw no difference -- it really was the same credential as
far as it could tell. Resolving by name alone is the "pool by URL" mistake
DESIGN.md warns about, one layer down.

`authorization_code` is not supported here: it needs a redirect listener
Psych does not run (DESIGN.md §1), and wiring one up is out of scope for a
local playground. Use `client_credentials`, the default.

## Tools available to every agent

Three code tools are registered at boot (`app/tools.py`) and any agent can
name them in its `tools` list:

- `lookup_order(order_id)` -- read-only, always succeeds.
- `issue_refund(order_id, cents)` -- annotated `destructive`. Point an
  agent's `approval_selectors` at `@destructive` (the default) to see a Run
  suspend for a human decision before this runs.
- `check_inventory(sku)` -- always raises, to demonstrate the failure-streak
  guard and the failure-guidance text a model actually sees when a tool is
  down.

Two more show what a tool actually is -- a plain Python function, nothing
Psych-specific about its body -- by using that to reach back into this
backend:

- `create_workflow(name, steps, description?)` -- publishes a `WorkflowSpec`
  out of tool steps the model composes, so "set up a check for order A1"
  becomes something a person can find under Workflows and run again. Reads
  the calling account off a context variable set per Attempt
  (`app.runtime_router`, `app.tools.current_scope`), because a tool is handed
  its arguments and nothing else and this one still needs to know whose
  workspace it is publishing into.
- `list_workflows()` -- the workspace's workflows, so the model can check
  before publishing a duplicate.

Every agent can also be given HTTP tools (`http_tools` on `POST /api/agents`,
`psych_runtime.HttpTool` underneath): a REST endpoint the model calls directly,
with a credential resolved by name at call time and never written into the
Spec.

## Settings persist to a JSON file

`GET /api/settings`, `PUT /api/settings/providers`, `PUT /api/settings/mcp`,
`PUT`/`DELETE /api/settings/secrets*` and
`POST /api/settings/providers/{id}/activate` let a real settings UI configure
this backend by hand instead of through environment variables: add a
provider, fix a pasted API key, register an MCP preset, rotate a secret --
and have it survive a restart, the same way an agent published or a Run
dispatched through Psych's own `Store` already does.

That state lives in one JSON file (`app/settings_store.py`), path from
`PSYCH_PLAYGROUND_STATE_FILE`, read at boot and rewritten on every settings
change. **It holds real API keys and secret values, in plaintext.** Account
isolation is not encryption: it stops one signed-in person reading another's
key through the API, and does nothing whatever about anyone who can read the
file. That is the same trust model `PSYCH_PLAYGROUND_SECRETS` already has -- a
tool you run on your own machine -- and the default
path (`.playground-state.json` next to this README) is gitignored
accordingly. Every response this API returns is narrowed first
(`app.schemas.SettingsResponse` and friends): a provider's key never comes
back as anything but `has_api_key: bool`, and a secret never comes back as
anything but its name.

On the first boot against a given state file (nothing stored yet), it seeds
itself from `PSYCH_PLAYGROUND_BASE_URL`/`_API_KEY`/`_MODEL`,
`PSYCH_PLAYGROUND_SECRETS` and `PSYCH_PLAYGROUND_MCP_SERVERS` -- so a
playground already configured through env vars keeps working exactly as
before, now as a provider named `"default"` a person can edit or add
alongside. Every boot after that reads the file as written; those env vars
are then no longer consulted for what it already holds.

**Activating a provider swaps the live `ModelClient`, with no restart and no
dropped Run.** `POST /api/settings/providers/{id}/activate` rebuilds an
`OpenAICompatibleClient` for the newly active provider and hands it to
`app/runtime_router.py`'s `ApprovalRoutedRuntime.set_model()`, which mutates
every `Runtime` it already built in place -- see that method's docstring for
why that is what makes an Attempt already in flight keep running on the
model it started with, while the very next model call (or the next Attempt
entirely) picks up the new one.

**Testing a provider makes one real call.** `POST
/api/settings/providers/{id}/test` tries `known_models()` first; if the
provider cannot say (an empty result is honestly "I don't know", not "you're
misconfigured" -- `psych_runtime.model.port.ModelClient.known_models`'s own
contract), it falls back to a one-token completion, which raises on the
failures that are actually worth telling apart: a bad key, a bad URL, a
model id the provider has never heard of. The response's `detail` is that
error's real text, not a generic "connection failed".

MCP presets set through `PUT /api/settings/mcp` are exactly that --
presets `POST /api/agents`'s `mcp` array can be filled in from. Nothing
connects until an agent actually names a server; a published Spec always
carries its own copy of whatever it was built with (MCP config is part of
the Version hash, DESIGN.md §4), so a preset changing later can never mutate
an agent already published against it.

## Describing a connection, and why it is not part of an agent

`PUT /api/settings/mcp` takes a `description` per server: what the system is
for, in a sentence or two, capped at 400 characters. It reaches the model in
the system prompt at run time, alongside the server's name and how many tools
this Run can reach.

It is deliberately **not** part of a published agent. `psych_runtime.McpServer` is
inside `AgentSpec`, so a description there would join the Version hash and
improving the wording would republish every agent naming that server. The
backend keeps it beside the connection and hands it to Psych through
`McpTools(describe_server=...)`, which reads it at every turn boundary. Editing
one takes effect on the next turn with no restart, and no agent changes.

Leaving it empty falls back to whatever the server says about itself: MCP's
handshake carries an optional `instructions` field, which Psych now reads.

The contrast worth knowing: `POST /api/agents` takes `answer_style`, and that
one *is* part of the published agent and does move its Version hash, because
it changes what the model is told about how to answer. A response style is
part of what the agent is. A server description is a fact about an external
system that moves on its own schedule.

## An agent is a name; a Version is what it points at

A Version is immutable, and that is not a stylistic preference. `psych_runtime.runtime.execute`
reloads the pinned Version at the top of *every* Attempt, including the one a
Worker reclaiming an expired lease runs. A Run whose Spec could change
underneath would resume a conversation the model never had, which is DESIGN.md
§23's second item.

What that does not require is a catalogue where editing an agent means
publishing a second one. So this backend keeps a pointer beside its Version
entries: a stable `agent_id` that survives every edit, the Version the agent
currently runs, and the ordered history of everything it has run. Docker tags
and Git branches are the same arrangement.

- `POST /api/agents` with an `agent_id` publishes a new Version and moves that
  agent to it. Without one it creates an agent.
- `GET /api/agents` lists agents. `GET /api/agents/{id}/versions` is the
  history, and it is where the audit question people assume immutability is for
  gets answered.
- `POST /api/runs` with an `agent_id` resolves the pointer **once**, at
  admission, and the hash it gets is what the Run pins for the rest of its life.
  Editing the agent a second later changes nothing about it.
- `POST /api/runs` with a `version_hash` pins one deliberately.
- `POST /api/runs` with a `continues_run_id` takes both fields from the Run it
  continues, so a thread keeps its agent and keeps running what it opened on,
  whatever has been published since.
- `POST /api/runs/{run_id}/fork` may name a *different* agent, and that
  inversion is deliberate: continuing or branching a thread must not switch
  agents halfway through, and a fork is a new conversation, so it may.

A Run records `agent_id` as well as `version_hash`, and neither answers the
other. A Version is content, so an agent duplicated and published unchanged
shares a hash with its twin; attributing a Run by hash would show one agent's
conversations under the other. Editing an agent moves it off the hash its
earlier Runs pinned; attributing by the *current* hash would orphan them. Both
questions are only answerable at admission, which is where the answer is
written down.

Every feature flag joins the Version hash, `may_ask_questions` and
`tasks_enabled` included, and both are reported back by `GET /api/agents` for
the edit form to restore. They have to be: an agent that can park a
conversation, or that keeps a plan, writes a different prompt from one that
cannot, so a form that silently dropped them would publish a different agent
from the one somebody meant to edit.

## Workflows: the other authoring surface

`POST /api/workflows` publishes a `WorkflowSpec` (DESIGN.md §5): a fixed order
of steps rather than a model deciding what to do next. A step is a registered
tool with its arguments written down, an existing agent, or another existing
workflow -- the latter two copied in whole at their *current* Version, the
same rule an agent's `subagents` roster follows and for the same reason: one
Version hash has to pin the whole tree so a crash-recovering Worker replays
exactly what it started with.

A workflow gets the same identity treatment an agent does: `workflow_id` is
stable, publishing a new Version moves it, and `GET /api/workflows/{id}`
answers with whatever it runs today. It is dispatched through the same
`POST /api/runs` door an agent uses (`{workflow_id, message}`), and the
message reaches an agent step as its own input; a workflow of tool steps
alone can ignore it. Each finished step is written to the log before the next
one starts, so a Worker that crashes mid-workflow resumes at the step that
was running rather than from the top -- the property DESIGN.md §5 chooses
memoisation over deterministic replay to get.

`create_workflow` (`app/tools.py`) is a tool like any other, registered the
same way as `lookup_order`, and it exists to show that publishing one is not
special: an agent asked to "set up a check for order A1" can call it and
build the pipeline itself, and it shows up under Workflows for a person to
run again, or edit first.

## Branching, and forking, which are not the same thing

Two operations, and the difference is one field. Both diverge at the same
message and carry the same history; what separates them is whether the result
is a version of this chat or a chat of its own.

`POST /api/runs/{run_id}/branch` asks one message again inside the conversation
it is already in. The new Run continues that message's *predecessor*, so
everything said before it is shared history and everything from it on is a
second future. Both futures keep the conversation's `conversation_id`, so
`GET /api/runs` still describes one chat and a console pages between the
versions rather than listing them separately. That is what an edited message
does in a versioned conversation, and it is what the small `1/2` arrows page
between. There is deliberately no `agent_id` on the body: a conversation is
with one agent for its whole life, and a branch that switched would produce a
chat whose two halves were answered by different Specs with nothing saying so.

`POST /api/runs/{run_id}/fork` diverges at the same place and takes a fresh
`conversation_id`. It is a chat in its own right from the moment it is
dispatched: its own row in the list, deleted on its own, and left standing when
the conversation it came from is deleted. `agent_id` is offered here and is the
reason to fork at all, since putting one question to two agents means having
two chats to compare.

Nothing is copied for a fork. It continues the very Runs it forked from, so its
history is the history rather than a snapshot that could drift. That is also
why `DELETE /api/runs?thread=true` needs Git's rule: those Runs belong to one
conversation and are read by another, so a Run a surviving fork still reaches
is kept and unlisted rather than deleted out from under it, and a collection
pass afterwards removes what an earlier deletion was holding for a fork that
has since gone. Deleting a conversation otherwise takes all of it, every branch
included, because branches are versions of a message rather than chats.

Both happen at Run boundaries and never mid-Run. Every user message opens a
Run, so "from this question" and "from that answer" already land on a boundary,
and a prefix bound into the log would buy nothing while costing a great deal: a
Run truncated partway is a Run whose tool calls may have no results, which
Psych's own reducer would be right to call corrupt. There is no merge either,
because two divergent lines over an append-only log have nothing to reconcile.

What neither operation needed was a way to dispatch -- that already existed --
but two identities. Two Runs continuing one predecessor make the chain a tree,
and `continues_run_id` alone cannot say which of two futures a Run belongs to,
so walking a conversation forward picks a child at random and one of the two
answers silently disappears from every list. Every Run therefore carries a
`branch_id`, stamped at dispatch: a new conversation mints one, a continuation
inherits its predecessor's, and a Run continuing a message that has already
been answered onward mints a fresh one. Within a branch each Run has at most one
child on that branch, which is what makes the forward walk single-valued again.
`conversation_id` is the second identity and answers the other question: which
of these branches are one chat.

`Store` gains nothing for this. DESIGN.md §7 gives it no parent-to-children
index on purpose, and enumerating branches is the consumer's job -- the same
division that makes `GET /api/runs` this backend's to provide. The branch ids
live in `PlaygroundIndex` beside the rest of this platform's bookkeeping.

Deleting a conversation deletes the whole tree, every branch of it. Keeping the
branches not named would mean either leaving one behind with its opening
question still listed and nothing else, or deleting nothing at all, since every
Run of the original is a Run the other branch reads as history.

## The skill library, and what "global" is allowed to mean

`PUT /api/settings/skills` holds a per-account library of skills (DESIGN.md
§16): procedures written once and attachable to any agent you build. Attaching
one **copies** its three fields into the published Spec, where they join the
Version hash.

The copy is the design. The alternative is a library the runtime reads at turn
time, which is exactly what a server description does (the section above), and
the two are worth telling apart. A description is a fact about somebody else's
system, so injecting it late is right precisely because the agent did not
change. A skill body is instructions the model follows, much closer to the
agent's own `instructions`. Let those change under a published Version and two
Runs of one Version behave differently, which DESIGN.md §23.1 asks not to
happen.

So "global" means available to every agent you build, never reaching into every
agent you have built. Editing the library changes what the next publish gets;
an existing agent picks it up when somebody edits and republishes it, under a
new hash.

## Approvals are per-agent, sort of

Psych's own `Runtime.approval_selectors` is a process-wide policy (its
docstring: "Assembled once at boot ... Holds no per-Run state"), which is the
right shape for a real deployment where one host enforces one policy for
every agent it runs. This playground's REST contract asks for
`approval_selectors` per created agent instead, which is a genuinely
different idea -- an approval policy chosen per agent rather than per host.
Rather than change Psych's `Runtime` to take a per-Spec override (which would
mean an approval policy joins a Spec's content hash, a real design decision
this example has no business making unilaterally), `app/runtime_router.py`
wraps a small family of `Runtime` instances, one per distinct selector tuple
in use, and picks the right one per Run from what its agent was created with.
A consumer with one approval policy for every agent they run needs none of
this and should construct one `Runtime` directly, the way `docs/api.md`
does.

## REST contract

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/config` | -- | `{model, store, mcp_servers, has_api_key, provider_label, default_tenant}` -- `model` and `provider_label` come from the *active* provider, not the environment this process booted with |
| `POST /api/agents` | see below | `{agent_id, version_hash, name, created}`. `agent_id` in the body edits that agent; omitted creates one |
| `GET /api/agents` | -- | One row per agent, not per Version: `[{agent_id, version_hash, name, instructions, model, tools, skills, mcp_servers, may_ask_questions, tasks_enabled, published_at, created_at, updated_at, version_count}]` |
| `GET /api/agents/{agent_id}` | -- | One agent, described by the Version it currently runs |
| `GET /api/agents/{agent_id}/versions` | -- | `[{version_hash, name, model, published_at, current}]`, newest first |
| `DELETE /api/agents/{agent_id}` | -- | `{ok: true}`. Catalogue removal, every Version included; the Versions stay in the Store because Runs that pinned them still read them |
| `POST /api/runs` | `{agent_id \| version_hash \| workflow_id, message, continues_run_id?}` | `{run_id}`. Exactly one of the three. A workflow's Run is admitted, dispatched and reported exactly like an agent's -- the pinned Version decides which engine the Worker drives |
| `POST /api/runs/{run_id}/branch` | `{message}` | `{run_id}`. Asks that message again on a branch of the same conversation, continuing its predecessor. No `agent_id`: a branch does not change agent |
| `POST /api/runs/{run_id}/fork` | `{message, agent_id?}` | `{run_id}`. Same divergence point, new conversation. `agent_id` puts the question to a different agent |
| `GET /api/runs` | -- | `[{run_id, agent_id, branch_id, name, tenant, state, started_at, version_hash, message, settled_at, continues_run_id}]`, newest first |
| `DELETE /api/runs/{run_id}?thread=true` | -- | `{ok: true}`. `thread=true` drops the whole conversation, every branch of it, which is what deleting a chat means to a person |
| `GET /api/runs/{run_id}/stream?after=N` | -- | SSE: one `data:` per Record, then `event: done` |
| `GET /api/runs/{run_id}/report` | -- | `psych_runtime.report()`, serialised |
| `GET /api/runs/{run_id}/status` | -- | `psych_runtime.status()`: the lifecycle a screen shows, the pending approval with its arguments, and the head sequence to reconnect from |
| `GET /api/runs/{run_id}/messages` | -- | `[{role, content, tool_name?, seq, at}]`, one Run's conversation |
| `GET /api/runs/{run_id}/answer` | -- | `psych_runtime.answer()`: what the run concluded, and the work behind it. The split is derived from the log, so a console can collapse the working turns and show the answer |
| `GET /api/runs/{run_id}/thread` | -- | `psych_runtime.thread()`: the whole conversation across every Run in its chain, each message carrying its `run_id` |
| `GET /api/threads/{run_id}/report` | -- | One **branch's** totals and every Run's own report. Resolves the newest Run on that Run's branch first, so entering from any message lands on the same page and a forked conversation never mixes two branches into one set of totals |
| `POST /api/runs/{run_id}/resume` | `{approved, by?}` | `{ok: true}` |
| `POST /api/runs/{run_id}/interrupt` | `{reason?}` | `{ok: true}` |
| `POST /api/runs/{run_id}/send` | `{message, queue: steer \| follow_up \| next_run}` | `{entry_id, queue}`. DESIGN.md §9's three queues, reachable from outside a test: a `steer` reaches the turn running now, a `follow_up` waits for it to finish, a `next_run` is the only one legal after an interrupt |
| `GET /api/runs/{run_id}/state` | -- | `psych_runtime.state()`, whole: the reducer's own working object -- open tool calls, the three queues, failure streaks, compaction boundaries, children |
| `GET /api/runs/{run_id}/records?after=&limit=` | -- | One page of the raw log, for a client that wants it whole rather than streamed |
| `GET /api/runs/{run_id}/text?after=` | -- | SSE over `psych_runtime.stream_text()`: only the assistant's words, for a client that renders nothing else. `event: error` rather than a silent close on a Run that ends without an answer |
| `GET /api/runs/{run_id}/subagents?depth=N` | -- | The subagent tree under this Run, each child with its own lifecycle, what it was asked for, the tools it actually holds, and its branch's tokens and cost. A projection over the logs: the parent's own records say what it spawned and steered, and `psych_runtime.report(child_depth=N)` walks into each child |
| `POST /api/runs/{run_id}/subagents/{child_run_id}/message` | `{message}` | `{ok: true}`. The same steering queue the parent agent's own tool uses, delivered at the child's next turn. `409` once the child has settled |
| `POST /api/runs/{run_id}/subagents/{child_run_id}/interrupt` | `{reason?}` | `{ok: true}`. Stops one child without stopping its parent |
| `POST /api/runs/{run_id}/subagents/{child_run_id}/retry` | -- | `{run_id, retried_from, version_hash}`. A **new** Run of the same pinned Version with the same input, not a second attempt at the old one: a Run is admitted once and its log is append-only, so the failed one stays in the tree beside it |
| `GET /api/tools` | -- | `[{name, description, input_schema, annotations, interruptible, safe_to_retry, source}]`, from the live `ToolRegistry` |
| `GET /api/settings` | -- | `{providers, mcp_servers, secrets, active_provider_id}`. Each MCP entry carries `last_connection` (the persisted result of the last real connect) and `live` (the connection this process is pooling right now) |
| `PUT /api/settings/providers` | `{providers: [{id?, label, base_url, model, api_key?}]}` | same shape as `GET /api/settings` |
| `POST /api/settings/providers/{id}/activate` | -- | `{ok: true}` |
| `POST /api/settings/providers/{id}/test` | -- | `{ok, detail, models?}` |
| `PUT /api/settings/mcp` | `{mcp_servers: [...]}` (see `POST /api/agents`'s `mcp` shape, plus `transport`, `preload` and `description`) | same shape as `GET /api/settings` |
| `POST /api/settings/mcp/{name}/test` | -- | `{ok, detail, tool_count?, tools?, error_type?}`, one real connection under the tenant a Run will use, persisted so a connected server still reads as connected after a restart |
| `PUT /api/settings/a2a` | `{a2a_peers: [{name, url, description, credential, scheme, tenant, allow, optional, extensions}]}` | same shape as `GET /api/settings`. Saved A2A peers, offered when building an agent. Presets: attaching one copies it into the published Spec |
| `PUT /api/settings/skills` | `{skills: [{name, description, body}]}` | same shape as `GET /api/settings`. Replaces the account's skill library |
| `PUT /api/settings/secrets` | `{secrets: {name: value}}` | `{secrets: [name, ...]}` |
| `DELETE /api/settings/secrets/{name}` | -- | `{ok: true}` |
| `GET /api/oauth/pending` | -- | `[{state, authorization_url, tenant, started_at}]`, authorizations waiting for a browser |
| `GET /api/oauth/callback` | -- | HTML. Where an `authorization_code` grant sends the browser back |
| `GET /api/scenarios` | -- | `[{id, title, proves, design_ref, requires, available, unavailable_reason}]` |
| `POST /api/scenarios/{id}/run` | -- | SSE: `{step, detail, at}` progress events, then one `{"result": {passed, summary, assertions, run_ids, report?}}` |
| `POST /api/workflows` | `{workflow_id?, name, description?, steps, limits?}` | `{workflow_id, version_hash, name, created}`. A step is a tool call with fixed arguments, an existing agent, or an existing workflow -- both of the latter copied in at their current Version, exactly as an agent's `subagents` roster copies its children |
| `GET /api/workflows` | -- | `[{workflow_id, version_hash, name, description, steps, tools, limits, published_at, created_at, updated_at, version_count}]` |
| `GET /api/workflows/{workflow_id}` | -- | One workflow, at the Version it currently runs |
| `DELETE /api/workflows/{workflow_id}` | -- | `{ok: true}`. Catalogue removal only, the same rule `DELETE /api/agents/{agent_id}` follows |
| `PUT /api/settings/runtime` | `{cost_policy, blob_offload_bytes, catalogue_budget_chars, sandbox_enabled, sandbox_limits, egress_allow, denied_tools}` | same shape as `GET /api/settings`. Every `Runtime` constructor argument this backend leaves to the consumer, per account -- none of it is part of any Spec, so none of it moves a version hash |
| `POST /api/settings/a2a/tokens` | `{secret_name, ttl_days?}` | `{secret_name, token, expires_at}`. A long-lived session token for a peer elsewhere to present as a bearer token, saved as a secret under that name, shown once |
| `POST /api/settings/a2a/peers/local` | `{agent_id, name?, secret_name?, description?}` | same shape as `GET /api/settings`. One of this account's own agents, added as an A2A peer of its others -- its card URL is this deployment's own, so attaching it and dispatching against it is a real, unmocked A2A round trip with nothing else to stand up |

## A2A contract

Another organisation's agent talks to this one over A2A (Agent2Agent v1.0),
which is a different front door with different rules: it lives outside
`/api`, it authenticates with a bearer token rather than the session cookie,
and its errors are the protocol's rather than this backend's `{detail,
issues}` shape. `app/a2a/` is the whole of it, and `psych_runtime.a2a` is the protocol
it speaks; neither is in `psych_runtime` itself, because DESIGN.md §1 refuses to own a
transport.

Discovery is the one exception. The Agent Card routes (`/.well-known/agent-card.json`
and `/a2a/v1/agents/{agent_id}/agent-card.json`) answer with no credential at
all, because §8.2 puts a card at a well-known path precisely so a client can
read it *before* it knows how to authenticate -- the card is what says how. A
caller who does send a bearer token gets the same public card back rather than
something richer; that only happens through `GetExtendedAgentCard` (below).
Every other operation needs two headers: `Authorization: Bearer <session
token>` -- the same token the console's cookie holds, and the scheme the Agent
Card declares -- and `A2A-Version: 1.0`. An absent version header means 0.3
(§3.6.2) and is refused with `VersionNotSupportedError`, which is deliberate:
serving 1.0 semantics to a client that said 0.3 is exactly what the header
exists to prevent.

A session token that outlives a browser session is minted with `POST
/api/settings/a2a/tokens`, saved as a secret under the name given so an MCP
server, an HTTP tool or an A2A peer preset can all refer to it by name. It is
shown once, the same rule an API key follows anywhere else in this backend.

Both HTTP bindings are served and answer identically (§5.1). gRPC is not: §5.2
requires an agent to declare what it supports, and these cards declare
`JSONRPC` and `HTTP+JSON` only.

| Method & path | Body | Returns |
|---|---|---|
| `GET /.well-known/agent-card.json?agent={agent_id}` | -- | The Agent Card (§8.2), signed as a JWS (§8.4). The `agent` selector is needed because this console hosts many agents behind one origin; with one agent it can be omitted |
| `GET /a2a/v1/agents/{agent_id}/agent-card.json` | -- | The same card at a direct per-agent URL, which is the form to hand another Psych deployment |
| `POST /a2a/v1/rpc` | JSON-RPC 2.0 `{jsonrpc, id, method, params}` | The JSON-RPC binding (§9). All eleven operations, PascalCase method names: `SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask`, `CreateTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig`, `GetExtendedAgentCard` |
| `POST /a2a/v1/{tenant}/rpc` | as above | The same, with the routing identifier in the path (the proto's `additional_bindings`) |
| `POST /a2a/v1/message:send` | `SendMessageRequest` | `SendMessageResponse`. Waits until the task is terminal or interrupted unless `configuration.returnImmediately` |
| `POST /a2a/v1/message:stream` | `SendMessageRequest` | SSE: one `data:` per `StreamResponse`, in log order, ending at the terminal or interrupted state |
| `GET /a2a/v1/tasks?contextId=&status=&pageSize=&pageToken=` | -- | `ListTasksResponse`, scoped to the caller |
| `GET /a2a/v1/tasks/{id}?historyLength=N` | -- | `Task` |
| `POST /a2a/v1/tasks/{id}:cancel` | -- | `Task`. `TaskNotCancelableError` for one already terminal |
| `GET \| POST /a2a/v1/tasks/{id}:subscribe` | -- | SSE, as `message:stream`. Both verbs, because the proto annotates GET and §11.3.2 writes POST |
| `POST /a2a/v1/tasks/{id}/pushNotificationConfigs` | `TaskPushNotificationConfig` | The stored config, with its id |
| `GET /a2a/v1/tasks/{id}/pushNotificationConfigs` | -- | `ListTaskPushNotificationConfigsResponse` |
| `GET /a2a/v1/tasks/{id}/pushNotificationConfigs/{configId}` | -- | `TaskPushNotificationConfig` |
| `DELETE /a2a/v1/tasks/{id}/pushNotificationConfigs/{configId}` | -- | `{}` |
| `GET /a2a/v1/extendedAgentCard?agent={agent_id}` | -- | The published agent's card, plus its instructions, for a caller holding a token (§13.3). The instructions are what the public card withholds -- they say how the agent behaves, not only what it is called and can reach |
| `POST {your webhook}` | -- | Sent *by* this backend, per registered config: a `StreamResponse` with `Authorization` from the config, `X-A2A-Notification-Token`, and `Content-Type: application/a2a+json` |

A task is a Run and a context is a conversation. `taskId` is a `RunId`, always
minted here (§3.4.2 forbids a client choosing one), and `contextId` is the
root of the `continues_run_id` chain, so a task started over A2A appears in
this console's own run list and its follow-ups thread with it. A message
naming a task that is waiting for input resumes that Run and keeps its task
id, which is how `TASK_STATE_INPUT_REQUIRED` round-trips; a message naming
only a context starts a new task in it.

Errors follow §5.4's table: over JSON-RPC an `error` object with a code in
`-32001..-32009` at HTTP 200, over REST a `google.rpc.Status` body at the
matching HTTP status, both carrying a `google.rpc.ErrorInfo` whose `reason`
names the A2A error type. Another account's task is `TASK_NOT_FOUND` and never
a permission error, because §13.1 says a server must not reveal that a
resource it will not show you exists.

Two environment variables belong to this door:
`PSYCH_PLAYGROUND_A2A_BASE_URL` (what the cards advertise as this
deployment's origin; defaults to the host and port it is bound to) and
`PSYCH_PLAYGROUND_A2A_SIGNING_KEY` (the HS256 card-signing secret; generated
once into `a2a-signing-key` beside the state file when unset). Two deployments
that want to verify each other's cards are given the same key.

### Calling your own agents over A2A, with nothing else to stand up

`POST /api/settings/a2a/peers/local` is the fast way to see this door working:
name one of your own published agents, and it comes back as a saved peer whose
address is this deployment's own per-agent card URL and whose credential is a
token minted for the purpose (an existing secret of that name is left alone).
Attach the peer to a second agent's `a2a` list and dispatch it, and what
happens next is the ordinary path -- the caller's Worker fetches the target's
card over real HTTP, is offered its skills as tools, and a call is a real
`SendMessage` to this same process's own A2A router. Nothing here is a
shortcut around the protocol: it is the protocol, with the second deployment
being this one.

Every non-2xx response *from the `/api` routes* is
`{"detail": "...", "issues": [{"path", "message"}]}`
-- `issues` is populated for a Spec pydantic rejected outright and for one
`SpecValidationError` refused at publish (DESIGN.md §4: validation happens
there, never at run), empty otherwise. Unknown runs and unknown
`version_hash`es are `404`; a bad Spec, a bad `limits` key, or resuming a Run
that is not suspended are `400`.

`POST /api/agents` body:

```jsonc
{
  "name": "support",
  "description": "Answers questions about orders.",  // optional, an A2A peer's card only
  "instructions": "Help the customer with their order. Be brief.",
  "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
  "temperature": 0.2,               // optional
  "model_options": {"top_p": 0.9, "fallbacks": ["backup-model"]},  // optional, ModelRef's rest
  "tools": ["lookup_order", "issue_refund"],
  "http_tools": [],                 // optional, HttpTool entries: {name, description, url, method, input_schema, headers, credential, timeout_seconds, interruptible}
  "mcp": [],                        // McpServer entries, see above
  "subagents": [],                  // optional, the delegation roster: {name, description, agent_id}
  "spawn": null,                    // optional, the SpawnEnvelope: {tools, models, max_depth, max_alive, may_message}
  "suspension": null,               // optional, the four expiries; see SuspensionPolicy
  "limits": {"max_turns": 12},      // optional, any Limits field
  "approval_selectors": ["@destructive"]  // optional, see above
}
```

Every field above but `agent_id` is exactly a field of `psych_runtime.AgentSpec`
or one of the models it holds, so this body has no shape of its own to learn
beyond what the library already documents.

### A worked example, end to end

```sh
AGENT=$(curl -s -X POST http://127.0.0.1:8080/api/agents -H 'Content-Type: application/json' -d '{
  "name": "support",
  "instructions": "Help the customer with their order. Be brief.",
  "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
  "tools": ["lookup_order", "issue_refund", "check_inventory"],
  "approval_selectors": ["@destructive"]
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_id"])')

RUN=$(curl -s -X POST http://127.0.0.1:8080/api/runs -H 'Content-Type: application/json' \
  -d "{\"agent_id\": \"$AGENT\", \"message\": \"Where is order A1?\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')

curl -N "http://127.0.0.1:8080/api/runs/$RUN/stream"       # watch it live
curl -s "http://127.0.0.1:8080/api/runs/$RUN/report" | python3 -m json.tool
curl -s "http://127.0.0.1:8080/api/runs/$RUN/messages" | python3 -m json.tool
```

### Configuring a provider through the API instead of env vars

```sh
# Add a provider (or edit one -- pass its id to keep its stored api_key
# unless you also pass a new api_key, and "" to clear it):
curl -s -X PUT http://127.0.0.1:8080/api/settings/providers -H 'Content-Type: application/json' -d '{
  "providers": [
    {"label": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": "sk-..."}
  ]
}' | python3 -m json.tool
# -> {"providers": [{"id": "<generated>", "label": "openai", ..., "has_api_key": true}], ...}
# the key itself never appears in that response, or in any other.

PROVIDER_ID=...   # from the response above
curl -s -X POST "http://127.0.0.1:8080/api/settings/providers/$PROVIDER_ID/test" | python3 -m json.tool
curl -s -X POST "http://127.0.0.1:8080/api/settings/providers/$PROVIDER_ID/activate" | python3 -m json.tool
# Every Run dispatched from here on calls the newly active provider -- no
# restart, and nothing already in flight is dropped.
```

## Capabilities scenarios

Most of what makes Psych worth choosing over a loop around a model
SDK is invisible in a chat window: a Run surviving its Worker being killed, a
client reconnecting without gaps, a Version hash that agrees across two
authoring forms, four store adapters behaving identically, a tool result too
large for a DynamoDB item. `app/scenarios/` is fifteen self-contained modules,
one per claim, each of which drives the real runtime end to end -- its own
`Store`, its own registry, its own `FakeModel` or a real provider -- and
reports a genuine pass or fail through the same kind of typed assertion
`tests/e2e/test_regression_gate.py` already uses. Nothing is pre-recorded.

```sh
scripts/dev-services.sh start     # real Postgres, MySQL, DynamoDB Local
uv run python examples/playground/backend/run.py
curl -s http://127.0.0.1:8080/api/scenarios | python3 -m json.tool
curl -s -N -X POST http://127.0.0.1:8080/api/scenarios/crash-recovery/run
```

`GET /api/scenarios` lists every one of them with `available` and, when
`false`, the real reason -- never a silent skip. `requires` names what a
scenario needs to be true at all: `postgres` / `mysql` / `dynamodb` mean
`scripts/dev-services.sh start`; `model-provider` means an active provider
configured through `/api/settings/providers` (see above). A scenario whose
`check_availability` names a reason refuses to run rather than passing
vacuously: `POST .../run` still streams the documented SSE shape, but its
final result is `passed: false` with that reason as the summary.

| id | design ref | requires | proves |
|---|---|---|---|
| `same-spec-two-authors` | §23.1 | -- | The same agent built through `psych_runtime.builder` and from a plain dict publish to the same Version hash and run identically. |
| `crash-recovery` | §23.2 | postgres | A real OS process is SIGKILLed mid-tool-call; a second Worker reclaims the expired lease, settles the dangling call as `UNKNOWN`, and the Run still completes. The tool's body is proven to have run exactly once via a marker file. |
| `interrupt-and-steer` | §23.3 | -- | An interrupt lands while a tool call is genuinely in flight (a real sleep, not a script); the Run stops, and a message dispatched immediately after starts a wholly separate Run. |
| `reconnect-without-gaps` | §23.4 | -- | A client reconnecting with `after=N` gets exactly the records after `N`, checked at every one of a Run's own sequence numbers, not just one convenient cut. |
| `workflow-resume` | §23.5 | -- | A workflow driven directly (so it can be cancelled mid-flight) resumes past two already-completed steps without repeating either one's side effect, proven by a counter rather than by the workflow merely finishing. |
| `mcp-mid-run` | §23.6 | -- | An MCP server the pool has never touched is connected to lazily, the moment a live Run's own resolver first needs it, reused (not reconnected) across later turns, with nothing pre-warmed and no restart. |
| `honest-accounting` | §23.7 | model-provider | `psych_runtime.report()`'s token totals, cost and latency breakdown reconcile exactly with the log, checked against a real provider's own reported usage rather than a scripted one. |
| `four-stores` | §23.8 | postgres, mysql, dynamodb | The identical Spec and scripted model produce the same terminal state, tool calls and usage totals on `MemoryStore`, real PostgreSQL, real MySQL and real DynamoDB Local. |
| `sandboxed-code` | §23.9 | -- | A model program runs in a real subprocess (`SubprocessSandbox`, not the container backend -- see below), calls a host tool through the ordinary registry path, and a program that raises has its traceback handed back as data instead of failing the turn. |
| `failure-streak` | §23.10 | -- | Three consecutive failures of one tool withdraw it from what the model is offered, with the reason delivered in the next turn's own system prompt, well before the turn budget runs out. |
| `approvals` | §11, §10.9 | -- | A destructive tool call suspends the Run and releases its lease; an approval and a denial are each resumed from a wholly separate `Runtime`, the way a human clicking a button in another process actually would. |
| `large-result-offload` | §10.8 | -- | A result over `Limits.large_result_bytes` stays whole in the log but reaches the model as a preview plus a handle; `read_tool_output` is offered only once there is something to read, never before. |
| `delegation` | §17 | -- | A parent delegates to a subagent that runs as its own Run, one delegation depth deeper, inheriting the parent's Scope exactly; a turn that tries to spawn more children than `max_fanout_per_turn` allows is refused. |
| `tenant-isolation` | §23.6 | -- | Two tenants calling the same MCP server URL through the same pool get two connections and, checked against what the stub server itself received, two distinct credentials -- never shared. |
| `degenerate-loop` | §23 | model-provider | **A known, currently-open gap, shown rather than hidden.** A model repeating one already-*successful* tool call is not caught by the failure-streak guard, because that guard counts consecutive failures. See below. |

### The two scenarios that need a real model

`honest-accounting` and `degenerate-loop` mark `requires: ["model-provider"]`
and use whichever provider is active through `/api/settings/providers` --
every other scenario scripts `psych_runtime.testing.fake_model.FakeModel` for
reproducibility. Configure one the same way the rest of this README does
(Cloudflare Workers AI, an OpenAI-compatible proxy, whatever you have), then
`POST /api/settings/providers/{id}/activate` before running either.

If nothing is configured, both report `available: false` with that reason --
correct, not a bug: no other run had a real provider to score honest
accounting against either. Verifying these two end to end while writing them
used a throwaway stdlib HTTP server speaking the OpenAI-compatible streaming
wire format over loopback, registered as a provider the same way any real
one would be; that script is not part of this backend and is not required to
use it.

### `sandboxed-code` uses the subprocess sandbox, not the container one

`psych_runtime.sandbox` has two real adapters: `SubprocessSandbox` and a container
backend. `sandboxed-code` exercises the subprocess one, because that is the
one this environment (and most laptops) can actually run -- no container
runtime here means no way to test the container adapter locally; its own
tests skip in this environment and run in CI instead. Nothing in this
scenario set exercises the container backend, and it does not claim to.

### `degenerate-loop` is a known gap, not a passing demo of a fix

A real model, asked "Where is order A1?" against an agent with one
working `lookup_order` tool, called that tool 32 times with byte-identical
arguments. Every call returned `ok`. The failure-streak guard (§10.6) counts
*consecutive failures* of one tool, and these all succeeded, so the streak
never advanced and the guard never had anything to trip on -- that is the
guard working exactly as specified, and exactly why it cannot help here. The
Run's only real defence was `max_turns`, a budget rather than a control: it
stopped the Run, but only after paying for every turn, and recorded
`budget_exhausted`, a symptom rather than the cause.

This scenario's assertions do not depend on a live model actually looping
today to hold -- that would make the demo flaky in exactly the way DESIGN.md
warns against -- and they are not written to pass by being weakened. They
check the gap structurally, from the real `Limits` model and a real Run's own
`failure_streak_trips` and per-tool streak counters: `Limits` has a turn
budget and nothing that bounds a repeated *successful* call, and whatever the
live model in front of it actually did, nothing in the log challenged it for
repeating. If a model happens to solve the question in one call on a given
run, the summary says so plainly rather than claiming the gap closed. The gap is
known and no fix is scheduled. If `Limits` ever grows one, the structural
assertion starts failing on purpose, which is the signal that this
module needs updating rather than deleting -- see its own module docstring.

## Things worth knowing about Psych while you drive this

- **`Store` has no listing query, on purpose** (DESIGN.md §7): no
  `list_versions`, no `list_runs`. `GET /api/agents` and `GET /api/runs` are
  answered from `app/store_index.py`, a small index this backend keeps
  itself -- exactly the kind of thing a real platform is expected to build on
  its own storage. It persists to its own JSON file
  (`PSYCH_PLAYGROUND_INDEX_FILE`), written on every publish and dispatch, and
  is **reconciled against the `Store` at boot**: an entry whose Version or Run
  the Store can no longer resolve is dropped rather than listed. So against
  `PostgresStore` the lists survive a restart along with the Runs themselves,
  while against the default `MemoryStore` -- which does not outlive the
  process -- they come back empty, because by then nothing they named still
  exists.
- **Interrupting a suspended Run doesn't settle it immediately.** DESIGN.md
  §9's "an abort is a Record, not a flag" is real: `psych_runtime.interrupt()` on a
  Run that is suspended for an approval writes the abort record, but nothing
  makes the Run runnable again to act on it -- it stays `suspended` until the
  next `resume()` (approved or not) or until the suspension expires, at
  which point it settles `aborted`. Worth knowing before you conclude
  interrupt "didn't work" on a suspended Run in this playground.
