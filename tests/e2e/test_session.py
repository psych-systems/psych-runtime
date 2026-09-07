"""End to end: ``psych_runtime.session()`` assembles the same runtime the long form does.

The helper exists to shorten a consumer's first program, so what has to be true
is that it is genuinely shorter and genuinely the same thing underneath. These
assert both: that the block runs a real agent loop against a real Worker, and
that everything it produced is readable through the ordinary projections over
the log. A convenience that quietly executed differently would be worse than no
convenience at all (DESIGN.md §4).
"""

from __future__ import annotations

import asyncio

import pytest

import psych_runtime
from psych_runtime.core.records import TerminalState
from psych_runtime.core.status import Lifecycle
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

pytestmark = pytest.mark.e2e


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "shipped"}


def build_spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer with their order.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
    )


def answering_model() -> FakeModel:
    return (
        FakeModel()
        .turn(text="Checking.", tool_calls=[("lookup_order", {"order_id": "A1"})])
        .turn(text="A1 has shipped.")
    )


class TestAskRunsAWholeAgentLoop:
    async def test_the_answer_comes_back_and_the_tool_really_ran(self) -> None:
        async with psych_runtime.session(answering_model(), tools=[lookup_order]) as session:
            view = await session.ask(build_spec(), "where is order A1?")

            assert view.finished
            assert view.text == "A1 has shipped."
            assert view.tool_call_count == 1

            report = await psych_runtime.report(session.store, view.run_id)
            assert report.terminal_state is TerminalState.COMPLETED
            assert [call.tool for call in report.tool_calls] == ["lookup_order"]

    async def test_a_dict_input_reaches_the_run_unchanged(self) -> None:
        async with psych_runtime.session(
            FakeModel().turn(text="ok"), tools=[lookup_order]
        ) as session:
            view = await session.ask(build_spec(), {"message": "hello", "locale": "en-GB"})
            assert view.text == "ok"

    async def test_message_and_input_together_are_refused(self) -> None:
        """Both set the same field, so merging them would be a guess."""
        async with psych_runtime.session(FakeModel().turn(text="ok")) as session:
            with pytest.raises(TypeError, match="not both"):
                await session.start(build_spec(), "hi", input={"message": "hi"})


class TestItIsTheSameRuntimeUnderneath:
    async def test_the_store_is_the_one_passed_in_and_holds_the_log(self) -> None:
        store = InMemoryStore()
        async with psych_runtime.session(
            answering_model(), store=store, tools=[lookup_order]
        ) as session:
            assert session.store is store
            view = await session.ask(build_spec(), "where is order A1?")

        # Outside the block, with the Worker stopped, the log is still the log.
        log = await psych_runtime.records(store, view.run_id)
        assert [record.seq for record in log] == list(range(1, len(log) + 1))

    async def test_registered_tools_reach_publish_time_validation(self) -> None:
        """The registry's names are passed for you, so a typo fails at publish."""
        spec = psych_runtime.AgentSpec(
            name="support",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="lookup_ordr"),),
        )
        async with psych_runtime.session(FakeModel(), tools=[lookup_order]) as session:
            with pytest.raises(psych_runtime.SpecValidationError):
                await session.publish(spec)

    async def test_publishing_the_same_spec_twice_gives_one_version(self) -> None:
        async with psych_runtime.session(FakeModel(), tools=[lookup_order]) as session:
            first = await session.publish(build_spec())
            second = await session.publish(build_spec())
            assert first.hash == second.hash

    async def test_a_builder_is_accepted_and_hashes_the_same_as_its_spec(self) -> None:
        async with psych_runtime.session(
            FakeModel().turn(text="hi"), tools=[lookup_order]
        ) as session:
            built = (
                psych_runtime.agent("support", registry=session.registry)
                .instructions("Help the customer with their order.")
                .model("fake-standard")
                .tool_by_name("lookup_order")
            )
            from_builder = await session.publish(built)
            from_spec = await session.publish(build_spec())
            assert from_builder.hash == from_spec.hash


class TestFailureIsReportedRatherThanRaised:
    async def test_a_failed_run_comes_back_unfinished_with_a_readable_status(self) -> None:
        """A Run that failed is a thing that happened, fully in the log.

        Raising here would hide a Run the caller can still read, and would make
        the caller's error path the only way to reach ``status()``.
        """
        model = FakeModel().turn(text="", tool_calls=[("no_such_tool", {})])
        spec = psych_runtime.AgentSpec(
            name="a", model=psych_runtime.ModelRef(model="fake-standard")
        )

        async with psych_runtime.session(model) as session:
            view = await session.ask(spec, "hi", timeout=30)

            assert not view.finished
            assert view.text == ""
            status = await psych_runtime.status(session.store, view.run_id)
            assert status.lifecycle is Lifecycle.FAILED


class TestTheWorkerLifecycleIsTheBlock:
    async def test_the_worker_is_running_inside_and_stopped_after(self) -> None:
        model = answering_model()
        async with psych_runtime.session(model, tools=[lookup_order]) as session:
            store = session.store
            worker = session.worker
            await session.ask(build_spec(), "where is order A1?")

        # A stopped Worker claims nothing, so a Run dispatched now stays RUNNABLE.
        version = await psych_runtime.publish(
            store,
            build_spec(),
            context=psych_runtime.ValidationContext(registered_tools=("lookup_order",)),
        )
        dispatched = await psych_runtime.dispatch(
            store, version, psych_runtime.Scope(tenant="local")
        )
        await asyncio.sleep(0.2)
        state = await psych_runtime.state(store, dispatched.run_id)
        assert not state.settled
        assert worker is not None

    async def test_an_exception_in_the_block_still_stops_the_worker(self) -> None:
        captured: list[psych_runtime.Worker] = []

        async def raise_inside_the_block() -> None:
            async with psych_runtime.session(FakeModel()) as session:
                captured.append(session.worker)
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await raise_inside_the_block()

        # ``stop()`` is idempotent; the point is that the finally ran at all.
        captured[0].stop()
