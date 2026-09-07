# psych_runtime.sandbox

## Owns
The `Sandbox` port (`port.py`), the subprocess adapter (`subprocess.py`) and
the container adapter (`container.py`), the framed host-binding wire protocol
both adapters speak (`protocol.py`), the child-side bootstrap script that
runs a model's program under it (`_bootstrap.py`, shared verbatim by both
adapters), and the shared contract suite that proves the two adapters agree
on what a `Sandbox` does (`contract.py`).

## Does not own
Anything in-process. `RestrictedPython`, `exec` with trimmed builtins and AST
filtering are all escapable, so in-process sandboxing is rejected outright and
process isolation is the floor (DESIGN.md §18). Also does not own the tool
registry, Policy checks or Record writing that a host binding routes through;
this package hands a caller-supplied async callable back and forth across the
wire and nothing more, which is what keeps it beneath `psych_runtime.tools` and
`psych_runtime.runtime` in the layering rather than needing to import either.

## Ports
Defines `Sandbox`.

## The rules that live here
One program per execution and no state between executions. Host bindings route
back through the normal tool path with the same Policy, egress and recording;
there is no privileged back door, and this package enforces that by construction
rather than by convention: it has no import path to a tool registry to reach
around in the first place. A failure returns its traceback as data so the
model can read it and fix the program (§18). Network access is off by default;
the subprocess adapter attempts denial and honestly reports whether it held
(`SandboxResult.network_denied`), while the container adapter's `--network=none`
is a kernel-level guarantee that does not depend on this process's own
privilege. Granted network access is not a bound HTTP client handed to the
program; anything the program is meant to fetch goes through a binding that
itself uses the egress seam (§14).
