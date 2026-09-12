"""Large tool results: the elision decision and the read_tool_output reader.

DESIGN.md §10.8. Pure logic, no IO: a ``RunStateView`` is built directly with
``psych_runtime.testing.logs.LogBuilder`` and ``psych_runtime.core.reducer.reduce`` rather than
through a Journal or a Store.
"""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import ToolCallId
from psych_runtime.core.reducer import RunStateView, reduce
from psych_runtime.testing.logs import LogBuilder
from psych_runtime.tools.large_results import (
    decide_elision,
    read_tool_output,
    read_tool_output_definition,
    read_tool_output_tools,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# decide_elision: the threshold and the preview
# ---------------------------------------------------------------------------


class TestDecideElision:
    def test_a_result_exactly_at_the_threshold_is_not_elided(self) -> None:
        result = "x" * 100
        decision = decide_elision(result, ToolCallId("call-1"), threshold=100)
        assert decision.elide is False
        assert decision.result_bytes == 100
        assert decision.handle is None
        assert decision.preview is None

    def test_a_result_one_byte_under_the_threshold_is_not_elided(self) -> None:
        result = "x" * 99
        decision = decide_elision(result, ToolCallId("call-1"), threshold=100)
        assert decision.elide is False

    def test_a_result_one_byte_over_the_threshold_is_elided(self) -> None:
        result = "x" * 101
        decision = decide_elision(result, ToolCallId("call-1"), threshold=100)
        assert decision.elide is True
        assert decision.result_bytes == 101
        assert decision.handle == "res_call-1"
        assert decision.preview is not None

    def test_the_handle_names_the_call_not_the_content(self) -> None:
        """Two calls with identical results still get distinct handles, because
        the handle addresses a call in the log, not a value."""
        result = "x" * 200
        first = decide_elision(result, ToolCallId("call-1"), threshold=100)
        second = decide_elision(result, ToolCallId("call-2"), threshold=100)
        assert first.handle != second.handle
        assert first.handle == "res_call-1"
        assert second.handle == "res_call-2"

    def test_the_preview_is_capped_at_2000_characters(self) -> None:
        result = "y" * 50_000
        decision = decide_elision(result, ToolCallId("call-1"), threshold=100)
        assert decision.preview is not None
        assert len(decision.preview) == 2000
        assert decision.preview == result[:2000]

    def test_a_short_elided_result_has_a_preview_no_longer_than_itself(self) -> None:
        result = "z" * 150
        decision = decide_elision(result, ToolCallId("call-1"), threshold=100)
        assert decision.preview == result

    def test_a_structured_result_is_rendered_before_measuring(self) -> None:
        """A dict is measured and previewed as the JSON it will actually
        become, not by some other size that has nothing to do with what the
        model or the reader will ever see."""
        result = {"rows": list(range(500))}
        decision = decide_elision(result, ToolCallId("call-1"), threshold=10)
        assert decision.elide is True
        assert decision.preview is not None
        assert decision.preview.startswith('{"rows"')

    def test_a_structured_result_s_preview_has_no_insertive_whitespace(self) -> None:
        """The preview is what an elided call's result shows the
        model (``psych_runtime.core.conversation`` embeds it verbatim), so it costs
        exactly as much as a full, non-elided result would to render
        prettily -- compact rendering applies here too, not only to the
        result the model sees in full."""
        result = {"a": 1, "b": [1, 2, 3]}
        decision = decide_elision(result, ToolCallId("call-1"), threshold=0)
        assert decision.preview == '{"a":1,"b":[1,2,3]}'

    def test_the_threshold_is_measured_against_the_same_compact_bytes(self) -> None:
        """A result that would cross the threshold when rendered with
        pretty-printing's whitespace must not be elided once that whitespace
        is gone: the byte count this function reports is the byte count the
        model is actually billed for, never a larger one no one downstream
        still pays."""
        result = {"a": 1, "b": 2}
        compact = '{"a":1,"b":2}'
        decision = decide_elision(result, ToolCallId("call-1"), threshold=len(compact))
        assert decision.elide is False
        assert decision.result_bytes == len(compact)

    def test_a_string_result_s_preview_is_untouched_even_if_it_looks_like_json(
        self,
    ) -> None:
        """A ``str`` result never reaches the JSON renderer at all: it is
        text the tool itself chose, not a structure this module
        serialises."""
        text = '{\n  "already": "pretty"\n}' + ("x" * 100)
        decision = decide_elision(text, ToolCallId("call-1"), threshold=10)
        assert decision.preview == text[:2000]


# ---------------------------------------------------------------------------
# Building a state with a stored large result, for the reader tests
# ---------------------------------------------------------------------------


def _state_with_result(
    result: object, *, call_id: str = "call-1", handle: str | None = None
) -> RunStateView:
    resolved_handle = handle if handle is not None else f"res_{call_id}"
    log = (
        LogBuilder()
        .admitted()
        .attempt()
        .turn()
        .tool_started(call_id, "big")
        .tool_finished(
            call_id,
            result=result,
            result_handle=resolved_handle,
            preview=str(result)[:2000],
            result_bytes=len(str(result)),
        )
    )
    return reduce(log.records)


# ---------------------------------------------------------------------------
# read_tool_output: offering the tool
# ---------------------------------------------------------------------------


class TestOffering:
    def test_a_run_with_no_stored_result_is_not_offered_the_tool(self) -> None:
        state = reduce(LogBuilder().admitted().attempt().records)
        assert read_tool_output_tools(state) == ()

    def test_a_run_with_a_stored_result_is_offered_the_tool(self) -> None:
        state = _state_with_result("x" * 5000)
        tools = read_tool_output_tools(state)
        assert len(tools) == 1
        assert tools[0].name == "read_tool_output"

    def test_the_definition_is_read_only(self) -> None:
        definition = read_tool_output_definition()
        assert "read-only" in definition.annotations

    def test_the_definition_schema_names_every_argument(self) -> None:
        schema = read_tool_output_definition().input_schema
        assert set(schema["properties"]) == {"handle", "offset", "limit", "pattern"}
        assert schema["required"] == ["handle"]


# ---------------------------------------------------------------------------
# read_tool_output: slicing a text result
# ---------------------------------------------------------------------------


class TestSlicing:
    def test_reading_from_the_start(self) -> None:
        text = "\n".join(f"line {i}" for i in range(10))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "limit": 3})
        assert out["content"] == "line 0\nline 1\nline 2"
        assert out["returned_lines"] == 3
        assert out["total_lines"] == 10
        assert out["truncated"] is True

    def test_reading_the_last_window_is_not_marked_truncated(self) -> None:
        text = "\n".join(f"line {i}" for i in range(10))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 8, "limit": 10})
        assert out["content"] == "line 8\nline 9"
        assert out["returned_lines"] == 2
        assert out["truncated"] is False

    def test_offset_moves_the_window(self) -> None:
        text = "\n".join(f"line {i}" for i in range(10))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 3, "limit": 2})
        assert out["content"] == "line 3\nline 4"
        assert out["offset"] == 3


class TestOffsetPastTheEnd:
    """Deliberate design decision: this is not an error. It behaves the way
    Python's own slicing does, and hands back the real length so a model that
    over-estimated an offset can correct itself without losing the turn."""

    def test_offset_past_the_end_returns_empty_content_not_an_error(self) -> None:
        text = "\n".join(f"line {i}" for i in range(5))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 1000})
        assert out["content"] == ""
        assert out["returned_lines"] == 0

    def test_offset_past_the_end_still_reports_the_real_length(self) -> None:
        text = "\n".join(f"line {i}" for i in range(5))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 1000})
        assert out["total_lines"] == 5

    def test_offset_past_the_end_is_not_marked_truncated(self) -> None:
        """Truncated means "there is more to read." Past the end, there is
        nothing more, so it would be a lie."""
        text = "\n".join(f"line {i}" for i in range(5))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 1000})
        assert out["truncated"] is False

    def test_offset_exactly_at_the_end_also_returns_empty_not_an_error(self) -> None:
        text = "\n".join(f"line {i}" for i in range(5))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "offset": 5})
        assert out["content"] == ""
        assert out["total_lines"] == 5


# ---------------------------------------------------------------------------
# read_tool_output: pattern search
# ---------------------------------------------------------------------------


class TestPatternSearch:
    def test_matching_lines_come_back_with_line_numbers(self) -> None:
        text = "alpha\nbeta\nalpha again\ngamma"
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "pattern": "alpha"})
        assert list(out["matches"]) == [
            {"line_number": 1, "text": "alpha"},
            {"line_number": 3, "text": "alpha again"},
        ]
        assert out["total_matches"] == 2

    def test_pattern_is_a_regular_expression(self) -> None:
        text = "order-1\norder-22\nreceipt-3"
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "pattern": r"^order-\d+$"})
        assert [m["text"] for m in out["matches"]] == ["order-1", "order-22"]

    def test_an_invalid_pattern_raises_an_actionable_error(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(ValueError, match="not a valid regular expression"):
            read_tool_output(state, {"handle": "res_call-1", "pattern": "("})

    def test_a_pattern_that_backtracks_forever_is_stopped(self) -> None:
        """The pattern is an executable language written by the model, and
        `re` backtracks: `(a+)+$` against a few dozen characters already runs
        for seconds, and a longer subject runs for longer than anyone waits.
        The bound is enforced on a process that gets killed, because a thread
        cannot be interrupted and enough of these would take the worker's
        whole pool with them.

        The wall clock is asserted loosely -- this is about the difference
        between seconds and never, not about a precise budget. The deadline
        covers starting an interpreter as well as matching, because a loaded
        machine can take longer to spawn one than the pattern is allowed to
        run, and counting that against the pattern would refuse ordinary
        searches whenever the host was busy.
        """
        text = "\n".join(["a" * 60 + "!"] * 10)
        state = _state_with_result(text)
        started = time.monotonic()
        with pytest.raises(ValueError, match="was stopped after"):
            read_tool_output(state, {"handle": "res_call-1", "pattern": "(a+)+$"})
        assert time.monotonic() - started < 60


class TestPatternWithNoMatch:
    """Deliberate design decision: distinguishable from an empty window caused
    by offset, via total_matches rather than an absent/empty content field
    doing double duty for two different situations."""

    def test_no_match_anywhere_is_total_matches_zero(self) -> None:
        text = "alpha\nbeta\ngamma"
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "pattern": "zzz-not-there"})
        assert out["total_matches"] == 0
        assert out["matches"] == ()

    def test_matches_exist_but_the_window_after_offset_is_empty(self) -> None:
        """total_matches is positive even though this call's own window came
        back empty, which is the tell that more exists past where it looked."""
        text = "\n".join(["alpha"] * 5)
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "pattern": "alpha", "offset": 10})
        assert out["total_matches"] == 5
        assert out["matches"] == ()


# ---------------------------------------------------------------------------
# read_tool_output: structured content
# ---------------------------------------------------------------------------


class TestStructuredContent:
    """Deliberate design decision: offset and limit mean lines of a
    pretty-printed JSON rendering, the same interpretation slicing text uses,
    never characters and never top-level elements."""

    def test_a_dict_is_rendered_as_pretty_printed_json(self) -> None:
        state = _state_with_result({"a": 1, "b": [1, 2, 3]})
        out = read_tool_output(state, {"handle": "res_call-1", "limit": 100})
        assert out["content"].startswith("{")
        assert "\n" in out["content"]  # pretty-printed, not one compact line

    def test_offset_counts_lines_of_the_rendering_not_top_level_elements(self) -> None:
        result = {"items": ["first", "second", "third"]}
        state = _state_with_result(result)
        full = read_tool_output(state, {"handle": "res_call-1", "limit": 100})
        # The rendering has more lines than the list has elements: offset=1
        # must land inside that rendering, not skip a whole top-level key.
        assert full["total_lines"] > 3
        windowed = read_tool_output(state, {"handle": "res_call-1", "offset": 1, "limit": 1})
        assert windowed["content"] == full["content"].split("\n")[1]


# ---------------------------------------------------------------------------
# read_tool_output: binary and non-text content
# ---------------------------------------------------------------------------


class TestBinaryContent:
    """Deliberate design decision: reported honestly rather than read. Raw
    bytes rendered through str() would look like real text and would not be,
    and that is worse than saying plainly that this handle is not readable
    this way."""

    def test_bytes_come_back_marked_binary_with_no_content(self) -> None:
        state = _state_with_result(b"\x00\x01\x02binary")
        out = read_tool_output(state, {"handle": "res_call-1"})
        assert out["binary"] is True
        assert out["content"] == ""
        assert out["total_lines"] == 0

    def test_bytes_still_report_a_real_size(self) -> None:
        payload = b"\x00" * 12345
        state = _state_with_result(payload)
        out = read_tool_output(state, {"handle": "res_call-1"})
        assert out["total_size_bytes"] == 12345

    def test_a_pattern_against_binary_content_does_not_crash(self) -> None:
        state = _state_with_result(b"whatever")
        out = read_tool_output(state, {"handle": "res_call-1", "pattern": "anything"})
        assert out["binary"] is True
        assert out["matches"] == ()


# ---------------------------------------------------------------------------
# read_tool_output, handle access
# ---------------------------------------------------------------------------


class TestHandleAccess:
    def test_a_handle_nobody_ever_issued_fails_as_access_denied(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(AccessDenied):
            read_tool_output(state, {"handle": "res_made_up"})

    def test_a_handle_from_a_different_run_fails_the_same_way(self) -> None:
        """A RunStateView only ever carries the handles its own log issued, so
        a handle minted for another Run is indistinguishable from one nobody
        issued at all: both are AccessDenied, never a not-found."""
        this_run = _state_with_result("some text", call_id="call-1")
        other_runs_handle = "res_call-from-a-different-run"
        with pytest.raises(AccessDenied, match="another Run"):
            read_tool_output(this_run, {"handle": other_runs_handle})

    def test_the_access_denied_message_names_the_handle(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(AccessDenied, match="res_nope"):
            read_tool_output(state, {"handle": "res_nope"})


class TestArgumentValidation:
    def test_a_missing_handle_is_a_validation_error(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(ValidationError):
            read_tool_output(state, {})

    def test_a_negative_offset_is_a_validation_error(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(ValidationError):
            read_tool_output(state, {"handle": "res_call-1", "offset": -1})

    def test_an_empty_pattern_is_a_validation_error(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(ValidationError):
            read_tool_output(state, {"handle": "res_call-1", "pattern": ""})

    def test_a_limit_over_the_maximum_is_a_validation_error(self) -> None:
        state = _state_with_result("some text")
        with pytest.raises(ValidationError):
            read_tool_output(state, {"handle": "res_call-1", "limit": 100_000})


# ---------------------------------------------------------------------------
# The response size bound
# ---------------------------------------------------------------------------


class TestBoundedResponse:
    def test_a_huge_limit_does_not_return_unbounded_content(self) -> None:
        text = "\n".join(f"line {i} of a moderately long line of text" for i in range(2000))
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "limit": 1000})
        assert len(out["content"]) <= 10_000
        assert out["truncated"] is True

    def test_one_enormous_line_is_truncated_rather_than_returned_whole(self) -> None:
        text = "a" * 500_000
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "limit": 1})
        assert len(out["content"]) < 500_000
        assert "truncated" in out["content"]

    def test_the_budget_still_returns_at_least_one_line(self) -> None:
        """A single enormous line must not produce a wholly empty response:
        the per-line cap already bounds it, so there is no reason to."""
        text = "b" * 500_000
        state = _state_with_result(text)
        out = read_tool_output(state, {"handle": "res_call-1", "limit": 1})
        assert out["returned_lines"] == 1
