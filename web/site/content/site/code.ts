/**
 * The code the site shows.
 *
 * Every sample here is lifted from something the repository executes: the
 * README's ten lines (run by the e2e suite), the `psych new` templates (each
 * generated and executed by `tests/functional/test_cli.py`), and `docs/api.md`
 * (whose worked example the test suite runs). `web/scripts/check-site.mjs`
 * checks that every runtime symbol called here is still exported, so a
 * renamed symbol breaks the build rather than a visitor's terminal.
 */

export const INSTALL = "pip install psych-runtime";

export const FIRST_RUN = `pip install psych-runtime          # Python 3.12+
psych new demo && cd demo && python main.py
`;

/** README: "What it looks like". Runs offline against the scriptable fake model. */
export const TEN_LINES = `import asyncio
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
`;

export const TEN_LINES_OUTPUT = `$ python main.py
A1 has shipped.
input=0 output=0 cache_read=0 cache_write=0 cache_write_1h=0 reasoning=0 None
`;

/** README: pointing the same file at a real provider. */
export const REAL_PROVIDER = `model = psych_runtime.OpenAICompatibleClient(
    base_url="https://api.openai.com/v1",
    api_key="...",
    transport=psych_runtime.HttpTransport(),
    scope=psych_runtime.Scope(tenant="acme"),
)
`;

/** docs/api.md: the long form a real deployment writes out. */
export const LONG_FORM = `import asyncio

import psych_runtime
from psych_runtime.store.postgres import PostgresStore

# 1. Register your tools. The functions stay yours; Psych keeps the names.
registry = psych_runtime.ToolRegistry()


@registry.register(annotations={"read-only"})
async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "shipped"}


@registry.register(interruptible=False, annotations={"destructive"})
async def issue_refund(order_id: str, cents: int) -> str:
    """Refund an order. Not interruptible: a half-issued refund is worse than a
    slow stop."""
    return f"refunded {cents} on {order_id}"


# 2. Describe the agent. This is data. It has no callables in it.
spec = psych_runtime.AgentSpec(
    name="support",
    instructions="Help the customer with their order. Be brief.",
    model=psych_runtime.ModelRef(model="gpt-4o", temperature=0.2),
    tools=(
        psych_runtime.CodeTool(name="lookup_order"),
        psych_runtime.CodeTool(name="issue_refund", interruptible=False),
    ),
    limits=psych_runtime.Limits(max_turns=12, deadline_seconds=300),
)


async def main() -> None:
    async with PostgresStore(dsn="postgresql://localhost/psych") as store:
        await store.migrate()
`;

/** The approval loop from the tour template. */
export const APPROVAL = `status = await psych_runtime.status(s.store, run_id)
if status.pending_approval is not None:
    print(f"waiting on approval for: {status.pending_approval.tool}")
    await psych_runtime.resume(s.store, run_id, approved=True, by="manager-7")
`;

/** The Runtime with approval selectors, from the approvals guide. */
export const APPROVAL_RUNTIME = `runtime = psych_runtime.Runtime(
    store=store,
    model=model,
    registry=registry,
    approval_selectors=("@destructive",),  # default is ("@write", "@destructive")
)
`;

/** The Worker, from DESIGN.md §8. */
export const WORKER = `runtime = psych_runtime.Runtime(store=store, model=model, registry=registry)
worker = psych_runtime.Worker(store, runtime, concurrency=4)
await worker.run()  # blocks, claims Runs, executes them
`;

/** Publish, dispatch, stream: the three calls a request handler makes. */
export const DISPATCH = `# Your auth decided who this is. Psych never guesses it.
scope = psych_runtime.Scope(tenant=request.tenant_id, principal=request.user_id)

version = await psych_runtime.publish(
    store, spec, context=psych_runtime.ValidationContext(registered_tools=registry.names)
)
run = await psych_runtime.dispatch(
    store, version, scope,
    input={"message": "where is order A1?"},
    idempotency_key="evt-8891",   # exactly once per key
)

# Pass the scope on the read as well. Without it the read is unchecked, so
# any run_id that leaks is readable by whoever holds it.
async for record in psych_runtime.stream(store, run.run_id, after=0, scope=scope):
    render(record)               # reconnect later with after=<last seq>
`;

/** MCP server in a Spec, from the mcp guide. */
export const MCP = `spec = psych_runtime.AgentSpec(
    name="researcher",
    instructions="Answer from the connected sources.",
    model=psych_runtime.ModelRef(model="gpt-4o"),
    mcp_servers=(
        psych_runtime.McpServer(
            name="docs",
            url="https://mcp.example.com/mcp",
            credential="docs-token",      # resolved per Scope by your SecretResolver
            allow=("search", "read_page"),
        ),
    ),
)
`;

export const WORKFLOW = `flow = psych_runtime.WorkflowSpec(
    name="close-ticket",
    steps=(
        psych_runtime.ToolStep(name="verify", tool="lookup_order", arguments={"order_id": "A1"}),
        psych_runtime.AgentStep(name="write-up", spec=spec),   # a nested agent
    ),
    tools=(psych_runtime.CodeTool(name="lookup_order"),),
)
`;

export const TOUR_OUTPUT = `$ psych new demo --template tour && cd demo && python main.py
No OPENAI_API_KEY set, so this tour uses the fake model.

====================================================================
1-3. Tool call, approval, and a skill loaded on demand
====================================================================
  waiting on approval for: issue_refund

answer: Refunded 4200 on order A1. I will email you from now on.
work:   4 turns, 4 tool calls
approvals granted: 1
  tool  lookup_order({'order_id': 'A1'}) -> ok
  tool  load_skill({'name': 'refund-policy'}) -> ok
  tool  issue_refund({'order_id': 'A1', 'cents': 4200}) -> ok
  tool  remember({'content': 'prefers email over SMS'}) -> ok

====================================================================
6. What all of it cost
====================================================================
  state:    completed
  usage:    input=0 output=0 cache_read=0 cache_write=0 cache_write_1h=0 reasoning=0
  cost:     None  (None means no price is known, never zero)
  unpriced: 5 model calls
  latency:  0.51s wall clock
`;

export const PLAYGROUND_RUN = `git clone https://github.com/psych-systems/psych-runtime && cd psych-runtime
docker compose up --build        # then http://localhost:3000
`;

export const SKILLS_PROMPT = `Read https://raw.githubusercontent.com/psych-systems/psych-runtime/main/.agents/skills/psych/SKILL.md
and follow it to add Psych to this project.
`;

export const GATE = `uv sync --all-extras --group dev
scripts/dev-services.sh start     # real Postgres, MySQL and DynamoDB Local
scripts/check.sh                  # ruff, mypy --strict, pytest, docs in sync
`;

export const CLI = `psych skills install                  # 26 guides for your AI coding agent
psych doctor                          # what is installed and configured; opens no socket
psych --help                          # and that is the whole command line
`;
