# Security

## Reporting a vulnerability

Report it privately through GitHub's advisory form:
[Report a vulnerability](https://github.com/psych-systems/psych-runtime/security/advisories/new).
Do not open a public issue for a security bug.

Include what you did, what happened, and the version or commit you were on. A
failing test or a short script that reproduces it is worth more than a paragraph
describing it, because it removes the round trip where we ask what you meant.

Psych is pre-alpha and maintained by one person, so promising a response time
would be a promise I cannot keep. I read reports as they arrive and will tell you
what I think within a few days. Only the latest release gets fixes; there are no
maintained older branches to backport to.

If you want credit in the advisory, say so and give me the name to use.

## What counts

These are the failures the design treats as serious, and a report about any of
them is one I want:

**Credential or data crossing a tenant boundary.** A `Scope` threads through
every call and MCP clients pool by `(scope, server, credential)`. Anything that
sends one tenant's token on another tenant's call, or lets a Run read a Record
stamped with a different scope, is the worst bug in the project.

**Access widening.** What the server offers contains what the tenant permits,
which contains what the Spec grants, which contains what is callable now. One
function computes that intersection and both the validator and the runtime call
it. A path that reaches a tool outside it is in scope, including through
subagents, deferred disclosure or model-written code.

**Sandbox escape.** Model-written code runs in a subprocess or a container, never
in the host interpreter. Reading a file the sandbox should not reach, opening a
network connection it should not open, or surviving the deadline that is supposed
to kill it all count.

**Egress that skips the seam.** Every outbound HTTP call, the model client and
MCP included, goes through one place. A route around it defeats any control a
consumer put there, which is worse than having no control at all, because they
believe it is working.

**A contradictory log that does not raise.** A corrupt Record log must fail the
Run loudly with a typed error. Silent repair hides the writer bug that caused it,
so a log that reduces to a plausible wrong state instead of raising is a security
bug, not a correctness nit.

## What does not count

**The playground is a demo.** `examples/playground/` supplies the HTTP server,
the auth and the UI that Psych deliberately refuses to own, and it exists to show
the library working. It has accounts and hashed passwords because a demo without
them teaches the wrong thing, not because it is hardened. Do not deploy it on the
public internet. Findings against it are welcome as ordinary issues and will not
get an advisory.

**In-process sandboxing.** `RestrictedPython`, `exec` with trimmed builtins and
AST filtering are all escapable, which is why Psych does not offer them. A report
showing that one of them can be escaped is describing why the design is what it
is.

**A consumer's own configuration.** Psych does not enforce budgets, route models,
or decide who may call it. If a deployment granted a Spec a destructive tool and
turned approvals off, that is the deployment's decision working as designed.

**Dependency advisories with no path to exploit here.** Tell us anyway if you
like, but as an issue. An advisory needs a way to actually reach the vulnerable
code through Psych.
