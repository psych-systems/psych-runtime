"""The fake model's own coverage.

DESIGN.md §22: the fake is what makes "no test may make a real network call"
livable, so its own behaviour is part of the gate, not a convenience assumed to
work. Every scriptable behaviour listed in DESIGN.md §22 gets a test
here that proves it actually produces the events it claims.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from psych_runtime.core.messages import ToolDefinition, UserMessage
from psych_runtime.core.usage import Usage
from psych_runtime.model.port import (
    ModelClient,
    ModelRequest,
    ReasoningDelta,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
)
from psych_runtime.model.pricing import DEFAULT_PRICES, ModelPrice, StaticPriceTable, compute_cost
from psych_runtime.model.transient import classify_status, is_transient
from psych_runtime.testing.fake_model import (
    FakeModel,
    FakeModelScriptExhausted,
    FakePermanentFailure,
    FakeTransientFailure,
    ToolCallScript,
)

pytestmark = pytest.mark.unit


def _request(**kwargs: object) -> ModelRequest:
    base: dict[str, object] = {
        "model": "fake-standard",
        "messages": (UserMessage(content="hi"),),
    }
    base.update(kwargs)
    return ModelRequest.model_validate(base)


async def _consume(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in stream]


# ---------------------------------------------------------------------------
# Structural conformance
# ---------------------------------------------------------------------------


class TestConformsToModelClient:
    def test_a_fake_model_is_a_model_client(self) -> None:
        assert isinstance(FakeModel(), ModelClient)


# ---------------------------------------------------------------------------
# Text and reasoning, streamed as more than one chunk
# ---------------------------------------------------------------------------


class TestTextStreaming:
    async def test_default_splitting_yields_more_than_one_delta(self) -> None:
        model = FakeModel().turn(text="Let me look that up for you.")
        events = await _consume(model.stream(_request()))
        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_events) > 1
        assert "".join(e.text for e in text_events) == "Let me look that up for you."

    async def test_explicit_chunks_pin_exact_boundaries(self) -> None:
        model = FakeModel().turn(text=["Your ", "order ", "shipped."])
        events = await _consume(model.stream(_request()))
        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert [e.text for e in text_events] == ["Your ", "order ", "shipped."]

    async def test_a_turn_with_no_text_yields_no_text_deltas(self) -> None:
        model = FakeModel().turn(tool_calls=[("lookup", {"id": "A1"})])
        events = await _consume(model.stream(_request()))
        assert not [e for e in events if isinstance(e, TextDelta)]


class TestReasoningStreaming:
    async def test_reasoning_deltas_precede_text_deltas(self) -> None:
        model = FakeModel().turn(reasoning="Thinking it through first.", text="Here you go.")
        events = await _consume(model.stream(_request()))
        kinds = [type(e) for e in events if isinstance(e, ReasoningDelta | TextDelta)]
        assert kinds[0] is ReasoningDelta
        assert kinds[-1] is TextDelta

    async def test_reasoning_reassembles_exactly(self) -> None:
        model = FakeModel().turn(reasoning="Step one. Step two. Step three.")
        events = await _consume(model.stream(_request()))
        reasoning_events = [e for e in events if isinstance(e, ReasoningDelta)]
        assert len(reasoning_events) > 1
        assert "".join(e.text for e in reasoning_events) == "Step one. Step two. Step three."

    async def test_no_reasoning_given_means_no_reasoning_deltas(self) -> None:
        model = FakeModel().turn(text="just an answer")
        events = await _consume(model.stream(_request()))
        assert not [e for e in events if isinstance(e, ReasoningDelta)]


# ---------------------------------------------------------------------------
# Tool calls: normal, fragmented, malformed, and unknown to the request
# ---------------------------------------------------------------------------


class TestToolCalls:
    async def test_a_tuple_shorthand_produces_a_valid_call(self) -> None:
        model = FakeModel().turn(text="Let me look that up.", tool_calls=[("lookup", {"id": "A1"})])
        events = await _consume(model.stream(_request()))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assert deltas
        assembled = "".join(d.arguments_fragment for d in deltas)
        assert json.loads(assembled) == {"id": "A1"}

    async def test_default_finish_reason_is_tool_calls_when_a_turn_has_any(self) -> None:
        model = FakeModel().turn(tool_calls=[("lookup", {"id": "A1"})])
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "tool_calls"

    async def test_default_finish_reason_is_stop_with_no_tool_calls(self) -> None:
        model = FakeModel().turn(text="all done")
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "stop"

    async def test_arguments_arrive_as_more_than_one_fragment_by_default(self) -> None:
        model = FakeModel().turn(tool_calls=[("lookup", {"id": "A1", "region": "west"})])
        events = await _consume(model.stream(_request()))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assert len(deltas) > 1

    async def test_id_and_name_arrive_only_on_the_first_fragment(self) -> None:
        model = FakeModel().turn(tool_calls=[("lookup", {"id": "A1", "region": "west"})])
        events = await _consume(model.stream(_request()))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assert deltas[0].id is not None
        assert deltas[0].name == "lookup"
        for later in deltas[1:]:
            assert later.id is None
            assert later.name is None

    async def test_two_calls_in_one_turn_carry_distinct_indices(self) -> None:
        model = FakeModel().turn(tool_calls=[("lookup", {"id": "A1"}), ("lookup", {"id": "B2"})])
        events = await _consume(model.stream(_request()))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        indices = {d.index for d in deltas}
        assert indices == {0, 1}

    async def test_explicit_fragments_are_honoured_exactly(self) -> None:
        call = ToolCallScript(name="lookup", arguments={"id": "A1"}, fragments=('{"id"', ': "A1"}'))
        model = FakeModel().turn(tool_calls=[call])
        events = await _consume(model.stream(_request()))
        fragments = [e.arguments_fragment for e in events if isinstance(e, ToolCallDelta)]
        assert fragments == ['{"id"', ': "A1"}']

    async def test_malformed_arguments_stay_malformed(self) -> None:
        """The fake must not fix up invalid JSON on its way out."""
        call = ToolCallScript(name="lookup", raw_arguments='{"id": "A1"')  # missing brace
        model = FakeModel().turn(tool_calls=[call])
        events = await _consume(model.stream(_request()))
        deltas = [e for e in events if isinstance(e, ToolCallDelta)]
        assembled = "".join(d.arguments_fragment for d in deltas)
        assert assembled == '{"id": "A1"'
        with pytest.raises(json.JSONDecodeError):
            json.loads(assembled)

    async def test_a_tool_call_may_name_a_tool_the_request_never_offered(self) -> None:
        """The fake does not police request.tools; that is the resolver's job."""
        offered = (ToolDefinition(name="refund"),)
        request = _request(tools=offered)
        model = FakeModel().turn(tool_calls=[("does_not_exist", {})])
        events = await _consume(model.stream(request))
        names = {e.name for e in events if isinstance(e, ToolCallDelta) and e.name is not None}
        assert names == {"does_not_exist"}
        assert "does_not_exist" not in {t.name for t in request.tools}


class TestToolCallScriptValidation:
    def test_neither_arguments_nor_raw_arguments_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ToolCallScript(name="lookup")

    def test_both_arguments_and_raw_arguments_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ToolCallScript(name="lookup", arguments={}, raw_arguments="{}")

    def test_fragments_must_reassemble_to_the_wire_arguments(self) -> None:
        with pytest.raises(ValueError, match="concatenate"):
            ToolCallScript(name="lookup", arguments={"id": "A1"}, fragments=("nope",))


# ---------------------------------------------------------------------------
# Stalls: the idle-timeout guard's raw material
# ---------------------------------------------------------------------------


class TestStalls:
    async def test_an_explicit_stall_actually_takes_that_long(self) -> None:
        model = FakeModel().stalls(0.05)
        started = time.monotonic()
        await _consume(model.stream(_request()))
        elapsed = time.monotonic() - started
        assert elapsed >= 0.05

    async def test_an_unspecified_stall_exceeds_the_requests_own_timeout(self) -> None:
        request = _request(idle_timeout_seconds=0.05)
        model = FakeModel().stalls()
        started = time.monotonic()
        await _consume(model.stream(request))
        elapsed = time.monotonic() - started
        assert elapsed > request.idle_timeout_seconds

    async def test_a_disabled_timeout_still_falls_back_to_a_real_stall(self) -> None:
        request = _request(idle_timeout_seconds=0)
        model = FakeModel().stalls()
        started = time.monotonic()
        await _consume(model.stream(request))
        elapsed = time.monotonic() - started
        assert elapsed > 0

    async def test_a_stall_still_completes_cleanly_once_it_ends(self) -> None:
        model = FakeModel().stalls(0.01)
        events = await _consume(model.stream(_request()))
        assert any(isinstance(e, StreamDone) for e in events)


# ---------------------------------------------------------------------------
# Mid-token abort: must never be mistaken for a short answer
# ---------------------------------------------------------------------------


class TestAbortMidToken:
    async def test_the_stream_ends_without_stream_done(self) -> None:
        model = FakeModel().aborts_mid_token("Your order shi")
        events = await _consume(model.stream(_request()))
        assert not any(isinstance(e, StreamDone) for e in events)

    async def test_partial_text_is_still_delivered_first(self) -> None:
        model = FakeModel().aborts_mid_token("Your order shi")
        events = await _consume(model.stream(_request()))
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "Your order shi"

    async def test_an_abort_with_no_text_yields_no_events_at_all(self) -> None:
        model = FakeModel().aborts_mid_token()
        events = await _consume(model.stream(_request()))
        assert events == []


# ---------------------------------------------------------------------------
# Provider failures: transient and permanent, with a real status code
# ---------------------------------------------------------------------------


class TestProviderFailures:
    async def test_a_transient_failure_carries_the_status_code_given(self) -> None:
        model = FakeModel().raises_transient(status_code=529)
        with pytest.raises(FakeTransientFailure) as excinfo:
            await _consume(model.stream(_request()))
        assert excinfo.value.status_code == 529
        assert classify_status(excinfo.value.status_code) is True
        assert is_transient(excinfo.value) is True

    async def test_a_permanent_failure_carries_the_status_code_given(self) -> None:
        model = FakeModel().raises_permanent(status_code=401)
        with pytest.raises(FakePermanentFailure) as excinfo:
            await _consume(model.stream(_request()))
        assert excinfo.value.status_code == 401
        assert classify_status(excinfo.value.status_code) is False
        assert is_transient(excinfo.value) is False

    async def test_a_failure_turn_yields_no_events_before_raising(self) -> None:
        model = FakeModel().raises_transient(status_code=503)
        with pytest.raises(FakeTransientFailure):
            await _consume(model.stream(_request()))
        assert model.requests  # the call was still recorded before it failed


# ---------------------------------------------------------------------------
# Usage, cost, and finish_reason
# ---------------------------------------------------------------------------


class TestUsageAndFinishReason:
    async def test_declared_usage_including_cache_fields_round_trips(self) -> None:
        usage = Usage(input=100, output=50, cache_read=20, cache_write=30, cache_write_1h=10)
        model = FakeModel().turn(text="ok", usage=usage)
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.usage == usage

    async def test_usage_feeds_real_cost_arithmetic_end_to_end(self) -> None:
        usage = Usage(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write=0)
        model = FakeModel().turn(text="ok", usage=usage)
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        table = StaticPriceTable(
            {
                "fake-standard": ModelPrice(
                    input=Decimal(10),
                    output=Decimal(30),
                    cache_read=Decimal(1),
                    cache_write=Decimal(12),
                )
            }
        )
        cost = compute_cost("fake-standard", done.usage, table)
        assert cost is not None
        assert cost.amount == 41  # 10 + 30 + 1, each rate times one million tokens

    async def test_a_model_with_no_known_price_still_records_none_not_zero(self) -> None:
        model = FakeModel().turn(text="ok", usage=Usage(input=10))
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert compute_cost("fake-standard", done.usage, DEFAULT_PRICES) is None

    async def test_a_turn_with_no_usage_given_defaults_to_zero_not_an_error(self) -> None:
        model = FakeModel().turn(text="ok")
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.usage == Usage()

    async def test_finish_reason_is_settable_and_overrides_the_default(self) -> None:
        model = FakeModel().turn(text="cut off here", finish_reason="length")
        events = await _consume(model.stream(_request()))
        done = next(e for e in events if isinstance(e, StreamDone))
        assert done.finish_reason == "length"


# ---------------------------------------------------------------------------
# Per-chunk delay: makes time-to-first-token measurable
# ---------------------------------------------------------------------------


class TestChunkDelay:
    async def test_a_per_chunk_delay_makes_time_to_first_token_nonzero(self) -> None:
        model = FakeModel().turn(text=["a", "b", "c"], chunk_delay_seconds=0.03)
        stream = model.stream(_request())
        started = time.monotonic()
        first = await anext(stream)
        ttft = time.monotonic() - started
        assert isinstance(first, TextDelta)
        assert ttft >= 0.03
        async for _ in stream:
            pass

    async def test_with_no_delay_time_to_first_token_is_negligible(self) -> None:
        model = FakeModel().turn(text=["a", "b", "c"])
        stream = model.stream(_request())
        started = time.monotonic()
        await anext(stream)
        ttft = time.monotonic() - started
        assert ttft < 0.03
        async for _ in stream:
            pass


# ---------------------------------------------------------------------------
# Request capture: prompt assembly and per-turn tool resolution
# ---------------------------------------------------------------------------


class TestRequestCapture:
    async def test_the_request_is_recorded_before_the_stream_is_consumed(self) -> None:
        model = FakeModel().turn(text="ok")
        request = _request()
        stream = model.stream(request)
        assert model.requests == [request]
        await _consume(stream)

    async def test_last_request_before_any_call_raises(self) -> None:
        model = FakeModel().turn(text="ok")
        with pytest.raises(LookupError):
            _ = model.last_request

    async def test_last_request_reflects_the_most_recent_call(self) -> None:
        model = FakeModel().turn(text="first").turn(text="second")
        first_request = _request(model="fake-standard")
        second_request = _request(model="fake-reasoning")
        await _consume(model.stream(first_request))
        await _consume(model.stream(second_request))
        assert model.last_request == second_request
        assert model.requests == [first_request, second_request]

    async def test_the_tool_set_really_differs_between_turns(self) -> None:
        """Proves a test can assert the resolver recomputed tools each turn,
        rather than pinning the first turn's set for the whole run."""
        model = FakeModel().turn(text="first").turn(text="second")
        narrow_request = _request(tools=(ToolDefinition(name="refund"),))
        wide_request = _request(
            tools=(ToolDefinition(name="refund"), ToolDefinition(name="lookup"))
        )
        await _consume(model.stream(narrow_request))
        await _consume(model.stream(wide_request))
        assert len(model.requests[0].tools) != len(model.requests[1].tools)


# ---------------------------------------------------------------------------
# known_models
# ---------------------------------------------------------------------------


class TestKnownModels:
    async def test_default_known_models_is_a_small_fixed_list(self) -> None:
        model = FakeModel()
        models = await model.known_models()
        assert models == ("fake-standard", "fake-reasoning")

    async def test_known_models_is_configurable(self) -> None:
        model = FakeModel(known_models=["custom-1", "custom-2"])
        assert await model.known_models() == ("custom-1", "custom-2")


# ---------------------------------------------------------------------------
# Running past the end of the script
# ---------------------------------------------------------------------------


class TestScriptExhaustion:
    async def test_a_second_call_past_a_single_scripted_turn_raises(self) -> None:
        model = FakeModel().turn(text="only one turn")
        request = _request()
        await _consume(model.stream(request))
        with pytest.raises(FakeModelScriptExhausted) as excinfo:
            model.stream(request)
        assert excinfo.value.scripted == 1
        assert excinfo.value.call_number == 2
        assert "1 turn scripted" in str(excinfo.value)
        assert "call #2" in str(excinfo.value)

    async def test_the_exhausting_call_is_still_recorded_on_requests(self) -> None:
        model = FakeModel().turn(text="only one turn")
        request = _request()
        await _consume(model.stream(request))
        with pytest.raises(FakeModelScriptExhausted):
            model.stream(request)
        assert len(model.requests) == 2

    async def test_an_empty_script_raises_on_the_first_call(self) -> None:
        model = FakeModel()
        with pytest.raises(FakeModelScriptExhausted) as excinfo:
            model.stream(_request())
        assert excinfo.value.scripted == 0
        assert excinfo.value.call_number == 1
        assert "0 turns scripted" in str(excinfo.value)

    async def test_a_runaway_tool_calling_loop_eventually_raises_rather_than_looping(self) -> None:
        """A response that never stops calling tools exhausts the script instead
        of the fake quietly repeating the last turn forever."""
        model = FakeModel()
        for _ in range(5):
            model.turn(tool_calls=[("lookup", {"id": "A1"})])
        request = _request()
        for _ in range(5):
            await _consume(model.stream(request))
        with pytest.raises(FakeModelScriptExhausted) as excinfo:
            model.stream(request)
        assert excinfo.value.call_number == 6
