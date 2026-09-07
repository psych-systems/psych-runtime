"""The positioned conversation, and the one property that makes it safe.

``psych_runtime.core.thread_view.message_views`` exists so a consumer rendering a chat
does not re-implement ``build_conversation``'s emission rules to recover where
each message came from -- which is what the example platform in this repository
did, complete with a length assertion guarding against the two drifting.

The property that matters is therefore not "it returns messages" but "it
returns *the same* messages, in the same order, as the projection the model
actually sees". A view that showed a person something the model was never sent
would be a worse bug than the duplication it replaced.
"""

from __future__ import annotations

import pytest

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.records import QueueKind, Record, SuspendReason, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.status import Lifecycle, status_of
from psych_runtime.core.thread_view import message_views
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def _busy_log() -> list[Record]:
    """A log exercising every branch both projections have: an input, a steer
    delivered mid-Run, an assistant turn with a tool call, a failed call, and a
    compaction boundary."""
    builder = (
        LogBuilder()
        .admitted(input={"message": "where is A1?"})
        .attempt()
        .turn()
        .model_started()
        .model_finished(text="", tool_calls=("call-1",))
        .tool_started("call-1", "lookup")
        .tool_finished("call-1", result={"status": "shipped"})
        .turn()
        .model_started()
        .model_finished(text="It shipped.")
    )
    builder.enqueued("e1", QueueKind.STEER, payload={"message": "and A2?"})
    builder.consumed("e1")
    builder.turn().model_started().model_finished(text="", tool_calls=("call-2",))
    builder.tool_started("call-2", "lookup")
    builder.tool_finished("call-2", outcome=ToolOutcome.ERROR)
    return builder.records


class TestItAgreesWithWhatTheModelSaw:
    def test_the_same_messages_in_the_same_order(self) -> None:
        views = message_views(_busy_log())
        messages = build_conversation(_busy_log())

        assert len(views) == len(messages)
        for view, message in zip(views, messages, strict=True):
            assert view.content == message.content

    def test_a_compacted_range_is_summarised_the_same_way(self) -> None:
        log = (
            LogBuilder()
            .admitted(input={"message": "hello"})
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="hi")
            .compacted(1, 5, summary="they said hello")
            .turn()
            .model_started()
            .model_finished(text="still here")
            .records
        )
        views = message_views(log)
        messages = build_conversation(log)
        assert [v.content for v in views] == [m.content for m in messages]
        assert "they said hello" in views[0].content


class TestEachMessageKnowsWhereItCameFrom:
    def test_every_view_carries_a_position_in_its_own_run(self) -> None:
        views = message_views(_busy_log())
        assert views, "no messages projected"
        run_ids = {view.run_id for view in views}
        assert len(run_ids) == 1
        # Strictly increasing: the seq is the record that produced it, and a
        # conversation is rendered in log order.
        assert [v.seq for v in views] == sorted(v.seq for v in views)

    def test_a_tool_result_names_its_tool_and_its_call(self) -> None:
        views = message_views(_busy_log())
        tools = [view for view in views if view.role == "tool"]
        assert [view.tool_name for view in tools] == ["lookup", "lookup"]
        assert [view.tool_call_id for view in tools] == ["call-1", "call-2"]
        assert [view.is_error for view in tools] == [False, True]

    def test_an_empty_log_projects_nothing_rather_than_raising(self) -> None:
        assert message_views([]) == []


class TestStatusIsWhatAScreenNeeds:
    def test_a_suspended_run_reports_the_call_awaiting_a_decision(self) -> None:
        log = (
            LogBuilder()
            .admitted(input={"message": "refund A1"})
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="", tool_calls=("call-1",))
            .tool_started("call-1", "issue_refund", arguments={"order_id": "A1", "cents": 500})
            .suspended(SuspendReason.APPROVAL, pending_call_id="call-1", question="Approve?")
            .records
        )
        status = status_of(reduce(log))

        assert status.lifecycle is Lifecycle.WAITING
        assert status.pending_approval is not None
        # The exact arguments that will run if approved, read from the open
        # call rather than parsed back out of the question text.
        assert status.pending_approval.tool == "issue_refund"
        assert status.pending_approval.arguments == {"order_id": "A1", "cents": 500}
        assert status.pending_approval.question == "Approve?"

    def test_an_aborted_run_that_has_not_settled_is_stopping_not_running(self) -> None:
        """A UI with no word for this shows "running" and a stop button that
        appears to do nothing, which is how a person concludes stop is broken."""
        log = LogBuilder().admitted().attempt().turn().model_started().abort().records
        assert status_of(reduce(log)).lifecycle is Lifecycle.STOPPING

    def test_a_run_nobody_has_claimed_is_queued(self) -> None:
        log = LogBuilder().admitted().records
        assert status_of(reduce(log)).lifecycle is Lifecycle.QUEUED

    def test_time_spent_waiting_does_not_spend_the_deadline(self) -> None:
        """A Run suspended for a day has not been running for a day, and
        treating it as though it had aborted every approved Run on the next
        claim."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="", tool_calls=("call-1",))
            .tool_started("call-1", "issue_refund")
            .suspended(SuspendReason.APPROVAL, pending_call_id="call-1")
        )
        before = status_of(reduce(builder.records))
        builder.resumed(approved=True)
        after = status_of(reduce(builder.records))

        assert before.deadline_at is not None
        assert after.deadline_at is not None
        assert after.deadline_at >= before.deadline_at
