"""``stream_text()`` yields the words and refuses to hide anything else.

The projection exists because every consumer writes this loop and every one of
them gets the same three things wrong: assistant text arrives on
`model_call_finished` rather than a record named for text, an abort is a Record
rather than an exception, and the iterator ends when the Run *settles* rather
than when the model stops talking.

The tests that matter here are the unhappy ones. A version of this that yielded
nothing and returned cleanly on an aborted Run would pass a naive happy-path
test and ship the exact bug the helper exists to prevent: a blank reply, no
error, and a log that says precisely what went wrong.
"""

from __future__ import annotations

import asyncio

import pytest

import psych_runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

pytestmark = pytest.mark.e2e

SCOPE = psych_runtime.Scope(tenant="acme", principal="user-1")


async def lookup_order(order_id: str) -> dict[str, str]:
    """Look up an order by its id."""
    return {"order_id": order_id, "status": "shipped"}


def build_spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
    )


async def collect(store: psych_runtime.Store, run_id: psych_runtime.RunId) -> list[str]:
    return [delta async for delta in psych_runtime.stream_text(store, run_id, scope=SCOPE)]


class TestItYieldsTheWords:
    async def test_only_assistant_text_reaches_the_caller(self) -> None:
        """Tool calls and their results happen; none of them is a word to render."""
        model = (
            FakeModel()
            .turn(text="Checking.", tool_calls=[("lookup_order", {"order_id": "A1"})])
            .turn(text="A1 has shipped.")
        )
        async with psych_runtime.session(model, tools=[lookup_order], tenant="acme") as session:
            started = await session.start(build_spec(), "where is order A1?", scope=SCOPE)
            deltas = await collect(session.store, started.run_id)

        assert deltas == ["Checking.", "A1 has shipped."]

    async def test_it_ends_when_the_run_settles_not_when_the_model_stops(self) -> None:
        """The iterator returning is the Run being over, not a pause in output."""
        model = FakeModel().turn(text="Done.")
        async with psych_runtime.session(model, tenant="acme") as session:
            spec = psych_runtime.AgentSpec(
                name="a", model=psych_runtime.ModelRef(model="fake-standard")
            )
            started = await session.start(spec, "hi", scope=SCOPE)
            await collect(session.store, started.run_id)

            status = await psych_runtime.status(session.store, started.run_id, scope=SCOPE)
            assert status.lifecycle is psych_runtime.Lifecycle.DONE

    async def test_after_skips_what_a_reconnecting_client_already_rendered(self) -> None:
        model = FakeModel().turn(text="first").turn(text="second")
        async with psych_runtime.session(model, tenant="acme") as session:
            spec = psych_runtime.AgentSpec(
                name="a",
                model=psych_runtime.ModelRef(model="fake-standard"),
                tools=(psych_runtime.CodeTool(name="lookup_order"),),
            )
            session.registry.register(lookup_order)
            started = await session.start(spec, "hi", scope=SCOPE)
            await session.wait(started.run_id)

            everything = await psych_runtime.records(session.store, started.run_id, scope=SCOPE)
            first_text = next(
                record for record in everything if record.type == "model_call_finished"
            )

            later = [
                delta
                async for delta in psych_runtime.stream_text(
                    session.store, started.run_id, after=first_text.seq, scope=SCOPE
                )
            ]
            assert "first" not in later


class TestItRefusesToSwallowABadEnding:
    async def test_a_failed_run_raises_rather_than_yielding_nothing(self) -> None:
        """The bug this helper exists to prevent, asserted directly.

        Returning cleanly here would leave a UI with a blank reply and no error.
        """
        model = FakeModel().turn(text="", tool_calls=[("no_such_tool", {})])
        spec = psych_runtime.AgentSpec(
            name="a", model=psych_runtime.ModelRef(model="fake-standard")
        )

        async with psych_runtime.session(model, tenant="acme") as session:
            started = await session.start(spec, "hi", scope=SCOPE)
            with pytest.raises(psych_runtime.RunFailed) as raised:
                await collect(session.store, started.run_id)

        assert raised.value.run_id == started.run_id

    async def test_an_interrupted_run_raises_and_says_it_was_aborted(self) -> None:
        """A user's stop is not an incident, so the terminal state comes with it.

        The Run has to still be executing when the interrupt lands, which is why
        the first turn calls a slow tool rather than answering: a Run that has
        already settled COMPLETED cannot be aborted, and the test would then be
        asserting nothing.
        """

        async def slow_lookup(order_id: str) -> dict[str, str]:
            """Look something up, slowly enough to be interrupted."""
            await asyncio.sleep(5)
            return {"order_id": order_id}

        model = (
            FakeModel()
            .turn(text="working", tool_calls=[("slow_lookup", {"order_id": "A1"})])
            .turn(text="never reached")
        )
        spec = psych_runtime.AgentSpec(
            name="a",
            model=psych_runtime.ModelRef(model="fake-standard"),
            tools=(psych_runtime.CodeTool(name="slow_lookup"),),
        )

        async with psych_runtime.session(model, tools=[slow_lookup], tenant="acme") as session:
            started = await session.start(spec, "hi", scope=SCOPE)
            await asyncio.sleep(0.5)
            await psych_runtime.interrupt(session.store, started.run_id, reason="user stopped")

            with pytest.raises(psych_runtime.RunAborted) as raised:
                await collect(session.store, started.run_id)

        assert raised.value.state == psych_runtime.TerminalState.ABORTED

    async def test_both_endings_share_one_base_a_caller_can_catch(self) -> None:
        """A caller who only wants "it did not answer" writes one except clause."""
        assert issubclass(psych_runtime.RunFailed, psych_runtime.RunEndedWithoutAnswer)
        assert issubclass(psych_runtime.RunAborted, psych_runtime.RunEndedWithoutAnswer)
        assert issubclass(psych_runtime.RunEndedWithoutAnswer, psych_runtime.PsychError)


class TestItIsScopedLikeEveryOtherRead:
    async def test_another_tenant_is_refused(self) -> None:
        model = FakeModel().turn(text="hello")
        spec = psych_runtime.AgentSpec(
            name="a", model=psych_runtime.ModelRef(model="fake-standard")
        )

        async with psych_runtime.session(model, tenant="acme") as session:
            started = await session.start(spec, "hi", scope=SCOPE)
            await session.wait(started.run_id)

            with pytest.raises(psych_runtime.AccessDenied):
                async for _ in psych_runtime.stream_text(
                    session.store,
                    started.run_id,
                    scope=psych_runtime.Scope(tenant="somebody-else"),
                ):
                    pass

    async def test_an_unknown_run_fails_before_the_first_delta(self) -> None:
        store = InMemoryStore()
        with pytest.raises(psych_runtime.RunNotFound):
            async for _ in psych_runtime.stream_text(store, psych_runtime.RunId("run_nope")):
                pass
