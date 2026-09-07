"""End to end: subagents a parent composes, runs in the background and steers.

DESIGN.md §17, by way of §11. A parent inside a ``SpawnEnvelope`` writes a child
Spec at run time, that Spec is published as a Version like any other, and the
child runs as its own claimable Run. The parent keeps working, looks in on it,
steers it, and -- when it has nothing left to do -- suspends rather than holding
a lease while somebody else's work finishes.

Everything is asserted through ``psych_runtime.report()`` and the log, never through
runtime internals, because internal state is exactly what the crash test
destroys.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.ids import RunId, WorkerId
from psych_runtime.core.records import (
    QueueEnqueued,
    SubagentSpawned,
    SuspendReason,
    TerminalState,
)
from psych_runtime.core.reducer import RunStateView, reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, ModelRef, SpawnEnvelope
from psych_runtime.core.usage import Usage
from psych_runtime.model.port import ModelRequest, StreamEvent
from psych_runtime.runtime.dispatch import dispatch as dispatch_run
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


class RoutedModel:
    """One ``ModelClient`` fronting a scripted ``FakeModel`` per agent.

    A parent and its children run concurrently under one Worker, so a single
    ``FakeModel`` -- which consumes one scripted turn per call, in order -- would
    hand the parent's next answer to whichever child happened to call first. The
    router keys on the system prompt, which is the one thing that identifies
    which agent is asking: the parent's carries its instructions, and every
    composed child's carries its own purpose (``child_instructions``).

    Nothing here weakens the fake. Each route is a full ``FakeModel`` with its
    own script, and running past the end of one still raises rather than
    silently repeating a turn.
    """

    def __init__(self, routes: dict[str, FakeModel], default: FakeModel) -> None:
        self._routes = routes
        self._default = default

    def _pick(self, request: ModelRequest) -> FakeModel:
        prompt = request.messages[0].content if request.messages else ""
        for marker, model in self._routes.items():
            if marker in prompt:
                return model
        return self._default

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._pick(request).stream(request)

    async def known_models(self) -> list[str]:
        return ["fake-standard"]


def registry_with_gate(gate: asyncio.Event, calls: list[str]) -> ToolRegistry:
    """Tools for these tests, one of which blocks until the test says so.

    The gate is what makes "a child is still running" a fact rather than a race:
    a test that steered a child it hoped had not finished yet would pass or fail
    depending on how the event loop interleaved two Runs.
    """
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    async def hold(label: str) -> str:
        """Wait until the test releases this."""
        calls.append(f"hold {label}")
        await gate.wait()
        return f"released {label}"

    @registry.register(annotations={"read-only"})
    def lookup(order_id: str) -> str:
        """Look up an order."""
        calls.append(f"looked up {order_id}")
        return f"{order_id} is shipped"

    @registry.register(annotations={"destructive"})
    def refund(order_id: str) -> str:
        """Refund an order."""
        calls.append(f"refunded {order_id}")
        return f"refunded {order_id}"

    return registry


def coordinator(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "coordinator",
        "instructions": "Coordinate the research.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"), CodeTool(name="hold"), CodeTool(name="refund")),
        "spawn": SpawnEnvelope(tools=("lookup", "hold")),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def spawn_call(name: str, purpose: str, task: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    arguments: dict[str, Any] = {
        "name": name,
        "purpose": purpose,
        "task": task,
        "deliverable": "The answer, plus where you got it.",
    }
    arguments.update(kwargs)
    return ("spawn_subagent", arguments)


async def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    """Poll a condition the way a consumer's own code would.

    Every wait in this file goes through the store, because a test that waited
    on an internal event would prove something about this process rather than
    about what is durably recorded.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def _two_children_scripted() -> RoutedModel:
    """The script for the headline case: a parent, and the two children it writes.

    Token counts are real rather than left at zero, so "rolled up through the
    tree" is a number the test can check instead of a sum of zeros that would
    pass whatever the rollup did.
    """
    parent = (
        FakeModel()
        .turn(
            text="I will split this.",
            tool_calls=[
                spawn_call(
                    "alpha",
                    "Price part 88-B across our suppliers.",
                    "Find the list price of part 88-B from every supplier we use.",
                    tools=["hold"],
                ),
                spawn_call(
                    "beta",
                    "Check the delivery status of order A1.",
                    "Look up order A1 and say where it is and when it arrives.",
                    tools=["lookup"],
                ),
            ],
        )
        .turn(
            tool_calls=[
                ("check_subagent", {"name": "alpha"}),
                (
                    "message_subagent",
                    {"name": "alpha", "message": "Only suppliers we have a contract with."},
                ),
            ]
        )
        # Nothing left to do while the children work: this turn is what makes
        # the Run suspend rather than complete over them.
        .turn(text="Waiting for both of them.", usage=Usage(input=200, output=8))
        .turn(
            text="Alpha and beta are both back; here is the summary.",
            usage=Usage(input=260, output=30),
        )
    )
    alpha = (
        FakeModel()
        .turn(tool_calls=[("hold", {"label": "alpha"})], usage=Usage(input=100, output=10))
        .turn(
            text="Six suppliers, cheapest is Acme at 12.40.",
            usage=Usage(input=120, output=20),
        )
    )
    beta = FakeModel().turn(
        text="Order A1 is shipped and arrives Tuesday.", usage=Usage(input=90, output=15)
    )
    return RoutedModel(
        {"Price part 88-B": alpha, "Check the delivery status": beta}, default=parent
    )


class TestTwoChildrenInTheBackground:
    """The headline case: spawn two, steer one, and read the whole tree back."""

    async def test_a_parent_spawns_two_children_steers_one_and_reports_the_tree(
        self,
    ) -> None:
        store = InMemoryStore()
        gate = asyncio.Event()
        calls: list[str] = []
        registry = registry_with_gate(gate, calls)

        model = _two_children_scripted()

        version = await psych_runtime.publish(store, coordinator())
        run = await psych_runtime.dispatch(
            store, version, SCOPE, input={"message": "price and status"}
        )

        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            # The parent suspends on its children rather than spinning or
            # holding its lease while they work (DESIGN.md §11).
            await _wait_until(lambda: _is_waiting_on_children(store, run.run_id))
            header = await store.get_run(run.run_id)
            assert header is not None
            assert header.state is RunState.SUSPENDED

            # Alpha is still alive, so the steer reached a Run that can still
            # read it -- and it is in alpha's own log as a queue entry.
            state = await psych_runtime.state(store, run.run_id)
            alpha_id = _child_id(state, "alpha")
            await _wait_until(lambda: _has_queue_entry(store, alpha_id))

            gate.set()
            await _wait_until(lambda: _is_settled(store, run.run_id), timeout=10)
        finally:
            gate.set()
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        report = await psych_runtime.report(store, run.run_id, child_depth=1)
        assert report.terminal_state is TerminalState.COMPLETED

        names = [child.name for child in report.subagents]
        assert names == ["alpha", "beta"]

        by_name = {child.name: child for child in report.subagents}
        assert by_name["alpha"].terminal_state is TerminalState.COMPLETED
        assert by_name["beta"].terminal_state is TerminalState.COMPLETED
        assert by_name["alpha"].messages_sent == 1
        assert by_name["beta"].messages_sent == 0

        # Narrowed, not granted: each child holds what it asked for, inside the
        # envelope, inside what the parent itself holds. Neither was given
        # `refund`, which the parent holds and the envelope does not offer.
        assert by_name["alpha"].tools == ("hold",)
        assert by_name["beta"].tools == ("lookup",)
        assert "refunded A1" not in calls

        # Each child is its own Run, at depth 1, naming its parent, pinning the
        # Version its parent composed.
        for child in report.subagents:
            assert child.report is not None
            assert child.delegation_depth == 1
            assert child.report.version_hash == child.child_version_hash
            child_state = await psych_runtime.state(store, child.child_run_id)
            assert child_state.parent_run_id == run.run_id

        # Tokens and cost roll up through the tree, and the rollup says so.
        assert report.subtree is not None
        assert report.subtree.runs == 3
        assert report.subtree.complete
        own = report.totals.usage.total_billable
        rolled = report.subtree.usage.total_billable
        assert rolled > own
        assert rolled == own + sum(
            child.report.totals.usage.total_billable
            for child in report.subagents
            if child.report is not None
        )

    async def test_the_whole_tree_is_reconstructable_from_the_parents_log_alone(
        self,
    ) -> None:
        """No side table: every spawn, message and result is a Record."""
        store = InMemoryStore()
        gate = asyncio.Event()
        gate.set()
        registry = registry_with_gate(gate, [])

        parent_model = (
            FakeModel()
            .turn(
                tool_calls=[
                    spawn_call(
                        "beta",
                        "Check the delivery status of order A1.",
                        "Look up order A1 and say where it is and when it arrives.",
                        tools=["lookup"],
                    )
                ]
            )
            .turn(text="Waiting.")
            .turn(text="Beta says it ships Tuesday.")
        )
        beta = FakeModel().turn(text="Order A1 is shipped.")
        model = RoutedModel({"Check the delivery status": beta}, default=parent_model)

        version = await psych_runtime.publish(store, coordinator())
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "status?"})
        worker = Worker(
            store,
            Runtime(store=store, model=model, registry=registry),
            poll_interval=0.01,
            supervisor_interval=0.05,
        )
        task = asyncio.create_task(worker.run())
        try:
            await _wait_until(lambda: _is_settled(store, run.run_id), timeout=10)
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        kinds = [type(record).__name__ for record in await store.read(run.run_id)]
        assert kinds.count("SubagentSpawned") == 1
        assert kinds.count("SubagentFinished") == 1
        assert kinds.index("SubagentSpawned") < kinds.index("Suspended")
        assert kinds.index("Suspended") < kinds.index("SubagentFinished")
        assert kinds.index("SubagentFinished") < kinds.index("Resumed")


class TestNarrowingAcrossTheBoundary:
    """A parent must not be able to write itself a child with more access."""

    async def test_a_child_cannot_be_given_a_tool_the_envelope_withholds(self) -> None:
        store = InMemoryStore()
        gate = asyncio.Event()
        gate.set()
        calls: list[str] = []
        registry = registry_with_gate(gate, calls)

        parent_model = (
            FakeModel()
            .turn(
                tool_calls=[
                    spawn_call(
                        "greedy",
                        "Refund the order the customer complained about.",
                        "Refund order A1 immediately and confirm that it went through.",
                        tools=["refund", "lookup"],
                    )
                ]
            )
            .turn(text="Waiting.")
            .turn(text="It could not do that.")
        )
        # The child tries anyway. It was never offered the tool, so the call is
        # refused as data and nothing is refunded.
        child = (
            FakeModel()
            .turn(tool_calls=[("refund", {"order_id": "A1"})])
            .turn(text="I do not have a refund tool.")
        )
        model = RoutedModel({"Refund the order": child}, default=parent_model)

        version = await psych_runtime.publish(store, coordinator())
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "refund A1"})
        worker = Worker(
            store,
            Runtime(store=store, model=model, registry=registry),
            poll_interval=0.01,
            supervisor_interval=0.05,
        )
        task = asyncio.create_task(worker.run())
        try:
            await _wait_until(lambda: _is_settled(store, run.run_id), timeout=10)
        finally:
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        report = await psych_runtime.report(store, run.run_id, child_depth=1)
        composed = report.subagents[0]
        assert composed.tools == ("lookup",)

        assert composed.report is not None
        refusal = composed.report.tool_calls[0]
        assert refusal.tool == "refund"
        assert refusal.failure is not None
        assert refusal.failure.kind == "unknown_tool"
        assert calls == []  # nothing was refunded, by anyone


class TestCrashWithChildrenInFlight:
    """A parent killed mid-tree is reclaimed and resumes it, never re-spawns it.

    Copied in shape from ``tests/e2e/test_durability.py``'s crash case: a dead
    Worker is simulated by writing exactly what one leaves behind -- records that
    stop, and a lease that expires without a terminal record.
    """

    async def test_a_reclaimed_parent_waits_on_the_child_it_already_started(self) -> None:
        store = InMemoryStore()
        gate = asyncio.Event()
        registry = registry_with_gate(gate, [])

        version = await psych_runtime.publish(store, coordinator())
        run = await psych_runtime.dispatch(
            store, version, SCOPE, input={"message": "price part 88-B"}
        )

        # A child, admitted by the Attempt that is about to die. Dispatched
        # exactly as `_ComposedChildren.spawn` dispatches one: background, this
        # Run's parent, depth 1.
        child_spec = AgentSpec(
            name="alpha",
            instructions="You are a subagent. Your job: Price part 88-B across our suppliers.",
            model=ModelRef(model="fake-standard"),
            tools=(CodeTool(name="hold"),),
        )
        child_version = await psych_runtime.publish(store, child_spec)
        # `psych_runtime.dispatch` does not take a parent: naming one is the runtime's
        # own business, not a consumer's (see `psych_runtime.runtime.dispatch`).
        child = await dispatch_run(
            store,
            child_version,
            SCOPE,
            input={"message": "Find the list price of part 88-B."},
            parent_run_id=run.run_id,
            delegation_depth=1,
        )

        first = await Journal.open(store, run.run_id, SCOPE)
        await first.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
        await first.append(type="turn_started", turn=1)
        await first.append(type="model_call_started", turn=1, model="fake-standard")
        await first.append(
            type="model_call_finished",
            turn=1,
            model="fake-standard",
            usage={"input": 10},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await first.append(
            type="tool_call_started",
            call_id="call-1",
            tool="spawn_subagent",
            arguments={"name": "alpha"},
            turn=1,
        )
        await first.append(
            type="subagent_spawned",
            child_run_id=child.run_id,
            name="alpha",
            call_id="call-1",
            child_version_hash=child_version.hash,
            purpose="Price part 88-B across our suppliers.",
            task="Find the list price of part 88-B from every supplier we use.",
            deliverable="A list of supplier and price.",
            tools=("hold",),
            model="fake-standard",
            delegation_depth=1,
        )
        await first.append(
            type="tool_call_finished",
            call_id="call-1",
            outcome="ok",
            result={"name": "alpha", "run_id": child.run_id, "state": "running"},
        )
        # The lease expires with no terminal record. That is what a kill looks
        # like from the log's side.
        await store.release(run.run_id, WorkerId("wrk_dead"), RunState.RUNNABLE)

        # The reclaiming Worker's parent script never spawns anything: if the
        # tree were re-created rather than resumed, there would be a second
        # child and this script would be the thing that noticed.
        parent_model = FakeModel().turn(text="Still waiting.").turn(text="Acme at 12.40.")
        alpha = (
            FakeModel()
            .turn(tool_calls=[("hold", {"label": "alpha"})])
            .turn(text="Six suppliers, cheapest is Acme at 12.40.")
        )
        model = RoutedModel({"Price part 88-B": alpha}, default=parent_model)

        worker = Worker(
            store,
            Runtime(store=store, model=model, registry=registry),
            poll_interval=0.01,
            supervisor_interval=0.05,
        )
        task = asyncio.create_task(worker.run())
        try:
            await _wait_until(lambda: _is_waiting_on_children(store, run.run_id), timeout=10)
            gate.set()
            await _wait_until(lambda: _is_settled(store, run.run_id), timeout=10)
        finally:
            gate.set()
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        report = await psych_runtime.report(store, run.run_id, child_depth=1)
        assert report.terminal_state is TerminalState.COMPLETED

        # One child, the one the dead Attempt started, resumed rather than
        # re-spawned.
        spawns = [r for r in await store.read(run.run_id) if isinstance(r, SubagentSpawned)]
        assert len(spawns) == 1
        assert len(report.subagents) == 1
        assert report.subagents[0].child_run_id == child.run_id
        assert report.subagents[0].terminal_state is TerminalState.COMPLETED
        assert report.subagents[0].child_version_hash == child_version.hash

        # And the parent really was picked up a second time.
        state = await psych_runtime.state(store, run.run_id)
        assert state.attempt_count >= 2


async def _is_waiting_on_children(store: InMemoryStore, run_id: RunId) -> bool:
    records = await store.read(run_id)
    if not records:
        return False
    state = reduce(records, run_id=run_id)
    return state.suspended and state.suspend_reason is SuspendReason.CHILDREN


async def _is_settled(store: InMemoryStore, run_id: RunId) -> bool:
    records = await store.read(run_id)
    return bool(records) and reduce(records, run_id=run_id).settled


async def _has_queue_entry(store: InMemoryStore, run_id: RunId) -> bool:
    return any(isinstance(record, QueueEnqueued) for record in await store.read(run_id))


def _child_id(state: RunStateView, name: str) -> RunId:
    for child in state.children.values():
        if child.name == name:
            return child.run_id
    raise AssertionError(f"no child named {name!r}")
