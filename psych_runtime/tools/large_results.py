"""Large tool results: elision, offload, and the ``read_tool_output`` reader.

DESIGN.md §10.8. A tool result over a threshold is written to the log in full and
elided in the model's context, replaced by a handle plus a preview. This module
owns three things: a pure description of that threshold decision, a second
threshold deciding where the bytes physically live once they stop fitting in a
log record at all, and the ``read_tool_output`` built-in tool that reads a
stored result back by handle, with an offset/limit window or a pattern search,
transparently to which of the two storage shapes actually holds it.

## Two thresholds, because they answer two different questions

**Elision** (``decide_elision``, existing) decides what the *model* sees. It is
``Limits.large_result_bytes``, a Spec field, because it is about context
economy and belongs to the agent's own budget: a Spec author who wants the
model to see more of a result inline raises it, exactly like any other budget
in ``Limits``.

**Offload** (``decide_offload``, this ticket) decides where the *bytes*
physically live: inline in the ``ToolCallFinished`` record below the
threshold, or in a ``psych_runtime.store.blob.BlobStore`` above it. This is not a Spec
field. DESIGN.md §23 item 1 requires a Spec built in Python or from a dict to
produce the same Version hash and run identically; which storage engine a given
deployment happens to be wired to is not a fact about the agent, so it must
never change that hash. The offload threshold instead lives on the Runtime
(``Runtime.blob`` plus the constant below), the same layer that already decides
which ``Store`` a Run is on, and it is sized against the tightest backend
(DynamoDB's 400KB item limit, DESIGN.md §7) rather than per Spec, so behaviour
does not vary by which Store a consumer picked (DESIGN.md §23 item 8): a 100KB
result can sit in the log inline while still being elided for the model; a 5MB
one cannot sit in the log at all, on any adapter.

**Offload always implies elision**, regardless of the Spec's own
``large_result_bytes``. An offloaded record's ``result`` field is ``None`` (the
payload lives in the BlobStore instead), and ``psych_runtime.core.conversation``'s
projection treats the absence of ``result_handle`` as "``result`` is the
model-visible payload." A Spec configured with a very large
``large_result_bytes`` must not leave the model looking at ``None`` for a call
whose real result needed a blob; ``psych_runtime.runtime.agent`` forces elision
(``force_elision``) whenever it offloads, so the two decisions cannot disagree
about whether ``result_handle`` is set.

## Resolving the tension between DESIGN.md §10.8 and §7

§10.8 says a large result is "written to the log in full... the log always
holds the whole thing." §7 says DynamoDB is a first-class Store target, not a
compromise, on the strength of one constraint: every write is a single
conditional operation against one item, and DynamoDB caps that item at 400KB.
Above that size, both sentences cannot describe the same physical write,
because there is no such write to make.

What gives is *where the bytes live*, not the guarantee. "The log always holds
the whole thing" is kept in the sense DESIGN.md §10.8 actually cares about:
nothing a tool returned is ever discarded, and every byte of it stays durably
retrievable for as long as the Run's log exists. Above the offload threshold,
what the ``ToolCallFinished`` record holds is not the payload but a reference
to it (``result_blob_key``), its size (``result_bytes``, already existed) and
how to decode it (``result_content_type``), and the payload itself lives in a
``BlobStore`` keyed by ``(tenant, run_id, call_id)`` (``psych_runtime.store.blob``).
Nothing is lost; only the model's view was ever meant to be trimmed, and now
one more thing is: which physical write holds the bytes.

## Where the actual write lives, and why this module only decides

``psych_runtime.runtime.agent.AgentLoop._record_success`` is what actually elides,
offloads and writes the ``ToolCallFinished`` record, because only it holds the
call id a handle names, the Run's scope for building a ``BlobKey``, and the
journal that writes the record. This module owns the two threshold decisions
(:func:`decide_elision`, :func:`decide_offload`) as pure functions with no
journal, no store and no event loop, so they are unit-testable in isolation and
this module's own docs and tests state the contract precisely rather than by
cross-reference; ``_record_success`` calls both and writes what they return,
never reimplementing the threshold arithmetic itself. Import-linter's layering
(DESIGN.md §21) puts ``psych_runtime.tools`` *below* ``psych_runtime.runtime``, so the
dependency direction is not optional: this module cannot import from
``psych_runtime.runtime.agent``, only the reverse.

``psych_runtime.tools`` sits *above* ``psych_runtime.store`` in that same layering, so this
module is free to depend on ``psych_runtime.store.blob`` directly, which is how a
``BlobStore``, a ``BlobKey`` and ``BlobNotFound`` end up imported below without
that being a layering violation in the other direction.

## The reader's boundaries, stated rather than assumed

The alternative to a structured reader is handing the model a shell and a copy
of ripgrep, which trades a small typed API for an arbitrary-execution seam and
still does not hold the result durably. A reader instead means every edge case
is this module's to answer, so each one is answered here rather than left to
whatever the first caller happens to try:

- **Offset past the end of the content is not an error.** It returns an empty
  ``content``/``matches`` window alongside the real ``total_lines`` (or
  ``total_matches``), the same way ``list[1_000_000:]`` returns ``[]`` rather
  than raising in Python. A model that over-estimated an offset can correct
  itself from the numbers in the response instead of losing the turn to an
  exception it cannot act on.
- **A pattern with no match is not an error either**, and is distinguishable
  from "the window is empty because of ``offset``": ``total_matches`` is the
  count across the *whole* result, independent of ``offset``/``limit``. Zero
  means nothing matched anywhere; a positive number with an empty returned
  window means the match exists past where this call looked.
- **Binary or otherwise non-text content is reported, not read.** A result
  that is ``bytes``/``bytearray``, or an object ``json.dumps`` genuinely cannot
  render, comes back with ``binary=True`` and its size, and no ``content``.
  Silently ``str()``-ing raw bytes would hand the model a mangled rendering
  that reads like real text and is not; reporting the fact instead is honest
  about what is actually there.
- **A handle from another Run, or one nobody ever issued, is an access
  problem, not a not-found.** It raises ``psych_runtime.core.errors.AccessDenied``
  rather than a lookup error, which is also literally true: a handle is
  meaningless outside the Run that minted it, so resolving one from elsewhere
  is exactly the cross-tenant read DESIGN.md §6's ``invalid_deferred_handle``
  exists to prevent. In the full agent loop this path is normally unreachable:
  ``psych_runtime.core.reducer._check_deferred_handles`` refuses to fold a
  ``read_tool_output`` call naming an unknown handle before the call is ever
  executed, raising ``CorruptLog`` at journal-append time. The check here is
  defence in depth for callers that hold a ``RunStateView`` directly, and it
  is what a unit test can exercise without going through a full run.
- **Offset and limit mean lines, uniformly, never characters or elements.**
  A string result is read as itself. A structured result (dict, list, or
  anything else JSON can render) is rendered to text first, as pretty-printed
  JSON (``indent=2``, sorted keys) rather than the compact single-line form
  the model's preview uses: a compact JSON blob is one line, arbitrarily wide,
  which would make "offset means lines" degenerate into "offset is always 0
  or an error" for every structured result. Once rendered, both cases are
  read the exact same way, which is the point: one interpretation, always.

## The bound that makes this tool safe to offer

Regardless of the requested ``limit``, a response never carries more than
``_MAX_RETURN_CHARS`` of content, and no single line contributes more than
``_MAX_LINE_CHARS`` of it. ``truncated=True`` says when either bound cut the
response short, so a model reading a result one window at a time can tell "I
have it all" from "there is more."

## Reading an offloaded result: two functions, one contract

:func:`read_tool_output` stays exactly as it was: synchronous, and correct for
every result small enough to still be inline (every result this module was
ever tested against before this ticket, and every record any log already
holds). It now refuses, loudly, a handle that resolves to an offloaded result,
because silently treating that record's ``result`` of ``None`` as if the tool
had truly returned ``null`` would be a wrong answer with no error to show for
it. :func:`read_tool_output_async` is the version that can actually reach a
``BlobStore``; ``psych_runtime.runtime.agent`` calls this one always, and the plain
synchronous function stays available for the ~40 existing unit and functional
tests (and any external caller) that only ever exercise inline results and
have no reason to become coroutines over it.

The model must not be able to tell which of the two backs a given handle: both
functions answer the exact same ``ReadToolOutputResult`` shape, computed by the
exact same slicing and searching logic in either case (:func:`_slice`,
:func:`_search`), operating on text produced by the exact same rendering
(:func:`_render_for_reading`) whether that text came from ``stored.result``
directly or from decoding a fetched blob (:func:`parse_result_bytes`).

The one place the two paths genuinely differ is *how much of the blob gets
fetched*, and that difference is real, not cosmetic:

- **A pattern search always reads the whole blob.** ``total_matches`` counts
  every match across the *whole* result, independent of ``offset``/``limit``,
  so there is no way to answer it from a prefix.
- **A JSON-typed offloaded result also always reads the whole blob.** JSON
  cannot be parsed, and therefore cannot be pretty-printed the way an inline
  structured result is, from an arbitrary byte prefix; it needs to be a
  complete document first.
- **A plain-text slice (no pattern) is the one case a bounded prefix can
  answer**, and it is also the shape of the bug this ticket fixes: a large
  CSV or an API page dump is exactly a big string, not a big JSON structure.
  ``_read_text_slice_source`` fetches a growing prefix (``head`` first, then
  ``get`` with a doubling-ish ``length``) until it holds enough lines to
  answer the request or the blob ends, so a window near the front of a huge
  result costs a small fetch instead of the whole object. ``total_lines`` for
  this path comes from metadata written at offload time
  (``decide_offload``'s ``line_count``), not from counting newlines in
  whatever was fetched, because a partial prefix's own line count would
  under-report the true total and this tool's contract (see above,
  "``total_lines``... says the real extent") does not get to be approximate.
  A blob with no such metadata (written by something other than this
  module, or from before this field existed) falls back to reading it in
  full rather than guessing.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from psych_runtime.core.errors import AccessDenied, PsychError
from psych_runtime.core.ids import ToolCallId
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.records import ResultAttachment
from psych_runtime.core.reducer import RunStateView, ToolResult
from psych_runtime.core.scope import Scope
from psych_runtime.store.blob import BlobKey, BlobMetadata, BlobNotFound, BlobStore, blob_key

__all__ = [
    "TOOL_NAME",
    "ElisionDecision",
    "MatchedLine",
    "OffloadDecision",
    "OutputNotKept",
    "ReadToolOutputArguments",
    "ReadToolOutputResult",
    "attachment_blob_key",
    "decide_elision",
    "decide_offload",
    "force_elision",
    "parse_result_bytes",
    "read_tool_output",
    "read_tool_output_async",
    "read_tool_output_definition",
    "read_tool_output_tools",
    "render_result_bytes",
]


class OutputNotKept(PsychError):
    """A handle names an attachment whose bytes beyond the preview were not
    kept.

    Not an access problem (the handle is this Run's) and not a missing blob
    (none was ever written): the Spec's ``OutputPolicy.preserve`` said not to
    keep them, or no ``BlobStore`` was wired and preservation was not
    required. The preview in the call's own result is all there is.
    """


def attachment_blob_key(scope: Scope, run_id: Any, call_id: ToolCallId, slot: str) -> BlobKey:
    """Where a ``run_code`` attachment's bytes live: the call's own key with
    the slot folded into the call segment.

    Built from the Run's own scope and run id, never from anything the model
    sent or the record stored as text, for the same reason ``blob_key`` is
    (DESIGN.md §14). One call has several attachments, so the slot is part
    of the address; ``BlobKey`` refuses a segment that could escape a path,
    and a slot is a short token this codebase mints.
    """
    return blob_key(scope, run_id, ToolCallId(f"{call_id}.{slot}"))


TOOL_NAME: Final = "read_tool_output"
"""Matches the entry in ``psych_runtime.core.spec.RESERVED_TOOL_NAMES``."""

_PREVIEW_CHARS: Final = 2000
"""How much of a rendered result :func:`decide_elision` and :func:`force_elision`
put in an elided call's preview. The one definition; ``psych_runtime.runtime.agent``
never restates it, it calls into this module instead (see the module
docstring's "Where the actual write lives" section)."""

_DEFAULT_LIMIT: Final = 200
_MAX_LIMIT: Final = 1000
_MAX_LINE_CHARS: Final = 2000
"""A single line longer than this is truncated with a marker, so one enormous
line (a minified JSON blob, a base64 field) cannot dominate a response."""
_MAX_RETURN_CHARS: Final = 10_000
"""Hard ceiling on the total size of ``content`` or ``matches`` in one
response, independent of ``limit``. Smaller than the default
``large_result_bytes`` threshold (32,768) so reading a stored result does not
itself routinely produce another large result to elide."""


# ---------------------------------------------------------------------------
# The elision decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ElisionDecision:
    """Whether one tool result should be elided in the model's context.

    Attributes:
        elide: whether the model's view should be replaced by the handle and
            preview rather than the result itself.
        result_bytes: the rendered result's size in UTF-8 bytes, recorded either
            way so a report can say how large a call's result actually was.
        preview: the first ``_PREVIEW_CHARS`` characters of the rendered result,
            set only when ``elide`` is true.
        handle: the handle ``read_tool_output`` will accept for this result, set
            only when ``elide`` is true.
    """

    elide: bool
    result_bytes: int
    preview: str | None
    handle: str | None


def _rendered_for_elision(result: Any) -> str:
    """The size/preview rendering both :func:`decide_elision` and
    :func:`force_elision` measure and slice.

    Not the same rendering :func:`render_result_bytes` uses for blob storage:
    that one special-cases ``bytes``/``bytearray`` (see its own docstring), and
    this one, matching the behaviour this function has always had, does not.
    The two agreeing for ``str`` and JSON-native results (both encode the same
    string) is what keeps ``ToolCallFinished.result_bytes`` a meaningful proxy
    for an offloaded blob's real size in the common case; they can only diverge
    for a raw ``bytes`` result, an existing, narrow gap this ticket does not
    widen.

    The JSON branch uses ``separators=(",", ":")``. This rendering
    is both what :func:`decide_elision` measures against the threshold *and*
    what an elided call's preview shows the model (``psych_runtime.core.conversation``
    embeds ``ElisionDecision.preview`` verbatim), so trimming the pretty-
    printing's insertive whitespace here does two things at once: it shrinks
    the preview the model is billed for, and it means the threshold now
    measures the same bytes that whitespace change makes cheaper, rather than
    a pretty-printed size no one downstream still pays for. A ``str`` result
    is returned as itself, above, and never reaches ``json.dumps`` at all --
    it is text the tool itself chose, not a structure Psych serialises, so
    its whitespace is never this function's to rewrite.
    """
    if isinstance(result, str):
        return result
    return json.dumps(result, separators=(",", ":"), default=str)


def decide_elision(result: Any, call_id: ToolCallId, *, threshold: int) -> ElisionDecision:
    """Whether ``result`` needs eliding for the model, at ``threshold`` bytes.

    A result at exactly the threshold is kept whole: the threshold is "elide
    above this many bytes," not "elide at or above," so a limit configured as
    "32KB is fine" is honoured to the byte rather than off by one.

    Args:
        result: the raw value a tool returned, exactly as it will be stored in
            the log.
        call_id: the call this result belongs to, folded into the handle so a
            handle names one specific call rather than one specific result
            value (two calls returning identical results still get distinct
            handles).
        threshold: ``spec.limits.large_result_bytes`` for this Run.
    """
    rendered = _rendered_for_elision(result)
    size = len(rendered.encode("utf-8"))
    if size <= threshold:
        return ElisionDecision(elide=False, result_bytes=size, preview=None, handle=None)
    return ElisionDecision(
        elide=True,
        result_bytes=size,
        preview=rendered[:_PREVIEW_CHARS],
        handle=f"res_{call_id}",
    )


def force_elision(result: Any, call_id: ToolCallId) -> ElisionDecision:
    """The elision an offloaded result gets, regardless of the Spec's own
    ``large_result_bytes``.

    See the module docstring's "Offload always implies elision" section for
    why this has to exist rather than every offload simply going through
    :func:`decide_elision` with the Spec's own threshold: an offloaded
    record's ``result`` is always ``None``, and a Spec configured with a large
    enough ``large_result_bytes`` to not have elided this result on its own
    would otherwise leave ``psych_runtime.core.conversation``'s projection reading
    ``None`` as if that were the real answer.
    """
    rendered = _rendered_for_elision(result)
    size = len(rendered.encode("utf-8"))
    return ElisionDecision(
        elide=True,
        result_bytes=size,
        preview=rendered[:_PREVIEW_CHARS],
        handle=f"res_{call_id}",
    )


# ---------------------------------------------------------------------------
# The offload decision: where the bytes physically live
# ---------------------------------------------------------------------------

_CONTENT_TYPE_BINARY: Final = "application/octet-stream"
_CONTENT_TYPE_TEXT: Final = "text/plain; charset=utf-8"
_CONTENT_TYPE_JSON: Final = "application/json"

_TEXT_LIKE: Final = re.compile(r"^application/(xml|x-yaml|yaml|toml|x-ndjson|javascript)")
"""Non-``text/`` types a ``run_code`` artifact may carry that still read as
text. Anything else that is not JSON or one of this module's own three types
is handed back as bytes, which the reader reports by size."""

_LINE_COUNT_META_KEY: Final = "line_count"
"""The ``BlobMetadata.metadata`` key :func:`decide_offload` writes a text
result's line count under, so a later windowed read can report
``total_lines`` exactly without reading the whole blob to recount it (see the
module docstring's "Reading an offloaded result" section)."""


def render_result_bytes(result: Any) -> tuple[bytes, str]:
    """The canonical bytes-and-content-type encoding of a tool result, for
    blob storage.

    Three cases, matching the ones :func:`_render_for_reading` and
    :func:`_measured_size` already distinguish for reading a stored result
    back: ``bytes``/``bytearray`` are stored as themselves; a ``str`` is
    stored as its own UTF-8 bytes; anything else JSON can render becomes
    compact JSON (``default=str`` for the rare object that is not itself
    JSON-native, which cannot raise, so an offloaded result is never lost to
    a serialisation error). :func:`parse_result_bytes` is the exact inverse.
    """
    if isinstance(result, bytes | bytearray):
        return bytes(result), _CONTENT_TYPE_BINARY
    if isinstance(result, str):
        return result.encode("utf-8"), _CONTENT_TYPE_TEXT
    return json.dumps(result, default=str).encode("utf-8"), _CONTENT_TYPE_JSON


def parse_result_bytes(payload: bytes, content_type: str) -> Any:
    """The inverse of :func:`render_result_bytes`: a blob's bytes and content
    type, back into the value a stored-inline result of the same shape would
    have been.

    Raises:
        ValueError: ``content_type`` is not one this module ever writes. A
            blob this codebase produced always carries one of the three; this
            is defence against a foreign or corrupted blob, not a path any
            call from ``psych_runtime.runtime.agent`` can reach.
    """
    if content_type == _CONTENT_TYPE_BINARY:
        return bytes(payload)
    if content_type == _CONTENT_TYPE_TEXT:
        return payload.decode("utf-8")
    if content_type == _CONTENT_TYPE_JSON:
        return json.loads(payload.decode("utf-8"))
    if content_type.startswith("text/"):
        # A run_code artifact carries the type guessed from its name (text/csv,
        # text/markdown); every text type reads back as text.
        return payload.decode("utf-8", errors="replace")
    if _TEXT_LIKE.match(content_type):
        return payload.decode("utf-8", errors="replace")
    return bytes(payload)


@dataclass(frozen=True, slots=True)
class OffloadDecision:
    """Whether one tool result needs to live in a BlobStore rather than inline.

    Attributes:
        offload: whether the payload is too large for a log record.
        payload: the exact bytes to store, from :func:`render_result_bytes`.
            Meaningful whether or not ``offload`` is true; a caller that
            offloads unconditionally (there is no such caller today, but
            nothing here assumes otherwise) can use it either way.
        content_type: how to decode ``payload`` back, needed at both write
            time (``BlobStore.put``) and read time (``parse_result_bytes``).
        metadata: small string metadata to store alongside ``payload``
            (``BlobStore.put``'s own ``metadata``), populated with a text
            result's line count so a later windowed read never has to
            recompute it from a partial fetch.
    """

    offload: bool
    payload: bytes
    content_type: str
    metadata: dict[str, str]


def decide_offload(result: Any, *, threshold: int) -> OffloadDecision:
    """Whether ``result`` is too large to keep inline in the log, at
    ``threshold`` bytes.

    Same off-by-one convention as :func:`decide_elision`: ``threshold`` is
    "offload above this many bytes," so a size exactly at the threshold stays
    inline.

    Args:
        result: the raw value a tool returned, exactly as :func:`decide_elision`
            receives it.
        threshold: the offload threshold in bytes. ``psych_runtime.runtime.agent``
            passes a constant sized against DynamoDB's item limit (see the
            module docstring), never a Spec field.
    """
    payload, content_type = render_result_bytes(result)
    metadata: dict[str, str] = {}
    if content_type == _CONTENT_TYPE_TEXT:
        metadata[_LINE_COUNT_META_KEY] = str(payload.count(b"\n") + 1)
    return OffloadDecision(
        offload=len(payload) > threshold,
        payload=payload,
        content_type=content_type,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# read_tool_output: arguments and result shapes
# ---------------------------------------------------------------------------


class ReadToolOutputArguments(BaseModel):
    """What the model sends to call ``read_tool_output``.

    Mirrors the schema :func:`read_tool_output_definition` describes to the
    model. Validated independently of ``psych_runtime.tools.registry.ToolRegistry``,
    because that registry's contract is "one function, called with only its own
    arguments," and this tool needs the Run's state to resolve a handle, which a
    registered function is never given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: str = Field(min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)
    pattern: str | None = Field(default=None, min_length=1, max_length=1024)


class MatchedLine(BaseModel):
    """One line a pattern search matched, with its position in the content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_number: int = Field(ge=1)
    """1-based, the way a human (and every grep-like tool) counts lines."""
    text: str


class ReadToolOutputResult(BaseModel):
    """What ``read_tool_output`` hands back.

    ``content`` is used in slice mode (no ``pattern``); ``matches`` is used in
    search mode. The other is left at its empty default rather than the field
    being absent, so a model reading the schema sees one stable shape instead
    of two tool results with different fields depending on which mode ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle: str
    binary: bool
    """True when the stored result cannot be read as text at all: see the
    module docstring's binary-content decision. Every field below is at its
    empty default when this is true."""
    total_size_bytes: int
    total_lines: int
    """0 when ``binary`` is true. Otherwise the line count of the full
    rendered result, independent of ``offset``/``limit``."""
    offset: int
    limit: int
    pattern: str | None
    total_matches: int | None
    """Set only in search mode (``pattern`` is not None). The match count
    across the whole result, independent of ``offset``/``limit``, which is what
    makes "no match anywhere" distinguishable from "the returned window, after
    offset, is empty"."""
    returned_lines: int
    truncated: bool
    """True when more lines (or matches) exist beyond what was returned,
    whether because ``limit`` was reached or because the response's own size
    budget was. False, including when ``offset`` lands past the end: there is
    nothing further to say "there is more" about."""
    content: str = ""
    matches: tuple[MatchedLine, ...] = ()


# ---------------------------------------------------------------------------
# The tool definition, offered only when there is something to read
# ---------------------------------------------------------------------------

_DESCRIPTION = (
    "Read a tool result that was too large to show in full, by the handle its "
    "elided preview named.\n\n"
    "Without `pattern`: returns lines `offset` through `offset + limit` "
    "(0-based, exclusive at the end) of the result's text. A structured "
    "(dict or list) result is rendered as pretty-printed JSON first, and "
    "`offset`/`limit` count lines of that rendering.\n\n"
    "With `pattern`: treated as a regular expression, matched against each "
    "line. Returns matching lines with their 1-based line numbers; `offset` "
    "and `limit` then apply to the sequence of matches rather than to every "
    "line. `total_matches` is the match count across the whole result, so you "
    "can tell nothing matched (0) apart from a match existing past the window "
    "you asked for.\n\n"
    "`offset` past the end returns an empty result, not an error; "
    "`total_lines` (or `total_matches`) says the real extent so you can "
    "adjust. A response is capped in size regardless of `limit`; `truncated` "
    "is true when more remains than was returned. A binary or otherwise "
    "unreadable result comes back with `binary=true` and its size instead of "
    "content."
)


def read_tool_output_definition() -> ToolDefinition:
    """How ``read_tool_output`` is described to the model."""
    schema = ReadToolOutputArguments.model_json_schema()
    schema.pop("title", None)
    return ToolDefinition(
        name=TOOL_NAME,
        description=_DESCRIPTION,
        input_schema=schema,
        annotations=frozenset({"read-only"}),
    )


def read_tool_output_tools(state: RunStateView) -> tuple[ToolDefinition, ...]:
    """The ``extra`` tuple for ``ToolResolver.resolve(..., extra=...)``.

    Empty when this Run has stored no large result: a turn with nothing to read
    should not be offered a tool that would do nothing.
    """
    if not state.result_handles:
        return ()
    return (read_tool_output_definition(),)


# ---------------------------------------------------------------------------
# The reader itself
# ---------------------------------------------------------------------------


def _resolve_handle(state: RunStateView, handle: str) -> tuple[ToolCallId, ToolResult]:
    """The call id and settled result a handle names, or ``AccessDenied``.

    Shared by :func:`read_tool_output` and :func:`read_tool_output_async`, so
    the two agree byte-for-byte on what counts as "this handle is not this
    Run's to read" (see the module docstring on why that is an access problem
    rather than a not-found).
    """
    call_id = state.result_handles.get(handle)
    if call_id is None:
        raise AccessDenied(
            f"handle {handle!r}",
            "it names no large result stored by this Run. A handle only ever "
            "resolves within the Run that issued it, so one from another Run "
            "-- or one nobody issued at all -- cannot be read here.",
        )

    stored = next((result for result in state.tool_results if result.call_id == call_id), None)
    if stored is None:
        # Unreachable through the full agent loop: the reducer's
        # _check_deferred_handles refuses to fold a read_tool_output call
        # naming a handle with no matching entry in result_handles before this
        # function is ever called, and every entry in result_handles is put
        # there by the same ToolCallFinished record that appends to
        # tool_results. Kept as an explicit check rather than an assertion
        # because a RunStateView built outside a real fold (a hand-built test
        # fixture, say) has no such guarantee, and AccessDenied is a truer
        # answer than an uncaught KeyError.
        raise AccessDenied(
            f"handle {handle!r}",
            "resolves to a call this Run's state has no settled result for.",
        )
    return call_id, stored


def read_tool_output(state: RunStateView, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a handle against ``state`` and read the slice or search asked for.

    Synchronous, and correct for every result small enough to still be inline.
    A handle resolving to an offloaded result is a caller-wiring error, not a
    Run-time condition to handle gracefully: ``psych_runtime.runtime.agent`` always
    calls :func:`read_tool_output_async` instead, which is the one that can
    actually reach a ``BlobStore``.

    Args:
        state: the Run's derived state, from ``psych_runtime.core.reducer.reduce``. Only
            handles this exact Run issued (``state.result_handles``) resolve;
            see the module docstring on why that is an access problem rather
            than a not-found.
        arguments: the raw tool-call arguments, validated here against
            :class:`ReadToolOutputArguments`.

    Returns:
        A plain dict (``ReadToolOutputResult.model_dump()``), the shape every
        other tool result in this codebase is stored and rendered as.

    Raises:
        pydantic.ValidationError: ``arguments`` does not match the schema. The
            agent loop turns this into a tool result the model can read and
            correct, the same as any other tool's bad arguments.
        AccessDenied: ``handle`` names no result this Run stored.
        ValueError: ``pattern`` is not a valid regular expression.
        RuntimeError: ``handle`` names a result offloaded to a BlobStore. Call
            :func:`read_tool_output_async` instead.
    """
    parsed = ReadToolOutputArguments.model_validate(dict(arguments))
    _call_id, stored = _resolve_handle(state, parsed.handle)
    if stored.blob_key is not None:
        raise RuntimeError(
            f"handle {parsed.handle!r} names a result offloaded to a BlobStore; "
            "read_tool_output cannot fetch it because it is synchronous. Call "
            "psych.tools.large_results.read_tool_output_async instead."
        )
    return _answer_from_value(parsed, stored.result)


async def read_tool_output_async(
    state: RunStateView,
    arguments: Mapping[str, Any],
    blob_store: BlobStore | None,
) -> dict[str, Any]:
    """``read_tool_output``, transparent to whether the result is inline or
    offloaded to a BlobStore. See the module docstring's "Reading an offloaded
    result" section for the shape of the two paths and why they cost different
    amounts of IO.

    Args:
        state: as :func:`read_tool_output`.
        arguments: as :func:`read_tool_output`.
        blob_store: where to fetch an offloaded result from. Only ever read
            when ``stored.blob_key`` is set; a Run whose results are all
            small enough to stay inline never touches it, so ``None`` is a
            legitimate value for a Runtime with no ``BlobStore`` configured.

    Raises:
        As :func:`read_tool_output`, plus:
        AccessDenied: ``handle`` resolves to an offloaded result but no
            ``blob_store`` was given.
        psych_runtime.store.blob.BlobNotFound: the record names a blob that no
            longer exists in the store (deleted by the consumer's own
            retention policy; see ``psych_runtime.store.blob``'s module docstring on
            why that is the consumer's to manage).
    """
    parsed = ReadToolOutputArguments.model_validate(dict(arguments))
    call_id, stored = _resolve_handle(state, parsed.handle)

    attachment = _attachment_for(stored, parsed.handle)
    if attachment is not None:
        return await _read_attachment(state, parsed, call_id, attachment, blob_store)

    if stored.blob_key is None:
        return await _answer_off_the_loop(parsed, stored.result)

    if blob_store is None:
        raise AccessDenied(
            f"handle {parsed.handle!r}",
            "names a result offloaded to a BlobStore, but this Worker has no "
            "BlobStore configured to read it back from.",
        )

    # ToolCallFinished's own validator enforces that result_content_type is
    # set exactly when result_blob_key is, so this is never actually None.
    assert stored.content_type is not None
    key = blob_key(state.scope, state.run_id, call_id)

    if stored.content_type == _CONTENT_TYPE_BINARY:
        meta = await blob_store.head(key)
        if meta is None:
            raise BlobNotFound(str(key))
        return ReadToolOutputResult(
            handle=parsed.handle,
            binary=True,
            total_size_bytes=meta.size,
            total_lines=0,
            offset=parsed.offset,
            limit=parsed.limit,
            pattern=parsed.pattern,
            total_matches=None,
            returned_lines=0,
            truncated=False,
        ).model_dump()

    if parsed.pattern is not None or stored.content_type == _CONTENT_TYPE_JSON:
        # Both need the whole object: total_matches counts across everything,
        # and JSON cannot be parsed (and so cannot be pretty-printed the way
        # an inline structured result is) from an arbitrary byte prefix.
        payload = await blob_store.get(key)
        value = parse_result_bytes(payload, stored.content_type)
        return await _answer_off_the_loop(parsed, value)

    # Plain text, slice mode: the one case a bounded prefix can answer.
    meta = await blob_store.head(key)
    if meta is None:
        raise BlobNotFound(str(key))
    text, total_lines = await _read_text_slice_source(
        blob_store, key, meta, parsed.offset + parsed.limit
    )
    lines = text.split("\n")
    return _slice(parsed, lines, meta.size, total_lines=total_lines).model_dump()


def _attachment_for(stored: ToolResult, handle: str) -> ResultAttachment | None:
    """The attachment ``handle`` names on this call, or ``None`` when the
    handle is the call's own result handle."""
    if handle == stored.result_handle:
        return None
    return next((a for a in stored.attachments if a.handle == handle and a.readable), None)


def _attachment_slot(call_id: ToolCallId, attachment: ResultAttachment) -> str:
    """The slot a handle was minted with: ``out_<call_id>_<slot>``.

    Derived from the handle rather than stored twice, and the prefix is
    checked rather than assumed so a record from a foreign writer cannot make
    this address a blob it did not write.
    """
    prefix = f"out_{call_id}_"
    if not attachment.handle.startswith(prefix):
        raise AccessDenied(
            f"handle {attachment.handle!r}",
            "does not name an attachment of the call it was recorded on.",
        )
    return attachment.handle[len(prefix) :]


async def _read_attachment(
    state: RunStateView,
    parsed: ReadToolOutputArguments,
    call_id: ToolCallId,
    attachment: ResultAttachment,
    blob_store: BlobStore | None,
) -> dict[str, Any]:
    """Read one ``run_code`` attachment, wherever its bytes live.

    Inline bytes are read directly. Blob-backed text takes the same bounded
    prefix path a plain offloaded text result does, so a window near the
    front of a large stdout costs a small fetch. Binary content is reported
    by size rather than rendered.
    """
    if attachment.stored == "preview_only":
        raise OutputNotKept(
            f"handle {parsed.handle!r} names output whose bytes beyond the preview were "
            "not kept: the agent's output policy did not require it and, or, no BlobStore "
            "is configured for this Runtime. The preview in the run_code result is all "
            "there is; re-run the program to produce less, or return what you need."
        )
    if attachment.stored == "inline":
        assert attachment.data is not None  # enforced by ResultAttachment's validator
        return await _answer_off_the_loop(
            parsed, parse_result_bytes(attachment.data, attachment.content_type)
        )

    if blob_store is None:
        raise AccessDenied(
            f"handle {parsed.handle!r}",
            "names output offloaded to a BlobStore, but this Worker has no BlobStore "
            "configured to read it back from.",
        )
    key = attachment_blob_key(
        state.scope, state.run_id, call_id, _attachment_slot(call_id, attachment)
    )
    if attachment.content_type == _CONTENT_TYPE_BINARY:
        meta = await blob_store.head(key)
        if meta is None:
            raise BlobNotFound(str(key))
        return ReadToolOutputResult(
            handle=parsed.handle,
            binary=True,
            total_size_bytes=meta.size,
            total_lines=0,
            offset=parsed.offset,
            limit=parsed.limit,
            pattern=parsed.pattern,
            total_matches=None,
            returned_lines=0,
            truncated=False,
        ).model_dump()
    if parsed.pattern is not None or attachment.content_type == _CONTENT_TYPE_JSON:
        payload = await blob_store.get(key)
        return await _answer_off_the_loop(
            parsed, parse_result_bytes(payload, attachment.content_type)
        )
    meta = await blob_store.head(key)
    if meta is None:
        raise BlobNotFound(str(key))
    text, total_lines = await _read_text_slice_source(
        blob_store, key, meta, parsed.offset + parsed.limit
    )
    return _slice(parsed, text.split("\n"), meta.size, total_lines=total_lines).model_dump()


async def _answer_off_the_loop(parsed: ReadToolOutputArguments, value: Any) -> dict[str, Any]:
    """:func:`_answer_from_value`, in a worker thread.

    Everything it does is CPU-bound over a result that can be megabytes:
    rendering a structure to text, splitting it into lines, and -- in search
    mode -- running a model-written regular expression over every one of them.
    Python's ``re`` has no timeout, so a catastrophic-backtracking pattern run
    on the event loop stalls the whole Worker: every other tenant's Run in the
    process, the lease heartbeat that keeps this Run claimed, and the
    supervisor that would otherwise notice. Off the loop it costs one thread
    and nothing else. ``_cap_line`` bounds the subject itself; this bounds who
    pays for it.
    """
    return await asyncio.to_thread(_answer_from_value, parsed, value)


def _answer_from_value(parsed: ReadToolOutputArguments, value: Any) -> dict[str, Any]:
    """The shared tail of both readers, once the actual result value is in
    hand, whether it came from ``stored.result`` directly or from decoding a
    fetched blob."""
    text = _render_for_reading(value)
    if text is None:
        return ReadToolOutputResult(
            handle=parsed.handle,
            binary=True,
            total_size_bytes=_measured_size(value),
            total_lines=0,
            offset=parsed.offset,
            limit=parsed.limit,
            pattern=parsed.pattern,
            total_matches=None,
            returned_lines=0,
            truncated=False,
        ).model_dump()

    lines = text.split("\n")
    total_size_bytes = len(text.encode("utf-8"))

    if parsed.pattern is not None:
        return _search(parsed, lines, total_size_bytes).model_dump()
    return _slice(parsed, lines, total_size_bytes).model_dump()


_INITIAL_BLOB_FETCH_BYTES: Final = 65_536
"""First prefix size :func:`_read_text_slice_source` tries. Small enough that
a request for the front of a huge result costs a small fetch; large enough
that most real results answer in one round trip."""
_BLOB_FETCH_GROWTH: Final = 4
"""How fast the prefix grows when it was not enough. Geometric, so a result
that needs several rounds still only takes a handful, not one per doubling of
a much larger object."""


def _line_count_from_metadata(meta: BlobMetadata) -> int | None:
    raw = meta.metadata.get(_LINE_COUNT_META_KEY)
    return int(raw) if raw is not None else None


async def _read_text_slice_source(
    blob_store: BlobStore, key: BlobKey, meta: BlobMetadata, lines_needed: int
) -> tuple[str, int]:
    """Enough of an offloaded plain-text result to answer a slice request,
    plus its true total line count.

    The line count metadata :func:`decide_offload` writes at offload time
    makes ``total_lines`` exact without reading the whole object; the text
    itself is fetched in a growing prefix, stopping as soon as it holds
    ``lines_needed`` newlines or the object ends, whichever comes first. A
    window near the front of a huge result costs a small prefix; a window
    that needs (or exceeds) the whole object costs exactly what reading it
    inline always did.

    Falls back to one full read when the metadata is missing (a blob written
    by something other than this module, or from before this field existed):
    reporting an approximate ``total_lines`` is not on the table (see the
    module docstring), so the honest fallback is to pay for an exact one.
    """
    total_lines = _line_count_from_metadata(meta)
    if total_lines is None:
        payload = await blob_store.get(key)
        text = payload.decode("utf-8")
        return text, text.count("\n") + 1

    total_size = meta.size
    if total_size == 0:
        return "", total_lines

    fetch_size = min(_INITIAL_BLOB_FETCH_BYTES, total_size)
    while True:
        chunk = await blob_store.get(key, offset=0, length=fetch_size)
        if len(chunk) >= total_size:
            return chunk.decode("utf-8"), total_lines
        # A prefix shorter than the whole object. A boundary landing mid
        # multi-byte character is possible and is corrected by dropping the
        # incomplete tail: this count only has to be a lower bound, and a
        # short count just grows the fetch again.
        text = chunk.decode("utf-8", errors="ignore")
        if text.count("\n") >= lines_needed:
            return text, total_lines
        fetch_size = min(fetch_size * _BLOB_FETCH_GROWTH, total_size)


def _render_for_reading(result: Any) -> str | None:
    """The text form of a stored result, or ``None`` when there is not one.

    A string is read as itself. Anything else JSON can render becomes
    pretty-printed JSON (see the module docstring on why pretty rather than
    compact). ``None`` covers raw bytes and anything ``json.dumps`` genuinely
    cannot serialise: both are real content this tool will not pretend is text.
    """
    if isinstance(result, bytes | bytearray):
        return None
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _measured_size(result: Any) -> int:
    """Best-effort byte size of a result that failed ``_render_for_reading``.

    Exact for bytes and bytearray, which is the common real case. Falls back to
    the same permissive rendering ``decide_elision`` uses (``default=str``,
    which never fails) for the rare non-serialisable object, so ``binary``
    responses still carry a real number rather than an unexplained 0.
    """
    if isinstance(result, bytes | bytearray):
        return len(result)
    return len(json.dumps(result, default=str).encode("utf-8"))


def _cap_line(line: str) -> str:
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return f"{line[:_MAX_LINE_CHARS]}… [line truncated at {_MAX_LINE_CHARS} characters]"


def _take_within_budget(pieces: Sequence[str]) -> list[str]:
    """Keep leading items until ``_MAX_RETURN_CHARS`` is spent.

    Always keeps at least the first item, even if it alone would exceed the
    budget: ``_cap_line`` already bounds how large one item can be, and a
    caller with any content at all should never see an empty response purely
    because that one line is large.
    """
    kept: list[str] = []
    used = 0
    for piece in pieces:
        cost = len(piece) + 1  # +1 for the separator joining this piece to the next
        if kept and used + cost > _MAX_RETURN_CHARS:
            break
        kept.append(piece)
        used += cost
    return kept


def _slice(
    parsed: ReadToolOutputArguments,
    lines: list[str],
    total_size_bytes: int,
    *,
    total_lines: int | None = None,
) -> ReadToolOutputResult:
    """``total_lines``, when given, overrides ``len(lines)``.

    Only ``_read_text_slice_source`` needs this: ``lines`` there can be a
    prefix of the real content (see its docstring), so ``len(lines)`` would
    under-report the true total the same way a partial ``list[:n]`` does.
    Every other caller passes the full content and leaves this at its default.
    """
    if total_lines is None:
        total_lines = len(lines)
    window = lines[parsed.offset : parsed.offset + parsed.limit]
    kept = _take_within_budget([_cap_line(line) for line in window])
    return ReadToolOutputResult(
        handle=parsed.handle,
        binary=False,
        total_size_bytes=total_size_bytes,
        total_lines=total_lines,
        offset=parsed.offset,
        limit=parsed.limit,
        pattern=None,
        total_matches=None,
        returned_lines=len(kept),
        truncated=(parsed.offset + len(kept)) < total_lines,
        content="\n".join(kept),
    )


_SEARCH_DEADLINE_SECONDS: Final = 10.0
"""The whole wall clock one search gets, from spawn to exit, before it is killed.

One number, and it is the only bound enforced. It covers starting an
interpreter, piping the subject over, compiling the pattern and matching, and
it is measured by the parent because it cannot be measured anywhere else: a
backtracking match holds the GIL, so a watchdog thread inside the child never
wakes to fire.

There is deliberately no separate limit on matching alone. Splitting the
deadline would mean claiming a bound on one part of it, and nothing here can
tell the parts apart from outside the process -- a search that took nine
seconds might have spent them matching or waiting for a loaded machine to
start Python. Ten seconds is sized so that neither reading is a refusal of
ordinary work: a pattern worth running over a few megabytes needs under two,
and spawning an interpreter on a busy host has been seen to take seconds.
"""

_SEARCH_PROGRAM: Final = """
import json, re, sys
request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
matcher = re.compile(request["pattern"])
hits = [
    [index + 1, line]
    for index, line in enumerate(request["lines"])
    if matcher.search(line)
]
sys.stdout.write(json.dumps({"matches": hits}))
"""
"""The search, run somewhere it can be killed. See ``_matching_lines``."""


def _creation_flags() -> int:
    # On Windows a console child would paint a window on every search; the
    # worker may well be running behind a GUI. Zero everywhere else.
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _matching_lines(pattern: str, lines: list[str]) -> list[tuple[int, str]]:
    """Every line matching ``pattern``, with a hard ceiling on the time spent.

    The pattern is written by the model and Python's ``re`` backtracks, so
    ``(a+)+$`` against a couple of dozen characters already runs for seconds
    and against a longer line runs for longer than anyone will wait. Neither
    capping the subject nor moving the call to a thread bounds that: the cap
    only changes the exponent, and a thread cannot be interrupted, so enough
    bad patterns exhaust the pool and take every other Run down with them.

    So the match runs in a child process that can be killed, and is. What is
    bounded is the whole search, spawn to exit: the child cannot police itself,
    because a backtracking match holds the GIL and a watchdog thread inside it
    never wakes, so the parent holds the clock and no narrower guarantee is
    available from out here. The cost is one interpreter spawn per search,
    which is a fair price for the one tool argument in this library that is an
    executable language written by the model.

    Raises:
        ValueError: the pattern did not finish inside the budget, or the
            search could not be run at all. Both reach the model as an
            ordinary tool failure it can correct, and neither is silently
            downgraded to an unbounded in-process search.
    """
    # Capped first: a pattern anchored with `$` should see what the model will
    # be shown, and a shorter subject is a smaller exponent even though it is
    # not the bound.
    subjects = [_cap_line(line) for line in lines]
    request = json.dumps({"pattern": pattern, "lines": subjects}).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _SEARCH_PROGRAM],
            input=request,
            capture_output=True,
            timeout=_SEARCH_DEADLINE_SECONDS,
            check=False,
            creationflags=_creation_flags(),
        )
    except subprocess.TimeoutExpired as err:
        raise ValueError(
            f"searching with {pattern!r} was stopped after {_SEARCH_DEADLINE_SECONDS}s. "
            "Some patterns take exponential time on some text; try a simpler one, or "
            "anchor it, or read the lines and filter them in a program."
        ) from err
    except OSError as err:
        raise ValueError(
            f"the pattern search could not be run on this host ({err}). Read the output "
            "in windows instead of searching it."
        ) from err
    if completed.returncode != 0:
        raise ValueError(f"{pattern!r} could not be searched with: the search exited abnormally")
    try:
        answer = json.loads(completed.stdout.decode("utf-8"))
        return [(int(number), str(text)) for number, text in answer["matches"]]
    except (ValueError, KeyError, TypeError) as err:
        raise ValueError(f"the pattern search returned nothing readable: {err}") from err


def _search(
    parsed: ReadToolOutputArguments, lines: list[str], total_size_bytes: int
) -> ReadToolOutputResult:
    assert parsed.pattern is not None  # only called in search mode
    try:
        re.compile(parsed.pattern)
    except re.error as err:
        raise ValueError(f"{parsed.pattern!r} is not a valid regular expression: {err}") from err

    all_matches = _matching_lines(parsed.pattern, lines)
    window = all_matches[parsed.offset : parsed.offset + parsed.limit]
    kept_text = _take_within_budget([_cap_line(line) for _, line in window])
    matches = tuple(
        MatchedLine(line_number=window[i][0], text=kept_text[i]) for i in range(len(kept_text))
    )

    return ReadToolOutputResult(
        handle=parsed.handle,
        binary=False,
        total_size_bytes=total_size_bytes,
        total_lines=len(lines),
        offset=parsed.offset,
        limit=parsed.limit,
        pattern=parsed.pattern,
        total_matches=len(all_matches),
        returned_lines=len(matches),
        truncated=(parsed.offset + len(matches)) < len(all_matches),
        matches=matches,
    )
