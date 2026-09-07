"""Where a compaction cuts, and what the summariser is asked.

``plan_cut`` is the pure half of the write side: given a log, the
boundary already in it and how many turns to keep, it says which sequence the
summary replaces. Everything crash-safe about compaction rests on the one rule
tested here -- the cut must advance the boundary -- so these are unit tests
with no store, no model and no clock.
"""

from __future__ import annotations

import pytest

import psych_runtime
from psych_runtime.core.messages import Message, SystemMessage, UserMessage
from psych_runtime.core.records import Record
from psych_runtime.core.spec import CompactionPolicy
from psych_runtime.model.port import ModelRequest
from psych_runtime.model.transient import is_context_overflow
from psych_runtime.runtime.compaction import plan_cut, summarisation_request
from psych_runtime.testing.fake_model import FakePermanentFailure, FakeTransientFailure
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def _log(turns: int) -> list[Record]:
    """A Run of ``turns`` complete turns, each calling one tool."""
    builder = LogBuilder().admitted().attempt()
    for index in range(turns):
        builder = (
            builder.turn()
            .model_started()
            .model_finished(text=f"turn {index}")
            .tool_started(f"call-{index}", "lookup")
            .tool_finished(f"call-{index}", result={"answer": index})
        )
    return builder.records


def _turn_seqs(records: list[Record]) -> list[int]:
    return [record.seq for record in records if record.type == "turn_started"]


def _cut(records: list[Record], boundary: int, keep: int) -> int:
    cut = plan_cut(records, boundary, keep)
    assert cut is not None, "this log was built to have something to compact"
    return cut


class TestWhereTheCutGoes:
    def test_nothing_to_do_until_there_are_more_turns_than_are_kept(self) -> None:
        assert plan_cut(_log(3), 0, 3) is None

    def test_the_cut_is_the_record_before_a_turn_start(self) -> None:
        """Never mid-turn: an assistant message above the line whose tool
        results fell below it is the one shape a provider refuses outright."""
        records = _log(5)
        assert _cut(records, 0, 2) + 1 in _turn_seqs(records)

    def test_keeping_more_turns_cuts_earlier(self) -> None:
        records = _log(6)
        assert _cut(records, 0, 2) > _cut(records, 0, 4)

    def test_a_cut_that_would_not_advance_the_boundary_is_declined(self) -> None:
        """The crash-safety rule, on its own. A Worker that died after the
        append replays into exactly this: the same log, the boundary already at
        the cut, and nothing to do."""
        records = _log(5)
        assert plan_cut(records, _cut(records, 0, 2), 2) is None

    def test_a_later_turn_makes_a_new_cut_available(self) -> None:
        """And the same rule does not freeze compaction: once the Run has moved
        on, there is something new below the line again."""
        first = _cut(_log(5), 0, 2)
        assert _cut(_log(7), first, 2) > first

    def test_turns_below_an_existing_boundary_are_not_counted_again(self) -> None:
        """The turns that matter are the ones still in the model's view.
        Counting the compacted ones too would keep the boundary moving on a Run
        whose visible tail is already shorter than the policy keeps."""
        records = _log(5)
        boundary = _turn_seqs(records)[3] - 1
        assert plan_cut(records, boundary, 2) is None


def _spec(**policy: object) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="support",
        instructions="You are Ada, and you never mention refunds.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        compaction=CompactionPolicy.model_validate({"trigger_tokens": 1000, **policy}),
    )


def _request(
    spec: psych_runtime.AgentSpec, records: list[Record], *, history: tuple[Message, ...] = ()
) -> ModelRequest:
    assert spec.compaction is not None, "these cases all build a compacting Spec"
    return summarisation_request(
        spec, spec.compaction, records, _cut(records, 0, 2), history=history
    )


def _rendered(request: ModelRequest) -> str:
    return "\n".join(
        message.content for message in request.messages if isinstance(message.content, str)
    )


class TestWhatTheSummariserIsAsked:
    def test_it_is_told_the_job_rather_than_the_agent_s_persona(self) -> None:
        request = _request(_spec(), _log(5))
        system = request.messages[0]
        assert isinstance(system, SystemMessage)
        assert "Ada" not in system.content
        assert "summar" in system.content.lower()

    def test_it_offers_no_tools(self) -> None:
        assert _request(_spec(), _log(5)).tools == ()

    def test_it_reads_only_the_range_being_replaced(self) -> None:
        rendered = _rendered(_request(_spec(), _log(5)))
        assert "turn 0" in rendered
        assert "turn 4" not in rendered

    def test_it_can_be_sent_to_a_different_model_from_the_agent_s(self) -> None:
        assert _request(_spec(model="fake-reasoning"), _log(5)).model == "fake-reasoning"

    def test_the_agent_s_own_instructions_are_added_to_psych_s(self) -> None:
        """Added, never substituted, and that is the whole design of the field.

        An agent author knows what their domain cannot afford to lose. They do
        not know, and should not have to restate, the general things a summary
        always needs. A terse "keep every order number" read as the entire
        brief would throw away the constraints and the outstanding work.
        """
        request = _request(
            _spec(summary_instructions="Always keep every order number and its status."),
            _log(5),
        )
        last = request.messages[-1]
        assert isinstance(last, UserMessage)
        assert isinstance(last.content, str)
        assert "Always keep every order number and its status." in last.content
        # Psych's own floor is still there, and still above.
        assert "Preserve identifiers" in last.content
        assert last.content.index("Preserve identifiers") < last.content.index("order number")
        # And the prompt says the two combine, so a model cannot read the
        # shorter, later instruction as permission to ignore the longer one.
        assert "do not replace" in last.content

    def test_no_instructions_leaves_the_prompt_exactly_as_it_was(self) -> None:
        assert _rendered(_request(_spec(summary_instructions="   "), _log(5))) == _rendered(
            _request(_spec(), _log(5))
        )

    def test_a_continued_thread_s_history_is_summarised_too(self) -> None:
        request = _request(
            _spec(),
            _log(5),
            history=(UserMessage(content="in an earlier run I asked about A1"),),
        )
        assert "A1" in _rendered(request)


class TestRecognisingAnOverLongPrompt:
    """The classifier's third answer. A false negative leaves the Run failing
    exactly as it did before compaction existed; a false positive would hide a
    real 400 behind a summary, so these cover both directions."""

    @pytest.mark.parametrize(
        "message",
        [
            "This model's maximum context length is 8192 tokens, however you requested 9001.",
            "prompt is too long: 210000 tokens > 200000 maximum",
            "error code: context_length_exceeded",
            "Please reduce the length of the messages.",
        ],
    )
    def test_a_provider_saying_the_prompt_was_too_long_is_recognised(self, message: str) -> None:
        assert is_context_overflow(FakePermanentFailure(400, message)) is True

    def test_an_unrelated_400_is_not(self) -> None:
        assert is_context_overflow(FakePermanentFailure(400, "invalid tool schema")) is False

    def test_a_transient_failure_is_never_an_overflow(self) -> None:
        """A 503 whose body happens to mention the context window is an outage,
        and retrying it is the right answer rather than summarising."""
        assert is_context_overflow(FakeTransientFailure(503, "context window unavailable")) is False

    def test_a_401_that_mentions_context_is_not(self) -> None:
        assert is_context_overflow(FakePermanentFailure(401, "maximum context")) is False
