"""End to end: the durability guarantees from DESIGN.md §23.

These are the items the v1 definition of done names by number:

- item 2: a Run survives its Worker being killed mid-tool-call. A second Worker
  reclaims the expired lease, replays the log, settles the dangling call and
  completes.
- item 3: an interrupt during a tool call stops the Run, a message sent
  immediately after starts a new Run carrying it, and both are visible in order.
- item 4: a client reconnecting with ``after=N`` receives every later record and
  misses none, including across an interrupt.
- item 5: a workflow with a nested agent resumes from its last completed step
  after a crash and does not re-execute completed steps.

Each is asserted through the log or through the public API, never through
internal state, because internal state is exactly what a crash destroys.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.ids import RunId, WorkerId
from psych_runtime.core.records import (
    RunSettled,
    TerminalState,
    ToolOutcome,
)
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, AgentStep, CodeTool, ModelRef, ToolStep, WorkflowSpec
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")


def registry_with_counter(counts: dict[str, int]) -> ToolRegistry:
    """A registry whose tools count their own executions.

    Memoisation is only provable by counting: a step that "did not re-run" looks
    exactly like a step that ran again and produced the same answer, unless
    something counted.
    """
    registry = ToolRegistry()

    @registry.register
    def slow_step(label: str) -> str:
        """Do a unit of work."""
        counts[label] = counts.get(label, 0) + 1
        return f"{label}-done"

    @registry.register
    def lookup(order_id: str) -> str:
        """Look up an order."""
        counts["lookup"] = counts.get("lookup", 0) + 1
        return f"{order_id} shipped"

    return registry


def agent_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="lookup"),),
    }
    base.update(kwargs)
    return AgentSpec(**base)


class TestCrashMidToolCall:
    """DESIGN.md §23, item 2."""

    async def test_a_second_worker_reclaims_replays_settles_and_completes(self) -> None:
        store = InMemoryStore()
        counts: dict[str, int] = {}
        registry = registry_with_counter(counts)
        spec = agent_spec()
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "where is A1?"})

        # Worker one gets as far as starting a tool call, then dies. Simulated by
        # writing exactly what a dead Worker leaves behind and never settling.
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
            tool="lookup",
            arguments={"order_id": "A1"},
            turn=1,
        )
        # Its lease expires without a release, which is what a kill looks like.
        await store.release(run.run_id, WorkerId("wrk_dead"), RunState.RUNNABLE)

        model = FakeModel().turn(text="I could not confirm that order.")
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)

        task = asyncio.create_task(worker.run())
        await _wait_for_settled(store, run.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        state = await psych_runtime.state(store, run.run_id)
        assert state.settled
        assert state.terminal_state is TerminalState.COMPLETED
        assert state.attempt_count == 2  # the dead one, and the reclaiming one
        assert not state.has_dangling_tool_calls
        assert state.tool_results[0].outcome is ToolOutcome.UNKNOWN
        # The reclaiming Worker did not re-run a call it could not prove was safe.
        assert counts.get("lookup", 0) == 0


class TestInterruptThenNewMessage:
    """DESIGN.md §23, item 3."""

    async def test_an_interrupt_stops_the_run_and_the_next_message_starts_a_new_one(
        self,
    ) -> None:
        store = InMemoryStore()
        registry = registry_with_counter({})
        spec = agent_spec()
        version = await psych_runtime.publish(store, spec)

        first = await psych_runtime.dispatch(
            store, version, SCOPE, input={"message": "where is A1?"}
        )
        await psych_runtime.interrupt(store, first.run_id, reason="the user pressed stop")

        second = await psych_runtime.dispatch(
            store, version, SCOPE, input={"message": "actually A2"}
        )

        model = FakeModel().turn(text="A2 is out for delivery.")
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _wait_for_settled(store, first.run_id)
        await _wait_for_settled(store, second.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        stopped = await psych_runtime.state(store, first.run_id)
        assert stopped.terminal_state is TerminalState.ABORTED

        carried = await psych_runtime.state(store, second.run_id)
        assert carried.terminal_state is TerminalState.COMPLETED

        # Both visible in the log, in order.
        records = await store.read(first.run_id)
        kinds = [type(r).__name__ for r in records]
        assert kinds.index("AbortRequested") < kinds.index("RunSettled")
        assert isinstance(records[-1], RunSettled)


class TestReconnectingStream:
    """DESIGN.md §23, item 4."""

    async def test_a_client_reconnecting_after_n_misses_nothing(self) -> None:
        store = InMemoryStore()
        registry = registry_with_counter({})
        spec = agent_spec()
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})

        model = (
            FakeModel().turn(tool_calls=[("lookup", {"order_id": "A1"})]).turn(text="A1 shipped.")
        )
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        await _wait_for_settled(store, run.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        whole = [record async for record in psych_runtime.stream(store, run.run_id)]
        assert [record.seq for record in whole] == list(range(1, len(whole) + 1))

        # Every reconnection point yields exactly the remainder, with no gap and
        # no duplicate.
        for cut in range(len(whole)):
            resumed = [
                record async for record in psych_runtime.stream(store, run.run_id, after=cut)
            ]
            assert [r.seq for r in resumed] == [r.seq for r in whole[cut:]]

    async def test_the_stream_survives_an_interrupt_in_the_middle(self) -> None:
        store = InMemoryStore()
        spec = agent_spec()
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "hi"})

        journal = await Journal.open(store, run.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="w", attempt_number=1)
        await journal.append(type="abort_requested", reason="stop")
        await journal.append(type="run_settled", state=TerminalState.ABORTED)

        after_abort = [record async for record in psych_runtime.stream(store, run.run_id, after=1)]
        kinds = [type(r).__name__ for r in after_abort]
        assert "AbortRequested" in kinds
        assert kinds[-1] == "RunSettled"


class TestWorkflowResume:
    """DESIGN.md §23, item 5."""

    async def test_a_crashed_workflow_resumes_from_its_last_completed_step(self) -> None:
        store = InMemoryStore()
        counts: dict[str, int] = {}
        registry = registry_with_counter(counts)

        workflow = WorkflowSpec(
            name="nightly",
            tools=(CodeTool(name="slow_step"),),
            steps=(
                ToolStep(name="one", tool="slow_step", arguments={"label": "one"}),
                ToolStep(name="two", tool="slow_step", arguments={"label": "two"}),
                AgentStep(name="summarise", spec=agent_spec(name="summariser", tools=())),
            ),
        )
        version = await psych_runtime.publish(store, workflow)
        run = await psych_runtime.dispatch(store, version, SCOPE)

        # First attempt: the first two steps land, then the process is killed
        # while the third is in flight. Cancelling the task is what that looks
        # like from the log's side: records simply stop.
        model_one = FakeModel().stalls(seconds=30)
        runtime = Runtime(store=store, model=model_one, registry=registry)
        journal = await Journal.open(store, run.run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)

        engine = runtime.engine_for(journal)
        attempt = asyncio.create_task(engine.run(workflow))
        for _ in range(500):
            await asyncio.sleep(0.005)
            if counts.get("one") and counts.get("two"):
                break
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt

        assert counts == {"one": 1, "two": 1}
        killed = await psych_runtime.state(store, run.run_id)
        assert not killed.settled
        assert len([s for s in killed.steps.values() if s.completed]) == 2

        # Second attempt: a fresh Worker replays the log.
        model_two = FakeModel().turn(text="Two steps ran.")
        runtime_two = Runtime(store=store, model=model_two, registry=registry)
        worker = Worker(store, runtime_two, poll_interval=0.01, supervisor_interval=0.05)
        await store.release(run.run_id, WorkerId("wrk_1"), RunState.RUNNABLE)
        task = asyncio.create_task(worker.run())
        await _wait_for_settled(store, run.run_id)
        worker.stop()
        await asyncio.wait_for(task, timeout=5)

        # The completed steps did not run again.
        assert counts == {"one": 1, "two": 1}

        state = await psych_runtime.state(store, run.run_id)
        assert state.terminal_state is TerminalState.COMPLETED
        completed = [s for s in state.steps.values() if s.completed]
        assert {s.name for s in completed} == {"one", "two", "summarise"}

    async def test_a_step_id_is_stable_across_attempts(self) -> None:
        """Everything above rests on this. A random step id would make a resumed
        Run compute new ids, find nothing memoised, and redo the work."""
        from psych_runtime.core.ids import derive_step_id

        run_id = RunId("run_fixed")
        path = ("nightly", 0, "one")
        assert derive_step_id(run_id, path) == derive_step_id(run_id, path)
        assert derive_step_id(run_id, path) != derive_step_id(RunId("run_other"), path)
        assert derive_step_id(run_id, path) != derive_step_id(run_id, ("nightly", 1, "one"))


class TestIdempotentAdmission:
    """DESIGN.md §8.3: delivery is at-least-once and pretending otherwise
    produces double refunds."""

    async def test_the_same_key_admits_one_run(self) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(store, agent_spec())

        first = await psych_runtime.dispatch(store, version, SCOPE, idempotency_key="order-42")
        second = await psych_runtime.dispatch(store, version, SCOPE, idempotency_key="order-42")

        assert first.run_id == second.run_id
        assert first.created
        assert not second.created

    async def test_no_key_means_a_new_run_every_time(self) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(store, agent_spec())
        first = await psych_runtime.dispatch(store, version, SCOPE)
        second = await psych_runtime.dispatch(store, version, SCOPE)
        assert first.run_id != second.run_id


class TestPublishIdempotence:
    """DESIGN.md §23, item 1, and what makes redeploying on every boot harmless."""

    async def test_republishing_an_identical_spec_returns_the_same_version(self) -> None:
        store = InMemoryStore()
        first = await psych_runtime.publish(store, agent_spec())
        second = await psych_runtime.publish(store, agent_spec())
        assert first.hash == second.hash
        assert first.published_at == second.published_at  # the stored one came back

    async def test_a_spec_built_from_a_dict_produces_the_same_version(self) -> None:
        store = InMemoryStore()
        in_python = await psych_runtime.publish(store, agent_spec())
        from_dict = await psych_runtime.publish(
            store,
            AgentSpec.model_validate(
                {
                    "kind": "agent",
                    "name": "support",
                    "instructions": "Help.",
                    "model": {"model": "fake-standard"},
                    "tools": [{"kind": "code", "name": "lookup"}],
                }
            ),
        )
        assert in_python.hash == from_dict.hash


async def _wait_for_settled(store: InMemoryStore, run_id: RunId, timeout: float = 5.0) -> None:
    """Poll until the Run has a terminal record, or give up.

    Polling rather than an event because that is what a consumer's own code would
    do, and a test that used an internal signal would not prove the Run is
    observably finished.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        records = await store.read(run_id)
        if records and reduce(records).settled:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")


class TestInterruptWhileAWorkerIsExecuting:
    """DESIGN.md §23 item 3, against a Worker that is actually running the Run.

    Every existing interrupt test aborts a Run *before* any Worker claims it,
    which never exercises the case a person actually hits: pressing stop while
    a tool call is in flight. That path used to fail the Run as
    ``attempt_failed`` -- the consumer's abort record took the sequence the
    Worker's next append wanted, the Worker raised ``SeqConflict``, and the
    last-resort handler settled it FAILED. The abort now lands at the tool-call
    boundary and the Run settles ABORTED.
    """

    async def test_an_interrupt_mid_tool_call_aborts_rather_than_failing(self) -> None:
        store = InMemoryStore()
        started = asyncio.Event()
        release = asyncio.Event()
        registry = ToolRegistry()
        second_ran = False

        @registry.register
        async def slow_lookup(order_id: str) -> str:
            """Look something up, slowly."""
            started.set()
            await release.wait()
            return f"{order_id} shipped"

        @registry.register
        async def never_reached(order_id: str) -> str:
            """The second call in the same turn."""
            nonlocal second_ran
            second_ran = True
            return "should not run"

        spec = agent_spec(tools=(CodeTool(name="slow_lookup"), CodeTool(name="never_reached")))
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "where is A1?"})

        model = FakeModel().turn(
            tool_calls=[
                ("slow_lookup", {"order_id": "A1"}),
                ("never_reached", {"order_id": "A2"}),
            ]
        )
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            # The interrupt lands while the first call is genuinely running.
            await psych_runtime.interrupt(store, run.run_id, reason="the user pressed stop")
            release.set()
            await _wait_for_settled(store, run.run_id)
        finally:
            release.set()
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        state = await psych_runtime.state(store, run.run_id)
        assert state.terminal_state is TerminalState.ABORTED
        assert not second_ran, "the turn kept going after the abort"

        # Every call the model asked for has a result, including the one that
        # never ran: a provider rejects an assistant message whose tool calls
        # are unanswered, so the conversation stays replayable.
        answered = {result.tool: result.outcome for result in state.tool_results}
        assert answered["slow_lookup"] is ToolOutcome.OK
        assert answered["never_reached"] is ToolOutcome.ERROR

        records = [type(r).__name__ for r in await store.read(run.run_id)]
        assert records.index("AbortRequested") < records.index("RunSettled")


class TestGracefulShutdownHandsTheRunOn:
    """A Worker asked to stop must not settle the Runs it was holding.

    Every rolling deploy calls ``Worker.stop()``. Treating that as the deadline
    ended every in-flight conversation with "the run passed its deadline"; the
    Run should simply go back to being claimable and continue on the next
    Worker, which is the crash-recovery path with the crash left out.
    """

    async def test_a_run_in_flight_survives_its_worker_stopping(self) -> None:
        store = InMemoryStore()
        started = asyncio.Event()
        release = asyncio.Event()
        registry = ToolRegistry()

        @registry.register(safe_to_retry=True)
        async def slow_lookup(order_id: str) -> str:
            """Look something up, slowly. Safe to run twice."""
            started.set()
            await release.wait()
            return f"{order_id} shipped"

        spec = agent_spec(tools=(CodeTool(name="slow_lookup"),))
        version = await psych_runtime.publish(store, spec)
        run = await psych_runtime.dispatch(store, version, SCOPE, input={"message": "where is A1?"})

        first_model = FakeModel().turn(tool_calls=[("slow_lookup", {"order_id": "A1"})])
        first = Worker(
            store,
            Runtime(store=store, model=first_model, registry=registry),
            poll_interval=0.01,
            supervisor_interval=0.05,
        )
        first_task = asyncio.create_task(first.run())
        await asyncio.wait_for(started.wait(), timeout=5)
        first.stop()
        release.set()
        await asyncio.wait_for(first_task, timeout=5)

        state = await psych_runtime.state(store, run.run_id)
        assert not state.settled, "a graceful shutdown settled a Run it should have handed on"
        header = await store.get_run(run.run_id)
        assert header is not None
        assert header.state is RunState.RUNNABLE

        # A second Worker picks it up and finishes it.
        second_model = FakeModel().turn(text="A1 shipped.")
        second = Worker(
            store,
            Runtime(store=store, model=second_model, registry=registry),
            poll_interval=0.01,
            supervisor_interval=0.05,
        )
        second_task = asyncio.create_task(second.run())
        await _wait_for_settled(store, run.run_id)
        second.stop()
        await asyncio.wait_for(second_task, timeout=5)

        final = await psych_runtime.state(store, run.run_id)
        assert final.terminal_state is TerminalState.COMPLETED
        # The tool said it was safe to retry, so the reclaiming Worker ran it
        # rather than recording an unknown outcome and discarding the answer.
        assert [r.outcome for r in final.tool_results] == [ToolOutcome.OK]
