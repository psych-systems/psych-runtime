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
  tool the model could have called directly, reached a different way — and that
  now includes every MCP tool and A2A skill the agent is currently granted, not
  only the ones written into the Spec.
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

## What a program may call

`bindings=None` — the default — means **every tool this agent can currently
call**, after every narrowing plane: registered `CodeTool`s, `HttpTool`s, and
whatever each granted MCP server and A2A peer offers this tenant right now. A
tuple narrows that set and can never widen it.

The set is resolved **at every turn boundary**, from the same catalogue the
model's own tool list comes from. A tenant policy that withdraws a tool stops a
program calling it in the same turn it stops the model calling it.

A tool an MCP server offers is bindable **even when its catalogue is deferred**
out of the prompt. That is the case code execution is best at: a program pays
nothing for a schema it never reads, so a 351-tool server is exactly where
looping beats forty turns.

### Inside the program

```python
rows = []
for sku in ("A", "B", "C"):
    rows.append(await support__lookup(sku=sku))  # a direct alias
raw = await call_tool("docs__list-repos", {"owner": "psych"})  # any name at all
return {"count": len(rows)}
```

- `call_tool(name, arguments)` reaches **every** offered tool by its
  model-facing name. It exists because a name is not an identifier: MCP servers
  legitimately offer `list-repos`, `2fa` and `class`.
- A **direct alias** exists for each name that *is* a usable identifier.
  Never a mangled one: two servers can offer `list-repos` and `list_repos`, and
  mangling would make one answer for the other.
- `TOOLS` is the sorted tuple of names, so a program can look before it calls.
- `ToolError` is raised when a call does not produce a result, carrying `.kind`
  and `.tool`. Branch on `.kind`, never on the message.

### Kinds a program can branch on

| `.kind` | What happened |
| --- | --- |
| `approval_required_in_program` | A human would have to approve it. Call it directly. |
| `input_required_in_program` | The far side wants more input. Call it directly. |
| `binding_not_available` | No tool of that name is callable by this Run now, including a built-in that programs are never offered. |
| `repeated_call` | The same call, arguments and answer, over and over. |
| `binding_calls_exhausted` (and `…_for_run`) | The call budget ran out. |
| `binding_arguments_too_large` | The arguments were over the per-call ceiling. |
| `binding_result_too_large` | A `read` window was wider than the ceiling. Ask for fewer lines. |
| `binding_result_not_paged` | Over the ceiling and not preserved, so there is nothing to page. |
| `binding_handle_not_yours` | A handle this program's own calls did not produce. |
| `binding_produced_bytes_exhausted` | The tools this program called produced too much. |
| `binding_preserved_bytes_exhausted_for_run` | This Run's programs have stored too much. |
| `result_not_inline` | A handle was used as if it were the result. Call `.read()`. |
| `binding_traffic_exhausted` | The execution's total byte budget ran out. |
| `McpToolError` | The tool ran and returned an error. |
| `McpServerUnreachable` | The server is not there. The tool never ran. |

### Built-ins, and why most are excluded

`list_tools` and `get_tool_info` are callable from a program — bounded,
read-only, and the only way to learn a deferred server's argument shapes.
`read_tool_output` is callable too, and is how a program reads a result too
large to hand it in one piece; it resolves only handles this program's own
calls produced.
Everything else Psych registers is refused, each for its own reason
(`psych_runtime.core.code_execution.EXCLUDED_BUILTINS` holds the sentence):
`ask_question` suspends the Run and nothing can resume into a subprocess;
`delegate`, `spawn_subagent`, `check_subagent` and `message_subagent` start or
address child Runs that outlive the execution; `remember`, `forget`,
`update_tasks`, `load_skill` and `show_component` act on the model's own
context or on the person in the conversation; `read_tool_output` is redundant,
because the program already holds its own bytes.

### Budgets

A program compresses model turns; it does not get to compress the host's work.
The **deployment** owns the numbers, on the profile:

```python
SandboxProfile(
    "default",
    local_sandbox(),
    binding_budget=psych_runtime.BindingBudget(max_calls=200, max_total_bytes=8 << 20),
)
```

Five things are metered, and they are not the same thing counted five ways:
calls, *transfer* (bytes that crossed into the sandbox), *produced* (bytes
the host's tools made, whether the program received them or not), *preserved*
(bytes still sitting in the log or the BlobStore afterwards), and the
per-call ceilings on arguments and results. A result replaced by a handle
transfers about a hundred bytes and may have cost the host megabytes of
fetching and storage, so metering only the transfer would price it at the
size of its handle.

Per execution and per Run, so a model cannot buy a fresh allowance by writing a
second program. The per-Run half is counted from the Run's own log rather than
in memory, so a Worker that dies mid-program does not hand the next Attempt a
fresh allowance either: a call is spent when its `tool_call_started` record is
written, whether it then succeeded, failed, was refused, was aborted, or was
left dangling by the crash. A `CodeExecutionPolicy` may narrow them per tenant; a Spec
cannot raise them, and does not carry them at all — a denial-of-service
question about somebody's host is not an agent author's to answer, and a Spec
field would put the answer in the Version hash.

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

`ENFORCED` means the execution observed the guarantee hold. `UNVERIFIED` means
the mechanism was requested and nothing contradicted it. `UNAVAILABLE` means
this platform does not offer it and the result says so rather than implying it.

The canary is the clearest case of the difference. The host writes a file
*outside* the workspace and the child reports whether it could read it. Reading
it proves exposure, so that grade is `UNAVAILABLE`. Failing to read it proves
only that this one file was out of reach — under a profile that allows reads
generally and denies specific subtrees, most of the filesystem is still
readable — so that grade is `UNVERIFIED`, and only a backend with a real
boundary (a mount namespace, a container) reports `ENFORCED`.

**A program that asked for more isolation than the backend provides is never
run.** The child reports its containment before the program is sent, and a
host that does not accept what it sees sends an abort instead: the result
carries an `isolation_unavailable` failure and nothing executed. Withholding
output afterwards is the backstop for a backend that misreported itself, not
the mechanism — a program that has run has already read what it could read and
reached what it could reach, and discarding its output does not undo that.

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

- **Linux without `bwrap`** cannot promise a filesystem boundary at all, so
  `guarantees.filesystem` is `UNAVAILABLE`. **macOS** confines reads with a
  seatbelt profile but cannot prove the confinement from one canary, so it is
  `UNVERIFIED`. Neither reports `ENFORCED`, and neither can reach `ISOLATED`.
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
Record, a report, a trace, a URL or an exception. A credential is refused over
plaintext `http://` unless the host is loopback or the caller passes
`allow_insecure_http=True` to say the network in between is trusted.

The same "refuse before running" rule applies across the wire, on both sides:
the adapter reads the service's description first and does not send a program
to a service that already says it cannot meet the terms, and a service that is
asked for terms it cannot meet must answer with an error frame *before*
executing. Responses are bounded here too — a single frame and a whole stream
each have a ceiling — because a limit the other side enforces is a request.
The bound is applied to bytes as they arrive rather than to assembled lines:
a reader that hands back whole lines has already allocated an endless one by
the time its length can be measured. Size is not the only budget either — the
number of lines and the number of *empty* lines have their own ceilings, since
a stream can sit well inside the byte ceiling and still be millions of them —
and the reader hands the event loop back every few hundred lines, because
parsing is synchronous and a reader that never yields has disabled the very
timeout meant to end a hostile stream.

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
  held. The child tests it directly, over IPv4 *and* IPv6, and the grade is
  its answer rather than an assumption: a host with v6 configured and v4
  unrouted still has a route to the world.

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
- **A binding must be something the agent holds.** Publishing fails if an
  explicit `bindings` entry is neither a granted tool, a callable built-in, nor
  addressed to a connected MCP server or A2A peer. Whether that server still
  *offers* it is not checked at publish — publication must not depend on
  somebody else's uptime — so a name that no longer resolves comes back in the
  `run_code` result as `unavailable_bindings` and is never quietly replaced
  with a different tool.
- **An unknown profile name fails at publish** when the Runtime knows its
  profiles, and at Attempt startup otherwise. It is never silently ignored.
- **Bindings go through Policy.** A program calling a binding for a
  destructive tool is refused with `approval_required_in_program` **before the
  tool runs** — the only refusal that prevents anything — and told to call it
  directly. Nothing can ask a person while a subprocess waits.
- **Cancellation kills the tree.** Timeout, interrupt or caller cancellation
  terminates the process group or Job Object; no child, grandchild or listener
  survives an execution. An abort is noticed at the boundaries the host
  controls: before the start, and around a binding call — where it also
  cancels the in-flight operation. A program doing nothing but arithmetic is
  not interrupted mid-loop; it ends at its wall clock, which is why the wall
  clock is the limit that always applies.
- **A program's calls are not replayed.** A Worker that dies with a binding
  call in flight records `UNKNOWN` and the next Attempt does not re-run it,
  exactly as for a direct call. Somebody else's MCP tool is not known to be
  idempotent because a program made the call.
- **The program sees more of the result than the model, and sometimes a
  handle to it.** A large tool result is elided, and above the offload
  threshold moved to the `BlobStore`, for the *model's* context. A program
  receives the whole thing whenever it fits the per-call transfer ceiling —
  filtering it down is why the program was written. Above that ceiling it
  receives a `ToolResultHandle` instead and reads the result in bounded
  windows on the host (`await handle.read(offset=…, limit=…)`, or
  `pattern=…` to search); the call itself is still recorded as the success it
  was. A window wider than the transfer ceiling is refused with
  `binding_result_too_large` rather than turned into a second handle: the
  program asked for too much of something it is already reading, and what
  it needs to hear is to ask for less. A result over the ceiling that was not preserved under a handle is
  refused with `binding_result_not_paged` rather than truncated — a program
  computing over a silently shortened result is the one wrong answer nobody
  downstream can detect.
- **Container and `bwrap` tests skip where the runtime is absent.** A skip is
  not evidence of support; the cross-platform matrix in CI runs the shared
  contract suite on Linux, Windows and macOS.
