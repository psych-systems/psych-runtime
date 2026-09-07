# psych_runtime.tools

## Owns
The tool registry, the per-turn resolver, the four executors (code, http, mcp,
a2a), the access-narrowing function, the failure-streak guard, large-result elision
and offload, deferred MCP tool disclosure (`deferred.py`), and a full OAuth 2.1
client (`psych_runtime.tools.oauth`) that the MCP client drives on a 401 or 403 from a
protected server.

`a2a.py` is the outbound half of A2A: another agent, as a tool source. Same
shape as the MCP client and for the same reasons, a pool keyed by
`(scope, peer, credential)` and never by URL, a catalogue (there, an Agent
Card) refreshed on a TTL, and a Spec's grants re-narrowed every turn rather
than pinned at connect. One tool per skill the peer's card declares, named
`{peer}__{skill}` so two peers offering a `search` skill do not collide.
`psych_runtime.a2a` holds the protocol it speaks.

`deferred.py` is what keeps a large catalogue out of the prompt: a server whose
tools are deferred contributes three discovery tools instead of its schemas,
and the model reads one tool's schema when it decides to use it. Measured
against a real 351-tool server, that is 1,911 request bytes per turn instead of
2,082,521. Discovery narrows exactly as an ordinary call does, so it is a door
rather than a bypass.

## Does not own
Tool authorization policy. Psych asks the consumer's `Policy` port and does not
model orgs, roles or permissions (DESIGN.md §14).

OAuth's own refusals, on top of that: Psych serves no Client ID Metadata
Document and runs no browser or redirect listener. `AuthorizationRedirectPort`
is the seam a consumer implements for the one HTTP hop the authorization code
grant needs (§1: Psych owns no HTTP server); a consumer using only
`client_credentials` never needs to implement it at all.

## Ports
Consumes Policy and SecretResolver. Defines the internal tool executor
interface, `OAuthTransport` (the outbound HTTP capability the OAuth client
needs), and `AuthorizationRedirectPort` (the browser round trip for the
authorization code grant).

## The rules that live here
MCP clients pool by `(scope, server, credential)` and never by URL alone. Pooling
by URL will eventually send one tenant's OAuth token on another's call (§10.4).
Access narrows and never widens, computed by one function both the validator and
the runtime call (§10.5). Resolution runs at the start of every turn, never at
boot (§10.2).

Elision (what the model sees) and offload (where the bytes live) are two
different thresholds answering two different questions, in `large_results.py`.
Elision is a Spec field (`Limits.large_result_bytes`); offload is a Runtime
constant sized against `psych_runtime.store.blob`'s tightest backend, never a Spec
field, so a Version's hash never depends on which storage engine a deployment
happens to be wired to. An offloaded result always elides too, regardless of
the Spec's own threshold, because the log record's `result` is `None` once the
payload is offloaded and the conversation projection needs a handle to know
that. See `large_results.py`'s module docstring for why both DESIGN.md §10.8
("the log always holds the whole thing") and §7 (DynamoDB as a first-class
target) are still true above DynamoDB's 400KB item limit.

A structured (dict/list) result's elided preview is rendered with
`separators=(",", ":")`: the threshold and the preview the model
reads are both measured from the same compact bytes, never from pretty-printed
whitespace no one downstream still pays for. A `str` result never reaches that
`json.dumps` call at all -- it is text the tool itself returned, not a
structure Psych serialises, so this never rewrites it.

`McpPool` takes an optional `OAuthClient`. When one is configured, a 401 with a
`WWW-Authenticate: Bearer` challenge calls `OAuthClient.start` and a 403 naming
`insufficient_scope` calls `OAuthClient.step_up`, each retried exactly once;
every ordinary request calls `OAuthClient.bearer_token` first so an expiring
token refreshes before it ever produces a 401. Acquiring a token during
`connect()` changes a connection's credential identity, and so its pool key,
from whatever was resolved before connecting (`None`, for a server with no
static credential) to the OAuth grant's identity. `McpPool.get_or_connect`
moves the connection to that new key rather than leaving it under the one a
future unauthenticated lookup could still find, which is the same isolation
mistake §10.4 already warns pooling by URL alone would make, aimed at one
tenant's own two requests instead of two tenants' requests. A refresh or a
step-up never move the key: both replace the session's token in place without
changing the grant's identity, so a connection stays pooled where it is for as
long as it lives.
