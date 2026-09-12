---
name: psych-sandbox
description: >-
  Let a Psych model write a program and run it in isolation: the `Sandbox` port,
  the local, container and remote backends, sandbox profiles and the
  `CodeExecution` field on an `AgentSpec`, `IsolationLevel` and the guarantees
  each platform can honestly prove, `SandboxLimits`, host bindings that route
  back through the normal tool path, output budgets and spilled output, and
  failures returned to the model as data. Use whenever someone wants a Psych
  agent to execute code, mentions a code interpreter, code execution or
  `run_code` in a Psych context, asks how to sandbox model-written Python
  safely, asks what is guaranteed on Linux, Windows or macOS, wires a sandbox
  service of their own, or proposes RestrictedPython, `exec` with trimmed
  builtins or AST filtering (all rejected outright). Read before wiring
  `sandboxes=` or `code_execution=`, because an agent without the second is
  never shown `run_code`, and because what a backend *achieves* is reported per
  guarantee rather than assumed.
---

# Code execution

The model writes one program, a `Sandbox` runs it, and the result comes back.

**In-process execution is rejected outright.** `RestrictedPython`, `exec` with
trimmed builtins and AST filtering are all escapable. Process isolation is the
floor and there is no configuration that lowers it.

Two decisions, kept apart on purpose:

- **The deployment** wires backends and their hard limits, under logical names.
- **The agent** asks, in its Spec, for a named profile and terms no wider than
  that profile's. It never holds a backend, a client or a credential.

## Wiring, and turning it on for one agent

```python
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.profiles import SandboxProfile

runtime = psych_runtime.Runtime(
    store=store,
    model=model,
    registry=registry,
    sandboxes=[SandboxProfile("default", local_sandbox())],
)

spec = psych_runtime.AgentSpec(
    name="analyst",
    model=psych_runtime.ModelRef(model="gpt-5"),
    tools=(psych_runtime.CodeTool(name="lookup_order"),),
    code_execution=psych_runtime.CodeExecution(
        profile="default",
        isolation=psych_runtime.IsolationLevel.PROCESS,
        bindings=("lookup_order",),
    ),
)
```

Without `code_execution`, the agent is never shown `run_code`, whatever the
deployment wired. `code_execution` joins the Version hash: an agent that can
write and run a program is a different agent from one that cannot.

`sandbox=` (a single `Sandbox`) still works and registers as the profile named
`default`. Nothing that used it needs rewriting, but an agent still has to opt
in with `code_execution` to see the tool.

## The contract, and every clause matters

- **One program per execution, no state between executions.** A model wanting a
  value in its next program puts it in the program. The workspace is a fresh
  empty directory every time (`WorkspacePolicy.EPHEMERAL`, the only policy).
- **Host bindings are ordinary Python calls in the program** that route back
  through the normal tool path with the same Scope, Policy, approvals, egress
  seam and recording. There is no privileged back door, and the package
  enforces that by construction rather than convention: it has no import path
  to a tool registry to reach around in the first place. A binding is the same
  tool the model could have called directly, reached a different way. A
  binding must also be a tool the Spec grants — publishing fails otherwise.
- **Failures are data.** stdout, stderr and the full traceback come back as
  part of the result, never raised so they kill the turn. A model shown only
  "it failed" rewrites the program from scratch and usually reproduces the
  mistake. A model shown the traceback fixes the line.
- **A refusal is data too.** If the wired backend cannot meet what the Spec
  asked for — no network grant, an isolation level this host cannot reach, a
  backend that is not ready — the agent still gets `run_code`, and calling it
  returns a structured refusal naming the reason. Its other tools keep working.
  The one loud failure is a profile name nothing is registered under: that is a
  deployment mistake, and it stops the Attempt at startup.

## Isolation levels, and what a backend may claim

- `IsolationLevel.ISOLATED` — a kernel boundary, not this process's word.
- `IsolationLevel.PROCESS` — a separate process with limits, a scrubbed
  environment and a killable tree, but no kernel boundary.

A Spec asks for a minimum. A backend reports what it *achieved*, per guarantee:

```python
result.isolation  # the level this execution actually reached
result.guarantees.filesystem  # Enforcement.ENFORCED / UNVERIFIED / UNAVAILABLE
result.guarantees.network
# and .process_tree, .identity, .cpu, .memory, .file_size,
# .process_count, .wall_clock, .environment
```

`ENFORCED` means the execution observed the guarantee hold — the filesystem
grade, for instance, comes from a canary file the host writes *outside* the
workspace and the child reports it could not read. `UNVERIFIED` means the
mechanism was requested and nothing contradicted it. `UNAVAILABLE` means this
platform does not offer it and the result says so rather than implying it.

**Output produced under weaker terms than requested is withheld.** If a
program asked for `ISOLATED` and the backend only reached `PROCESS`, the result
carries an `isolation_unavailable` failure and no stdout, stderr or value. A
model never sees bytes produced on terms it did not get.

Ask a backend before running anything:

```python
description = await sandbox.describe()
```

`ready`, `isolation`, `guarantees`, `mechanisms`, `problems` and
`limit_ceiling`: what this backend is, what it can prove, and what is wrong
with it if anything is.

`describe()` never runs model-written code; it runs a fixed probe. That is what
a health check in an operator console should call.

## Backends

```python
local_sandbox(
    isolation=IsolationLevel.PROCESS,
    python_bin=None,
    default_limits=None,
    env_allowlist=None,
    allow_same_uid=False,
)
detect_local_backends()  # name, isolation, available, reason — per platform
```

`local_sandbox` picks the strongest local backend that reaches at least
`isolation` and **raises `SandboxSetupError` with install guidance when nothing
does**. It never silently hands back something weaker.

| Platform | Backend | Reaches | Mechanism |
|---|---|---|---|
| Linux, with `bwrap` | `NamespaceSandbox` | `ISOLATED` | user/pid/net/mount namespaces, capabilities dropped, read-only system binds, tmpfs workspace |
| Linux, without | `SubprocessSandbox` | `PROCESS` | rlimits, process group, dropped uid, network namespace where unprivileged user namespaces allow it |
| macOS | `SubprocessSandbox` | `PROCESS` | seatbelt profile (filesystem confinement and network denial), rlimits, process group |
| Windows | `WindowsJobSandbox` | `PROCESS` | Job Object with process, memory, CPU and UI limits, kill-on-close, isolated workspace |
| any, with Docker/Podman | `ContainerSandbox` | `ISOLATED` | the container runtime's own namespaces and `--network=none` |
| any | `RemoteSandbox` | what the service reports | a sandbox service over HTTP |

Nothing is ever installed, downloaded or pulled implicitly. A missing `bwrap`
or a missing image is a configuration error you learn about from
`detect_local_backends()` or `describe()`, not on a customer's first request.

### Honest limits per platform

- **Linux without `bwrap`** cannot promise a filesystem boundary. It says so:
  `guarantees.filesystem` is `UNVERIFIED` unless the canary proves otherwise.
- **macOS** cannot cap memory (no address-space rlimit that CPython respects
  usefully), so `local_sandbox(isolation=ISOLATED)` refuses there and points at
  a container or a remote service.
- **Windows** runs the child as the worker's own account: there is no drop to a
  lesser identity, so `guarantees.identity` is `UNAVAILABLE` and the level is
  `PROCESS`. Containment of the process tree, memory, CPU and process count is
  a Job Object and is `ENFORCED`.

### Container

```python
ContainerSandbox(
    runtime=None,  # docker or podman, auto-detected
    image="python:3.12-slim",  # you pull or build it; never pulled implicitly
    container_python_bin="/usr/local/bin/python3",
    default_limits=None,
    env_allowlist=None,
    run_as=...,
    extra_run_args=(),
)
```

Prefer an image digest (`python@sha256:…`) where a deployment needs to be
reproducible.

### A sandbox service of your own

`RemoteSandbox` speaks a small documented HTTP protocol — see the module
docstring of `psych_runtime.sandbox.remote` for the four routes and the NDJSON
frames. Credentials come from a callback resolved fresh per request
(`SecretResolver` in a deployment), never from the Spec, and never appear in a
Record, a report, a trace, a URL or an exception.

Its failure semantics are deterministic, because arbitrary code must not be
re-run by accident: a non-200, a stream that ends before the program does, or a
malformed frame becomes a `provider_error` failure; a wall-clock timeout and a
caller cancellation each post a cancel and report `timeout` or `cancelled`.
**Only `describe()` retries.** An execution never does.

Anything else — a VM, a WASI runtime, a queue — only has to satisfy the
`Sandbox` protocol in `psych_runtime.sandbox.port`, which is a `run()` and a
`describe()`.

## Profiles, and how limits narrow

```python
from psych_runtime.sandbox.profiles import SandboxProfile, SandboxProfiles

profiles = SandboxProfiles(
    [
        SandboxProfile("default", local_sandbox()),
        SandboxProfile("heavy", container, hard_limits=SandboxLimits(wall_seconds=120)),
        SandboxProfile("fetch", container, allow_network=True),
    ]
)
runtime = psych_runtime.Runtime(..., sandboxes=profiles)
await runtime.verify_sandboxes()  # describe() each one at startup
```

The terms of one execution are the **intersection** of four things, in this
order, and each may only narrow:

1. what the backend can provide (`describe()`),
2. what the tenant's Policy allows (`code_execution_policy=` on the Runtime, a
   `CodeExecutionPolicy` that returns a grant no wider than the one it got),
3. what the `AgentSpec` asks for,
4. what the profile's `hard_limits` cap.

A deployment cap of 10 wall seconds and an agent asking for 60 gives 10. An
agent asking for 5 gives 5. `allow_network=False` on the profile and
`network=UNRESTRICTED` in the Spec gives a refusal, not network.

## Limits

```python
psych_runtime.SandboxLimits(
    cpu_seconds=10.0,  # CPU consumed, not wall clock
    address_space_bytes=512 * 1024 * 1024,
    file_size_bytes=10 * 1024 * 1024,
    process_count=64,
    wall_seconds=30.0,
)
```

In a Spec the same dimensions are requests, each optional, under
`CodeExecutionLimits` (`cpu_seconds`, `wall_seconds`, `memory_bytes`,
`file_size_bytes`, `process_count`). `None` means "whatever the profile
allows".

- `cpu_seconds` is CPU actually consumed. A program blocked waiting on a host
  binding's reply burns no CPU budget while it waits.
- `memory_bytes` is address space to a process backend, a cgroup to a
  container, and a job commit limit on Windows. On CPython an exceeded address
  space usually surfaces to the program as an ordinary `MemoryError`.
- `process_count` is a **per-user** limit on Linux, threads counted, across
  everything running as that user. That matters for concurrent runs.

`SandboxResult.limit_hit` names which cap ended an execution, or is `None` on a
clean completion or a plain program error no limit caused.

## The environment is built from nothing

No adapter starts from `os.environ` minus a blocklist. Each builds the child's
environment from `env_allowlist` plus the handful of variables it sets itself
(`HOME`, `TMPDIR`, `PATH`, `PYTHONDONTWRITEBYTECODE`). A credential nobody
explicitly allowed never reaches the child, no matter what gets added to this
process's environment later.

## Network

Off by default, and a grant has to come from both the profile and the Spec.

- **Namespace and container backends** deny by unsharing or `--network=none`, a
  kernel guarantee that does not depend on this process's privilege.
- **Subprocess and Windows backends** attempt denial and report whether it
  held. The child tests it directly and the grade is its answer, never an
  assumption.

Granted network access is **not** a bound HTTP client handed to the program.
Anything the program is meant to fetch goes through a binding that itself uses
the egress seam.

## Output, previews and spill

A program can print a gigabyte. None of it is held unbounded and none of it is
poured into the model's context.

```python
psych_runtime.OutputPolicy(
    preview_bytes=4_000,  # per stream, what the model sees
    max_bytes=8 * 1024 * 1024,  # per stream, what a backend captures at all
    preserve=psych_runtime.OutputPreservation.WHEN_AVAILABLE,
)
```

Each stream — stdout, stderr, the returned value, and each collected artifact —
becomes a `ResultAttachment` on the `ToolCallFinished` record: a name, a handle,
a content type, the byte size, whether it was truncated, and where it lives
(`inline`, `blob`, or `preview_only`). The model's payload gets a deterministic
head-and-tail preview plus the handle, and reads the rest with
`read_tool_output` — by line window or by regular expression — so a large output
is paged, never re-inserted whole. See **psych-blobs** for handles, tenancy and
retention.

`preserve=REQUIRED` with no `BlobStore` wired fails the call loudly rather than
quietly dropping bytes. `WHEN_AVAILABLE`, the default, keeps the preview and
records how many bytes were let go.

Files the program writes to its workspace come back the same way when
`ArtifactPolicy.collection` is `COLLECT`, **by workspace-relative path**. Host
paths never appear, and symlinks, junctions and other reparse points are never
followed and never collected.

### Why a program instead of more tools

A model that can write a program can loop, filter, join and aggregate without a
round trip per step — one call and one result instead of a call per row. That
is the context saving, and `benchmarks/` measures it rather than asserting it.
The cost is that a program is arbitrary code, which is exactly why process
isolation is not optional.

## Results

```python
result.stdout, result.stderr  # decoded previews
result.stdout_data, result.stderr_data  # captured bytes
result.stdout_size, result.stderr_size  # bytes produced, even if truncated
result.value  # what the top-level code evaluated to
result.failure  # a SandboxFailure, or None
result.artifacts, result.artifacts_omitted
result.duration_seconds, result.limit_hit, result.cancelled
result.isolation, result.guarantees, result.network_denied
```

`value` is `None` both when the program returned nothing and when it genuinely
returned `None`. The two are not distinguished, because JSON does not
distinguish them either.

## Gotchas

- **`run_code` is a reserved tool name.** A Spec cannot claim it.
- **A binding must be a granted tool.** Publishing fails if `bindings` names
  something outside `tools`.
- **An unknown profile name fails at publish** when the Runtime knows its
  profiles, and at Attempt startup otherwise. It is never silently ignored.
- **Bindings go through Policy.** A program calling a binding for a destructive
  tool suspends for approval exactly as a direct call would, and the program is
  torn down rather than parked mid-execution.
- **Cancellation kills the tree.** Timeout, interrupt or caller cancellation
  terminates the process group or Job Object; no child, grandchild or listener
  survives an execution.
- **Container and `bwrap` tests skip where the runtime is absent.** A skip is
  not evidence of support; the cross-platform matrix in CI runs the shared
  contract suite on Linux, Windows and macOS.
