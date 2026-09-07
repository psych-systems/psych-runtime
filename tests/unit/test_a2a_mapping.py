"""Every state mapping between a Run and an A2A Task, with no socket.

`psych_runtime.a2a.mapping` is the whole reason the package is a library module rather
than routes: it is a total function from projections Psych already owns to
protocol objects. So it is tested the way the reducer is -- build a log, fold
it, assert the answer -- and nothing here touches HTTP.

The cases that matter most are the correspondences the two models did not have
to agree on: a question becoming `INPUT_REQUIRED`, an authorization wait
becoming `AUTH_REQUIRED`, and a continuation chain becoming one `contextId`.
"""

from __future__ import annotations

import pytest

from psych_runtime.a2a.errors import InvalidParamsError, TaskNotFoundError
from psych_runtime.a2a.mapping import (
    LIFECYCLE_STATES,
    SUSPEND_TASK_STATES,
    TERMINAL_TASK_STATES,
    MessageIntent,
    artifact_of,
    context_id_of,
    resolve_message,
    status_message_of,
    stream_events,
    task_of,
    task_state_of,
)
from psych_runtime.a2a.models import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
    TaskStatusUpdateEvent,
)
from psych_runtime.core.answer import split_answer
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import SuspendReason, TerminalState
from psych_runtime.core.reducer import reduce as reduce_log
from psych_runtime.core.status import Lifecycle, RunStatus, status_of
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def _status(builder: LogBuilder) -> RunStatus:
    return status_of(reduce_log(builder.records))


class TestTaskState:
    """Every ``Lifecycle`` and every ``TerminalState`` has a Task state."""

    def test_every_lifecycle_is_mapped(self) -> None:
        assert set(LIFECYCLE_STATES) == set(Lifecycle)

    def test_every_terminal_state_is_mapped(self) -> None:
        assert set(TERMINAL_TASK_STATES) == set(TerminalState)

    def test_every_suspend_reason_is_mapped(self) -> None:
        assert set(SUSPEND_TASK_STATES) == set(SuspendReason)

    def test_admitted_but_unclaimed_is_submitted(self) -> None:
        builder = LogBuilder().admitted()
        assert task_state_of(_status(builder)) is TaskState.SUBMITTED

    def test_a_claimed_run_is_working(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        assert task_state_of(_status(builder)) is TaskState.WORKING

    def test_a_question_is_input_required(self) -> None:
        """The correspondence this mapping exists for: `ask_question` and
        `INPUT_REQUIRED` mean the same thing, and §3.4.3 resumes both the same
        way."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1", tool="ask_question")
            .suspended(
                reason=SuspendReason.QUESTION,
                pending_call_id="call_1",
                question="Which invoice?",
            )
        )
        assert task_state_of(_status(builder)) is TaskState.INPUT_REQUIRED

    def test_an_approval_is_input_required(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1", tool="refund")
            .suspended(
                reason=SuspendReason.APPROVAL,
                pending_call_id="call_1",
                question="Approve calling 'refund'?",
            )
        )
        assert task_state_of(_status(builder)) is TaskState.INPUT_REQUIRED

    def test_an_authorization_wait_is_auth_required(self) -> None:
        """An external suspension is a Run parked on something the caller has
        to go and do, which for A2A is `AUTH_REQUIRED` and not "send words"."""
        builder = LogBuilder().admitted().attempt().suspended(reason=SuspendReason.EXTERNAL)
        assert task_state_of(_status(builder)) is TaskState.AUTH_REQUIRED

    def test_a_scheduled_wait_is_still_working(self) -> None:
        """A timer is nobody's turn to act, so a client is not told to act."""
        builder = LogBuilder().admitted().attempt().suspended(reason=SuspendReason.CHILDREN)
        assert task_state_of(_status(builder)) is TaskState.WORKING

    def test_a_consumer_can_override_the_suspension_table(self) -> None:
        """A platform whose EXTERNAL suspensions are batch webhooks rather than
        authorizations says so, instead of forking the module."""
        builder = LogBuilder().admitted().attempt().suspended(reason=SuspendReason.EXTERNAL)
        overridden = dict(SUSPEND_TASK_STATES) | {SuspendReason.EXTERNAL: TaskState.WORKING}
        assert task_state_of(_status(builder), suspend_states=overridden) is TaskState.WORKING

    @pytest.mark.parametrize(
        ("terminal", "expected"),
        [
            (TerminalState.COMPLETED, TaskState.COMPLETED),
            (TerminalState.FAILED, TaskState.FAILED),
            (TerminalState.ABORTED, TaskState.CANCELED),
            (TerminalState.ABANDONED, TaskState.FAILED),
            (TerminalState.FORCE_SETTLED, TaskState.FAILED),
        ],
    )
    def test_terminal_states(self, terminal: TerminalState, expected: TaskState) -> None:
        builder = LogBuilder().admitted().attempt()
        if terminal is TerminalState.ABORTED:
            builder = builder.abort()
        builder = builder.settled(terminal)
        assert task_state_of(_status(builder)) is expected

    def test_stopping_is_working_until_the_terminal_record_lands(self) -> None:
        """An abort in the log with no settlement yet is not CANCELED: the
        stream is about to carry the real ending."""
        builder = LogBuilder().admitted().attempt().abort()
        assert _status(builder).lifecycle is Lifecycle.STOPPING
        assert task_state_of(_status(builder)) is TaskState.WORKING


class TestContextId:
    """A conversation is one context, however many Runs it took."""

    def test_a_lone_run_is_its_own_context(self) -> None:
        assert context_id_of(RunId("run_a"), {RunId("run_a"): None}) == "run_a"

    def test_a_chain_reports_its_root(self) -> None:
        chain = {
            RunId("run_c"): RunId("run_b"),
            RunId("run_b"): RunId("run_a"),
            RunId("run_a"): None,
        }
        assert context_id_of(RunId("run_c"), chain) == "run_a"
        assert context_id_of(RunId("run_b"), chain) == "run_a"

    def test_an_unloaded_parent_ends_the_walk(self) -> None:
        assert context_id_of(RunId("run_b"), {}) == "run_b"

    def test_a_cycle_does_not_hang(self) -> None:
        """A corrupt log is refused, never repaired -- and never looped over."""
        chain = {RunId("run_a"): RunId("run_b"), RunId("run_b"): RunId("run_a")}
        assert context_id_of(RunId("run_a"), chain) in {"run_a", "run_b"}


class TestArtifactAndStatusMessage:
    def test_the_answer_becomes_an_artifact(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="Order A1 shipped on Tuesday.")
            .settled()
        )
        artifact = artifact_of(split_answer(builder.records))
        assert artifact is not None
        assert artifact.parts[0].text == "Order A1 shipped on Tuesday."
        assert artifact.artifact_id == "answer"

    def test_an_unfinished_run_has_no_artifact(self) -> None:
        """§3.7 puts outputs in artifacts; an empty one would read as an agent
        that answered with nothing."""
        builder = LogBuilder().admitted().attempt().turn()
        assert artifact_of(split_answer(builder.records)) is None

    def test_a_question_becomes_the_status_message(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1", tool="ask_question")
            .suspended(
                reason=SuspendReason.QUESTION,
                pending_call_id="call_1",
                question="Which invoice?",
            )
        )
        message = status_message_of(_status(builder), context_id="ctx")
        assert message is not None
        assert message.role is Role.AGENT
        assert message.text == "Which invoice?"

    def test_the_status_message_id_is_stable_across_reads(self) -> None:
        """Polling the same suspended task twice must not mint a new message."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1", tool="ask_question")
            .suspended(
                reason=SuspendReason.QUESTION, pending_call_id="call_1", question="Which one?"
            )
        )
        first = status_message_of(_status(builder), context_id="ctx")
        second = status_message_of(_status(builder), context_id="ctx")
        assert first is not None
        assert second is not None
        assert first.message_id == second.message_id

    def test_a_working_run_says_nothing(self) -> None:
        builder = LogBuilder().admitted().attempt().turn()
        assert status_message_of(_status(builder), context_id="ctx") is None


class TestTask:
    def test_a_completed_run_is_a_completed_task_with_its_answer(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="Done.")
            .settled()
        )
        task = task_of(
            _status(builder),
            context_id="ctx-1",
            answer=split_answer(builder.records),
        )
        assert task.id == "run_test"
        assert task.context_id == "ctx-1"
        assert task.status.state is TaskState.COMPLETED
        assert task.artifacts[0].parts[0].text == "Done."

    def test_include_artifacts_false_omits_them(self) -> None:
        """§3.1.4 defaults artifacts off when listing, "to reduce payload
        size"."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="Done.")
            .settled()
        )
        task = task_of(
            _status(builder),
            context_id="ctx-1",
            answer=split_answer(builder.records),
            include_artifacts=False,
        )
        assert task.artifacts == ()

    def test_history_length_takes_the_most_recent(self) -> None:
        """The proto says "the maximum number of most recent messages", so
        truncation is from the front."""
        from psych_runtime.core.thread_view import message_views

        builder = (
            LogBuilder()
            .admitted(input={"message": "first"})
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="one")
            .turn()
            .model_started()
            .model_finished(text="two")
            .settled()
        )
        history = message_views(builder.records)
        task = task_of(_status(builder), context_id="c", history=history, history_length=1)
        assert len(task.history) == 1
        assert task.history[0].parts[0].text == "two"

        none_at_all = task_of(_status(builder), context_id="c", history=history, history_length=0)
        assert none_at_all.history == ()


class TestStreamEvents:
    def test_the_lifecycle_becomes_ordered_status_events(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="Done.")
            .settled()
        )
        events = stream_events(
            builder.records, context_id="ctx", answer=split_answer(builder.records)
        )
        states = [
            event.status_update.status.state for event in events if event.status_update is not None
        ]
        assert states == [TaskState.SUBMITTED, TaskState.WORKING, TaskState.COMPLETED]

    def test_the_answer_arrives_before_the_terminal_state(self) -> None:
        """§11.7: updates until the task is terminal, then the stream closes.
        An artifact after the terminal event would reach a client that had
        already stopped reading."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished(text="Done.")
            .settled()
        )
        events = stream_events(
            builder.records, context_id="ctx", answer=split_answer(builder.records)
        )
        kinds = ["artifact" if event.artifact_update is not None else "status" for event in events]
        assert kinds[-2:] == ["artifact", "status"]
        assert events[-2].artifact_update is not None
        assert events[-2].artifact_update.last_chunk is True

    def test_a_suspension_and_its_resumption_are_both_events(self) -> None:
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1", tool="ask_question")
            .suspended(
                reason=SuspendReason.QUESTION, pending_call_id="call_1", question="Which one?"
            )
            .resumed(payload={"answer": "the first"})
            .settled()
        )
        events = stream_events(builder.records, context_id="ctx")
        updates = [e.status_update for e in events if e.status_update is not None]
        assert [u.status.state for u in updates] == [
            TaskState.SUBMITTED,
            TaskState.WORKING,
            TaskState.INPUT_REQUIRED,
            TaskState.WORKING,
            TaskState.COMPLETED,
        ]
        asked = updates[2]
        assert isinstance(asked, TaskStatusUpdateEvent)
        assert asked.status.message is not None
        assert asked.status.message.text == "Which one?"

    def test_turn_level_records_do_not_produce_events(self) -> None:
        """A tool call is progress within WORKING, and repeating the state
        would tell a client nothing while leaking another organisation's agent
        internals."""
        builder = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .tool_started("call_1")
            .tool_finished("call_1")
            .turn()
            .settled()
        )
        events = stream_events(builder.records, context_id="ctx")
        assert len(events) == 3

    def test_every_event_carries_the_context(self) -> None:
        builder = LogBuilder().admitted().attempt().settled()
        events = stream_events(builder.records, context_id="ctx-9")
        assert all(
            event.status_update is not None and event.status_update.context_id == "ctx-9"
            for event in events
        )


def _request(**kwargs: object) -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id="m1",
            role=Role.USER,
            parts=(Part.from_text("where is order A1"),),
            **kwargs,  # type: ignore[arg-type]  # the test names real Message fields
        )
    )


class TestResolveMessage:
    """§3.4's identifier rules, applied once for both bindings."""

    def test_no_ids_starts_a_new_context(self) -> None:
        resolved = resolve_message(_request())
        assert resolved.intent is MessageIntent.NEW_CONTEXT
        assert resolved.task_id is None
        assert resolved.text == "where is order A1"

    def test_a_context_without_a_task_starts_a_new_task_in_it(self) -> None:
        resolved = resolve_message(_request(context_id="ctx-1"))
        assert resolved.intent is MessageIntent.NEW_TASK_IN_CONTEXT
        assert resolved.context_id == "ctx-1"

    def test_a_task_id_continues_that_task(self) -> None:
        resolved = resolve_message(
            _request(task_id="run_a"), known_context_of={RunId("run_a"): "ctx-1"}
        )
        assert resolved.intent is MessageIntent.CONTINUE_TASK
        assert resolved.task_id == RunId("run_a")

    def test_the_context_is_inferred_from_the_task(self) -> None:
        """§3.4.3: "Agents MUST infer contextId from the task if only taskId is
        provided"."""
        resolved = resolve_message(
            _request(task_id="run_a"), known_context_of={RunId("run_a"): "ctx-7"}
        )
        assert resolved.context_id == "ctx-7"

    def test_a_mismatched_pair_is_rejected(self) -> None:
        """§3.4.3: "Agents MUST reject messages containing mismatching contextId
        and taskId"."""
        with pytest.raises(InvalidParamsError) as err:
            resolve_message(
                _request(task_id="run_a", context_id="ctx-other"),
                known_context_of={RunId("run_a"): "ctx-1"},
            )
        assert err.value.http_status == 400
        assert err.value.jsonrpc_code == -32602

    def test_an_unknown_task_is_not_found(self) -> None:
        """§13.1 makes "not found" the answer for another tenant's task too, so
        this is the only outcome a caller can distinguish."""
        with pytest.raises(TaskNotFoundError) as err:
            resolve_message(_request(task_id="run_zzz"), known_context_of={})
        assert err.value.http_status == 404
        assert err.value.jsonrpc_code == -32001

    def test_a_message_with_no_text_is_refused(self) -> None:
        request = SendMessageRequest(
            message=Message(
                message_id="m1",
                role=Role.USER,
                parts=(Part(url="https://example.test/a.pdf", media_type="application/pdf"),),
            )
        )
        with pytest.raises(InvalidParamsError):
            resolve_message(request)
