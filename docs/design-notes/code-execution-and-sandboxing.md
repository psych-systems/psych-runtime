# Code execution and the sandbox

Covers DESIGN.md §18. Read it before touching `psych_runtime/sandbox/`.

A model writes one program. The runtime executes it in isolation and returns what
it printed and what it returned. That is the whole contract, and the rest of this
document is what "in isolation" has to mean before it is worth saying.

## The contract

**One program per run, no state between runs.** Each execution gets a fresh
process. There is no session, no kernel and no variable surviving into the next
program. A persistent-kernel mode is a different product; not having one is what
makes crash recovery trivial.

**Host bindings route through the normal tool path.** The program calls
host-provided functions as ordinary calls, and those calls go back through the same
policy, egress and recording every other tool call goes through. There is no
privileged shortcut and no fast path for any binding, however the program uses it.

**A binding is any tool the agent can currently call.** Registered Python
tools, HTTP tools, and every tool a granted MCP server or A2A peer offers this
tenant right now. Not a copy of a catalogue: the set is resolved at each turn
boundary from the same live catalogue the model's own tool list comes from, so
a tool withdrawn between turns stops being callable from a program in the turn
it stops being callable directly. Deriving the set from the Spec instead —
which is what this replaced — meant an MCP tool could never be bound at all,
because MCP tools are not in the Spec to enumerate; and pinning it once per
attempt would have kept access the tenant had taken away.

**Failures are data.** A traceback comes back as a field on the result so the model
can read it and fix the program. It is not an exception that kills the turn. The
result's error carries a kind from a closed set, which names both what the program
did (an exception, a timeout, an abort, a worker exit, invalid output, an output
limit) and what the deployment could not do for it (isolation unavailable, network
not allowed, backend not ready, policy refused, cancelled, a provider error,
artifacts unavailable). Each kind carries its own guidance, because "it failed"
sends a model back to rewriting a program that was never the problem. The `run` call
itself raises only for misuse of the seam, such as submitting a run after disposal,
and for a configuration error such as a sandbox profile nothing is registered under.

## In-process sandboxing is rejected

`RestrictedPython`, `exec` with trimmed builtins and AST filtering are all
escapable. Process isolation is the floor, and it stays the floor even though both
sides of the boundary happen to speak Python. Sharing a language removes a
marshalling problem. It removes nothing about containment.

## Making top-level await work

Do not `exec` the program text directly. Parse it, wrap the parsed module body in a
synthetic async function, anchor the wrapper's source position on the program's first
real statement so line numbers in a traceback still point at the model's own code, and
compile with a filename that marks the frames as the model's.

Compile with inherited compiler flags disabled. Otherwise the bootstrap module's own
`from __future__` imports leak into the compiled program and change what a legal
program observes at run time, for example by stringifying its annotations.

That filename marker is then what the traceback filter uses: on an uncaught exception,
drop every frame whose filename is not the model's, so the model sees its own program
and not the bootstrap's plumbing.

## The wire protocol

Frames are newline-delimited JSON on a dedicated file descriptor, not on stdout.
Stdout and stderr stay entirely free for the program's own printing, which is captured
separately as logs and never mixed into the protocol channel.

The frame vocabulary is small:

| Direction | Frame | Purpose |
|---|---|---|
| child to host | ready | limits applied, plus what the child observed about its own containment: whether it can reach the network, whether it can read a file placed outside its workspace, its identity and its platform |
| host to child | run | the program text, sent only after `ready` |
| child to host | call | one host-binding invocation |
| host to child | reply | answers exactly one call, by id |
| child to host | done | the final value or error, ending the run |

The `run` frame also carries the names of the tools this execution may call.
In the frame rather than on the child's command line, because an agent
connected to a large MCP server may hold hundreds of them and every operating
system caps a command line somewhere different — a limit that would have turned
"this agent has many tools" into "the sandbox will not start", on one platform,
at an unpredictable size.

A `reply` that carries a failure carries a **kind** as well as a message. A
program handed one sentence can only tell a refusal from an outage by matching
on prose; a program handed a kind can branch. "A human would have to approve
this", "the server is unreachable", "the tool ran and returned an error" and
"this program has spent its call budget" are four different next moves.

Stdout and stderr are not frames. They are the child's own streams, read and
capped separately, so a program printing megabytes never competes with the
protocol for the channel and can never forge a frame by printing one.

The channel itself is whatever the backend can give the child: an inherited
file descriptor where processes inherit them, a bind-mounted Unix socket
where the child is in its own mount namespace or container, and a loopback
socket with a one-time token where neither is available. The child-side
bootstrap is the same source in every case.

**Call ids are consecutive from zero with no gaps, claimed only after a successful
write.** The host answers a call only when its id is the exact successor of the last
one, which bounds the state the host retains to a single number. A frame that never
reached the host must therefore not consume an id, so the counter advances only once
the write succeeds. The whole claim, write and advance sequence runs under a lock,
because the model's program can spawn a thread that makes its own binding call
concurrently.

**Inbound frames from the child are hostile input.** The model's program has full
access to the protocol descriptor and can forge any frame: junk fields, missing
required ones, a non-finite call id. A declared union type means nothing on that
channel. Every accepted frame is **rebuilt field by field** rather than cast, so forged
extras never ride along, a nonsense call id can never be echoed back into a reply, and
malformed input is dropped rather than thrown.

Pydantic can give you this, with strict per-field validation and extra fields forbidden
and no coercion. It gives it only if you choose it deliberately. Parsing JSON and
handing the dict to a model with default settings is casting with extra steps.

**Meter and cap bytes before parsing, never after.** A cap enforced on the decoded value
has already paid for decoding an unbounded payload.

**Impossible configuration fails at construction, not at the first affected run.** If a
configured output cap could exceed what a single protocol frame can carry, reject the
configuration at load. Otherwise a payload the cap admits but the frame cannot carry
degrades a clean output-limit failure into an opaque process exit, months later, in
production.

## What the subprocess actually enforces

Applied inside the child, immediately after the boot handshake and before the program
is compiled. Order matters.

1. **Core dumps off, first.** A CPU-limit signal's default disposition dumps core, and
   the child inherits the host's core limit. Disabling it after arming the CPU limit is
   too late, and the cost is a large memory-bearing file written into the workspace on
   every timeout.
2. **CPU limit**, soft at the configured budget and hard one second above it. The soft
   limit's signal terminates the child and is classified as a timeout. The hard limit is
   a backstop for a program that traps the soft signal and keeps burning CPU.
3. **Address space**, on platforms where it can be set at all. Some platforms map
   multi-gigabyte shared caches into every process at exec, so any practical cap sits
   below current usage and the kernel refuses it; there the CPU limit and the host's
   wall-clock timeout are the containment. Detect and skip rather than failing every run.
4. **File size and process count.** Neither falls out of the above and both are needed:
   without them a program can fill a disk or fork until the host cannot schedule.

Every limit is **clamped against what the child inherited, never raised**. A requested
soft limit above the inherited one would *raise* the effective limit, which is the
opposite of containment. Take the stricter of requested and inherited, on each side
independently, then reconcile the soft-below-hard constraint.

## Environment, interpreter and working directory

**The environment is an allow-list built fresh**, not a copy of the host's with some keys
deleted. A deny-list reliably misses whatever the next credential-shaped variable turns
out to be. In practice the allow-list is one entry, the temp directory, because some
interpreters warn without it.

**Resolve the interpreter to one absolute path at load time**, not per run. The child gets
no `PATH`, so a bare name would fall back to the platform default and miss an interpreter
that lives only on the caller's `PATH`. A name that cannot be resolved at load fails
configuration outright rather than silently resolving to something else at spawn time.

**Run the interpreter in isolated mode**, which ignores its environment variables, skips
user site-packages and keeps the script's own directory off the import path. Force
unbuffered output so raw writes reach the host's capture before teardown.

**Give the child a temporary working directory of its own.** Scrubbing the environment and
withholding filesystem bindings is not the same as controlling where the process starts.
Inheriting the host's working directory is a gap, not a simplification.

**Network access is off by default**, and when granted it passes through the same egress
seam as everything else. This needs a real mechanism, such as an unprivileged network
namespace or a firewall rule scoped to the child. Interposing on a library's TLS
verification is not a network boundary, and neither is hoping the program has no socket
module.

## Teardown

Spawn the child **in its own process group** (`start_new_session=True`), so signalling the
group reaches subprocesses the model's program spawned, not only the interpreter.

Teardown is SIGTERM, then a grace window, then SIGKILL, aimed at the process group rather
than the direct child. Close the child's stdin immediately after spawn, so a descendant
that escapes the group cannot hold a host-side handle open past the run: it sees EOF
rather than a live pipe that would block the host from exiting.

The kill timer must not keep the host process alive on its own.

## Defaults

The subprocess adapter's shipped defaults are a starting point rather than a law, and they
are chosen so that the common case never sees them: a CPU budget in the tens of seconds, a
wall-clock cap in the minutes, an address space in the hundreds of megabytes, log and
return-value caps in the tens of kilobytes, and a teardown grace of a few seconds. A
consumer with a different workload changes them; a consumer who does not think about them
gets containment anyway.


## Levels, and saying only what was proved

A backend does not describe itself with a marketing word. It reports two
things: the level it reached, and a grade for each guarantee separately.

| Level | Means |
|---|---|
| `isolated` | a kernel boundary the worker's own privilege does not underwrite: namespaces, a container, or a service that provides one |
| `process` | a separate process with resource limits, a scrubbed environment, a private workspace and a killable tree, but no kernel boundary |

The ten guarantees — filesystem, network, process tree, identity, CPU, memory,
file size, process count, wall clock, environment — are each `enforced`,
`unverified`, or `unavailable`. A mechanism that was requested and not
contradicted is `unverified`, which is a weaker claim on purpose. A platform
that cannot offer the guarantee at all says `unavailable` rather than leaving
it implied.

The asymmetry of the evidence matters more than the grades. A canary file
written outside the workspace that the child *can* read proves exposure, and
the grade is `unavailable`. The same file failing to open proves only that one
path was out of reach: under a profile that allows reads generally and then
denies particular subtrees, everything not named is still readable. So an
unreadable canary earns `unverified`, and `enforced` is kept for a backend
whose boundary makes the question moot — a mount namespace, a container — where
nothing of the host is there to read. The network probe is symmetric in the
same way and is run over both address families, because a host with IPv6
routed and IPv4 unrouted is not a host without a network.

The level follows from the grades rather than being asserted alongside them, so
there is one function and no second opinion.

**A request that cannot be met stops the program before it runs.** The
handshake has two phases for this reason. The child connects and reports what
it observes about its own containment; only then does the host send the
program, and a host that does not accept the report sends an abort instead, so
nothing is compiled and nothing executes. A refusal issued after execution
would be theatre: a program that has run has already read what it could read,
reached what it could reach and written what it wrote, and none of that is
undone by discarding its output.

The same rule crosses the wire. An adapter for a remote service reads that
service's description before sending a program and refuses locally when what
it advertises is not enough; a service asked for terms it cannot meet must
answer with an error before executing rather than after.

Output produced under weaker terms is still withheld, with an
`isolation_unavailable` failure, because a backend can misreport itself. That
is the backstop. It is not the control, and a backend that reaches it is a
backend to stop using.

## Four narrowings, never a widening

The terms of one execution are the intersection of what the backend can do,
what the tenant's policy allows, what the Spec asks for, and what the
deployment profile caps. Each stage may only narrow. This is the same rule the
rest of the library follows for tool access, and it is enforced the same way:
by intersecting grants rather than by trusting each stage to behave.

A Spec therefore carries a profile **name** and a set of requests. It never
carries a backend, a client, a credential, a host path or a callable, because a
Spec is data that is hashed, stored and replayed, and none of those things
survive that. A deployment maps the name to an implementation. A name nothing
is registered under is a deployment error and stops the attempt loudly; a
backend that cannot meet a request is a refusal returned to the model as data,
so an agent whose sandbox is unavailable keeps working with its other tools.

### What a program is given

Three names and one function per tool:

- `call_tool(name, arguments)` reaches every offered tool by its model-facing
  name. It has to exist, because a tool name is not a Python identifier: MCP
  servers legitimately offer `list-repos`, `2fa` and `class`. The target name
  travels as the frame's own `name` field, so the protocol's check that a call
  names a tool actually offered to this execution still applies to it.
- a direct alias for each name that *is* a usable identifier, because
  `await search(query=...)` is what a model writes without being told twice.
  Deliberately never a mangled alias: two servers can offer `list-repos` and
  `list_repos`, and mangling would make one silently answer for the other.
- `TOOLS`, the names, so a program can look before it calls, and `ToolError`,
  carrying the host's `kind`.

### What a program may not call

The rule is capability, not origin. A tool is bindable when it takes a
JSON-shaped request, completes with a JSON-shaped result, runs inside the
execution's own limits, and **never suspends the Run**. The last one decides
everything interesting: a suspension is a Run stopping and later resuming into
a *turn*, there is no turn inside a subprocess, and nothing can resume into
one. So a call that would suspend is refused before it runs — the only refusal
that prevents anything — and the model is told to make it directly.

That excludes `ask_question`, an MCP elicitation, and any approval-gated tool.
It also excludes the Psych built-ins that are not questions of capability at
all: `delegate` and the composed-subagent tools start Runs that outlive the
execution, `remember`, `forget`, `update_tasks`, `load_skill` and
`show_component` act on the model's own context or on the person in the
conversation. `list_tools`, `get_tool_info` and `read_tool_output` are allowed:
the first two are bounded read-only catalogue lookups and the only way to learn
a deferred server's argument shapes, and the third is how a program reads a
result too large to hand it in one piece (below). `read_tool_output` was
previously excluded as redundant — "a program already holds its own bytes" —
which was true in every case except the one that matters, the case where the
bytes were too large to give it.

### A program's work is budgeted too

A program compresses model turns. It must not also compress the host's work:
one `run_code` call could otherwise issue a million MCP calls, or assemble a
gigabyte of arguments, without the model paying a turn for any of it. So every
binding call is counted and measured — calls per execution and per Run,
argument bytes per call, result bytes per call, total bytes per execution —
against a budget the **deployment** owns on the profile.

The per-Run count is read from the Run's folded log, not accumulated in the
process. A Worker can die holding a lease and another can reclaim the same
Attempt; an allowance kept in memory would reset exactly there, so a Run that
crashed its way through ten programs would get ten fresh allowances and the
per-Run cap would bound nothing. The fold counts a call at its
`tool_call_started` — the point where the host accepted it — so a call that
then failed, was refused, was aborted, or was left dangling by the crash has
spent the allowance just the same, and nothing is given back on recovery.

### A result too large to send, and the three honest answers

The per-call result ceiling raises a question of its own: what a program gets
when the tool it called returned more than that. Truncation is the one answer
that cannot be allowed — a program that computes over a silently shortened
result returns a confident wrong number, and nothing downstream can tell. So
there are three answers and they are kept apart:

- **Under the ceiling**: the whole result, as before.
- **Over it, and preserved under a handle**: an opaque descriptor naming the
  stored result, which the program reads in bounded windows or searches by
  pattern through `read_tool_output` — on the host, against the Run's own log
  or `BlobStore`, under the same call and byte budgets as any other binding
  call, and recorded under the same parent `run_code`. Nothing about the
  storage crosses the boundary: no key, path, URL, client or credential, only
  a handle that is meaningless outside the Run that minted it. The handle is
  scoped tighter still — a program may read only what its *own* calls
  produced, so a handle from a direct model call, from an earlier program, or
  from another Run is refused even though the first of those would resolve.
- **Over it, and not preserved**: refused with `binding_result_not_paged`,
  which says exactly that rather than pretending to an answer.

The distinction between a result and a handle is carried **on the reply
frame**, never by a key inside the value. A tool may legitimately return a
dictionary containing any field name at all, including whichever one a
marker-based scheme picked; a host that decided what a value *was* by
looking inside it would hand that program a paging object instead of its
data, silently, and only for the results whose contents happened to
collide. The host returns a `ResultHandle` — a type nothing a tool returns
can be — and the protocol says which of the two the frame carries.

In the child the descriptor becomes a `ToolResultHandle` that refuses to be
indexed, iterated, measured or `.get()`-ed. `len(rows)` on a reference is the
shape of the mistake this is guarding against, and raising there is what turns
a silent wrong answer into a failure the program can catch and act on.

The deployment rather than the Spec, deliberately. A denial-of-service question
about somebody's host is not an agent author's to answer, and a Spec field
would put the answer inside the Version hash, where changing it means
republishing every agent. A tenant policy may narrow it further; nothing can
raise it.

The budget is enforced host-side, around the callback every backend reaches its
bindings through, rather than inside the framed wire protocol. A counter in the
protocol would be a counter the remote backend does not have, because that one
speaks NDJSON over HTTP and never runs that code.

### What the program sees that the model does not

A large tool result is elided for the model, and above a threshold moved to the
`BlobStore`, so it does not fill a prompt. A program receives it **whole**:
reducing it is the reason the program was written, and handing it the elided
view would make it filter nothing and return a confident wrong answer. This is
the asymmetry the whole feature is for, and it is the reason a binding's return
value comes from what the executor produced rather than from the record written
beside it — an offloaded record's `result` field is empty by that record's own
rule.

## What each platform can actually do

Nothing is installed, downloaded or pulled implicitly, so what a host can offer
is what is already on it. That is discoverable before a customer's first
request rather than during it.

- **Linux with unprivileged user namespaces and `bwrap`** reaches `isolated`:
  user, pid, ipc, uts, mount and network namespaces unshared, every capability
  dropped, system directories bound read-only, a tmpfs workspace, and the
  child dying with its parent. Selection runs `bwrap` rather than looking for
  it: a distribution that ships the binary and forbids unprivileged user
  namespaces would otherwise be handed an isolated backend whose every
  execution fails at spawn, which is a worse answer than an honest weaker one.
- **Linux without it** reaches `process`: rlimits, a process group, a dropped
  uid where the worker is privileged enough to drop one, and a network
  namespace where an unprivileged user namespace permits one. There is no
  filesystem mechanism at all here, so that guarantee is `unavailable`.
- **macOS** reaches `process`: a generated seatbelt profile confines the
  filesystem and denies the network, and rlimits cap CPU, file size and
  processes. The profile allows reads generally and denies named subtrees, so
  its filesystem guarantee is `unverified` rather than `enforced` however the
  canary comes back. It cannot usefully cap memory either, so a request for
  `isolated` is refused there rather than approximated.
- **Windows** reaches `process`: a Job Object contains the whole tree with
  active-process, job-memory and job-CPU limits, kill-on-close so no
  descendant outlives the handle, and UI restrictions. The child runs as the
  worker's own account — there is no drop to a lesser identity — so identity is
  `unavailable` and the level says so.
- **A container runtime** reaches `isolated` through its own namespaces and
  `--network=none`. The channel is a bind-mounted Unix socket, so the *worker*
  must be POSIX; a Windows worker is refused outright rather than silently
  falling back.
- **A remote service** reports its own description, and it is trusted only as
  far as the same withholding rule allows: a service that claims less than was
  asked for gets its output withheld like any other backend.

## Threat model

The program is assumed hostile. Not "might contain a bug" — written by
something that will do whatever the prompt that reached it asked for.

**Escaping the workspace.** Traversal, absolute paths, symlinks, hard links,
junctions and other reparse points are all the same attack with different
spelling. The workspace is a fresh private directory per execution; artifact
collection walks it without following any link, resolves each candidate and
keeps only regular files whose resolved path is still inside; cleanup does the
same in reverse, clearing read-only attributes rather than following a link out
of the tree to delete something else. Where a kernel boundary is available the
question does not arise; where it is not, the canary says so honestly.

**Reading the host's secrets.** The environment is built from nothing —
allow-list plus the handful of variables the adapter sets — so a credential
nobody named never reaches the child, whatever gets added to the worker's
environment later. Provider credentials for a remote backend are resolved per
Scope, per request, and are never written into a Spec, a Record, a report, a
trace, a URL or an exception message.

**Reaching the network.** Denial is a mechanism and a check, never an
assumption: unshared or `--network=none` where the kernel provides it, and a
child-side probe that reports what it found either way. Granting network does
not hand the program an HTTP client; anything it is meant to fetch goes through
a binding, which goes through the egress seam, which is the one place a
destination is decided. That closes SSRF, loopback and metadata-service
reachability at the same seam as everything else rather than in the sandbox.

**Exhausting the host.** CPU, wall clock, memory, file size, process count and
output size all have caps, and every one of them is clamped against what the
child inherited rather than raised. A fork bomb meets the process-count limit;
an output flood meets the capture cap, and the reader keeps draining so a full
pipe never deadlocks the host instead of being throttled by it.

**Surviving teardown.** Timeout, cancellation, a crashed worker and a normal
exit all end the same way: the process group or Job Object is terminated, not
the direct child. A descendant that detached still dies, because it was never
outside the group. Cancellation is honoured before start, during execution and
in the middle of a binding, and each produces a coherent record rather than a
dangling call.

**Getting privilege through a binding.** A binding is the same tool the model
could have called directly. It carries the same Scope, Policy, approvals,
egress and recording, and the sandbox package has no import path to a tool
registry, so there is no back door to close — there is no door. A binding the
Spec does not grant fails publication.

**Reading someone else's output.** An attachment handle resolves against the
Run's own recorded handles, with its slot prefix checked, and the blob address
behind it folds in the tenant. A fabricated handle, or a real handle from
another Run or another tenant, is denied rather than served.

**Lying about the protocol.** Everything the child sends is hostile input: it
holds the descriptor and can forge any frame. Frames are rebuilt field by
field, bytes are metered before parsing, and a malformed frame ends the
execution as a protocol error rather than being interpreted. A remote service
is treated the same way rather than as infrastructure: a single frame and a
whole response stream each have a ceiling here, because the limits it was sent
are a request to it and not a property of it. The ceiling is applied to the
bytes as they arrive, not to assembled lines: a line that never ends is
already allocated by the time anything can measure it. Bytes are not the only
budget. A stream can stay well inside them and still be millions of lines, or
millions of empty ones, so lines and blanks are counted against ceilings of
their own and the failure names whichever was spent. The reader also hands the
event loop back every few hundred lines: parsing is synchronous, and a reader
that runs a whole hostile chunk without yielding has disabled the wall-clock
timeout that was supposed to end the stream. Its credential is refused over
plaintext unless the service is on loopback or the operator said explicitly
that the path is trusted.

**A pattern as a denial of service.** One tool argument in this library is an
executable language the model writes: the regular expression `read_tool_output`
searches with. Python's engine backtracks, so a pattern of a dozen characters
can run for longer than anyone will wait, and neither shortening the subject
nor moving the call to a thread bounds it — the first only changes the
exponent, and the second cannot be interrupted, so enough of them exhaust the
pool and take every other Run with them. The match therefore runs in a child
process with a deadline, and the deadline is enforced by killing it from
outside — the child cannot police itself, because a backtracking match holds
the interpreter lock and a watchdog thread inside it never wakes. The deadline
is wall clock from spawn to exit, and that is the only bound claimed: from
outside the process there is no way to tell a second spent matching from a
second spent starting Python, so there is no separate matching limit to
enforce. Ten seconds is sized so that neither reading refuses ordinary work.

**Replaying arbitrary code.** Nothing retries an execution. A remote provider's
timeout, disconnection, malformed answer or error is reported as a failure with
its own kind; only the capability description, which runs no model-written
code, is retried.

**Supply chain.** No new runtime dependency is added for any of this, no binary
is fetched and no image is pulled. `bwrap`, a container runtime and an image
are things an operator installs deliberately and the library detects; an image
digest is available where a deployment needs reproducibility.

## Output has a budget, not a truncation

Two numbers, because they answer different questions. The capture cap bounds
what this process will hold at all, per stream. The preview budget bounds what
reaches the model, per stream, as a deterministic head and tail with the
omitted middle counted. Between them sits the durable copy: each stream, the
returned value and each collected artifact becomes a named attachment with its
own handle, kept inline when small, in the blob store when not, and as a
preview alone when there is no blob store — unless the agent said preservation
is required, which fails the call rather than losing bytes quietly.

The model then reads what it needs by window or by pattern. That is the point
of the whole arrangement: one program that loops, filters and joins costs one
call and one bounded result, where the same work as tool calls costs a round
trip and a full result per step.

## The container backend


The container adapter cannot be exercised where no container runtime exists. Its tests skip
in that environment and say so, and run in CI. The exit-code and out-of-memory conventions
it reads are documented runtime behaviour rather than something verified locally, which is
worth remembering when one of them changes.
