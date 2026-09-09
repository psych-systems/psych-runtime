---
name: psych-mcp
description: >-
  Connect MCP servers to a Psych agent: `psych_runtime.McpServer` in a Spec, wiring
  `McpPool` and `McpTools` on the Runtime, the allow-list and three-plane access
  narrowing, connection pooling by (scope, server, credential), deferred
  catalogue disclosure for large servers, and optional versus required servers.
  Use whenever someone adds an MCP server to Psych, asks why MCP tools are
  missing from the prompt or why a huge server blows the context budget, asks
  how tenant isolation works for MCP, hits McpServerUnreachable, or asks whether
  stdio is supported (it is not). Read before wiring any MCP connection, because
  pooling by URL instead of by (scope, server, credential) is the single line
  that leaks one tenant's OAuth token onto another tenant's call.
---

# MCP

An `McpServer` in a Spec is data. The tools it contributes are discovered from
the server, never declared in the Spec.

```python
psych_runtime.McpServer(
    name="github",
    url="https://mcp.example.com/github",
    transport="http",  # Streamable HTTP. See "Transports" below.
    credential="github-token",  # a NAME the SecretResolver resolves
    allow=("search_issues", "list_*"),
    optional=False,
    preload=None,
)
```

## Wiring

```python
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.model.egress import HttpTransport

pool = McpPool(transport=HttpTransport(), secrets=secrets)  # ONE per process
runtime = psych_runtime.Runtime(store=store, model=model, registry=registry, mcp=McpTools(pool))
```

Without `mcp=`, a Spec can declare `mcp_servers`, publish, run, and the model is
never offered a single one of those tools. No error and no warning: they are
simply absent. That is the silent-incapacity failure the `mcp=` field exists to
prevent, so wire it whenever a Spec might name a server.

`McpTools` carries both halves at once: the catalogue the resolver reads and the
caller the executor uses. They must narrow identically, and wiring them
separately is how they drift apart. A drift in that direction is a model calling
a tool the Spec excluded.

## The rule that matters more than the rest

**Never pool by URL. Pool by `(scope, server, credential)`.** `McpPool` keys by
a frozen `McpPoolKey` carrying the Scope's tenant and principal, the server URL
and transport, and the **resolved credential's identity**, never the credential
name from the Spec and never its value.

One pool per process is the intended shape. Every Run for every tenant shares
it, and isolation comes entirely from the key. Building a pool per tenant just
moves the same one-line mistake to whoever wires up the per-tenant pools.

Because a cached catalogue lives inside one pool key's connection, a server's
`cacheScope: "private"` is satisfied by construction: there is no second cache
for it to leak into.

## Access narrows through three planes

What the **server offers** contains what the **tenant permits** contains what
the **Spec grants** contains what is **callable now**. One function computes
that intersection, and both the validator and the runtime call it so the two
cannot drift.

`allow` is the Spec's plane. Empty means every tool the server offers, still
subject to the tenant's plane. Patterns support a trailing `*`
(`allow=("list_*",)`). Grant order carries no meaning, so `allow` is stored as a
sorted set and does not change the Version hash.

The executor narrows **again** at call time. The resolver already filtered what
the model was shown, and that is not a control, because the model chooses the
name it sends. A name that does not survive narrowing is refused however the
model got hold of it.

Every remote tool is exposed to the model as `server__tool`, for example
`github__search_issues`. The qualified name preserves the owning connection
through execution and replay, and lets two servers safely publish the same raw
tool name. Deferred `call_tool` accepts a server plus its raw tool name and
performs the same qualification internally. If any local, remote, built-in, or
peer tool still resolves to a duplicate public name, resolution fails instead
of silently choosing one.

## Resolution is per turn, never at boot

`resolve_mcp_server` re-narrows against the live catalogue on every turn, so a
Spec's grant is re-evaluated each time rather than pinned at connect. This is
what makes a server connected mid-Run usable on the next turn without a restart,
and it is one of the ten things Psych's e2e suite asserts.

The catalogue itself refreshes on connect, on a TTL sweep, on demand, and on
`notifications/tools/list_changed`.

The official client follows every `nextCursor` from `tools/list`; a paginated
server therefore contributes its complete catalogue. Modern
`subscriptions/listen` and the older tools-list-changed notification both
invalidate the same scoped cache. Structured tool results are retained and
rendered alongside text instead of being discarded.

## Large servers: deferred disclosure

A server with a big catalogue does not put its schemas in the prompt. It
contributes three discovery tools instead (`list_tools`, `get_tool_info`,
`call_tool`), and the model reads one tool's schema when it decides to use it.

Measured against a real 351-tool server whose schemas are 2.0 MB: 1,911 request
bytes per turn instead of 2,082,521. Preloaded, that 2 MB is sent on **every
turn of every Run**, past several providers' request limits outright and billed
whenever it is not.

`preload` decides:

| Value | Behaviour |
|---|---|
| `None` (default) | Decide from the catalogue's size against the Runtime's `catalogue_budget_chars` (20,000). Honest, because the size is a fact about the server discovered at run time. |
| `False` | Always defer. |
| `True` | Always preload, when you would rather pay for schemas than have the model spend a turn discovering. |

The budget is a `Runtime` field, not a Spec field, because it describes this
deployment's prompt budget rather than the agent. Two Workers with different
budgets can run the same Version. It is counted in **characters**, deliberately
never scaled to resemble tokens: a token count needs a tokenizer, tokenizers
differ per provider, and the same number would then mean different things
depending on which model an agent named.

Discovery narrows exactly as an ordinary call does. It is a door, not a bypass.

## Unreachable servers

By default an unreachable server **fails the Run**. Set `optional=True` to omit
its tools and tell the model they are unavailable instead.

Defaulting `optional` to `True` would produce an agent that confidently tells a
customer it cannot issue refunds today. Being wrong loudly beats being wrong
quietly, so the default is the strict one.

## Transports

`"http"` is Streamable HTTP (MCP revision 2026-07-28) and is the default. The
client negotiates down to 2025-11-25 and 2025-06-18 servers through one
connection, deciding once at connect rather than branching per request.

`"sse"` uses the older HTTP+SSE transport through the official MCP client. MCP
has deprecated it, so use `"http"` for new connections unless the server only
supports the older transport.

`"stdio"` is **not** an option. It was removed rather than left as an enum
member Psych never implemented: a Spec could ask for it, validation passed, and
the connection could never work. Adding a real stdio transport is open work, not
a silent revival.

## Gotchas

- **`credential` is a name, never a value.** Your `SecretResolver` resolves it
  fresh for the calling Scope on every use.
- **A server behind OAuth needs an `OAuthClient` on the pool.** Without one, its
  first 401 is `McpServerUnreachable` with a message saying the server wants
  OAuth. See `psych-mcp-oauth`.
- **Structured tool results are preserved.** Text content is returned as text;
  `structuredContent` is serialized when it carries additional information.
- **`describe_server` on `McpTools` is called at every turn boundary,** so it
  must be a cheap in-memory lookup. It is deliberately not `async`, which is
  that signature's way of saying "no IO here".
- **Unannotated MCP tools are treated as `write` for approvals,** and a
  partially annotated one reads `destructiveHint`'s own default of true. See
  `psych-approvals`.
