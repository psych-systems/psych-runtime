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

**Failures are data.** A traceback comes back as a field on the result so the model
can read it and fix the program. It is not an exception that kills the turn. The
result's error carries a kind from a closed set: exception, timeout, abort, worker
exit, invalid output, output limit. The `run` call itself raises only for misuse of
the seam, such as submitting a run after disposal.

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
| host to child | boot | resource limits and the declared binding namespaces, sent first |
| child to host | boot ack | limits applied, ready to run |
| host to child | run | the program text, sent only after the ack |
| child to host | call | one host-binding invocation |
| host to child | reply | answers exactly one call, by id |
| child to host | log | one captured output chunk, streamed as it arrives |
| child to host | done | the final value or error, ending the run |

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

## The container backend

The container adapter cannot be exercised where no container runtime exists. Its tests skip
in that environment and say so, and run in CI. The exit-code and out-of-memory conventions
it reads are documented runtime behaviour rather than something verified locally, which is
worth remembering when one of them changes.
