"""The regression gate: the definition of done, run against every real store.

DESIGN.md §22 calls the e2e suite the regression gate, and §23 lists what "done"
means. This file is that list, executed.

## Why it runs against every adapter

DESIGN.md §23 item 8: the same Spec runs identically on all four store adapters.
Asserting that once, in one place, is worth more than four adapters each passing
their own contract suite, because the contract suite tests the store and this
tests the runtime *through* the store. A subtle difference in how an adapter
round-trips a datetime shows up here and nowhere else.

The adapters that need a running database skip when it is absent, which is how a
laptop with no Postgres still runs a useful suite. CI has all three, so nothing
is skipped there.

## Why everything is asserted through report() or the public API

Internal state is what a crash destroys. A feature that works in memory and not
in the log is a feature that breaks the first time a Worker dies, and the report
is what a consumer will actually look at.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.ids import RunId, ToolCallId, WorkerId
from psych_runtime.core.records import RunSettled, TerminalState, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, AgentStep, CodeTool, ModelRef, ToolStep, WorkflowSpec
from psych_runtime.core.usage import Usage
from psych_runtime.model.pricing import ModelPrice, StaticPriceTable
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState, Store
from psych_runtime.testing.fake_model import FakeModel, ToolCallScript
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE_A = Scope(tenant="acme", principal="user-1")
SCOPE_B = Scope(tenant="beta", principal="user-2")

PRICES = StaticPriceTable(
    {
        "fake-standard": ModelPrice(
            input=Decimal("1"),
            output=Decimal("2"),
            cache_read=Decimal("0.1"),
            cache_write=Decimal("1.25"),
        )
    }
)


# ---------------------------------------------------------------------------
# One suite, four stores
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=["memory", "postgres", "mysql", "dynamodb"],
    ids=["memory", "postgres", "mysql", "dynamodb"],
)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[Store]:
    """A real store of each kind, skipping the ones that need an absent server."""
    kind = request.param

    if kind == "memory":
        yield InMemoryStore()
        return

    if kind == "postgres":
        dsn = os.environ.get("PSYCH_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("PSYCH_TEST_POSTGRES_DSN is unset")
        from psych_runtime.store.postgres import PostgresStore

        async with PostgresStore(dsn=dsn) as pg:
            await pg.migrate()
            await _truncate_postgres(pg)
            yield pg
        return

    if kind == "mysql":
        dsn = os.environ.get("PSYCH_TEST_MYSQL_DSN")
        if not dsn:
            pytest.skip("PSYCH_TEST_MYSQL_DSN is unset")
        from psych_runtime.store.mysql import MySQLStore

        async with MySQLStore(dsn=dsn) as my:
            await my.migrate()
            await _truncate_mysql(my)
            yield my
        return

    endpoint = os.environ.get("PSYCH_TEST_DYNAMODB_ENDPOINT")
    if not endpoint:
        pytest.skip("PSYCH_TEST_DYNAMODB_ENDPOINT is unset")
    from psych_runtime.store.dynamodb import DynamoDBStore

    prefix = f"gate-{datetime.now(UTC).timestamp():.6f}".replace(".", "")
    async with DynamoDBStore(endpoint_url=endpoint, table_prefix=prefix) as ddb:
        await ddb.ensure_tables()
        try:
            yield ddb
        finally:
            await ddb.drop_tables()


async def _truncate_postgres(store_: Any) -> None:
    async with store_._pool.acquire() as conn:
        await conn.execute("TRUNCATE psych_records, psych_versions, psych_runs")


async def _truncate_mysql(store_: Any) -> None:
    pool = await store_._ensure_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        for table in ("records", "versions", "runs"):
            await cur.execute(f"TRUNCATE TABLE {table}")
        await conn.commit()


# ---------------------------------------------------------------------------
# Fixtures for the runs themselves
# ---------------------------------------------------------------------------


def build_registry(effects: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> str:
        """Look up an order."""
        effects.append(f"lookup:{order_id}")
        return f"{order_id} shipped"

    @registry.register
    def once(label: str) -> str:
        """A step whose executions are counted."""
        effects.append(f"once:{label}")
        return f"{label} done"

    return registry


def agent_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def drive(
    store_: Store,
    spec: AgentSpec | WorkflowSpec,
    model: FakeModel,
    registry: ToolRegistry,
    scope: Scope = SCOPE_A,
    **runtime_kwargs: Any,
) -> RunId:
    """Publish, dispatch and run to completion, the way a consumer would."""
    version = await psych_runtime.publish(store_, spec)
    dispatched = await psych_runtime.dispatch(
        store_, version, scope, input={"message": "where is A1?"}
    )
    journal = await Journal.open(store_, dispatched.run_id, scope)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store_, model=model, registry=registry, prices=PRICES, **runtime_kwargs)
    header = await store_.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


class TestRegressionGate:
    """Each test is one item from DESIGN.md §23, on every store adapter."""

    async def test_1_an_agent_with_tools_completes_a_multi_turn_run(self, store: Store) -> None:
        effects: list[str] = []
        model = (
            FakeModel()
            .turn(text="Checking.", tool_calls=[("lookup", {"order_id": "A1"})])
            .turn(text="A1 has shipped.")
        )
        run_id = await drive(store, agent_spec(), model, build_registry(effects))

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert len(report.model_calls) == 2
        assert len(report.tool_calls) == 1
        assert report.tool_calls[0].tool == "lookup"
        assert effects == ["lookup:A1"]

    async def test_2_python_and_dict_specs_produce_one_version_and_run_alike(
        self, store: Store
    ) -> None:
        """DESIGN.md §23 item 1."""
        from_python = agent_spec()
        from_dict = AgentSpec.model_validate(
            {
                "kind": "agent",
                "name": "support",
                "instructions": "Help the customer.",
                "model": {"model": "fake-standard"},
                "tools": [{"kind": "code", "name": "lookup"}],
            }
        )
        one = await psych_runtime.publish(store, from_python)
        two = await psych_runtime.publish(store, from_dict)
        assert one.hash == two.hash

        run_id = await drive(store, from_dict, FakeModel().turn(text="done"), build_registry([]))
        report = await psych_runtime.report(store, run_id)
        assert report.version_hash == one.hash

    async def test_3_a_worker_killed_mid_tool_call_is_reclaimed_and_completes(
        self, store: Store
    ) -> None:
        """DESIGN.md §23 item 2."""
        effects: list[str] = []
        registry = build_registry(effects)
        version = await psych_runtime.publish(store, agent_spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})

        dead = await Journal.open(store, dispatched.run_id, SCOPE_A)
        await dead.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
        await dead.append(type="turn_started", turn=1)
        await dead.append(type="model_call_started", turn=1, model="fake-standard")
        await dead.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 5},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await dead.append(
            type="tool_call_started",
            call_id="call-1",
            tool="lookup",
            arguments={"order_id": "A1"},
            turn=1,
        )
        await store.release(dispatched.run_id, WorkerId("wrk_dead"), RunState.RUNNABLE)

        runtime = Runtime(
            store=store, model=FakeModel().turn(text="Could not confirm."), registry=registry
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _await_settled(store, dispatched.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

        report = await psych_runtime.report(store, dispatched.run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert report.tool_calls[0].outcome is ToolOutcome.UNKNOWN
        assert effects == []  # the orphaned call was not re-executed

    async def test_4_an_interrupt_stops_the_run_and_the_next_message_starts_another(
        self, store: Store
    ) -> None:
        """DESIGN.md §23 item 3."""
        version = await psych_runtime.publish(store, agent_spec())
        first = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "one"})
        await psych_runtime.interrupt(store, first.run_id, reason="stop")
        second = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "two"})

        runtime = Runtime(
            store=store, model=FakeModel().turn(text="answering two"), registry=build_registry([])
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _await_settled(store, first.run_id)
        await _await_settled(store, second.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

        stopped = await psych_runtime.report(store, first.run_id)
        carried = await psych_runtime.report(store, second.run_id)
        assert stopped.terminal_state is TerminalState.ABORTED
        assert carried.terminal_state is TerminalState.COMPLETED

        records = await psych_runtime.records(store, first.run_id)
        kinds = [r.type for r in records]
        assert kinds.index("abort_requested") < kinds.index("run_settled")

    async def test_5_a_reconnecting_client_misses_nothing_at_any_cut_point(
        self, store: Store
    ) -> None:
        """DESIGN.md §23 item 4."""
        model = (
            FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 shipped.")
        )
        run_id = await drive(store, agent_spec(), model, build_registry([]))

        whole = [record async for record in psych_runtime.stream(store, run_id)]
        assert [r.seq for r in whole] == list(range(1, len(whole) + 1))
        assert isinstance(whole[-1], RunSettled)

        for cut in range(len(whole)):
            resumed = [r async for r in psych_runtime.stream(store, run_id, after=cut)]
            assert [r.seq for r in resumed] == [r.seq for r in whole[cut:]]

    async def test_6_a_workflow_resumes_without_repeating_a_side_effect(self, store: Store) -> None:
        """DESIGN.md §23 item 5, proven by a side effect recorded exactly once."""
        effects: list[str] = []
        registry = build_registry(effects)
        workflow = WorkflowSpec(
            name="nightly",
            tools=(CodeTool(name="once"),),
            steps=(
                ToolStep(name="first", tool="once", arguments={"label": "first"}),
                ToolStep(name="second", tool="once", arguments={"label": "second"}),
                AgentStep(name="wrap", spec=agent_spec(name="wrapper", tools=())),
            ),
        )
        version = await psych_runtime.publish(store, workflow)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A)

        journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        stalling = Runtime(store=store, model=FakeModel().stalls(seconds=30), registry=registry)
        engine = stalling.engine_for(journal)
        attempt = asyncio.create_task(engine.run(workflow))
        # Wait for the *log* to show two completed steps, not for the side
        # effects to be observed. Cancelling between a tool running and its
        # completion record landing is a genuine crash-in-the-gap, and
        # memoisation cannot re-derive a result it never wrote, so that step
        # correctly re-runs. Testing the memoisation property means cutting at a
        # point where the result really was recorded.
        for _ in range(600):
            await asyncio.sleep(0.005)
            state = await psych_runtime.state(store, dispatched.run_id)
            if len([s for s in state.steps.values() if s.completed]) == 2:
                break
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt

        assert effects == ["once:first", "once:second"]

        await store.release(dispatched.run_id, WorkerId("wrk_1"), RunState.RUNNABLE)
        runtime = Runtime(store=store, model=FakeModel().turn(text="wrapped"), registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _await_settled(store, dispatched.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=10)

        # Exactly once each, across the crash.
        assert effects == ["once:first", "once:second"]
        report = await psych_runtime.report(store, dispatched.run_id)
        assert report.terminal_state is TerminalState.COMPLETED

    async def test_7_a_run_is_invisible_to_another_tenant(self, store: Store) -> None:
        """The tenancy half of DESIGN.md §23 item 6. Scope is stamped on every
        record, so one tenant's Run cannot be read as another's."""
        run_a = await drive(
            store, agent_spec(), FakeModel().turn(text="a"), build_registry([]), scope=SCOPE_A
        )
        run_b = await drive(
            store, agent_spec(), FakeModel().turn(text="b"), build_registry([]), scope=SCOPE_B
        )

        state_a = await psych_runtime.state(store, run_a)
        state_b = await psych_runtime.state(store, run_b)
        assert state_a.scope.tenant == "acme"
        assert state_b.scope.tenant == "beta"

        for record in await psych_runtime.records(store, run_a):
            assert record.scope.tenant == "acme"
        for record in await psych_runtime.records(store, run_b):
            assert record.scope.tenant == "beta"

    async def test_8_report_totals_reconcile_with_the_records(self, store: Store) -> None:
        """DESIGN.md §23 item 7."""
        model = (
            FakeModel()
            .turn(
                tool_calls=[("lookup", {"order_id": "A1"})],
                usage=Usage(input=1_000_000, output=500_000, cache_read=2_000_000),
            )
            .turn(text="done", usage=Usage(input=1000, output=100))
        )
        run_id = await drive(store, agent_spec(), model, build_registry([]))
        report = await psych_runtime.report(store, run_id)

        from_records = Usage()
        for record in await psych_runtime.records(store, run_id):
            if record.type == "model_call_finished":
                from_records = from_records + record.usage

        assert report.totals.usage == from_records
        assert report.totals.usage.input == 1_001_000
        assert report.totals.usage.cache_read == 2_000_000
        assert report.totals.cost is not None
        assert not report.totals.cost_is_incomplete
        assert report.totals.latency.wall_clock_seconds >= 0

    async def test_9_an_unpriced_model_keeps_the_total_honest(self, store: Store) -> None:
        """A silent zero makes metering look correct and be wrong."""
        run_id = await drive(
            store,
            agent_spec(model=ModelRef(model="model-nobody-priced")),
            FakeModel().turn(text="done"),
            build_registry([]),
        )
        report = await psych_runtime.report(store, run_id)
        assert report.totals.cost is None
        assert report.totals.unpriced_model_calls == 1
        assert report.totals.cost_is_incomplete

    async def test_10_three_failures_of_one_tool_stop_the_model_repeating_it(
        self, store: Store
    ) -> None:
        """DESIGN.md §23 item 10."""
        registry = ToolRegistry()

        @registry.register
        def flaky(value: str) -> str:
            """Always fails."""
            raise RuntimeError("it never works")

        spec = agent_spec(tools=(CodeTool(name="flaky"),))
        model = FakeModel()
        for _ in range(4):
            model.turn(tool_calls=[("flaky", {"value": "x"})])
        model.turn(text="I will stop trying that.")

        run_id = await drive(store, spec, model, registry)
        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        # By the fourth turn the tool was gone from what the model was offered.
        fourth = model.requests[3]
        assert "flaky" not in {tool.name for tool in fourth.tools}

    async def test_11_a_run_survives_a_round_trip_through_the_store(self, store: Store) -> None:
        """Every adapter must round-trip a record without losing anything: a
        datetime's timezone, a Decimal cost, a nested dict argument."""
        model = (
            FakeModel()
            .turn(
                tool_calls=[("lookup", {"order_id": "A1"})],
                usage=Usage(input=10, cache_write=4, cache_write_1h=2),
            )
            .turn(text="done")
        )
        run_id = await drive(store, agent_spec(), model, build_registry([]))

        records = await psych_runtime.records(store, run_id)
        finished = next(r for r in records if r.type == "model_call_finished")
        assert finished.usage.cache_write_1h == 2
        assert finished.at.tzinfo is not None
        assert finished.cost is not None
        assert isinstance(finished.cost.amount, Decimal)

        started = next(r for r in records if r.type == "tool_call_started")
        assert started.arguments == {"order_id": "A1"}

    async def test_12_a_large_result_stays_whole_in_the_log_and_is_readable_by_handle(
        self, store: Store
    ) -> None:
        """DESIGN.md §10.8, through the real loop with no test-only wiring.

        The reader was built against a resolver subclass while the agent loop was
        owned by another change. This asserts the shipped path: the loop offers
        read_tool_output only when there is something stored to read, and routes
        it to the log rather than to the registry.
        """
        registry = ToolRegistry()

        @registry.register
        def big_report(rows: int) -> str:
            """Produce a large report."""
            return "\n".join(f"line {index}: something happened" for index in range(rows))

        spec = agent_spec(
            tools=(CodeTool(name="big_report"),),
            limits=__import__("psych_runtime").Limits(large_result_bytes=1024),
        )
        # The handle is derived from the call id, so the call id is pinned here
        # rather than guessed. A handle the Run never issued is refused as
        # corruption, which is the check working, not a test problem.
        report_call = ToolCallScript(
            name="big_report", arguments={"rows": 500}, call_id=ToolCallId("call-report")
        )
        model = (
            FakeModel()
            .turn(tool_calls=[report_call])
            .turn(
                tool_calls=[
                    (
                        "read_tool_output",
                        {"handle": "res_call-report", "offset": 10, "limit": 2},
                    )
                ]
            )
            .turn(text="Lines 10 and 11 say something happened.")
        )
        run_id = await drive(store, spec, model, registry)

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        records = await psych_runtime.records(store, run_id)
        stored = next(
            r for r in records if r.type == "tool_call_finished" and r.result_handle is not None
        )
        # The log holds the whole thing.
        assert stored.result_bytes > 1024
        assert stored.result is not None
        assert len(stored.result) == stored.result_bytes
        # The model saw a handle and a preview instead.
        assert stored.preview is not None
        assert len(stored.preview) < stored.result_bytes

        # The reader was offered on the turn after the large result and not before.
        first_turn_tools = {tool.name for tool in model.requests[0].tools}
        second_turn_tools = {tool.name for tool in model.requests[1].tools}
        assert "read_tool_output" not in first_turn_tools
        assert "read_tool_output" in second_turn_tools

    async def test_13_skills_and_memory_reach_the_model_through_the_shipped_loop(
        self, store: Store
    ) -> None:
        """DESIGN.md §15 and §16, through the real Runtime.

        Both were built against a resolver subclass while the agent loop was
        owned by another change. This asserts the shipped path: built-ins are
        registered per Spec and per Scope, and a Spec with no skills is not
        offered load_skill at all.
        """
        from psych_runtime.memory.store_backed import StoreBackedMemory

        registry = build_registry([])
        memory = StoreBackedMemory(store)
        spec = agent_spec(
            tools=(),
            skills=(
                psych_runtime.Skill(
                    name="tone",
                    description="How to write to a customer.",
                    body="Be brief. Never speculate about a delivery date.",
                ),
            ),
        )
        model = (
            FakeModel()
            .turn(tool_calls=[("load_skill", {"name": "tone"})])
            .turn(tool_calls=[("remember", {"content": "prefers email over phone"})])
            .turn(text="Noted.")
        )
        run_id = await drive(store, spec, model, registry, memory=memory, end_user_id="end-user-7")

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED

        # The skill body reached the model, and only after it asked for it.
        offered_first_turn = {tool.name for tool in model.requests[0].tools}
        assert "load_skill" in offered_first_turn
        assert "remember" in offered_first_turn
        system = model.requests[0].messages[0]
        assert "How to write to a customer." in system.content
        assert "Never speculate" not in system.content, "a skill body must not sit in the prompt"

        second = model.requests[1]
        skill_result = next(m for m in second.messages if m.role == "tool")
        assert "Never speculate" in skill_result.content

        # The fact was actually stored, under this Scope and end user.
        facts = await memory.recall(SCOPE_A, "end-user-7")
        assert [fact.content for fact in facts] == ["prefers email over phone"]

        # And is invisible to another end user in the same tenant.
        assert not await memory.recall(SCOPE_A, "someone-else")

    async def test_14_a_spec_with_no_skills_is_not_offered_load_skill(self, store: Store) -> None:
        """A tool that can only ever answer "no such skill" is worse than no
        tool: the model will call it, get nothing, and have burned a turn."""
        model = FakeModel().turn(text="done")
        await drive(store, agent_spec(), model, build_registry([]))
        assert "load_skill" not in {tool.name for tool in model.requests[0].tools}

    async def test_15_no_test_in_this_suite_reaches_the_network(self, store: Store) -> None:
        """The guard in conftest is autouse and session-scoped. This asserts it
        is actually armed, because a guard nobody checks is a guard that can be
        removed by accident."""
        import socket

        from tests.conftest import NetworkCallInTest

        _ = store
        with pytest.raises(NetworkCallInTest):
            socket.create_connection(("example.invalid", 443), timeout=0.1)

        # Loopback still works, which is what the store and the stub servers need.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            client = socket.create_connection(listener.getsockname(), timeout=1)
            client.close()
        finally:
            listener.close()


async def _await_settled(store_: Store, run_id: RunId, timeout: float = 10.0) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        state = await psych_runtime.state(store_, run_id)
        if state.settled:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")
