# Deploying an application built on Psych

Psych is a library. It runs inside your process, writes to your database, and
calls the model and tool endpoints you configure. That means most of what
makes a deployment safe is yours to decide, and this document says which
decisions those are. It describes what the library does today; it is not a
promise about deployments it has not seen.

## Scope is your authorisation, not Psych's

`Scope(tenant, principal)` is threaded through every call and stamped on every
Record. Psych never decides who a caller is. Build the Scope from your own
authenticated request:

```python
scope = psych_runtime.Scope(tenant=request.tenant_id, principal=request.user_id)
```

Never from a value the client supplies, and never from a run id: possession
of a run id is not authorisation. Pass `scope=` on every read
(`status()`, `answer()`, `report()`, `records()`, `stream()`); a Run belonging
to another tenant is then refused with `AccessDenied`. A read without a scope
returns any Run by id, which is right for an operator's own tooling and wrong
for anything that serves end users.

The `Policy` port decides whether a Scope may call a tool right now. Approval
selectors decide which classes of tool need a person. Both are yours to
configure; the defaults gate `@write` and `@destructive` calls and allow the
rest.

## Secrets

Psych holds no credentials of its own. A model client takes an API key you
pass it; an MCP server or HTTP tool names a credential that your
`SecretResolver` returns per Scope. Keep secrets in your secret manager and
resolve them at call time. Do not put a key in a Spec: a Spec is data, it is
hashed into a Version, and it may be stored, logged and reviewed.

Tool arguments and results are written to the Record log in full. If a tool
takes or returns a secret, that secret is in your database. Redact in the
tool, or do not pass it through a tool.

## Storage, durability and retention

`session()` defaults to `InMemoryStore`, which keeps nothing across a process.
Anything that must survive a restart, an approval that waits hours, or a
Worker being replaced needs `PostgresStore`, `MySQLStore` or `DynamoDBStore`,
migrated before the first Run.

The log is append-only and Psych never deletes from it. Every model prompt,
tool argument and tool result a Run touched is in there, and `report()` reads
it back. Decide your retention policy and enforce it in your database:
Psych has no retention setting, on purpose, because it does not know your
obligations. The same applies to a `BlobStore` holding large tool results.

## Workers, leases and side effects

A `Worker` claims a Run with a lease and renews it while the process lives. A
Worker that dies mid-tool-call leaves a call recorded as started and never
finished. The next Worker to claim the Run settles that call as `unknown` and
carries on; it does not execute the tool again unless the tool was registered
`safe_to_retry=True`.

That is the whole of Psych's crash guarantee: it never loses what happened,
and it never repeats a side effect it cannot prove is repeatable. Whether a
refund, an email or a database write is safe to run twice is a property of
your tool, and you say so at registration. Mark nothing `safe_to_retry` that
is not idempotent.

`interruptible=False` on a tool means an interrupt waits for the call to
finish rather than cancelling it. Use it for anything that must not be
half-done.

## Sandboxes

Model-written code runs in a separate process, never in the host interpreter.
A deployment registers backends under logical names, and an agent's Spec asks
for one of those names:

```python
runtime = psych_runtime.Runtime(
    ...,
    sandboxes=[
        psych_runtime.SandboxProfile("default", local_sandbox()),
        psych_runtime.SandboxProfile("heavy", container, hard_limits=limits),
    ],
)
await runtime.verify_sandboxes()  # describe() each one, at startup
```

A Spec holds the name and its requested terms. It never holds a backend, a
client or a credential, and the terms of one execution are the intersection of
what the backend can do, what the tenant's Policy allows, what the Spec asks
for and what the profile caps. Each stage narrows and none widens.

What a host can offer is discoverable before the first request, with
`psych_runtime.sandbox.local.detect_local_backends()`:

- **Linux with `bwrap`** reaches full isolation: namespaces, dropped
  capabilities, read-only system binds, a tmpfs workspace.
- **Linux without it, and macOS** reach process-level containment: rlimits, a
  process group, a private workspace, and on macOS a seatbelt profile that
  confines the filesystem and denies the network. macOS cannot cap memory, so
  a request for full isolation is refused there.
- **Windows** reaches process-level containment through a Job Object: the whole
  tree is contained and killed together, with memory, CPU and process limits.
  The child runs as the worker's own account, and the result says identity is
  unavailable rather than implying a boundary.
- **A container runtime** reaches full isolation. The worker itself must be
  POSIX: the channel is a bind-mounted Unix socket, and a Windows worker is
  refused rather than silently downgraded.
- **A sandbox service** of your own, or a vendor's, plugs in through
  `RemoteSandbox` and a documented HTTP protocol. Its credential resolves per
  Scope through your `SecretResolver` and never enters a Spec, a Record, a
  report, a trace or a URL.

Nothing is installed, downloaded or pulled implicitly. `bwrap`, a container
runtime and an image are things you provision; prefer an image digest where a
deployment must be reproducible.

Run untrusted code on a backend that reaches isolation. Give a process-level
backend a host with nothing on it worth reading.

## Egress

Every outbound HTTP call the library makes, the model client and MCP
included, goes through one transport with an `EgressPolicy`. Configure it
with the hosts your deployment may reach. A tool you write yourself is your
own code and is outside that seam; if it makes network calls, they are yours
to control.

## Approvals

An approval is a suspension: the Run releases its lease, persists, and waits.
The pending call, with its exact arguments, is on `status().pending_approval`.
Show that to a person and record who decided with
`resume(..., approved=..., by=...)`. `by` is written to the log and
interpreted by nothing; it exists so the audit trail can say who approved a
destructive call. A suspension expires; a decision that arrives after expiry
settles the Run as abandoned rather than executing a stale approval.

## The example console is an example

`examples/playground/` has accounts and hashed passwords because a demo
without them teaches the wrong thing, not because it is hardened. Run it on
your own machine. Do not expose it to the internet.

## What Psych does not do

No rate limiting, no budget enforcement, no model routing, no scheduler, no
authentication. Each of these is something your application already has or
should own outright. Metering is recorded so you can enforce a budget; the
enforcement is yours.
