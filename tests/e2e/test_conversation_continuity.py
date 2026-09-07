"""End to end: a conversation that survives a second message.

Two things are proven here, deliberately kept apart because they were two
separate gaps:

- ``TestConversationContinuity`` proves ``psych_runtime.dispatch(continues=...)``
  actually carries an earlier Run's conversation into a new one, that
  tenancy cannot be crossed by it, and that ``Limits.max_history_records``
  bounds how much of it a turn replays.
- ``TestSteeringQueues`` proves ``psych_runtime.send()`` actually reaches the three
  queues DESIGN.md §9 defines, and that the after-abort rule (STEER and
  FOLLOW_UP refused, NEXT_RUN allowed) holds through the public function
  rather than only inside the reducer's own unit tests.

The headline case (``test_the_second_run_answers_using_only_what_the_first_run_was_told``)
is deliberately not "two Runs share a version hash" -- DESIGN.md §62's own
review note is that this proves nothing. The model here is scripted with an
answer that is correct only if the exact fact from the first Run's
conversation reached it, and the assertion reads that fact out of the second
Run's own ``ModelRequest.messages`` -- what the model was actually sent, not
what the test assumes it must have been.
"""

from __future__ import annotations

from typing import Any

import pytest

import psych_runtime
from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId
from psych_runtime.core.messages import UserMessage
from psych_runtime.core.records import QueueKind, TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, Limits, ModelRef
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE_A = Scope(tenant="acme", principal="user-1")
SCOPE_B = Scope(tenant="beta", principal="user-2")


def _spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "chat",
        "instructions": "Help the customer.",
        "model": ModelRef(model="fake-standard"),
    }
    base.update(kwargs)
    return AgentSpec(**base)


async def _drive(
    store: Store,
    spec: AgentSpec,
    model: FakeModel,
    scope: Scope,
    *,
    message: str,
    continues: RunId | None = None,
) -> RunId:
    """Publish once, dispatch, and run one Attempt to completion in-process.

    No ``Worker`` and no background task: every scripted turn here runs to
    exhaustion synchronously, so driving the single ``Runtime.__call__`` pass
    directly is enough and keeps the test free of a poll loop.
    """
    version = await psych_runtime.publish(store, spec)
    dispatched = await psych_runtime.dispatch(
        store, version, scope, input={"message": message}, continues=continues
    )
    journal = await Journal.open(store, dispatched.run_id, scope)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    runtime = Runtime(store=store, model=model, registry=ToolRegistry())
    header = await store.get_run(dispatched.run_id)
    assert header is not None
    await runtime(journal, header, AbortSignal())
    return dispatched.run_id


class TestConversationContinuity:
    async def test_the_second_run_answers_using_only_what_the_first_run_was_told(self) -> None:
        store = InMemoryStore()
        spec = _spec()

        first_model = FakeModel().turn(text="Got it, I'll keep that in mind.")
        first_run = await _drive(
            store,
            spec,
            first_model,
            SCOPE_A,
            message="My account number is ACME-7042, please remember it.",
        )
        first_report = await psych_runtime.report(store, first_run)
        assert first_report.terminal_state is TerminalState.COMPLETED

        # The second Run's *only* scripted line is correct exclusively because
        # the first Run's fact reached it -- a FakeModel that saw nothing of
        # the first Run has no way to have been scripted this specific answer
        # for this generic a question.
        second_model = FakeModel().turn(text="Your account number is ACME-7042.")
        second_run = await _drive(
            store,
            spec,
            second_model,
            SCOPE_A,
            message="What's my account number again?",
            continues=first_run,
        )
        second_report = await psych_runtime.report(store, second_run)
        assert second_report.terminal_state is TerminalState.COMPLETED
        assert second_report.continues_run_id == first_run

        # The proof: the request the model actually received carried the first
        # Run's exchange, in order, ahead of the second Run's own message.
        sent = second_model.last_request.messages
        texts = [m.content for m in sent if isinstance(m, UserMessage)]
        assert any("ACME-7042" in t for t in texts), (
            f"the first Run's fact never reached the model: {texts}"
        )
        first_user_index = next(i for i, t in enumerate(texts) if "ACME-7042" in t)
        second_user_index = next(i for i, t in enumerate(texts) if "account number again" in t)
        assert first_user_index < second_user_index

        # psych_runtime.report() and the conversation the model was sent cannot
        # disagree about which Runs make up the thread, because both are
        # read off the same RunAdmitted.continues_run_id (DESIGN.md §6).
        assert second_report.continues_run_id == first_report.run_id

        # Cost stays per-Run: the second Run's totals reflect only its own
        # model call, not the first Run's added in.
        assert second_report.totals.model_calls == 1

    async def test_a_thread_with_no_continues_run_id_has_no_history_to_load(self) -> None:
        """A Run's first message is not a degenerate continuation of nothing --
        ``load_thread_history`` returns immediately without touching the store,
        checked here by the ordinary path simply working with no ancestor."""
        store = InMemoryStore()
        model = FakeModel().turn(text="hello yourself")
        run_id = await _drive(store, _spec(), model, SCOPE_A, message="hello")
        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert report.continues_run_id is None
        assert [m for m in model.last_request.messages if isinstance(m, UserMessage)] == [
            UserMessage(content="hello")
        ]

    async def test_a_run_cannot_continue_one_from_another_scope(self) -> None:
        """DESIGN.md §10.4's discipline applied to a chain link: a continuation
        is a new way to reach another Run's content, and crossing a tenant with
        it is refused exactly like pooling an MCP client by URL alone would be."""
        store = InMemoryStore()
        spec = _spec()
        first_run = await _drive(
            store, spec, FakeModel().turn(text="ok"), SCOPE_A, message="secret: PONY"
        )

        version = await psych_runtime.publish(store, spec)
        with pytest.raises(AccessDenied):
            await psych_runtime.dispatch(
                store,
                version,
                SCOPE_B,
                input={"message": "what's the secret?"},
                continues=first_run,
            )

        # Refused before anything was written: no second Run exists that
        # claims to continue the first one.
        state = await psych_runtime.state(store, first_run)
        assert state.settled

    async def test_history_is_bounded_by_the_spec_field_and_the_bound_is_versioned(self) -> None:
        """The ``Limits`` field that bounds replay: ``max_history_records``
        set to ``0`` makes a continuation still record the chain link
        (`continues_run_id` on the report) while carrying none of the
        ancestor's conversation to the model -- and two Specs differing only
        in this field hash differently,
        which is the whole point of it being a Spec field rather than a
        Runtime one."""
        store = InMemoryStore()
        spec = _spec(limits=Limits(max_history_records=0))
        first_run = await _drive(
            store, spec, FakeModel().turn(text="ok"), SCOPE_A, message="the code word is PELICAN"
        )

        second_model = FakeModel().turn(text="I don't have that.")
        second_run = await _drive(
            store,
            spec,
            second_model,
            SCOPE_A,
            message="what's the code word?",
            continues=first_run,
        )
        report = await psych_runtime.report(store, second_run)
        assert report.continues_run_id == first_run

        sent = second_model.last_request.messages
        texts = [m.content for m in sent if isinstance(m, UserMessage)]
        assert not any("PELICAN" in t for t in texts)

        default_hash = (await psych_runtime.publish(store, _spec())).hash
        zero_hash = (await psych_runtime.publish(store, spec)).hash
        assert default_hash != zero_hash


class TestSteeringQueues:
    """DESIGN.md §9's three queues, reached through the public ``psych_runtime.send``
    rather than through ``psych_runtime.testing.logs.LogBuilder``, which was
    the only writer they had before this."""

    async def test_steer_is_drained_into_the_turn_it_precedes(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})

        entry_id = await psych_runtime.send(
            store, dispatched.run_id, message="also, call me Sam", queue=QueueKind.STEER
        )
        assert entry_id

        journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        model = FakeModel().turn(text="Hi Sam!")
        runtime = Runtime(store=store, model=model, registry=ToolRegistry())
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())

        texts = [m.content for m in model.last_request.messages if isinstance(m, UserMessage)]
        assert any("call me Sam" in t for t in texts)

        records = await psych_runtime.records(store, dispatched.run_id)
        kinds = [r.type for r in records]
        assert "queue_enqueued" in kinds
        assert "queue_consumed" in kinds
        assert kinds.index("queue_enqueued") < kinds.index("queue_consumed")

    async def test_follow_up_keeps_the_run_going_past_a_turn_with_nothing_left_to_do(
        self,
    ) -> None:
        store = InMemoryStore()
        spec = _spec()
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})

        await psych_runtime.send(
            store,
            dispatched.run_id,
            message="one more thing: what's 2+2?",
            queue=QueueKind.FOLLOW_UP,
        )

        journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        # The first turn has no tool calls -- ordinarily that completes the
        # Run. A pending follow-up must stop that and drive a second turn.
        model = FakeModel().turn(text="Hi there.").turn(text="2+2 is 4.")
        runtime = Runtime(store=store, model=model, registry=ToolRegistry())
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())

        report = await psych_runtime.report(store, dispatched.run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert len(report.model_calls) == 2
        second_turn_texts = [
            m.content for m in model.requests[1].messages if isinstance(m, UserMessage)
        ]
        assert any("2+2" in t for t in second_turn_texts)

    async def test_next_run_is_recorded_and_readable_from_the_public_api(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})

        entry_id = await psych_runtime.send(
            store,
            dispatched.run_id,
            message="actually, check order A2 instead",
            queue=QueueKind.NEXT_RUN,
        )

        state = await psych_runtime.state(store, dispatched.run_id)
        assert [e.entry_id for e in state.pending_next_run] == [entry_id]
        assert state.pending_next_run[0].payload["message"] == "actually, check order A2 instead"

    async def test_steer_is_refused_once_the_run_has_aborted(self) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(store, _spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})
        await psych_runtime.interrupt(store, dispatched.run_id, reason="stop")

        with pytest.raises(CorruptLog) as excinfo:
            await psych_runtime.send(
                store, dispatched.run_id, message="too late", queue=QueueKind.STEER
            )
        assert excinfo.value.reason is CorruptionReason.QUEUE_AFTER_ABORT

    async def test_follow_up_is_refused_once_the_run_has_aborted(self) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(store, _spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})
        await psych_runtime.interrupt(store, dispatched.run_id, reason="stop")

        with pytest.raises(CorruptLog) as excinfo:
            await psych_runtime.send(
                store, dispatched.run_id, message="too late", queue=QueueKind.FOLLOW_UP
            )
        assert excinfo.value.reason is CorruptionReason.QUEUE_AFTER_ABORT

    async def test_next_run_is_explicitly_legal_after_an_abort(self) -> None:
        """The exemption DESIGN.md §9 calls out by name: "stop mid-response and
        immediately send another request" is exactly a next_run enqueue after
        an abort, and it must succeed where STEER and FOLLOW_UP do not."""
        store = InMemoryStore()
        version = await psych_runtime.publish(store, _spec())
        dispatched = await psych_runtime.dispatch(store, version, SCOPE_A, input={"message": "hi"})
        await psych_runtime.interrupt(store, dispatched.run_id, reason="stop")

        entry_id = await psych_runtime.send(
            store,
            dispatched.run_id,
            message="actually, check order A2 instead",
            queue=QueueKind.NEXT_RUN,
        )
        assert entry_id

        state = await psych_runtime.state(store, dispatched.run_id)
        assert state.aborted
        assert [e.entry_id for e in state.pending_next_run] == [entry_id]

    async def test_send_is_refused_once_the_run_has_settled(self) -> None:
        store = InMemoryStore()
        run_id = await _drive(store, _spec(), FakeModel().turn(text="done"), SCOPE_A, message="hi")
        with pytest.raises(psych_runtime.RunAlreadySettled, match="already settled"):
            await psych_runtime.send(store, run_id, message="too late for any queue")


class TestNextRunFeedsAContinuation:
    """The two halves DESIGN.md §9 and §23.3 describe working together: a
    message queued while a Run is unwinding from an interrupt, delivered by
    dispatching the next Run as a continuation once that Run has settled."""

    async def test_interrupt_then_send_then_continue_carries_the_first_runs_input_forward(
        self,
    ) -> None:
        store = InMemoryStore()
        spec = _spec()
        version = await psych_runtime.publish(store, spec)
        dispatched = await psych_runtime.dispatch(
            store, version, SCOPE_A, input={"message": "find A1, then check A2"}
        )
        await psych_runtime.interrupt(store, dispatched.run_id, reason="stop")
        entry_id = await psych_runtime.send(
            store,
            dispatched.run_id,
            message="never mind, what did you find on A1?",
            queue=QueueKind.NEXT_RUN,
        )
        assert entry_id

        # The Attempt that was mid-response settles the aborted Run the way
        # psych_runtime.runtime.agent's own stop-condition check would: an abort
        # record already present, so its very first check returns ABORTED
        # without the model ever being called again.
        journal = await Journal.open(store, dispatched.run_id, SCOPE_A)
        await journal.append(type="run_settled", state=TerminalState.ABORTED)

        # A next_run entry only records that the message arrived (send()'s
        # own docstring says so); it is the continuation's own admission --
        # not this entry -- that actually carries the conversation forward,
        # which is what this asserts: the first Run's *original input*
        # reaches the second Run's model call, read out of the ancestor's own
        # conversation rather than out of the queue entry at all.
        second_model = FakeModel().turn(text="Found it.")
        second_run = await _drive(
            store,
            spec,
            second_model,
            SCOPE_A,
            message="never mind, what did you find on A1?",
            continues=dispatched.run_id,
        )
        report = await psych_runtime.report(store, second_run)
        assert report.continues_run_id == dispatched.run_id
        texts = [
            m.content for m in second_model.last_request.messages if isinstance(m, UserMessage)
        ]
        assert any("find A1, then check A2" in t for t in texts)


class TestAConversationWithTwoEndings:
    """One message of a conversation asked again, keeping both answers.

    Nothing in the runtime is new here, and proving that is the point. A branch
    is ``psych_runtime.dispatch(continues=...)`` naming a Run that already has a
    successor, so two Runs share one predecessor and the chain becomes a tree.
    Everything a consumer needs falls out of what the log already records.

    The console has to do one thing the library will not: say which of two
    futures a Run belongs to. ``Store`` has no query from a Run to the Runs
    continuing it (DESIGN.md §7), so a forward walk is the consumer's own, and
    a consumer without a branch identity of its own picks a child at random.
    That is bookkeeping over the log rather than a fact in it, which is why it
    is the example platform's and not tested here.
    """

    async def test_two_branches_share_a_prefix_and_diverge_after_it(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        opening = await _drive(store, spec, FakeModel().turn(text="Two."), SCOPE_A, message="1+1?")
        original = await _drive(
            store,
            spec,
            FakeModel().turn(text="Four."),
            SCOPE_A,
            message="and 2+2?",
            continues=opening,
        )
        # The branch continues the opening message, not the Run above it: that
        # is what "ask this again" means, and it is why the answer already
        # given is left exactly where it is.
        branch = await _drive(
            store,
            spec,
            FakeModel().turn(text="Nine."),
            SCOPE_A,
            message="and 3+3?",
            continues=opening,
        )

        original_view = await psych_runtime.thread(store, original, scope=SCOPE_A)
        branch_view = await psych_runtime.thread(store, branch, scope=SCOPE_A)

        assert original_view.run_ids == (opening, original)
        assert branch_view.run_ids == (opening, branch)
        assert [(m.role, m.content) for m in original_view.messages] == [
            ("user", "1+1?"),
            ("assistant", "Two."),
            ("user", "and 2+2?"),
            ("assistant", "Four."),
        ]
        assert [(m.role, m.content) for m in branch_view.messages] == [
            ("user", "1+1?"),
            ("assistant", "Two."),
            ("user", "and 3+3?"),
            ("assistant", "Nine."),
        ]

    async def test_the_branch_carries_the_shared_history_into_its_model_call(self) -> None:
        """The assertion that cannot be satisfied by accident: what the model
        was actually sent, read out of its own request.

        A branch that merely *looked* right in the projection while starting
        the model from nothing would be a different feature with the same
        screenshot.
        """
        store = InMemoryStore()
        spec = _spec()
        opening = await _drive(
            store,
            spec,
            FakeModel().turn(text="Order A1 ships Tuesday."),
            SCOPE_A,
            message="when does order A1 ship?",
        )
        await _drive(
            store,
            spec,
            FakeModel().turn(text="Yes."),
            SCOPE_A,
            message="is that confirmed?",
            continues=opening,
        )

        branch_model = FakeModel().turn(text="Tuesday.")
        await _drive(
            store,
            spec,
            branch_model,
            SCOPE_A,
            message="say that again but shorter",
            continues=opening,
        )

        said = [m.content for m in branch_model.last_request.messages if isinstance(m, UserMessage)]
        assert any("when does order A1 ship?" in text for text in said)
        assert not any("is that confirmed?" in text for text in said), (
            "the other branch's message is not this branch's history"
        )

    async def test_the_original_log_is_untouched_by_the_branch(self) -> None:
        """The append-only claim, asserted rather than assumed. Everything a
        branch needs is written into the branch's *own* log, so the Runs it
        continues have nothing added, removed or rewritten."""
        store = InMemoryStore()
        spec = _spec()
        opening = await _drive(store, spec, FakeModel().turn(text="Two."), SCOPE_A, message="1+1?")
        original = await _drive(
            store,
            spec,
            FakeModel().turn(text="Four."),
            SCOPE_A,
            message="and 2+2?",
            continues=opening,
        )
        before = {run: await store.read(run) for run in (opening, original)}

        await _drive(
            store,
            spec,
            FakeModel().turn(text="Nine."),
            SCOPE_A,
            message="and 3+3?",
            continues=opening,
        )

        assert {run: await store.read(run) for run in (opening, original)} == before

    async def test_a_branch_may_run_a_different_agent(self) -> None:
        """The most useful reason to branch, and it needs nothing: a branch is
        an ordinary dispatch, so it pins whatever Version it is given.

        Continuing a thread pins the Version the thread opened on, because a
        conversation whose agent changed halfway is two conversations wearing
        one title. A branch *is* the second conversation, so it may.
        """
        store = InMemoryStore()
        brief = _spec(name="brief", instructions="Answer in one word.")
        thorough = _spec(name="thorough", instructions="Explain your reasoning.")
        opening = await _drive(store, brief, FakeModel().turn(text="Two."), SCOPE_A, message="1+1?")

        second_opinion = FakeModel().turn(text="Two, because one plus one is two.")
        branch = await _drive(
            store, thorough, second_opinion, SCOPE_A, message="1+1?", continues=opening
        )

        opening_report = await psych_runtime.report(store, opening)
        branch_report = await psych_runtime.report(store, branch)
        assert branch_report.continues_run_id == opening
        assert branch_report.version_hash != opening_report.version_hash
        assert "Explain your reasoning." in str(second_opinion.last_request.messages[0].content)

    async def test_a_branch_cannot_cross_a_tenant(self) -> None:
        """Branching is a new way to reach another Run's content, which
        DESIGN.md §10.4 treats as the shape that ends a project. It is refused
        by the same check a continuation is, because it *is* a continuation."""
        store = InMemoryStore()
        spec = _spec()
        opening = await _drive(store, spec, FakeModel().turn(text="Two."), SCOPE_A, message="1+1?")
        version = await psych_runtime.publish(store, spec)

        with pytest.raises(AccessDenied):
            await psych_runtime.dispatch(
                store, version, SCOPE_B, input={"message": "let me see"}, continues=opening
            )


class TestTheThreadProjection:
    """``psych_runtime.thread`` is what a chat UI renders, and what stops every
    consumer re-deriving message positions from the log themselves."""

    async def test_it_returns_the_whole_conversation_across_every_run(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        first = await _drive(store, spec, FakeModel().turn(text="Hello."), SCOPE_A, message="hi")
        second = await _drive(
            store,
            spec,
            FakeModel().turn(text="Still here."),
            SCOPE_A,
            message="and again",
            continues=first,
        )

        view = await psych_runtime.thread(store, second, scope=SCOPE_A)

        assert view.run_ids == (first, second)
        assert [(m.role, m.content) for m in view.messages] == [
            ("user", "hi"),
            ("assistant", "Hello."),
            ("user", "and again"),
            ("assistant", "Still here."),
        ]
        # Every message says which Run it belongs to: seq is unique only
        # within one Run's log, so a thread flattening several needs it.
        assert {m.run_id for m in view.messages} == {first, second}

    async def test_another_tenant_cannot_read_the_thread(self) -> None:
        store = InMemoryStore()
        run_id = await _drive(
            store, _spec(), FakeModel().turn(text="private"), SCOPE_A, message="hi"
        )

        with pytest.raises(psych_runtime.AccessDenied):
            await psych_runtime.thread(store, run_id, scope=Scope(tenant="globex"))

    async def test_the_read_api_refuses_another_tenant(self) -> None:
        """Every read entry point took a bare run_id, so one leaked id -- a log
        line, a URL -- was a whole conversation. Passing a Scope refuses it."""
        store = InMemoryStore()
        run_id = await _drive(
            store, _spec(), FakeModel().turn(text="private"), SCOPE_A, message="hi"
        )
        intruder = Scope(tenant="globex")

        with pytest.raises(psych_runtime.AccessDenied):
            await psych_runtime.state(store, run_id, scope=intruder)
        with pytest.raises(psych_runtime.AccessDenied):
            await psych_runtime.status(store, run_id, scope=intruder)
        with pytest.raises(psych_runtime.AccessDenied):
            await psych_runtime.report(store, run_id, scope=intruder)
        with pytest.raises(psych_runtime.AccessDenied):
            await psych_runtime.records(store, run_id, scope=intruder)
        with pytest.raises(psych_runtime.AccessDenied):
            async for _ in psych_runtime.stream(store, run_id, scope=intruder):
                pass

        # And the owner still reads it.
        assert (
            await psych_runtime.status(store, run_id, scope=SCOPE_A)
        ).lifecycle is psych_runtime.Lifecycle.DONE

    async def test_streaming_an_unknown_run_raises_rather_than_hanging(self) -> None:
        """It used to poll forever: a typo'd id produced a stream that never
        yielded and never ended, which every consumer had to guard themselves."""
        store = InMemoryStore()
        with pytest.raises(psych_runtime.RunNotFound):
            async for _ in psych_runtime.stream(store, RunId("run_does_not_exist")):
                pass
