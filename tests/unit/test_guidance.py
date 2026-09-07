"""What the model is told about what happened to its tool call.

Two different kinds of exhaustiveness are asserted here, deliberately
not the same hand-maintained list twice:

- ``TestEveryFailureKindIsGuided`` and ``TestEveryToolOutcomeIsGuided`` iterate
  ``list(FailureKind)`` and ``list(ToolOutcome)``, so a member added to either
  enum without a matching branch fails these tests without anyone updating a
  second list here.
- ``TestNoLiteralKindEscapesTheEnum`` parses the real source under ``psych/``
  for every literal string passed as ``kind=`` to a ``ToolFailure`` or
  ``SandboxFailure`` construction and checks it against the enum, so a new call
  site choosing a new literal kind and forgetting to register it in
  ``psych_runtime.tools.guidance`` fails this test too, independently of the other two.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from psych_runtime.core.conversation import (
    _ABORTED_GUIDANCE,
    _UNKNOWN_OUTCOME_GUIDANCE,
    _result_text,
)
from psych_runtime.core.ids import new_run_id, new_tool_call_id
from psych_runtime.core.records import ToolCallFinished, ToolFailure, ToolOutcome
from psych_runtime.core.scope import Scope
from psych_runtime.tools.guidance import (
    ABORTED_GUIDANCE,
    UNKNOWN_OUTCOME_GUIDANCE,
    FailureKind,
    SandboxFailureKind,
    describe_exception,
    elided_result_guidance,
    failure_guidance,
    format_traceback,
    sandbox_failure_guidance,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A phrase from this set appearing in a message is treated as "this text takes
# a position on whether retrying makes sense, or says what to do instead".
# Deliberately a loose, varied set rather than one fixed phrase: the point made
# in the module docstring is that different failures call for different advice,
# not that every message reads the same.
_ADVICE_MARKERS = (
    "do not",
    "already ended",
    "reasonable",
    "unlikely to succeed",
    "check before",
    "fix the fields",
    "use one of the tools",
    "ask the user",
    "ask how they would",
    "tell the user",
    "read the traceback",
    "retrying is safe",
    "retrying is reasonable",
    "safe to retry",
    "may or may not have taken effect",
    "rather than retrying",
    "send the arguments again",
)

_PASS_THROUGH_KINDS = frozenset({FailureKind.MALFORMED_ARGUMENTS, FailureKind.REPEATED_CALL})
"""Kinds where ``failure_guidance`` trusts the caller's ``message`` to already
carry the advice (see this member's own case in ``_known_failure_text``) and
adds nothing of its own. A synthetic marker string, standing in for the
call-specific detail a real message would carry, therefore has nothing to
match against for this one -- unlike every other kind, where the module adds
its own advice regardless of what ``message`` says. Exercised instead by a
dedicated test below using the real production wording."""


def _has_advice(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ADVICE_MARKERS)


class TestThePrinciple:
    """The three-part shape stated at the top of ``psych_runtime.tools.guidance``."""

    def test_a_generic_transient_failure_says_retrying_may_help(self) -> None:
        text = failure_guidance("ConnectionResetByPeer", "the socket was reset", transient=True)
        assert "the socket was reset" in text
        assert "transient" in text.lower()
        assert _has_advice(text)

    def test_a_generic_permanent_failure_says_retrying_will_not_help(self) -> None:
        text = failure_guidance(
            "WeirdVendorError", "the vendor rejected the payload", transient=False
        )
        assert "the vendor rejected the payload" in text
        assert "unlikely to succeed" in text.lower()
        assert _has_advice(text)

    def test_a_traceback_is_appended_after_the_guidance_not_lost(self) -> None:
        text = failure_guidance(
            "SomeBug", "it broke", transient=False, traceback="Traceback...\nValueError: nope"
        )
        assert text.endswith("Traceback...\nValueError: nope")
        assert "it broke" in text


class TestEveryFailureKindIsGuided:
    """Every member of :class:`FailureKind`, enumerated from the enum itself.

    Each case is exercised with a distinctive marker standing in for the
    call-specific detail a real message would carry, so the assertions prove
    both that the detail survives (unless the kind is documented as
    discarding it) and that retry/next-step advice was actually added, not
    merely that *some* string came back.
    """

    @pytest.mark.parametrize("kind", sorted(set(FailureKind) - _PASS_THROUGH_KINDS))
    def test_every_kind_produces_guidance_with_advice(self, kind: FailureKind) -> None:
        marker = f"__marker_{kind.name}__"
        text = failure_guidance(kind.value, marker, transient=False)
        assert _has_advice(text), f"{kind!r} produced no retry/next-step advice: {text!r}"
        if kind is not FailureKind.DENIED:
            assert marker in text, f"{kind!r} lost the original detail: {text!r}"

    @pytest.mark.parametrize("kind", sorted(_PASS_THROUGH_KINDS))
    def test_pass_through_kinds_add_nothing_and_lose_nothing(self, kind: FailureKind) -> None:
        marker = f"__marker_{kind.name}__"
        assert failure_guidance(kind.value, marker, transient=False) == marker

    @pytest.mark.parametrize("kind", list(FailureKind))
    def test_every_kind_is_dispatched_not_generically_caught(self, kind: FailureKind) -> None:
        """A kind that parses as a FailureKind member never falls through to
        the unclassified-exception fallback, which would mean it is not
        actually receiving bespoke text."""
        marker = f"__marker_{kind.name}__"
        bespoke = failure_guidance(kind.value, marker, transient=False)
        generic = failure_guidance("NotARealFailureKind", marker, transient=False)
        assert bespoke != generic

    def test_denied_is_one_fixed_message_regardless_of_the_writer(self) -> None:
        """The two call sites that write kind="denied" today disagree on
        wording (see FailureKind.DENIED's docstring); this is the guarantee
        that a model reading either one sees the same thing."""
        from_the_gate = failure_guidance("denied", "A human declined this call.")
        from_settling_a_dangling_call = failure_guidance(
            "denied",
            "A human declined this call. Do not try it again. Tell the user it "
            "was not approved and ask how they would like to proceed.",
        )
        assert from_the_gate == from_settling_a_dangling_call

    def test_malformed_arguments_real_message_already_carries_advice(self) -> None:
        """A pass-through kind: this is agent.py's actual message text, not a
        synthetic marker, since the whole point of pass-through is that the
        caller's message is already the complete answer."""
        text = failure_guidance(
            "malformed_arguments",
            "The arguments you sent were not valid JSON: Expecting value. Send "
            "the arguments again as a single valid JSON object.",
        )
        assert _has_advice(text)

    def test_deadline_keeps_the_clean_abort_detail(self) -> None:
        text = failure_guidance(
            "deadline",
            "The run passed its deadline and was asked to stop. It unwound "
            "within the grace period, so this is a clean abort rather than a "
            "force-settlement.",
        )
        assert "clean abort" in text
        assert _has_advice(text)

    def test_invalid_arguments_keeps_the_validation_detail(self) -> None:
        text = failure_guidance(
            "invalid_arguments",
            "The arguments you sent to 'refund' do not match its schema. "
            "1 problem(s): amount: field required",
        )
        assert "amount: field required" in text
        assert "call the tool again" in text.lower()

    def test_unknown_tool_keeps_the_available_list(self) -> None:
        text = failure_guidance(
            "unknown_tool",
            "There is no tool called 'delete_everything' available to you. "
            "Available tools: lookup, refund.",
        )
        assert "lookup, refund" in text
        assert "do not call that name again" in text.lower()

    def test_access_denied_keeps_the_policy_reason(self) -> None:
        text = failure_guidance("access_denied", "You are not allowed to call 'refund': over limit")
        assert "over limit" in text
        assert "permissions decision" in text.lower()

    def test_budget_and_streak_failures_say_the_run_is_already_over(self) -> None:
        for kind in (
            "failure_streak",
            "budget_exhausted",
            "attempts_exhausted",
            "attempt_failed",
        ):
            text = failure_guidance(kind, "some run-level detail")
            assert "already ended" in text.lower()

    def test_mcp_unreachable_names_the_server_problem_and_hedges_the_retry(self) -> None:
        text = failure_guidance(
            "McpServerUnreachable", "MCP server 'billing' is unreachable: connect timeout"
        )
        assert "connect timeout" in text
        assert "temporary outage" in text.lower()

    def test_timeout_asks_about_side_effects_before_retrying(self) -> None:
        text = failure_guidance("TimeoutError", "the call did not finish in time")
        assert "side effect" in text.lower()


class TestEveryToolOutcomeIsGuided:
    """Every member of ``ToolOutcome``, driven through the real projection
    function rather than a copy of its logic."""

    @pytest.mark.parametrize("outcome", list(ToolOutcome))
    def test_every_outcome_produces_non_empty_text(self, outcome: ToolOutcome) -> None:
        record = _finished(outcome=outcome, result="fine" if outcome is ToolOutcome.OK else None)
        text = _result_text(record)
        assert text != ""

    def test_aborted_is_the_fixed_wording_regardless_of_failure(self) -> None:
        record = _finished(outcome=ToolOutcome.ABORTED)
        assert _result_text(record) == _ABORTED_GUIDANCE

    def test_unknown_is_the_fixed_wording_even_when_a_failure_disagrees(self) -> None:
        """Two write sites attach different ``failure.message`` values to an
        UNKNOWN outcome (see UNKNOWN_OUTCOME_GUIDANCE's docstring). The model
        must see the same thing either way."""
        one = _finished(
            outcome=ToolOutcome.UNKNOWN,
            failure=ToolFailure(kind="orphaned", message="agent.py's wording"),
        )
        other = _finished(
            outcome=ToolOutcome.UNKNOWN,
            failure=ToolFailure(kind="orphaned", message="worker.py's different wording"),
        )
        assert _result_text(one) == _result_text(other) == _UNKNOWN_OUTCOME_GUIDANCE

    def test_error_relays_the_failure_message(self) -> None:
        record = _finished(
            outcome=ToolOutcome.ERROR,
            failure=ToolFailure(
                kind="ValueError",
                message="no such order",
                traceback="Traceback...",
                transient=False,
            ),
        )
        text = _result_text(record)
        assert "no such order" in text

    def test_a_host_tools_traceback_is_kept_out_of_the_model_context(self) -> None:
        """A consumer's own traceback carries their paths, their module names
        and whatever the exception embedded -- a DSN, a bucket, an internal
        URL. It stays in the log for an operator and out of the prompt."""
        record = _finished(
            outcome=ToolOutcome.ERROR,
            failure=ToolFailure(
                kind="ConnectionError",
                message="the orders service is down",
                traceback='File "/srv/app/db.py", line 8: postgresql://admin:hunter2@db',
                transient=True,
            ),
        )
        text = _result_text(record)
        assert "the orders service is down" in text
        assert "hunter2" not in text
        assert "/srv/app/db.py" not in text

    def test_a_programs_own_traceback_still_reaches_the_model(self) -> None:
        """DESIGN.md §18: the model wrote the program, so the traceback is
        exactly what lets it fix the line."""
        record = _finished(
            outcome=ToolOutcome.ERROR,
            failure=ToolFailure(
                kind="ZeroDivisionError",
                message="division by zero",
                traceback='File "<program>", line 1, in <module>',
                traceback_is_for_the_model=True,
                transient=False,
            ),
        )
        assert _result_text(record).endswith('File "<program>", line 1, in <module>')

    def test_ok_without_elision_is_just_the_result(self) -> None:
        record = _finished(outcome=ToolOutcome.OK, result={"status": "shipped"})
        # Compact, not pretty-printed: a structure Psych serialises
        # itself costs nothing to render without insertive whitespace.
        assert _result_text(record) == '{"status":"shipped"}'

    def test_ok_with_elision_names_the_handle(self) -> None:
        record = _finished(
            outcome=ToolOutcome.OK,
            result_handle="res_abc123",
            result_bytes=50_000,
            preview="the first part",
        )
        text = _result_text(record)
        assert "res_abc123" in text
        assert "read_tool_output" in text
        assert "the first part" in text


class TestElidedResultGuidanceMatchesTheConversationCopy:
    """psych.core.conversation cannot import psych_runtime.tools.guidance (DESIGN.md
    §21's layering). Both keep an identical copy of this wording instead; this
    is the test that catches them drifting apart."""

    def test_the_two_copies_produce_the_same_text(self) -> None:
        via_guidance = elided_result_guidance(50_000, "res_abc123", "the first part")
        record = _finished(
            outcome=ToolOutcome.OK,
            result_handle="res_abc123",
            result_bytes=50_000,
            preview="the first part",
        )
        assert _result_text(record) == via_guidance

    def test_aborted_wording_matches(self) -> None:
        assert ABORTED_GUIDANCE == _ABORTED_GUIDANCE

    def test_unknown_outcome_wording_matches(self) -> None:
        assert UNKNOWN_OUTCOME_GUIDANCE == _UNKNOWN_OUTCOME_GUIDANCE


class TestSandboxFailureGuidance:
    """SandboxFailure is its own closed vocabulary (see FailureKind's sibling
    SandboxFailureKind); this is exercised the same way as the ToolFailure
    kinds, directly against the function ``psych_runtime.tools.code`` calls rather
    than through a full sandbox run (see the module docstring)."""

    @pytest.mark.parametrize("kind", list(SandboxFailureKind))
    def test_every_sandbox_kind_produces_guidance_with_advice(
        self, kind: SandboxFailureKind
    ) -> None:
        marker = f"__marker_{kind.name}__"
        text = sandbox_failure_guidance(kind.value, marker)
        assert marker in text
        assert _has_advice(text)

    def test_an_uncaught_program_exception_says_to_read_the_traceback(self) -> None:
        text = sandbox_failure_guidance(
            "exception", "ValueError: bad input", traceback="Traceback...\nValueError: bad input"
        )
        assert text.endswith("Traceback...\nValueError: bad input")
        assert "read the traceback" in text.lower()

    def test_an_unknown_sandbox_kind_still_gets_guidance(self) -> None:
        text = sandbox_failure_guidance("some_future_kind", "it broke somehow")
        assert "it broke somehow" in text
        assert _has_advice(text)


class TestNoLiteralKindEscapesTheEnum:
    """Parses every ``ToolFailure(...)`` and ``SandboxFailure(...)`` call under
    ``psych/`` for a literal string passed as ``kind=``, and checks it is a
    member of the matching enum here. A call site cannot silently invent a new
    kind without this test noticing, independent of and in addition to the
    mypy ``assert_never`` exhaustiveness inside ``failure_guidance`` and
    ``sandbox_failure_guidance`` themselves.

    ``psych_runtime/testing`` is excluded: its fixtures script arbitrary scenarios for
    other tests (``kind="http_500"``, ``kind="tool_error"``) that are not real
    runtime classifiers and were never meant to be registered here.
    """

    def test_every_literal_tool_failure_kind_is_a_known_failure_kind(self) -> None:
        found = _literal_kinds("ToolFailure")
        known = {member.value for member in FailureKind}
        unregistered = found - known
        assert not unregistered, (
            f"psych/ constructs ToolFailure(kind=...) with a literal not in "
            f"FailureKind: {sorted(unregistered)}. Add it to "
            f"psych.tools.guidance.FailureKind and give it guidance."
        )

    def test_every_literal_sandbox_failure_kind_is_a_known_sandbox_kind(self) -> None:
        found = _literal_kinds("SandboxFailure")
        known = {member.value for member in SandboxFailureKind}
        unregistered = found - known
        assert not unregistered, (
            f"psych/ constructs SandboxFailure(kind=...) with a literal not in "
            f"SandboxFailureKind: {sorted(unregistered)}. Add it to "
            f"psych.tools.guidance.SandboxFailureKind and give it guidance."
        )

    def test_the_scan_actually_finds_the_known_call_sites(self) -> None:
        """A regression check on the scanner itself: if this starts returning
        an empty set, the other two tests in this class are passing for the
        wrong reason (nothing was found), not because coverage is real."""
        found = _literal_kinds("ToolFailure")
        assert "malformed_arguments" in found
        assert "denied" in found
        assert "orphaned" in found


def _literal_kinds(constructor_name: str) -> set[str]:
    """Every literal string passed as ``kind=`` to ``constructor_name(...)``
    anywhere under ``psych/``, excluding ``psych_runtime/testing``'s fixtures."""
    found: set[str] = set()
    for path in (_REPO_ROOT / "psych_runtime").rglob("*.py"):
        if "testing" in path.relative_to(_REPO_ROOT / "psych_runtime").parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            name = name or (node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if name != constructor_name:
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "kind"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    found.add(kw.value.value)
    return found


_SCOPE = Scope(tenant="acme", principal="user-1")


def _finished(
    *,
    outcome: ToolOutcome,
    result: object = None,
    failure: ToolFailure | None = None,
    result_handle: str | None = None,
    result_bytes: int = 0,
    preview: str | None = None,
) -> ToolCallFinished:
    return ToolCallFinished(
        run_id=new_run_id(),
        seq=1,
        at=datetime.now(UTC),
        scope=_SCOPE,
        call_id=new_tool_call_id(),
        outcome=outcome,
        result=result,
        failure=failure,
        result_handle=result_handle,
        result_bytes=result_bytes,
        preview=preview,
    )


class TestRepeatedCallGuidanceCarriesItsOwnAdvice:
    """``REPEATED_CALL`` is a pass-through kind, so the advice a model reads is
    whatever ``psych_runtime.tools.repetition`` wrote rather than anything this module
    appends. That makes it worth pinning the real wording here: if the advisory
    ever stops telling the model what to do instead, the generic guard above
    cannot notice, because it deliberately does not check this kind."""

    def test_the_real_advisory_survives_unchanged_and_still_advises(self) -> None:
        from psych_runtime.tools.repetition import advisory_message

        advisory = advisory_message(4)
        text = failure_guidance("repeated_call", advisory)

        assert text == advisory, "a pass-through kind must not have text appended to it"
        assert _has_advice(text), (
            f"the advisory must tell the model what to do instead of repeating; got {text!r}"
        )
        assert "already have this answer" in text, (
            "the specific thing worth saying is that the model already holds the "
            "result, not merely that it should stop"
        )


class TestExceptionRendering:
    """``describe_exception`` and ``format_traceback`` are what every terminal
    failure record is built from, so they must never lose the cause and never
    exceed what a record can hold."""

    def test_describe_walks_the_cause_chain(self) -> None:
        def inner() -> None:
            raise ValueError("root")

        try:
            try:
                inner()
            except ValueError as err:
                raise RuntimeError("outer") from err
        except RuntimeError as caught:
            text = describe_exception(caught)
        assert text == "RuntimeError: outer <- caused by ValueError: root"

    def test_describe_names_a_message_less_exception_by_type(self) -> None:
        assert describe_exception(KeyboardInterrupt()) == "KeyboardInterrupt"

    def test_format_traceback_is_bounded_from_the_tail(self) -> None:
        def recurse(depth: int) -> None:
            if depth == 0:
                raise RuntimeError("bottom")
            recurse(depth - 1)

        try:
            recurse(900)
        except RuntimeError as caught:
            text = format_traceback(caught)
        assert len(text) <= 65_536
        assert text.endswith("RuntimeError: bottom\n")

    def test_tool_resolution_names_the_reason_and_says_nothing_ran(self) -> None:
        text = failure_guidance("tool_resolution", "MCP server 'crm' did not answer")
        assert "'crm'" in text
        assert "before any model call" in text
