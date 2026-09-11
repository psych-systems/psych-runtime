# Psych Runtime

A Python library for running AI agents inside your application. It runs an
agent's tool calls, saves a Run's progress as it goes, pauses for approval
before the calls you choose, and records everything a Run did so you can read
it back. You supply the model, the tools and the database.

[psychruntime.com](https://psychruntime.com) ·
[Documentation](https://psychruntime.com/docs) ·
[Browser demo](https://psychruntime.com/playground) ·
[Changelog](CHANGELOG.md)

[![gate](https://github.com/psych-systems/psych-runtime/actions/workflows/gate.yml/badge.svg)](https://github.com/psych-systems/psych-runtime/actions/workflows/gate.yml)
[![PyPI](https://img.shields.io/pypi/v/psych-runtime)](https://pypi.org/project/psych-runtime/)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-green)

**Status: pre-alpha.** The public API changes between minor releases, and
every breaking change is in `CHANGELOG.md` before it ships. Names scheduled
for removal warn for at least one minor release first. The PyPI badge above
shows the current release.

## What it looks like

One agent, one tool, offline. The test suite runs this file on every commit.

```python
import asyncio
import psych_runtime
from psych_runtime.testing.fake_model import FakeModel


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "shipped"}


spec = psych_runtime.AgentSpec(
    name="support",
    instructions="Help the customer with their order.",
    model=psych_runtime.ModelRef(model="fake-standard"),
    tools=(psych_runtime.CodeTool(name="lookup_order"),),
)


async def main() -> None:
    model = (
        FakeModel()
        .turn(text="Checking.", tool_calls=[("lookup_order", {"order_id": "A1"})])
        .turn(text="A1 has shipped.")
    )
    async with psych_runtime.session(model, tools=[lookup_order]) as s:
        view = await s.ask(spec, "where is order A1?")
        print(view.text)  # A1 has shipped.

        report = await psych_runtime.report(s.store, view.run_id)
        print(report.totals.usage, report.totals.cost)


asyncio.run(main())
```

```text
A1 has shipped.
input=0 output=0 cache_read=0 cache_write=0 cache_write_1h=0 reasoning=0 None
```

The tool ran: `report.tool_calls` says so, from the log. Cost is `None`
because no price table was passed; it is never rounded to zero.

## Run it

Python 3.12 or newer. The package depends on `pydantic` and `httpx`.

```sh
pip install psych-runtime
psych new demo && cd demo && python main.py
```

Expected:

```text
No OPENAI_API_KEY set, so this run uses the fake model.

Order A1 has shipped with DHL.

completed | 1 turn, 1 tool call | 0 tokens | cost None
```

Set `OPENAI_API_KEY` and the same file calls a real provider. `psych new`
writes a `main.py` you own; the command line never runs an agent for you.

- The distribution is `psych-runtime`, the import is `psych_runtime`, the command is `psych`.
- `session()` defaults to an in-memory store that dies with the process. Pass a `PostgresStore`, `MySQLStore` or `DynamoDBStore` for anything that must survive a restart.
- [Get started](https://psychruntime.com/docs/next/get-started) walks through the file above, then a real model, a second tool, and reading a Run back.

## What it handles

| | Guide |
|---|---|
| **Recover a run after a worker stops.** Another worker claims the expired lease, replays the log and carries on. A call the dead worker left open is settled `unknown`, not re-run, unless the tool is registered `safe_to_retry=True`. Needs a durable store. | [Persist and recover runs](https://psychruntime.com/docs/next/guides/stores) |
| **Require approval for selected tool calls.** By name or by annotation. A matching call suspends the Run with its exact arguments and waits for a decision from any process. | [Approvals](https://psychruntime.com/docs/next/guides/approvals) |
| **Connect Python functions, HTTP endpoints, MCP servers and A2A agents** as tools, resolved fresh every turn. | [Code tools](https://psychruntime.com/docs/next/guides/code-tools), [MCP](https://psychruntime.com/docs/next/guides/mcp) |
| **Inspect a run:** status for a screen, the answer with its work, a report with tool calls, usage and cost, or the raw log. Every read takes a `scope=`. | [Reading a Run](https://psychruntime.com/docs/next/guides/report) |
| **Track token usage and cost** per model call, split by cache state, with provider-versus-estimated provenance. | [Pricing](https://psychruntime.com/docs/next/guides/pricing) |
| **Serve multiple tenants** from one process: a `Scope` on every record and every outbound call. | [Multi-tenancy](https://psychruntime.com/docs/next/guides/multitenancy) |

## Where it runs

Psych is a dependency you import, not a service you deploy. Your request
handler admits a Run and returns; a `Worker` in a process of yours claims it
and executes it; the two share your database and nothing else.

Psych brings no HTTP server, auth, user model, scheduler, UI, prompt library,
eval framework, vector store, budget enforcement or model router. Your
application already has those, and a library that brought its own would
argue with them. `psych new api --template fastapi` shows the split.

Limitations worth knowing before you build on it:

- **Only what you register is safe to retry.** After a crash, a tool call
  recorded as started is settled `unknown` unless it was registered
  `safe_to_retry=True`. Psych does not make an external side effect
  repeatable; it refuses to guess.
- **The subprocess sandbox limits, it does not isolate.** It sets rlimits and
  drops privileges, but shares the host filesystem, and its network denial is
  a self-report. The container sandbox is the one with kernel isolation.
- **Cost is only as good as its source.** `Cost.source` distinguishes an amount
  returned by the provider from one estimated using the bundled, dated price
  snapshot or your override. A model with no known price records `cost=None`.
  Token totals carry their own `usage_source`; missing provider usage is never
  presented as a measured zero.
- **Provider support is the OpenAI-compatible chat API.** One client reaches
  OpenAI and any gateway that speaks the same protocol; anything else is a
  `ModelClient` you implement.

## The example console

```sh
git clone https://github.com/psych-systems/psych-runtime && cd psych-runtime
docker compose up --build        # then http://localhost:3000
```

[`examples/playground/`](examples/playground/README.md) is an application
built on the library: publish an agent, talk to it, watch the log, read the
report. It supplies the HTTP server and the UI Psych does not. It is an
example, not a hardened service; do not put it on the public internet.

## For coding agents

The repository ships 26 skills, one per feature, in
[`.agents/skills/`](.agents/skills). Give your coding agent this instruction:

```text
Read https://raw.githubusercontent.com/psych-systems/psych-runtime/main/.agents/skills/psych/SKILL.md
and follow it to add Psych to this project.
```

`psych skills install` copies them into a project.

## Documentation

**[psychruntime.com/docs](https://psychruntime.com/docs)** is generated from
this repository: the API reference from `psych_runtime.__all__`, the guides
from the skills above, the changelog and design notes copied verbatim. The
build fails when any page disagrees with its source.

- [`DESIGN.md`](DESIGN.md), the settled design.
- [`docs/design-notes/`](docs/design-notes), why each area is shaped the way it is.
- [`docs/api.md`](docs/api.md), the public API with a worked example the test suite runs.
- [`docs/deployment.md`](docs/deployment.md), what a deployment is responsible for: scope, secrets, sandboxes, retention.

## Contributing, support and security

- [`CONTRIBUTING.md`](CONTRIBUTING.md): what is welcome, how to set up, and what a change has to pass.
- [Issues](https://github.com/psych-systems/psych-runtime/issues) for bugs and questions. Most bugs reproduce against the fake model with no provider and no network; a failing test beats a description.
- [`SECURITY.md`](SECURITY.md): report vulnerabilities privately through the advisory form, never as a public issue.

## Development

```sh
uv sync --all-extras --group dev
scripts/dev-services.sh start        # real Postgres, MySQL and DynamoDB Local
scripts/check.sh                     # ruff, mypy --strict, import-linter, generated docs, three test layers
```

## Licence

Apache-2.0. See [LICENSE](LICENSE), and [NOTICE](NOTICE) for the one
third-party attribution the package carries.
