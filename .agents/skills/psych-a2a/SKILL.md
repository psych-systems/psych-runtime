---
name: psych-a2a
description: >-
  Speak A2A (Agent2Agent) v1.0 with Psych in both directions: expose a Psych Run
  as an A2A Task (Agent Cards, JSON-RPC and REST envelopes, version and extension
  negotiation, JWS card signing, push notifications) and call another agent as a
  tool source via `psych_runtime.A2APeer`, `A2APool` and `A2ATools`. Use whenever someone
  mentions A2A, Agent2Agent, an Agent Card, taskId or contextId, agent
  federation, or wants one Psych agent to delegate to a remote agent it does not
  own. Read before writing any A2A route or peer wiring, because Psych ships the
  protocol and the mapping but deliberately no server, and because a peer pool
  keyed by URL leaks credentials the same way an MCP pool does.
---

# A2A

Psych speaks A2A v1.0 (April 2026, Linux Foundation). It ships the protocol and
the mapping, and **no server**, because A2A is a transport and Psych owns none.
The routes live in `examples/playground/backend/app/a2a/`, which is a complete
set to copy.

Two directions, and they live in different packages because they are different
jobs.

## Outbound: another agent is a tool source

From a running agent's point of view, a peer is where tools come from. So this
half sits beside the MCP client and is pooled, narrowed and gated identically.

```python
spec = psych_runtime.AgentSpec(
    name="support",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    a2a_peers=(
        psych_runtime.A2APeer(
            name="research",
            url="https://research.example.com",
            credential="research-token",  # a NAME
            scheme="Bearer",
            tenant=None,  # the peer's own opaque routing tenant
            allow=("search_*",),
            optional=False,
            extensions=(),
        ),
    ),
)

from psych_runtime.tools.a2a import A2APool, A2ATools

pool = A2APool(...)  # ONE per process
runtime = psych_runtime.Runtime(store=store, model=model, registry=registry, a2a=A2ATools(pool))
```

Without `a2a=`, a Spec granting `a2a_peers` publishes, runs, and never offers
the model a single peer skill. Same silent-incapacity failure as a missing
`mcp=`.

One tool per skill the peer's card declares, named `{peer}__{skill}`, so two
peers offering a `search` skill do not collide.

**`A2APool` keys by `(scope, peer, credential)` and never by URL**, for the
reason `McpPool` does. Its key also carries `routing_tenant`, the peer's own
opaque `tenant` value, because one peer URL may front several agents and two
Specs pointing at different ones must not share a cached card.

The card is fetched and cached on a TTL, and the Spec's grants are re-narrowed
every turn rather than pinned at connect. Exactly the MCP shape.

## Inbound: a Run is a Task

`psych_runtime.a2a` is a pure mapping over `psych_runtime.core`. It imports nothing else, so it
cannot acquire a database or a clock by accident, and all of it is unit-tested
without a socket.

```python
from psych_runtime.a2a import agent_card, task_of, context_id_of, AgentInterface

card = agent_card(spec, interfaces=[...], version="2026.4.1")
task = task_of(
    await psych_runtime.status(store, run_id),
    context_id=context_id_of(run_id, chain),
    answer=await psych_runtime.answer(store, run_id),
)
```

The mapping is total: a `RunId` is the `taskId`, the continuation chain is the
`contextId`, a pending question is `INPUT_REQUIRED`, and `psych_runtime.answer()` is an
`Artifact`. Your routes call it and decide nothing about the protocol.

What the package owns: the data model ported from `a2a.proto` (normative over
every generated artifact per §1.4), the JSON-RPC 2.0 envelope and method table,
the REST route table and SSE framing, the error taxonomy where each type carries
its own JSON-RPC code, HTTP status and gRPC status so two bindings cannot report
one failure differently, version and extension negotiation, Agent Card
construction, RFC 8785 canonicalisation and JWS signing, and push-notification
payloads, headers and retry schedule.

## Rules the package enforces

- **A `taskId` is server-generated. A client may never mint one** (§3.4.2).
- **A `contextId` and `taskId` that contradict each other are rejected, not
  reconciled** (§3.4.3). `resolve_message` is where that happens, once, for both
  bindings.
- **Streaming events are delivered in generation order** (§3.5.2). Psych gets
  this free: the log is already gapless and totally ordered and `stream_events`
  is a loop over it, so ordering is not maintained by the endpoint and cannot be
  lost by one.
- **Field presence is the protocol's, not Pydantic's.** `wire_dict` omits an
  unset absent field and keeps an explicitly set one, and the same function
  produces both the served bytes and the signed bytes, so a card's signature
  cannot fail to verify against the document it was served in.

## Deliberate omissions

**gRPC.** The proto defines three bindings; §5.2 requires an agent to *declare*
what it supports, not to support all three. gRPC needs a gRPC server and
generated stubs, and Psych's dependency floor is pydantic and httpx. The two
HTTP bindings are functionally equivalent per §5.1, so nothing is unreachable.
Add an `AgentInterface` and your own server if you want it; the mapping does not
change.

**Asymmetric card signatures.** All of §8.4's canonicalisation and JWS assembly
is implemented, with a complete HS256 signer over the standard library. ES256
and RS256 need `cryptography`, which is not a dependency, so they are a
`CardSigner` you supply: about fifteen lines over your own key material, with
the hard half already done.

**Authentication.** The card *declares* security schemes, all five of §4.5, and
Psych verifies none of them. Who a caller is stays your `Policy` and your own
middleware.

**SSRF protection for webhook URLs.** §13.2 asks for it and Psych already has
one place where an outbound URL is approved or refused: `EgressPolicy`. A second
check here would be a second control that disagrees with the configured one.

## Gotchas

- **`A2APeer.tenant` is the peer's tenant, not yours.** Your own tenancy is in
  the `Scope`.
- **`optional=False` fails the Run when a peer is unreachable,** matching
  `McpServer`. Marking a peer optional omits its skills and says so.
- **A peer skill call goes through the same approval selectors** as any other
  tool.
