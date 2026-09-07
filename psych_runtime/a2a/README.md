# psych_runtime.a2a

A2A (Agent2Agent) protocol v1.0, April 2026, Linux Foundation.

## Owns
The protocol data model as validated pydantic models (`models.py`), ported from
`specification/a2a.proto`, which §1.4 of the specification makes "the single
authoritative normative definition of all protocol data objects" over every
generated artifact.

The two HTTP bindings' wire concerns: the JSON-RPC 2.0 envelope, method table
and error codes (`jsonrpc.py`, §9), and the REST route table, query-parameter
rules and SSE framing (`rest.py`, §11).

The error taxonomy, with each type carrying its own JSON-RPC code, HTTP status
and gRPC status from §5.4's table, so two bindings cannot report one failure
differently (`errors.py`).

Version negotiation and extension opt-in (`negotiation.py`, §3.6 and §4.6).

Agent Card construction from an `AgentSpec` (`card.py`), RFC 8785
canonicalisation and JWS signing and verification (`signing.py`, §8.4).

Push-notification payloads, headers and retry schedule (`push.py`, §4.3).

And the reason the package exists: the pure mapping from a Run to a Task
(`mapping.py`). Run state to `TaskState`, the `continues_run_id` chain to
`contextId`, `RunId` to `taskId`, Records to `TaskStatusUpdateEvent` and
`TaskArtifactUpdateEvent`, `psych_runtime.answer()` to an `Artifact`, and one incoming
`SendMessage` to the decision it implies under §3.4's identifier rules. All of
it is unit-tested without a socket.

## Does not own
The server. DESIGN.md §1 refuses "an HTTP server, a route table, or any
transport", and A2A is a transport. The routes, the SSE endpoint, the webhook
sender and the signing keys live in `examples/playground/backend/app/a2a`,
which is the worked example a consumer copies.

The outbound client. From a running agent's point of view another agent is a
tool source, so it lives beside the MCP client in `psych_runtime.tools.a2a` and is
pooled, narrowed and gated exactly as MCP is.

Authentication. The card *declares* security schemes -- all five of §4.5 --
and Psych verifies none of them: who a caller is remains the consumer's
`Policy` and their own middleware (§14).

SSRF protection for webhook URLs. §13.2 asks for it and Psych already has one
place where an outbound URL is approved or refused: `EgressPolicy`
(`psych_runtime.model.egress`). A second check here would be a second control that
disagrees with the configured one.

## Deliberately not implemented: gRPC
The proto defines three bindings. §5.2 requires an agent to *declare* what it
supports, not to support all three, and a card built by `card.py` declares
exactly the two HTTP bindings this package serves. gRPC needs a gRPC server and
generated protobuf stubs; Psych's dependency floor is pydantic and httpx, and
its example platform is FastAPI over HTTP. The two HTTP bindings are
functionally equivalent per §5.1, so nothing is unreachable over them. A
consumer who wants gRPC adds an `AgentInterface` and their own server; the
mapping in `mapping.py` is unchanged either way.

## Deliberately narrow: asymmetric card signatures
`signing.py` implements all of §8.4's canonicalisation and JWS assembly, and
ships a complete HS256 signer over the standard library. ES256 and RS256 need
`cryptography`, which is not a Psych dependency, so they are a `CardSigner`
implementation a consumer supplies -- fifteen lines over their own key
material, with the hard half already done here.

## Ports
Defines `CardSigner` and `CardVerifier`. Consumes nothing: this package imports
only `psych_runtime.core`, so the mapping cannot acquire a database or a clock by
accident.

## The rules that live here
A `taskId` is server-generated and a client may never mint one (§3.4.2). A
`contextId` and `taskId` that contradict each other are rejected rather than
reconciled (§3.4.3); `resolve_message` is where that happens, once, for both
bindings.

Streaming events are delivered in the order they were generated (§3.5.2). Psych
gets this for free and it is worth knowing why: the log is already gapless and
totally ordered, and `stream_events` is a loop over it, so ordering is not
maintained by the streaming endpoint -- it cannot be lost by one.

Field presence is the protocol's, not pydantic's (§5.7, §8.4.1): `wire_dict`
omits an unset absent field and keeps an explicitly set one, and the same
function produces both the served bytes and the signed bytes so a card's
signature cannot fail to verify against the document it was served in.
