"""The agent loop compacting its own conversation, against the fake model.

The loop is driven directly here, over a real store and the scriptable
fake model, because what these check is the sequence of model requests the loop
actually sends: that the one after a compaction carries the summary and the
tail, that a second Worker replaying the same log does not summarise again, and
that a provider refusing an over-long prompt leads to a compaction and a retry
rather than a settled failure.

Nothing is mocked. Every assertion is against the log the loop wrote or the
``ModelRequest`` objects the fake model was handed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from psych_runtime.core.ids import RunId, WorkerId, new_run_id
from psych_runtime.core.records import CompactionApplied, ModelCallFailed, Record, TerminalState
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, CompactionPolicy, Limits, ModelRef
from psych_runtime.core.usage import Usage
from psych_runtime.core.version import publish
from psych_runtime.model.port import ModelRequest
from psych_runtime.runtime.agent import AgentLoop, LoopOutcome, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="acme", principal="user-1")

OVER_LONG = (
    "This model's maximum context length is 8192 tokens, however your messages "
    "resulted in 9001 tokens."
)


def _spec(**policy: Any) -> AgentSpec:
    settings: dict[str, Any] = {"trigger_tokens": 500, "keep_recent_turns": 1}
    settings.update(policy)
    return AgentSpec(
        name="support",
        instructions="Help the customer.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="lookup"),),
        compaction=CompactionPolicy(**settings),
        limits=Limits(max_turns=8, deadline_seconds=60),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def lookup(order_id: str) -> dict[str, str]:
        """Look up an order by id."""
        return {"order_id": order_id, "status": "shipped"}

    return registry


async def _admit(store: InMemoryStore, spec: AgentSpec) -> RunId:
    version = publish(spec)
    await store.put_version(version)
    run_id = new_run_id()
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=spec.limits.deadline_seconds)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=SCOPE,
            version_hash=version.hash,
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=deadline,
        )
    )
    from psych_runtime.core.records import RECORD_ADAPTER

    await store.append(
        run_id,
        1,
        RECORD_ADAPTER.validate_python(
            {
                "type": "run_admitted",
                "run_id": run_id,
                "seq": 1,
                "at": now,
                "scope": SCOPE,
                "version_hash": version.hash,
                "input": {"message": "where is A1?"},
                "deadline_at": deadline,
            }
        ),
    )
    return run_id


async def _attempt(
    store: InMemoryStore, run_id: RunId, spec: AgentSpec, model: FakeModel
) -> LoopOutcome:
    """One Attempt over this Run, exactly as a Worker makes one."""
    registry = _registry()
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(
        type="attempt_started",
        worker_id="wrk_1",
        attempt_number=journal.state.attempt_count + 1,
    )
    loop = AgentLoop(journal, spec, model, ToolResolver(registry), ToolExecutor(registry))
    return await loop.run()


def _rendered(request: ModelRequest) -> str:
    return "\n".join(
        message.content for message in request.messages if isinstance(message.content, str)
    )


def _compactions(records: list[Record]) -> list[CompactionApplied]:
    return [record for record in records if isinstance(record, CompactionApplied)]


def _two_long_turns() -> FakeModel:
    """Two turns whose prompts the provider counted well over the trigger."""
    return (
        FakeModel()
        .turn(
            text="looking up the first order",
            tool_calls=[("lookup", {"order_id": "AAA"})],
            usage=Usage(input=1_000, output=20),
        )
        .turn(
            text="looking up the second order",
            tool_calls=[("lookup", {"order_id": "BBB"})],
            usage=Usage(input=1_000, output=20),
        )
    )


class TestCrossingTheThreshold:
    async def test_the_next_request_carries_the_summary_and_the_tail(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        run_id = await _admit(store, spec)
        model = (
            _two_long_turns()
            .turn(text="Earlier: the customer asked after two orders.")
            .turn(text="Both shipped.", usage=Usage(input=100, output=10))
        )

        outcome = await _attempt(store, run_id, spec, model)
        assert outcome.state is TerminalState.COMPLETED

        records = await store.read(run_id)
        compactions = _compactions(records)
        assert len(compactions) == 1
        assert compactions[0].reason == "threshold"

        # The third request is the summarising call: it reads the range being
        # replaced and nothing after it.
        summarising = _rendered(model.requests[2])
        assert "first order" in summarising
        assert "second order" not in summarising

        # The fourth is the turn that follows, and this is the assertion the
        # feature exists for.
        after = _rendered(model.requests[3])
        assert "Earlier: the customer asked after two orders." in after
        assert "second order" in after
        assert "first order" not in after

    async def test_the_replaced_records_are_still_in_the_log(self) -> None:
        """Compaction changes what the model sees next, never what happened."""
        store = InMemoryStore()
        spec = _spec()
        run_id = await _admit(store, spec)
        model = _two_long_turns().turn(text="Earlier: two orders.").turn(text="Both shipped.")

        await _attempt(store, run_id, spec, model)

        records = await store.read(run_id)
        cut = _compactions(records)[0].replaced_to_seq
        assert [record.seq for record in records[:cut]] == list(range(1, cut + 1))
        assert any("AAA" in str(getattr(record, "arguments", "")) for record in records)

    async def test_an_agent_that_did_not_ask_for_it_never_compacts(self) -> None:
        store = InMemoryStore()
        spec = AgentSpec(
            name="support",
            model=ModelRef(model="fake-standard"),
            tools=(CodeTool(name="lookup"),),
            limits=Limits(max_turns=8, deadline_seconds=60),
        )
        run_id = await _admit(store, spec)
        model = _two_long_turns().turn(text="Both shipped.")

        await _attempt(store, run_id, spec, model)

        assert _compactions(await store.read(run_id)) == []
        assert len(model.requests) == 3

    async def test_a_summarising_call_that_fails_stops_the_run_saying_so(self) -> None:
        """Rather than carrying on quietly: by this point the conversation is
        at the size its author said it must not exceed, and a Run that silently
        stopped compacting would fail later for an unrelated-looking reason."""
        store = InMemoryStore()
        spec = _spec()
        run_id = await _admit(store, spec)
        model = _two_long_turns().raises_permanent(400, message="the summariser is misconfigured")

        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.FAILED
        assert outcome.failure is not None
        assert outcome.failure.kind == "compaction_failed"
        assert _compactions(await store.read(run_id)) == []


async def _dead_worker_log(store: InMemoryStore, run_id: RunId, *, compacted: bool) -> int:
    """Exactly what a Worker that died mid-Run leaves behind, and where it died.

    Written through the Journal rather than run, because what these cases are
    about is the log a second Worker reads: two turns whose prompts the
    provider counted over the trigger, and then either a compaction record or
    nothing, which are the two sides of the crash window.

    Returns the sequence the compaction covered, or would have covered.
    """
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_dead", attempt_number=1)
    cut = 0
    for turn, order in enumerate(("AAA", "BBB"), start=1):
        if turn == 2:
            cut = journal.head
        await journal.append(type="turn_started", turn=turn)
        await journal.append(type="model_call_started", turn=turn, model="fake-standard")
        await journal.append(
            type="model_call_finished",
            turn=turn,
            model="fake-standard",
            usage={"input": 1_000, "output": 20},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            text=f"looking up order {order}",
            tool_calls=(f"call-{turn}",),
        )
        await journal.append(
            type="tool_call_started",
            call_id=f"call-{turn}",
            tool="lookup",
            arguments={"order_id": order},
            turn=turn,
        )
        await journal.append(
            type="tool_call_finished",
            call_id=f"call-{turn}",
            outcome="ok",
            result={"order_id": order, "status": "shipped"},
        )
    if compacted:
        await journal.append(
            type="compaction_applied",
            reason="threshold",
            replaced_from_seq=1,
            replaced_to_seq=cut,
            summary="Earlier: the customer asked after order AAA.",
            model="fake-standard",
            usage={"input": 900, "output": 40},
            cost=None,
        )
    await store.release(run_id, WorkerId("wrk_dead"), RunState.RUNNABLE)
    return cut


class TestReplayingAfterACrash:
    """The two sides of the window between the summarising call and the append.

    Both rest on one rule: a cut is only taken when it advances the boundary
    (``psych_runtime.runtime.compaction``). Nothing else in the loop remembers that a
    compaction was in progress, and nothing needs to.
    """

    async def test_a_worker_that_died_after_the_append_does_not_summarise_again(self) -> None:
        store = InMemoryStore()
        spec = _spec()
        run_id = await _admit(store, spec)
        cut = await _dead_worker_log(store, run_id, compacted=True)

        model = FakeModel().turn(text="Both shipped.", usage=Usage(input=100, output=10))
        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.COMPLETED
        # One model call: this Attempt's own turn. A second summary would be
        # a second request, and the range it replaced is still the same range.
        assert len(model.requests) == 1
        assert len(_compactions(await store.read(run_id))) == 1
        assert _compactions(await store.read(run_id))[0].replaced_to_seq == cut

        carried = _rendered(model.requests[0])
        assert "Earlier: the customer asked after order AAA." in carried
        assert "looking up order AAA" not in carried
        assert "looking up order BBB" in carried

    async def test_a_worker_that_died_before_the_append_summarises_once_and_no_more(self) -> None:
        """The summary was paid for and lost with the Worker. Nothing in the log
        says otherwise, so the next Attempt does the work again -- one call paid
        for twice, and one range compacted once."""
        store = InMemoryStore()
        spec = _spec()
        run_id = await _admit(store, spec)
        cut = await _dead_worker_log(store, run_id, compacted=False)

        model = (
            FakeModel()
            .turn(text="Earlier: the customer asked after order AAA.")
            .turn(text="Both shipped.", usage=Usage(input=100, output=10))
        )
        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.COMPLETED
        compactions = _compactions(await store.read(run_id))
        assert len(compactions) == 1
        assert compactions[0].replaced_to_seq == cut


class TestOverflow:
    async def test_a_provider_refusing_an_over_long_prompt_compacts_and_retries(self) -> None:
        store = InMemoryStore()
        # A trigger far above anything the fake reports, so nothing compacts
        # proactively and the only path into a compaction is the refusal.
        spec = _spec(trigger_tokens=10_000_000)
        run_id = await _admit(store, spec)
        model = (
            _two_long_turns()
            .raises_permanent(400, message=OVER_LONG)
            .turn(text="Earlier: two orders.")
            .turn(text="Both shipped.", usage=Usage(input=100, output=10))
        )

        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.COMPLETED
        records = await store.read(run_id)
        compactions = _compactions(records)
        assert len(compactions) == 1
        assert compactions[0].reason == "overflow"

        failed = [record for record in records if isinstance(record, ModelCallFailed)]
        assert len(failed) == 1
        assert failed[0].will_retry is True

    async def test_a_conversation_with_nothing_left_to_compact_settles_on_the_error(self) -> None:
        """The refusal cannot be answered by summarising when the tail alone is
        too long, and pretending otherwise would loop forever."""
        store = InMemoryStore()
        spec = _spec(trigger_tokens=10_000_000, keep_recent_turns=8)
        run_id = await _admit(store, spec)
        model = _two_long_turns().raises_permanent(400, message=OVER_LONG)

        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.FAILED
        assert _compactions(await store.read(run_id)) == []

    async def test_an_unrelated_400_is_still_a_settled_failure(self) -> None:
        store = InMemoryStore()
        spec = _spec(trigger_tokens=10_000_000)
        run_id = await _admit(store, spec)
        model = _two_long_turns().raises_permanent(400, message="your tool schema is invalid")

        outcome = await _attempt(store, run_id, spec, model)

        assert outcome.state is TerminalState.FAILED
        assert _compactions(await store.read(run_id)) == []
        state = reduce(await store.read(run_id), run_id=run_id)
        assert state.failed_model_calls == 1
