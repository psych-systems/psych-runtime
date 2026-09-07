"""``psych_runtime.core.conversation``: whitespace in what the model sees.

``_stringify`` renders a tool's result into the ``ToolResultMessage``
content the model reads. It is private and reached only through
``build_conversation``'s public surface -- the same surface the real agent
loop calls -- so these tests go through that rather than importing the
private helper directly.
"""

from __future__ import annotations

import json

import pytest

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.messages import ToolResultMessage
from psych_runtime.core.records import TerminalState, ToolFailure
from psych_runtime.testing.logs import LogBuilder

pytestmark = pytest.mark.unit


def _tool_result_content(result: object) -> str:
    log = (
        LogBuilder()
        .admitted()
        .attempt()
        .turn()
        .tool_started("call-1", "lookup")
        .tool_finished("call-1", result=result)
    )
    messages = build_conversation(log.records)
    tool_message = next(m for m in messages if isinstance(m, ToolResultMessage))
    return tool_message.content


class TestStructuredResultsAreCompact:
    """A dict or list a tool returned is a structure Psych serialises itself,
    so rendering it is compact: no insertive whitespace, billed on
    every turn the result stays in context, for formatting that carries no
    information."""

    def test_a_dict_result_has_no_insertive_whitespace(self) -> None:
        result = {"a": 1, "b": [1, 2, 3]}
        content = _tool_result_content(result)
        assert content == json.dumps(result, sort_keys=True, separators=(",", ":"))
        assert " " not in content
        assert "\n" not in content

    def test_a_list_result_has_no_insertive_whitespace(self) -> None:
        content = _tool_result_content([{"x": 1}, {"y": 2}])
        assert " " not in content
        assert "\n" not in content

    def test_a_nested_result_has_no_insertive_whitespace_at_any_depth(self) -> None:
        content = _tool_result_content({"rows": [{"id": 1, "tags": ["a", "b"]}]})
        assert content == '{"rows":[{"id":1,"tags":["a","b"]}]}'


class TestStringResultsAreUntouched:
    """A result that already arrives as a ``str`` is text the tool itself
    chose to return, never a structure this module serialises, so it must
    reach the model byte-for-byte -- whitespace included."""

    def test_a_plain_string_result_is_byte_identical(self) -> None:
        text = "line one\n  indented line two\nline three   with trailing spaces   "
        assert _tool_result_content(text) == text

    def test_a_json_looking_string_is_not_reparsed_or_reformatted(self) -> None:
        """A tool can legitimately return a *string* that already looks like
        pretty JSON -- it rendered its own report, say. That whitespace is
        the tool's choice, not a structure for Psych to normalise."""
        text = '{\n  "already": "pretty",\n  "spacing": "is the tool\\u2019s choice"\n}'
        assert _tool_result_content(text) == text

    def test_a_markdown_table_keeps_its_alignment_whitespace(self) -> None:
        table = "| a  | b |\n| -- | - |\n| 1  | 2 |\n"
        assert _tool_result_content(table) == table

    def test_an_empty_string_result_is_untouched(self) -> None:
        assert _tool_result_content("") == ""


class TestASubagentReportingBack:
    """A background child's ending reaches the parent's model as a user message.

    Not as a tool result: the ``spawn_subagent`` call that started it was
    answered turns earlier, with the child's id, because starting it was the
    thing that succeeded. Answering that call a second time would give one tool
    call two results, which providers reject and the reducer refuses to write.
    """

    def _messages(self, **kwargs: object) -> list[str]:
        log = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .spawned("run_child", name="alpha")
            .child_finished("run_child", name="alpha", **kwargs)
        )
        return [m.content for m in build_conversation(log.records)]

    def test_the_child_is_named_the_way_the_parent_named_it(self) -> None:
        content = self._messages(output={"text": "Six suppliers, cheapest is Acme."})[-1]
        assert "'alpha'" in content
        assert "run_child" in content
        assert "Six suppliers" in content

    def test_a_failed_child_reports_its_failure_rather_than_an_empty_answer(self) -> None:
        content = self._messages(
            state=TerminalState.FAILED,
            failure=ToolFailure(kind="budget_exhausted", message="It ran out of turns."),
        )[-1]
        assert "failed" in content
        assert "It ran out of turns." in content
