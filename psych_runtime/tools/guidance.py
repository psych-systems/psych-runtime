"""What the model is told about what happened to its tool call.

DESIGN.md §10.6, §10.8 and §18 each already say a version of this for one
mechanism: an aborted call, an orphaned call, a declined call and the
failure-streak guard were each given good text at the site that needed it. This
module exists because "good text, written five times, independently" is exactly
how coverage gets uneven without anyone deciding it should be: the sixth site
gets written by someone who has not read the other five, and does not know the
bar.

## The principle

Every message this module produces says three things, in this order:

1. **What happened.** Plainly, and including whatever call-specific detail the
   failure carries (a validation error, an available-tools list, a traceback).
   Losing that detail to a generic wrapper is its own bug: a model told only
   "the call failed" rewrites the same call and fails the same way again.
2. **Whether retrying is sensible.** Not left to the model to guess from tone.
   A transient problem (a timeout, a dropped connection) is worth one more try;
   a policy decision, a bad tool name or a schema mismatch will fail exactly
   the same way a second time and retrying it just burns the turn budget.
3. **What to do instead.** Fix the specific thing that was wrong, use a
   different tool, or stop and ask the user. "Do not look for another way to
   call it" (the failure-streak guard's own phrasing) is the model of what this
   looks like: it forecloses the workaround the model would otherwise try.

``psych_runtime.tools.failure_streak.advisory_message`` already follows this shape, for
the tool-withholding advisory; the orphaned-call text in
``psych_runtime.core.conversation`` is the other existing example. Neither is
reimplemented here: the withholding advisory rides in a resolved tool set
rather than a ``ToolFailure`` and stays exactly where it is, and the
orphaned-call text is quoted below rather than duplicated in code, for the
reason the next section explains.

## Where the text actually lands, and why this module is not imported by
## ``psych_runtime.core.conversation``

DESIGN.md §21 and ``import-linter``'s "core imports nothing from the other
subpackages" contract mean ``psych_runtime.core`` may never import ``psych_runtime.tools``,
which is where ``psych_runtime.core.conversation`` lives and where the model's view of
a ``ToolCallFinished`` record is actually assembled. That module's own
docstring already states the discipline this module has to work within: "this
function does no truncating of its own: it reads what the writer decided.
Deciding again here would let the log and the conversation disagree about what
the model was shown." The same discipline applies to failure guidance, not just
elision.

So this module is written to be called from the *write* side: wherever
``psych_runtime.runtime`` (``psych_runtime.core``'s inward neighbour, but not the reverse) is
about to build a ``ToolFailure`` for a ``ToolCallFinished``, a ``RunSettled``, a
``ModelCallFailed`` or a ``StepCompleted`` record, it calls
:func:`failure_guidance` and puts what comes back straight into
``ToolFailure.message``. By the time ``psych_runtime.core.conversation`` reads that
message off the log, it is already the whole answer, and the projection layer
can go on doing nothing cleverer than relaying it.

Two outcomes are the deliberate exception: a call cancelled by an interrupt
(``ToolOutcome.ABORTED``) and a call whose fate is unknown after a crash
(``ToolOutcome.UNKNOWN``). Neither needs a tool-specific ``kind`` to decide what
to say, only the outcome itself, and DESIGN.md §8.4 wants exactly one wording
for "unknown" regardless of which of several write sites (a Worker's own crash
recovery, or the supervisor's force-settlement) produced it. ``psych_runtime.core.
conversation`` owns those two strings directly for that reason: unlike an
error's message, they carry no call-specific detail to preserve, so there is
nothing lost by fixing the wording at the one place all of them are read rather
than the several places they might be written. ``ABORTED_GUIDANCE`` and
``UNKNOWN_OUTCOME_GUIDANCE`` below are this module's own copies of those same
two strings, kept here only so one place documents the wording and a test
(``tests/unit/test_guidance.py``) can catch the two copies drifting apart;
nothing imports them from here, the same way ``psych_runtime.tools.large_results``
documents (and cannot avoid) reproducing ``psych_runtime.runtime.agent``'s elision
thresholds for the identical reason, one layer down.

## Exhaustiveness, and how it stays true

``ToolFailure.kind`` is a free-form string, not a closed type: most of it comes
from ``type(err).__name__`` on whatever a tool or the model client happened to
raise, and that set cannot be enumerated. What *can* be enumerated is the
smaller set of literal tokens the runtime chooses on purpose (``"denied"``,
``"invalid_arguments"`` and so on) plus the handful of well-known exception
class names worth bespoke handling (``McpServerUnreachable``, ``TimeoutError``).
:class:`FailureKind` is that closed set, and it is the only source of truth for
it: :func:`failure_guidance` matches over it with ``assert_never`` in the
fallthrough, so mypy refuses to compile this module the day a new member is
added to the enum without a matching case. ``tests/unit/test_guidance.py``
iterates ``list(FailureKind)`` for the pytest side of the same guarantee, and
separately parses every ``ToolFailure(...)`` and ``SandboxFailure(...)``
construction under ``psych/`` to confirm every literal string a call site
passes as ``kind=`` is a member here (or the one dynamic pattern that cannot
be, ``type(err).__name__``) -- so a new call site choosing a new literal kind
and forgetting to register it here fails that test too, without anyone having
to remember to update a second, hand-maintained list.

Anything that does not parse as a :class:`FailureKind` at all -- the common
case, an arbitrary exception's class name -- is not left without guidance
either: :func:`failure_guidance` falls back to text built from the failure's
own ``message`` and ``transient`` flag, which by construction covers the same
three points for any input, known or not.
"""

from __future__ import annotations

import traceback
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, assert_never

__all__ = [
    "ABORTED_GUIDANCE",
    "UNKNOWN_OUTCOME_GUIDANCE",
    "FailureKind",
    "SandboxFailureKind",
    "describe_argument_errors",
    "describe_exception",
    "elided_result_guidance",
    "failure_guidance",
    "format_traceback",
    "sandbox_failure_guidance",
]


# ---------------------------------------------------------------------------
# The two outcomes conversation.py owns directly. See the module docstring.
# ---------------------------------------------------------------------------

ABORTED_GUIDANCE: Final = "This call was cancelled because the run was interrupted."
"""Kept identical to ``psych_runtime.core.conversation``'s copy on purpose: retrying an
aborted call is never right (the Run that would receive the retry's result is
already over), and there is exactly one code path that produces
``ToolOutcome.ABORTED`` today, so there is nothing to reconcile across sites.
"""

UNKNOWN_OUTCOME_GUIDANCE: Final = (
    "The outcome of this call is unknown: the worker running it stopped before "
    "recording a result. It may or may not have taken effect. Do not assume "
    "either way; check before retrying anything with a side effect."
)
"""The model to copy, per this ticket's brief: it refuses to assert whether the
side effect happened, and tells the model to check first rather than guess
either way. Two write sites (``psych_runtime.runtime.agent``'s crash recovery and
``psych_runtime.runtime.worker``'s force-settlement) each attach their own
``ToolFailure.message`` to a ``ToolOutcome.UNKNOWN`` record for their own
report-facing reasons, and neither is wrong -- but a model reading either one
instead of this fixed wording would see the same situation described two
different ways depending on which failure mode produced it, which is the exact
inconsistency this ticket is about. ``psych_runtime.core.conversation`` uses this
wording unconditionally for ``ToolOutcome.UNKNOWN`` regardless of what either
write site put in ``.message``, which is what keeps it one wording.
"""


# ---------------------------------------------------------------------------
# Tool-call and Run failures, dispatched by ToolFailure.kind
# ---------------------------------------------------------------------------


class FailureKind(StrEnum):
    """Every ``ToolFailure.kind`` literal a call site chooses on purpose.

    Each member below names the file and the situation it is written for. A
    new call site that invents a new literal kind belongs here first; see the
    module docstring for how a test catches the case where it is not.
    """

    MALFORMED_ARGUMENTS = "malformed_arguments"
    """``psych_runtime.runtime.agent._parse_arguments``: the model's tool-call JSON did
    not parse, or was not an object."""

    INVALID_ARGUMENTS = "invalid_arguments"
    """``psych_runtime.runtime.agent.AgentLoop._execute_and_record``: the arguments
    parsed as JSON but failed the tool's schema."""

    UNKNOWN_TOOL = "unknown_tool"
    """``psych_runtime.runtime.agent.AgentLoop._run_tool_calls``: the model named a
    tool that this turn was not offered."""

    DENIED = "denied"
    """A human's approval decision was "no". Written both when the decision was
    already in the log before a dangling call was settled
    (``AgentLoop._settle_dangling_calls``) and when it arrives during the gate
    (``AgentLoop._gate``); the two sites currently disagree on wording, which
    :func:`failure_guidance` corrects by using one fixed message for this kind
    regardless of which site wrote it, the same way ``UNKNOWN_OUTCOME_GUIDANCE``
    does for the outcome-level case above. Nothing about *why* it was declined
    is call-specific, so nothing is lost by not preserving the original
    ``message``."""

    ACCESS_DENIED = "access_denied"
    """``AgentLoop._gate``: the consumer's ``Policy`` port refused the call.
    ``failure.message`` carries the reason, which is call-specific and is
    preserved."""

    REPEATED_CALL = "repeated_call"
    """``AgentLoop._run_tool_calls``: the repetition guard refused a call the
    model had already made, with the same arguments, and had already been given
    the same answer for.

    Unlike ``FAILURE_STREAK`` this is a ``ToolCallFinished`` failure and the
    model reads it mid-run, which is the point: it still has every other tool,
    and the same tool with different arguments, so there is a way forward and
    it needs to be told what it is."""

    FAILURE_STREAK = "failure_streak"
    """``AgentLoop._check_stop_conditions``: the failure-streak guard's hard
    stop ended the Run. This is a ``RunSettled`` failure, not a
    ``ToolCallFinished`` one -- the Run is already over by the time it is
    written, so this reaches ``psych_runtime.report`` and whatever the consumer shows a
    human, never another model turn."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """``AgentLoop._check_stop_conditions``: the turn or step budget ran out.
    Also a ``RunSettled`` failure for the same reason as above."""

    NOT_EXECUTED = "not_executed"
    """``AgentLoop._answer_unstarted``: the model asked for this call, and the
    turn stopped before reaching it -- a suspension, an abort, or the
    repetition guard failing the turn. Recorded rather than left unstarted
    because a provider rejects an assistant message whose tool calls have no
    results (DESIGN.md §9), so every id the model named has to be answered."""

    APPROVAL_REQUIRED_IN_PROGRAM = "approval_required_in_program"
    """``AgentLoop.call_as_binding``: a program written for ``run_code`` called
    a host tool that needs a human approval. There is no turn to suspend into
    while a subprocess waits on a socket, so the call is refused with the one
    thing that does work: call it directly."""

    BINDING_HANDLE_NOT_YOURS = "binding_handle_not_yours"
    """``AgentLoop.call_as_binding``: a program asked to read a stored result
    by a handle its own calls did not produce -- one it invented, one from
    another Run or tenant, one the model earned with a direct call, or one an
    earlier program in this Run earned. The attempt is recorded rather than
    dropped, because a read a program was not entitled to make is a thing
    that happened and belongs in the log under the program that tried it.

    Every variety is refused in the same words on purpose: two of them name a
    result this Run really holds, and a refusal that read differently for
    those would turn the reader into a way of enumerating the Run's store."""

    SUSPENSION_EXPIRED = "suspension_expired"
    """``psych_runtime.runtime.dispatch.resume``: a decision arrived after the
    suspension's own expiry (DESIGN.md §11). The Run is settled ``ABANDONED``
    rather than executing a call decided against a world that has moved on."""

    ORPHANED = "orphaned"
    """``AgentLoop._settle_dangling_calls`` and ``Worker._force_settle``: a call
    whose result a dead Attempt never recorded. Always paired with
    ``ToolOutcome.UNKNOWN`` on a ``ToolCallFinished`` record, where
    ``psych_runtime.core.conversation`` already overrides the model-facing text with
    ``UNKNOWN_OUTCOME_GUIDANCE`` regardless of this kind's ``message`` -- so
    this case matters for ``psych_runtime.report`` and other consumers of the raw
    ``ToolFailure``, not for what the model sees in that path."""

    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    """``Worker._fail_exhausted``: a Run has been claimed and crashed its
    Worker more times than the fleet will keep reclaiming it for. A
    ``RunSettled`` failure."""

    ATTEMPT_FAILED = "attempt_failed"
    """``Worker._settle_failure``: an Attempt raised before it could settle the
    Run itself. A ``RunSettled`` failure. The message names the exception and
    carries its traceback, because "the attempt raised" on its own sends a
    person to the Worker's stdout to learn what actually happened, and a
    platform reading the log has no stdout to go to."""

    TOOL_RESOLUTION = "tool_resolution"
    """``AgentLoop._turn``: the tool set for a turn could not be resolved, which
    today means a required MCP server did not answer (DESIGN.md §10.7) or a
    credential it needs is missing. A ``RunSettled`` failure written by the
    loop itself rather than an exception escaping to the Worker, so the record
    names the server and the reason instead of a generic attempt failure."""

    FORCE_SETTLED = "force_settled"
    """``Worker._force_settle``: the supervisor wrote a terminal record over
    work that did not unwind within its grace period (DESIGN.md §8.4). A
    ``RunSettled`` failure, and every dangling call it orphans on the way is
    recorded with :data:`FailureKind.ORPHANED` instead."""

    DEADLINE = "deadline"
    """``psych_runtime.runtime.execute``: a delegated child Run passed its deadline and
    unwound cleanly within the grace period. A ``RunSettled`` failure on the
    child, distinct from :data:`FailureKind.FORCE_SETTLED` in that this one
    *did* unwind in time."""

    BLOB_STORE_REQUIRED = "blob_store_required"
    """``AgentLoop._record_success``: a result crossed the offload threshold
    but the Run has no ``BlobStore`` configured to hold it. Landed in
    ``psych_runtime.runtime.agent`` concurrently with this ticket; picked up by the
    scan ``tests/unit/test_guidance.py`` runs over the real source rather than
    a hand-maintained list, which is the point of that test."""

    COMPACTION_FAILED = "compaction_failed"
    """``AgentLoop._compact``: the conversation had to be summarised to carry
    on and the summarising call failed, or came back empty. A
    ``RunSettled`` failure. There is no half-compacted state to describe: the
    ``compaction_applied`` record is written only once a summary exists, so a
    Run that failed here is exactly the Run it was before it tried."""

    MCP_SERVER_UNREACHABLE = "McpServerUnreachable"
    """``psych_runtime.tools.mcp.McpServerUnreachable``, reached through
    ``AgentLoop._execute_and_record``'s generic exception handler, which stores
    ``type(err).__name__`` as the kind. Named for the exception class rather
    than a chosen token because nothing constructs this ``ToolFailure``
    directly; it is worth a bespoke case anyway because "the integration is
    down" is common enough to deserve better than the generic fallback."""

    TIMEOUT = "TimeoutError"
    """``TimeoutError`` (and ``asyncio.TimeoutError``, the same class since
    Python 3.11) surfacing through the same generic exception handler as
    above. Also worth a bespoke case: whether retrying a timeout is safe
    depends on whether the call was a read or had a side effect, which the
    generic fallback cannot know but can at least ask about."""


_TRACEBACK_LIMIT: Final = 65_536
"""``ToolFailure.traceback``'s own bound (``psych_runtime.core.records``). Every write
site formats through :func:`format_traceback` so a traceback longer than the
record allows (a recursion error, say) is trimmed rather than raising a
validation error inside the very append that was recording the failure."""


def describe_exception(err: BaseException) -> str:
    """``TypeName: message`` for a failure record, with the chain walked.

    A bare ``str(err)`` loses the type, and for an exception raised ``from``
    another it loses the cause, which is usually the part that says why. This
    renders the outermost exception and every cause after it, innermost last.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current)
        parts.append(f"{type(current).__name__}: {text}" if text else type(current).__name__)
        implicit = None if current.__suppress_context__ else current.__context__
        current = current.__cause__ or implicit
    return " <- caused by ".join(parts)


def format_traceback(err: BaseException) -> str:
    """A failure's full traceback, chained causes included, within the bound.

    Kept from the tail when it must be cut: the innermost frames are the ones
    that say what went wrong.
    """
    text = "".join(traceback.format_exception(err))
    if len(text) <= _TRACEBACK_LIMIT:
        return text
    return "...\n" + text[-(_TRACEBACK_LIMIT - 4) :]


def describe_argument_errors(errors: Sequence[Mapping[str, Any]]) -> str:
    """Render Pydantic's argument-validation errors as the model should read them.

    Takes ``ValidationError.errors()`` rather than ``str(err)``, which is
    written for a Python developer reading a traceback and carries three things
    that do not belong in a model's context:

    - The arguments model's own name, as in ``1 validation error for
      OrderArgs``. That name is an implementation detail. Worse, it differs
      between the two registration doors: the decorator synthesises
      ``<tool>_Arguments`` while a consumer calling ``register_dynamic`` names
      the model whatever they like, so the same malformed call produced
      different text depending on how the tool happened to be registered.
    - Pydantic's own diagnostics, ``[type=extra_forbidden, input_value=True,
      input_type=bool]``, which restate the sentence before them.
    - A ``https://errors.pydantic.dev/`` link. Sending a model a URL and the
      words "for further information visit" invites it to go and fetch one,
      which is a strange thing for a tool-argument error to ask.

    What is left is the part that lets the model fix the call: which field, and
    what is wrong with it.
    """
    lines: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ())) or "(root)"
        lines.append(f"{location}: {error.get('msg', 'is invalid')}")
    return "; ".join(lines)


def failure_guidance(
    kind: str,
    message: str,
    *,
    transient: bool = False,
    traceback: str | None = None,
) -> str:
    """The full text for ``ToolFailure.message``, given the pieces of one.

    Called from the write side (``psych_runtime.runtime``) before a ``ToolFailure`` is
    constructed, so that by the time it reaches the log and then
    ``psych_runtime.core.conversation``, the message is already the whole answer. See
    the module docstring for why this cannot be called the other way around,
    from the read side.

    Args:
        kind: the failure's classifier. Matched against :class:`FailureKind`
            for bespoke text; anything that does not parse as a member (an
            arbitrary exception's class name, chiefly) gets the generic
            fallback.
        message: what actually happened, as already composed at the call site.
            Preserved verbatim in every case that carries call-specific detail
            (a validation error, a tool name, a status). Discarded only for
            :data:`FailureKind.DENIED`, whose message never carries anything
            call-specific and whose sites currently disagree on wording (see
            that member's docstring).
        transient: whether the classifier judged this worth retrying
            (``ToolFailure.transient``, DESIGN.md §8.6). Drives the fallback's
            retry framing; ignored by the bespoke cases, which each already
            know their own retry story better than a boolean can say.
        traceback: appended after the guidance when present. Leave this
            ``None`` when the result is going into ``ToolFailure.message`` for
            a ``ToolCallFinished``: ``psych_runtime.core.conversation`` already
            appends ``ToolFailure.traceback`` itself after ``.message``, so
            passing it here too would print it twice. Pass it only when
            nothing downstream will append it separately (a ``RunSettled`` or
            ``ModelCallFailed`` failure that some consumer renders as one
            string rather than as fields).

    Returns:
        Text that says what happened, whether retrying is worth it, and what
        to do instead, for any input.
    """
    try:
        known = FailureKind(kind)
    except ValueError:
        return _with_traceback(_generic_guidance(message, transient=transient), traceback)
    return _with_traceback(_known_failure_text(known, message), traceback)


def _known_failure_text(kind: FailureKind, message: str) -> str:  # noqa: PLR0912 - one case per member
    """The bespoke text for one :class:`FailureKind`, before any traceback.

    Split out of :func:`failure_guidance` so each has a small, ruff-legal
    branch and return count; the two together are what that function's
    docstring describes. One assignment per case and a single return at the
    end, rather than returning from inside each case, is what keeps this
    version's own return count down despite covering fifteen members.
    """
    match kind:
        case FailureKind.MALFORMED_ARGUMENTS | FailureKind.REPEATED_CALL:
            # The two pass-through kinds. Each arrives with a message that
            # already says what happened and what to do instead -- send a valid
            # JSON object, or use the answer you already have -- so appending
            # generic retry advice would only dilute wording written to be
            # specific. `tests/unit/test_guidance.py` names this same pair in
            # `_PASS_THROUGH_KINDS`, and pins each one's real wording, because
            # the guard that checks every other kind for advice deliberately
            # cannot check these.
            text = message
        case FailureKind.INVALID_ARGUMENTS:
            text = (
                f"{message}\n\n"
                "Fix the fields named above and call the tool again with corrected "
                "arguments. If you cannot tell what is wrong, ask the user rather "
                "than guessing at the schema."
            )
        case FailureKind.UNKNOWN_TOOL:
            text = (
                f"{message}\n\n"
                "Do not call that name again; it does not exist for this run. Use "
                "one of the tools listed above, or tell the user if none of them do "
                "what they are asking for."
            )
        case FailureKind.DENIED:
            # message is discarded: see this member's own docstring.
            text = (
                "A human declined this call. Do not try it again. Tell the user it "
                "was not approved and ask how they would like to proceed."
            )
        case FailureKind.ACCESS_DENIED:
            text = (
                f"{message}\n\n"
                "This is a permissions decision, not a mistake in how the call was "
                "made. Calling it again unchanged will be refused again. Tell the "
                "user what you were not able to do and ask how they would like to "
                "proceed."
            )
        case (
            FailureKind.FAILURE_STREAK
            | FailureKind.BUDGET_EXHAUSTED
            | FailureKind.ATTEMPTS_EXHAUSTED
            | FailureKind.ATTEMPT_FAILED
        ):
            text = (
                f"{message}\n\nThe run has already ended because of this; there is "
                "nothing left to retry within it."
            )
        case FailureKind.TOOL_RESOLUTION:
            text = (
                f"{message}\n\n"
                "The run ended before any model call was made, so nothing was "
                "attempted on the user's behalf. Retrying is reasonable once the "
                "server is reachable or the connection is marked optional; nothing "
                "here needs to be checked for a partial side effect."
            )
        case FailureKind.NOT_EXECUTED:
            text = (
                f"{message}\n\n"
                "Nothing about this call ran, so retrying is safe. Ask for it again "
                "if it is still what you want to do, or tell the user what stopped."
            )
        case FailureKind.APPROVAL_REQUIRED_IN_PROGRAM:
            text = (
                f"{message}\n\n"
                "Do not try to call it from a program again; it will be refused the "
                "same way. Call the tool directly, as its own tool call, so the "
                "approval can be asked for."
            )
        case FailureKind.BINDING_HANDLE_NOT_YOURS:
            text = (
                f"{message}\n\n"
                "A program may page back only the results its own calls produced, so "
                "do not try the same handle from another program -- it will be "
                "refused the same way. Call `read_tool_output` directly, as its own "
                "tool call, with a handle you were given."
            )
        case FailureKind.SUSPENSION_EXPIRED:
            text = (
                f"{message}\n\n"
                "Nothing that was waiting on the decision ran, so retrying is safe. "
                "Tell the user the request timed out and ask whether to start again."
            )
        case FailureKind.ORPHANED | FailureKind.FORCE_SETTLED:
            text = (
                f"{message}\n\nWhatever was in flight when this happened may or may "
                "not have taken effect. Do not assume either way; check before "
                "retrying anything with a side effect."
            )
        case FailureKind.DEADLINE:
            text = (
                f"{message}\n\n"
                "Nothing here needs to be checked for a partial side effect the way "
                "a force-settlement would; it stopped cleanly. Retrying is "
                "reasonable if the work still needs doing."
            )
        case FailureKind.BLOB_STORE_REQUIRED:
            text = (
                f"{message}\n\n"
                "Retrying the same call will fail the same way until this Run's "
                "environment is configured to hold results this large. Tell the "
                "user the result was too large to return here rather than retrying."
            )
        case FailureKind.COMPACTION_FAILED:
            text = (
                f"{message}\n\n"
                "Nothing was compacted, so the conversation is exactly as it was "
                "and no work was lost. The run has already ended: the next model "
                "call would have carried a conversation too long to send. Tell the "
                "user the conversation outgrew the model's context window, and "
                "continue in a new one."
            )
        case FailureKind.MCP_SERVER_UNREACHABLE:
            text = (
                f"{message}\n\n"
                "This may be a temporary outage or network problem. Retrying once is "
                "reasonable; if it fails again, stop and tell the user this "
                "integration is currently unavailable rather than retrying "
                "repeatedly."
            )
        case FailureKind.TIMEOUT:
            text = (
                f"{message}\n\n"
                "The call may or may not have completed before it timed out. If it "
                "has a side effect, check whether it took effect before retrying; if "
                "it is a pure read, retrying is safe."
            )
        case _ as unreachable:
            assert_never(unreachable)
    return text


def _generic_guidance(message: str, *, transient: bool) -> str:
    """What any input, however unclassified, still gets.

    Every ``kind`` that is not a :class:`FailureKind` member reaches here,
    which is most of them: ``type(err).__name__`` on whatever a tool, an MCP
    call or the model client happened to raise. The only signal available for
    an arbitrary exception is ``ToolFailure.transient`` (DESIGN.md §8.6's own
    classifier already decided this), so that is what the retry framing turns
    on.
    """
    if transient:
        advice = (
            "This looks like a transient problem: a timeout, a dropped connection, "
            "or a temporary error upstream. Retrying the same call once or twice is "
            "reasonable, but if it keeps failing, stop and tell the user rather than "
            "retrying indefinitely."
        )
    else:
        advice = (
            "This is unlikely to succeed by retrying the same call unchanged. "
            "Change your approach based on what the message above says, use a "
            "different tool, or tell the user what is wrong and ask how they would "
            "like to proceed."
        )
    return f"{message}\n\n{advice}"


def _with_traceback(text: str, traceback: str | None) -> str:
    if not traceback:
        return text
    return f"{text}\n\n{traceback}"


# ---------------------------------------------------------------------------
# Sandboxed programs. Wired into psych_runtime.tools.code's "error" field; see
# sandbox_failure_guidance's own docstring for the shape of that call.
# ---------------------------------------------------------------------------


class SandboxFailureKind(StrEnum):
    """Every ``SandboxFailure.kind`` a sandbox adapter writes.

    Deliberately its own enum rather than folded into :class:`FailureKind`:
    ``psych_runtime.sandbox.port.SandboxFailure`` is field-for-field identical to
    ``ToolFailure`` but is a separate type on purpose (see that module's own
    docstring), so its ``kind`` vocabulary is closed independently of
    ``ToolFailure``'s and should not be matched against the wrong list.
    """

    EXCEPTION = "exception"
    """The program's own code raised, uncaught."""

    TIMEOUT = "timeout"
    """The wall-clock cap (``SandboxLimit.wall_seconds``) ended it."""

    RESOURCE_LIMIT = "resource_limit"
    """A cap in ``SandboxLimit`` other than wall-clock ended it: CPU time,
    address space, file size or process count."""

    PROTOCOL_VIOLATION = "protocol_violation"
    """The child broke the framed wire protocol (``psych_runtime.sandbox.protocol``)
    rather than the program itself failing."""

    TERMINATED = "terminated"
    """The process or container was killed by a signal that was none of the
    above (an OOM kill, an external ``kill -9``)."""

    SETUP = "setup"
    """The execution could not be confined the way it was configured to be, so
    it was not run (or its result was withheld). ``SubprocessSandbox(
    require_network_denial=True)`` on a worker without ``CAP_NET_ADMIN`` is
    one case: the program would have run with network access it was told it
    would not have, and a result produced under weaker isolation than the
    consumer asked for is not one they can trust."""

    ISOLATION_UNAVAILABLE = "isolation_unavailable"
    """The backend cannot reach the isolation level the agent requires
    (``psych_runtime.sandbox.profiles.resolve_execution``, and every adapter's
    own check after the child reported what it achieved). The program's
    output is withheld: nothing produced under weaker terms than requested is
    ever returned as if it were not."""

    NETWORK_NOT_ALLOWED = "network_not_allowed"
    """The agent asked for raw network access and the profile does not allow
    it, or the backend cannot grant it."""

    BACKEND_NOT_READY = "backend_not_ready"
    """The profile's backend reported it cannot run anything right now: no
    interpreter, no container runtime, an unreachable remote service."""

    POLICY_REFUSED = "policy_refused"
    """The tenant's ``CodeExecutionPolicy`` raised, which is a refusal."""

    CANCELLED = "cancelled"
    """The caller ended the execution before the program finished. The whole
    process tree was killed; nothing it did after that point took effect."""

    PROVIDER_ERROR = "provider_error"
    """A remote sandbox service failed, answered with something the adapter
    could not read, or disconnected mid-execution. Whether the program ran to
    completion is unknown."""


def sandbox_failure_guidance(  # noqa: PLR0911 - one return per SandboxFailureKind
    kind: str, message: str, *, traceback: str | None = None
) -> str:
    """The full text for a sandboxed program's failure.

    ``psych_runtime.tools.code`` calls this for its ``error`` field: a program's
    ``stdout``, ``stderr``, ``error``, ``error_type`` and ``traceback`` are
    returned as fields on a *successful* tool result rather than as a
    ``ToolFailure`` (DESIGN.md §18: "failures are data", by design, not raised
    and not routed through ``ToolOutcome.ERROR`` at all), so this is reached
    directly from that module rather than through ``failure_guidance``.
    ``psych_runtime.tools.code`` does not pass ``traceback`` here: it keeps the
    program's traceback as its own separate ``"traceback"`` field on the same
    payload, and folding it into ``error`` too would print it twice in one
    tool result.

    Args:
        kind: matched against :class:`SandboxFailureKind`. Anything else (there
            is currently nothing else; the type is closed) gets the same
            generic, non-transient fallback :func:`failure_guidance` uses,
            since a sandboxed program's own bugs are never worth retrying
            unchanged.
        message: what the sandbox adapter reported.
        traceback: the program's own traceback, when the failure came from the
            program raising rather than from a limit or the protocol. Leave
            this ``None`` when the caller already carries the traceback as its
            own separate field, the same rule :func:`failure_guidance`'s own
            ``traceback`` parameter documents.

    Returns:
        Text that says what happened, whether retrying is worth it, and what
        to do instead.
    """
    try:
        known = SandboxFailureKind(kind)
    except ValueError:
        return _with_traceback(_generic_guidance(message, transient=False), traceback)

    match known:
        case SandboxFailureKind.EXCEPTION:
            return _with_traceback(
                f"{message}\n\n"
                "Read the traceback and fix the line it points at, rather than "
                "rewriting the program from scratch. Running the same program again "
                "unchanged will fail the same way.",
                traceback,
            )
        case SandboxFailureKind.TIMEOUT:
            return _with_traceback(
                f"{message}\n\n"
                "The program did not finish before its wall-clock limit. If it can "
                "do less work per call, split it into smaller programs across "
                "several calls rather than retrying the same one unchanged.",
                traceback,
            )
        case SandboxFailureKind.RESOURCE_LIMIT:
            return _with_traceback(
                f"{message}\n\n"
                "The program used more of one resource (memory, CPU time, output "
                "size) than it is allowed. Reduce what it does in one call rather "
                "than retrying it unchanged.",
                traceback,
            )
        case SandboxFailureKind.PROTOCOL_VIOLATION:
            return _with_traceback(
                f"{message}\n\n"
                "This is not a mistake in the program's logic; the execution "
                "environment itself misbehaved. Retrying the same program once is "
                "reasonable; if it keeps happening, tell the user that code "
                "execution is currently unreliable.",
                traceback,
            )
        case SandboxFailureKind.TERMINATED:
            return _with_traceback(
                f"{message}\n\n"
                "The program was killed from outside rather than failing on its own. "
                "Retrying is reasonable if the program is unchanged; if it keeps "
                "being killed, it is likely still over a resource limit even though "
                "no specific limit was reported.",
                traceback,
            )
        case (
            SandboxFailureKind.SETUP
            | SandboxFailureKind.ISOLATION_UNAVAILABLE
            | SandboxFailureKind.NETWORK_NOT_ALLOWED
            | SandboxFailureKind.BACKEND_NOT_READY
            | SandboxFailureKind.POLICY_REFUSED
        ):
            return _with_traceback(
                f"{message}\n\n"
                "Nothing about the program ran to completion, so nothing it would "
                "have done took effect. Retrying it unchanged will be refused the "
                "same way: tell the user code execution is not correctly configured "
                "on this host rather than trying again.",
                traceback,
            )
        case SandboxFailureKind.CANCELLED:
            return _with_traceback(
                f"{message}\n\n"
                "The execution was stopped from outside before the program finished, "
                "and everything it started was ended with it. Do not retry on your "
                "own: whoever stopped it decides whether to run it again.",
                traceback,
            )
        case SandboxFailureKind.PROVIDER_ERROR:
            return _with_traceback(
                f"{message}\n\n"
                "The sandbox service failed rather than the program. Whether the "
                "program ran to completion is unknown, so retry only if it has no side "
                "effects; if this keeps happening, tell the user code execution is "
                "currently unreliable.",
                traceback,
            )
        case _ as unreachable:
            assert_never(unreachable)


# ---------------------------------------------------------------------------
# The one success case that needs guidance: a result too large to inline.
# ---------------------------------------------------------------------------


def elided_result_guidance(result_bytes: int, handle: str, preview: str) -> str:
    """The text for a successful call whose result was too large to inline.

    Not imported by ``psych_runtime.core.conversation`` for the same layering reason as
    everything else here (see the module docstring); that module keeps its own
    identical copy, and ``tests/unit/test_guidance.py`` asserts the two stay in
    sync. Reproduced here anyway, rather than leaving it undocumented, because
    the ticket's inventory names it explicitly and because a success can carry
    exactly as much of a "what to do instead" as a failure can: a model told
    only "truncated" either gives up or invents the missing part, the same
    failure mode as a bare "it failed".

    Args:
        result_bytes: the full result's size, as decided by
            ``psych_runtime.tools.large_results.decide_elision``.
        handle: the handle ``read_tool_output`` accepts to read the rest.
        preview: the first slice of the rendered result, already computed by
            the same decision.

    Returns:
        Text naming the size, the handle, and the tool that reads the rest,
        followed by the preview.
    """
    return (
        f"This result is {result_bytes} bytes, too large to include in full. It is "
        f"stored under handle {handle!r}. Call `read_tool_output` with that handle "
        f"to read it, with an offset or a search pattern.\n\nFirst part of the "
        f"result:\n{preview}"
    )
