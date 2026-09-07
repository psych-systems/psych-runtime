# Contributing to Psych

Issues and pull requests are the whole process. This file says what is
welcome, how to set up, what a change has to pass, and how to raise something
larger than a pull request.

## What is welcome

- Bug reports, with a reproduction. Most bugs reproduce against `FakeModel`
  with no provider and no network, and a failing test saves the round trip
  where we ask what you meant.
- Fixes, with the test that fails without them.
- A new adapter (a `Store`, `BlobStore`, `ModelClient`, `Sandbox`,
  `Telemetry` or `MemoryStore`) that passes the shared contract suite for its
  port.
- Corrections to a skill, a design note, a docstring or a README that no
  longer matches the code. A document that has drifted is a bug here.
- Anything that makes a first run clearer without making the library decide
  more.

Not welcome, on principle rather than quality: an HTTP server, auth, users or
roles, a database of its own, a scheduler, a UI, a prompt library, an eval
framework, a vector store, budget enforcement or a model router. DESIGN.md §1
refuses each because the consumer already has one. If a change seems to need
one, open a discussion first and say what you are trying to do; the answer is
usually a port you can implement in your application.

## Before a larger change

DESIGN.md is settled. A change that contradicts it, adds a Record type,
changes how leases, approvals or narrowing work, or widens the public API is a
conversation before it is a commit. Open a discussion or an issue that says
what you are trying to do and which section it touches, and wait for a reply
before writing it. A pull request that arrives with a design change nobody
discussed is closed with a pointer here, whatever its quality.

## Set up a checkout

```sh
git clone https://github.com/psych-systems/psych-runtime && cd psych-runtime
uv sync --all-extras --group dev
# On a Linux environment with the databases already installed:
scripts/dev-services.sh start
```

`scripts/dev-services.sh` starts already-installed databases using Linux paths
and system users. It does not install them and is not a portable setup command.
The full gate targets Linux, including POSIX sandbox APIs. Windows contributors
need a Linux environment for that gate. CI uses service containers instead.
Export `PSYCH_TEST_POSTGRES_DSN` after starting it, or the store contract
suite skips the database and says so.

## Focused checks while you work

```sh
scripts/check.sh fast                              # lint, format, types, imports, generated docs
uv run pytest tests/unit -q                        # one layer
uv run pytest tests/e2e/test_durability.py -q      # one file
uv run pytest -k approval -q                       # one topic
```

## The rules

These are rules, not suggestions. They come from DESIGN.md §22, which calls
them "enforced in review and in CI". Where CI can check one it does; the rest
are here because a reviewer has to.

### Before you write anything

Read `DESIGN.md`. It is settled, not a draft. If your change contradicts it,
either you are wrong or the design needs an explicit change first, and the second
is a conversation rather than a commit.

Read the design note for the area you are working in. `docs/design-notes/` holds
one document per area, and each says why that area is shaped the way it is and
what breaks under the obvious alternative. The reasoning matters more than it
sounds: most of what looks like an arbitrary detail there is a bug somebody
already had.

### The full gate

```sh
scripts/check.sh                # ruff, ruff format, mypy --strict, import-linter, generated docs, three test layers
```

All of it green, before every commit. CI runs the same checks with the three
test layers as separate jobs, so a red build names the layer that broke.

### What a pull request says

The template asks for the problem, what changes for a user, how it is
verified, which documentation moved with it, and whether it is breaking. The
diff already says what changed; the pull request says why. One verified
increment per pull request, and a change that makes code unused deletes that
code in the same change.

## Correctness rules

**No mocked store.** Store tests run against real PostgreSQL, real MySQL and real
DynamoDB Local. The in-memory adapter exists to test other components quickly and
never proves the store contract. Four implementations with no shared test is four
divergent behaviours discovered in production.

**No test makes a real network call.** Every external call goes through an
injectable seam: `ModelClient`, `fetch_impl`, `Sandbox`. `tests/conftest.py`
enforces this by patching the socket layer for the whole session: loopback and
unix sockets are allowed, everything else raises. A local stub server you started
is fine. `api.openai.com` is not.

**The fake model must stay strong.** It scripts multi-step tool-calling runs
including malformed tool calls, stalled streams and streams that abort mid-token.
A weak fake produces a weak suite, and every other test in this repo is only as
good as that file.

**Type hints everywhere.** `mypy --strict` passes. No `Any` as an escape hatch.
No `# type: ignore` without a comment naming the reason.

**Pydantic models at every boundary.** Records, Specs, tool arguments and tool
results are validated models, not dicts.

**Chain the cause when you re-raise** (`raise X from err`). A lost original
traceback costs hours.

**One canonical owner per type.** No duplicate definitions, no forwarding shims.

## Nothing mocked and nothing demoed

No placeholder implementations. No `NotImplementedError` on a shipped path. No
"TODO: wire this up". No demo-only branch. No sample data standing in for real
behaviour.

A component that cannot be finished is not started. A ticket is done when its
acceptance criteria pass against real adapters, not when the shape exists.

## Testing

Three layers, all blocking:

**Unit** covers pure logic with no IO: the reducer, narrowing, pricing
arithmetic, the transient classifier, step id derivation, prompt assembly.

**Functional** runs a component against a real adapter: the store contract suite
against all four stores, the agent loop against the fake model, sandbox execution
against a real subprocess, MCP against a local stub server.

**End to end** runs a real Run from dispatch to report and asserts through
`psych_runtime.report()`. This is the regression gate. Every feature ships with an e2e
case that fails when the feature breaks.

**Coverage is not the metric.** A feature with no e2e case asserting its
behaviour is not done, whatever the line coverage says.

A test that is wrong is worse than no test. When a test fails, work out which of
the two is wrong before changing either; several of the tests in this repo caught
real bugs, and several were themselves the bug.

## Commits

One verified increment per commit, with a message explaining **why** rather than
what. The diff already says what.

A change that makes code unused deletes that code in the same change.

Migrations are sequential and forward-only. Check the highest existing number
first.

Comments explain intent, trade-offs and constraints. They never restate the code.

Breaking changes go in `CHANGELOG.md` at commit time, not reconstructed later.
The public API stays at `0.x` until the design survives a second consumer.
The release workflow validates the package version, changelog, documentation,
build artifacts, and tag before it publishes anything.

## How a breaking change arrives

`0.x` and "breaking changes are expected" tells a team nothing they can plan
around. This is the mechanism.

**A name scheduled for removal ships at least one minor release emitting a
`DeprecationWarning`, and the warning names its replacement.** A warning that
says something is deprecated without saying what to use instead makes the reader
do the search, which is the work the warning existed to save.

```python
warnings.warn(
    "psych_runtime.old_name is deprecated and will be removed in 0.4. "
    "Use psych_runtime.new_name, which takes the same arguments.",
    DeprecationWarning,
    stacklevel=2,
)
```

**Deprecated in 0.N means removed no earlier than 0.N+2.** One release to notice
and one to act, which is roughly one sprint for a team that is not watching this
repository daily. Removing in 0.N+1 makes the warning a formality.

**`CHANGELOG.md` gets a `Deprecated` section**, beside `Added`, `Changed` and
`Fixed`, naming the replacement and the release the removal lands in.

Two exemptions:

- **Security fixes.** A vulnerability is removed when it is found. A deprecation
  window on a security bug is a window in which it is exploitable.
- **Anything never documented as public.** Not in `psych_runtime.__all__`, not
  in `docs/api.md`, not in a skill: it changes without ceremony, which is what
  `0.x` is for. This exemption is what keeps the promise above affordable. A
  project that treats every internal name as public stops being able to move,
  and then stops keeping the promise at all.

The line between the two is mechanical rather than a judgement call, because
`tests/unit/test_public_surface.py` already enforces what the public surface is.

## Documentation

Every package carries a `README.md` saying what it owns, what it does not own,
and which port it defines or implements. **A package whose README no longer
matches its code is a bug**, not a stale document.

Aim for a one-sentence summary that does all three at once: what the package
does, what it deliberately knows nothing about, and which port that is. A
"known limitations" section describes what the package cannot do today. It is
not a task list, and it must not become one.

`.agents/skills/` holds one guide per feature, written for someone (or
something) using Psych rather than building it. **A skill that no longer matches
its code is a bug**, on the same terms as a package README. When you change a
public behaviour, the skill covering it is part of the change, not a follow-up:
every code block in one is parsed by the gate, and the README's and
`docs/api.md`'s worked examples are executed by the e2e suite, because an
example that does not run is the first thing a new consumer copies and it fails
for them in a way that looks like their own mistake.

`psych_runtime` is the whole public surface and that is enforced rather than asserted.
`tests/unit/test_public_surface.py` fails when an exported model has a field
whose type is not itself exported, so export a type as soon as a consumer can
hold one.

## What Psych refuses to own

DESIGN.md §1: no HTTP server, no auth, no orgs or roles, no database of its own,
no scheduler, no UI, no prompt library, no eval framework, no vector store or
RAG, no budget enforcement, no model router.

A pull request adding one of these is rejected on principle rather than on
quality. Each refusal exists because the consumer already has one, or because
owning it turns Psych into a platform. If a ticket seems to need one, the ticket
has been misread.

Psych is a library, not a framework. A framework inverts control and Psych does
not. Do not call it a framework in code, comments or docs.
