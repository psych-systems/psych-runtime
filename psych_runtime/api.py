"""The functions a consumer calls.

Psych is a library, not a framework. A framework inverts control; Psych does not.
Everything here is something the consumer's own code calls, and Psych calls back
only through ports the consumer supplied.

This module is the whole public surface. Anything not re-exported from
``psych/__init__.py`` is internal and will move without ceremony while the
package is ``0.x``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from psych_runtime.core.answer import AnswerView, split_answer
from psych_runtime.core.errors import AccessDenied, RunAborted, RunFailed, RunNotFound
from psych_runtime.core.ids import RunId, VersionHash
from psych_runtime.core.records import (
    ModelCallFinished,
    QueueKind,
    Record,
    RunSettled,
    TerminalState,
)
from psych_runtime.core.reducer import RunStateView, reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import Spec
from psych_runtime.core.status import RunStatus, status_of
from psych_runtime.core.thread_view import MessageView, ThreadView, message_views
from psych_runtime.core.validation import ValidationContext, validate_spec
from psych_runtime.core.version import Version
from psych_runtime.core.version import publish as _make_version
from psych_runtime.report.build import build_report
from psych_runtime.report.model import RunReport
from psych_runtime.runtime.dispatch import Dispatched
from psych_runtime.runtime.dispatch import dispatch as _dispatch
from psych_runtime.runtime.dispatch import interrupt as _interrupt
from psych_runtime.runtime.dispatch import resume as _resume
from psych_runtime.runtime.dispatch import send as _send
from psych_runtime.runtime.stream import stream as _stream
from psych_runtime.store.port import Store

__all__ = [
    "MessageView",
    "RunStatus",
    "ThreadView",
    "dispatch",
    "interrupt",
    "publish",
    "records",
    "report",
    "resume",
    "send",
    "state",
    "status",
    "stream",
    "stream_text",
    "thread",
]


async def publish(
    store: Store,
    spec: Spec,
    *,
    context: ValidationContext | None = None,
) -> Version:
    """Validate a Spec, content-hash it, and store it as an immutable Version.

    Republishing an identical Spec returns the existing Version rather than
    creating a duplicate, which is what makes redeploying on every boot harmless.

    Validation happens here and never at run time (DESIGN.md §4). A customer
    waiting on a response is not the right place to discover a typo, so a Spec
    naming an unregistered tool or a dangling skill link is refused now.

    Args:
        store: where the Version is kept.
        spec: the Spec to publish.
        context: what exists at publish time: registered tool names, known
            models, reachable MCP servers. Omitted means structural checks only,
            which is right for an offline validation pass and wrong for a real
            deployment.

    Returns:
        The Version. The same one every time for the same Spec.

    Raises:
        SpecValidationError: carrying every problem found, each naming its path.
    """
    validate_spec(spec, context)
    version = _make_version(spec)

    existing = await store.get_version(version.hash)
    if existing is not None:
        return existing

    await store.put_version(version)
    return version


async def dispatch(
    store: Store,
    version: Version | VersionHash,
    scope: Scope,
    *,
    input: dict[str, Any] | None = None,  # noqa: A002
    idempotency_key: str | None = None,
    deadline_seconds: float | None = None,
    continues: RunId | None = None,
) -> Dispatched:
    """Admit a Run, exactly once per idempotency key.

    The only trigger Psych provides. The consumer owns the clock: their CronJob,
    their queue consumer, their webhook handler calls this (DESIGN.md §20).

    Args:
        continues: pass the Run a second chat message follows to make this new
            Run see that earlier exchange (DESIGN.md §23.3). Refused
            with ``AccessDenied`` when that Run belongs to a different Scope
            -- a continuation is a new way to reach another Run's content and
            gets the same scrutiny as the MCP pool key (DESIGN.md §10.4).
    """
    return await _dispatch(
        store,
        version,
        scope,
        input=input,
        idempotency_key=idempotency_key,
        deadline_seconds=deadline_seconds,
        continues=continues,
    )


async def stream(
    store: Store, run_id: RunId, *, after: int = 0, scope: Scope | None = None
) -> AsyncIterator[Record]:
    """Every Record after ``after``, then tail until the Run settles.

    A client reconnecting passes the highest sequence it already has and misses
    nothing, including across an interrupt. The log is the stream, so there is no
    separate stream state that could break (DESIGN.md §12).

    Args:
        scope: whose read this is. Passing it refuses a Run belonging to
            another tenant -- see ``psych_runtime.records``.

    Raises:
        RunNotFound: no such Run. Raised before the first record rather than
            tailing an id nobody ever admitted, which is what this used to do:
            a typo'd id produced a stream that never yielded and never ended.
        AccessDenied: ``scope`` names a different tenant than the Run's.
    """
    header = await store.get_run(run_id)
    if header is None:
        raise RunNotFound(run_id)
    _check_scope(header.scope, scope, run_id)
    async for record in _stream(store, run_id, after=after):
        yield record


async def resume(
    store: Store,
    run_id: RunId,
    *,
    payload: dict[str, Any] | None = None,
    approved: bool | None = None,
    by: str | None = None,
) -> None:
    """Deliver a decision or payload to a suspended Run.

    Approvals, clarifying questions and webhook waits are one mechanism rather
    than three, because they all run through the same lease and log machinery
    (DESIGN.md §11).

    Args:
        by: who decided, as the consumer identifies people. Recorded on the
            ``resumed`` record and interpreted by nothing (DESIGN.md §14 keeps
            identity out of the library) -- but an approval of a destructive
            call whose log cannot say who approved it is not an audit trail,
            so there has to be somewhere to put the answer.

    Raises:
        RunNotFound: no such Run.
        RunNotSuspended: the Run is not waiting for anything.
        SuspensionExpired: the decision arrived after the suspension's own
            expiry. The Run is settled ``ABANDONED`` first, so a stale approval
            never executes (DESIGN.md §11).
    """
    await _resume(store, run_id, payload=payload, approved=approved, by=by)


async def send(
    store: Store,
    run_id: RunId,
    *,
    message: dict[str, Any] | str,
    queue: QueueKind = QueueKind.STEER,
) -> str:
    """Put a message into a Run that is still executing.

    DESIGN.md §9's three queues, finally reachable from outside a test: steer
    the turn running right now, follow up once it settles, or hand a message
    to whatever Run comes next -- see ``psych_runtime.runtime.dispatch.send`` for what
    each one is for and when each is refused. For "a second chat message
    after the first has already settled", this is not what you want; that is
    ``dispatch(continues=run_id, ...)``, which starts a fresh Run instead of
    trying to inject into one that is no longer there to receive it.

    Returns:
        The queue entry's id.
    """
    return await _send(store, run_id, message=message, queue=queue)


async def interrupt(
    store: Store, run_id: RunId, *, reason: str = "", by: str | None = None
) -> None:
    """Stop a Run.

    An abort is a Record, not a flag. A message sent immediately afterwards
    lands in the next-Run queue and both are visible in the log in order, which
    is what makes "stop and send another" a modelled transition rather than a
    race (DESIGN.md §9).
    """
    await _interrupt(store, run_id, reason=reason, by=by)


async def state(store: Store, run_id: RunId, *, scope: Scope | None = None) -> RunStateView:
    """Fold a Run's log into its current state.

    Cheaper than a report and enough for "is it done, and what is it waiting
    on". ``psych_runtime.status`` is the same answer shaped for a UI; the report is
    the full projection.

    Args:
        scope: whose read this is. Passing it refuses a Run belonging to
            another tenant -- see ``psych_runtime.records``.

    Raises:
        RunNotFound: no such Run.
        AccessDenied: ``scope`` names a different tenant than the Run's.
        CorruptLog: the log could not have been produced by the protocol.
    """
    records = await store.read(run_id)
    if not records:
        raise RunNotFound(run_id)
    _check_scope(records[0].scope, scope, run_id)
    return reduce(records, run_id=run_id)


async def status(store: Store, run_id: RunId, *, scope: Scope | None = None) -> RunStatus:
    """What a Run is doing right now, shaped for a person to be shown.

    ``psych_runtime.state`` returns the reducer's own working dataclass, which carries
    bookkeeping that exists to make the *next* fold cheap (``open_tool_calls``,
    ``repeat_counts``, ``approval_decisions``) and is not JSON. Every consumer
    with a UI therefore wrote a projection of it, and the one in this
    repository's own example had to decide field by field what a status even
    is. This is that projection, as a frozen Pydantic model that serialises
    directly: the lifecycle, what it is waiting on and the exact call awaiting
    approval, the turn and step counts against their budgets, and the head
    sequence a stream should reconnect from.
    """
    return status_of(await state(store, run_id, scope=scope))


async def answer(store: Store, run_id: RunId, *, scope: Scope | None = None) -> AnswerView:
    """One Run split into what it concluded and how it got there.

    The other way to read a Run. ``psych_runtime.thread()`` gives the conversation in
    order, which is what a transcript is; this gives the answer with the work
    behind it, which is what a person who asked a question wants. Both project
    the same log, so neither can show something the other denies.

    The split is derived rather than decided: the answer is the turn the loop
    itself finished on, and the work is everything before it (see
    ``psych_runtime.core.answer``). Nothing asks the model to classify its own
    question, which is a judgement it is bad at and does not need to make.

    Raises:
        RunNotFound: no such Run.
        AccessDenied: ``scope`` names a different tenant than the Run's.
    """
    header = await store.get_run(run_id)
    if header is None:
        raise RunNotFound(run_id)
    _check_scope(header.scope, scope, run_id)
    return split_answer(await store.read(run_id))


async def thread(
    store: Store, run_id: RunId, *, scope: Scope | None = None, limit: int | None = None
) -> ThreadView:
    """One whole conversation, across every Run in its chain.

    A second message starts a new Run (``psych_runtime.runtime.thread`` argues why), so
    one Run's messages are one exchange rather than the conversation a person
    sees. This walks ``continues_run_id`` back to the Run that opened the
    thread and projects each one, oldest first, with every message carrying its
    Run id, sequence and timestamp.

    Distinct from ``psych_runtime.runtime.thread.load_thread_history``, which answers a
    different question: what to replay *into the model*, bounded by
    ``Limits.max_history_records`` because replaying a long chat is the dominant
    cost of a chat agent. A person scrolling back expects their whole
    conversation, not the slice the model was shown.

    Args:
        scope: whose read this is. Every Run in the chain must share its
            tenant; the walk stops rather than crossing one (DESIGN.md §10.4
            calls a link that can silently cross a tenant boundary the mistake
            that ends the project, and a control enforced at write time but not
            at read time is worse than none).
        limit: at most this many Runs, counting back from ``run_id``. ``None``
            walks the whole chain.

    Raises:
        RunNotFound: no such Run.
        AccessDenied: ``scope``, or an ancestor's own Scope, names a different
            tenant.
    """
    header = await store.get_run(run_id)
    if header is None:
        raise RunNotFound(run_id)
    _check_scope(header.scope, scope, run_id)

    chain: list[RunId] = []
    seen: set[RunId] = set()
    current: RunId | None = run_id
    while current is not None and current not in seen:
        seen.add(current)
        ancestor = await store.get_run(current)
        if ancestor is None:
            # A link naming no Run: the consumer's own retention policy reached
            # it. Render what survives rather than failing the whole thread,
            # which is what load_thread_history does with the same broken chain.
            break
        if ancestor.scope.tenant != header.scope.tenant:
            raise AccessDenied(
                f"run {current}",
                f"belongs to tenant {ancestor.scope.tenant!r}, not "
                f"{header.scope.tenant!r}. A thread never crosses a tenant.",
            )
        chain.append(current)
        if limit is not None and len(chain) >= limit:
            break
        current = ancestor.continues_run_id
    chain.reverse()

    messages: list[MessageView] = []
    for ancestor_id in chain:
        messages.extend(message_views(await store.read(ancestor_id)))
    return ThreadView(run_ids=tuple(chain), messages=tuple(messages))


def _check_scope(owner: Scope, asked: Scope | None, run_id: RunId) -> None:
    """Refuse a read whose Scope names a different tenant than the Run's.

    DESIGN.md §14 says every entry point takes a Scope and every store query is
    filtered by it, and these read paths took a bare ``run_id``. The parameter
    is optional so existing callers keep working while they add it, and a
    consumer serving end users should always pass it: without it, one leaked
    run id -- a log line, a URL -- is a whole conversation.
    """
    if asked is not None and owner.tenant != asked.tenant:
        raise AccessDenied(
            f"run {run_id}",
            f"belongs to tenant {owner.tenant!r}, not {asked.tenant!r}",
        )


async def records(
    store: Store,
    run_id: RunId,
    *,
    after: int = 0,
    limit: int | None = None,
    scope: Scope | None = None,
) -> Sequence[Record]:
    """The raw log. For a consumer building their own projection.

    Args:
        scope: whose read this is. Passing it refuses a Run belonging to
            another tenant; omitting it reads any Run by id, which is right for
            an operator's own tooling and wrong for anything serving end users.
    """
    log = await store.read(run_id, after=after, limit=limit)
    if scope is not None and log:
        _check_scope(log[0].scope, scope, run_id)
    return log


async def report(
    store: Store, run_id: RunId, *, child_depth: int = 0, scope: Scope | None = None
) -> RunReport:
    """Everything a Run did, as a typed object.

    The Spec version and hash, the system prompt as sent, every step, tool call
    and model call in order, usage split by cache state, computed cost, the
    latency breakdown, every suspension and resume, and the terminal state.

    This ships with Psych rather than being left to consumers because it is the
    most visible thing they get on day one, and if every consumer writes their
    own projection they will each get the token arithmetic wrong in a different
    way (DESIGN.md §13.4).

    Args:
        store: where the log lives.
        run_id: the Run to report on.
        child_depth: how many levels of nested Run to include. 0 reports this Run
            only. Bounded rather than unlimited so a deep delegation tree cannot
            read the whole database by accident.
        scope: whose read this is. Passing it refuses a Run belonging to
            another tenant -- see ``psych_runtime.records``.
    """
    built = await build_report(store, run_id, child_depth=child_depth)
    _check_scope(built.scope, scope, run_id)
    return built


async def stream_text(
    store: Store, run_id: RunId, *, after: int = 0, scope: Scope | None = None
) -> AsyncIterator[str]:
    """Just the assistant's words, for a chat UI that only renders words.

    ``psych_runtime.stream()`` stays the complete truth and this is a projection
    over it, exactly as ``answer()`` is a projection over the same log. It ships
    for the reason ``answer()``, ``status()`` and ``thread()`` ship: every
    consumer writes this loop, and every one of them gets the same three things
    wrong the first time. Assistant text arrives on ``model_call_finished``
    rather than on a record named for text; an abort is a Record rather than an
    exception; and the iterator ends when the Run settles rather than when the
    model stops talking.

    **It refuses to swallow what it does not understand.** A Run that aborts
    raises, a Run that fails raises, and a Run that settles any way other than
    ``COMPLETED`` raises. Yielding nothing and returning cleanly would be the
    bug this exists to prevent: a UI showing a blank reply and no error, for a
    Run whose log says exactly what went wrong. Someone who wanted only the
    words gets the words; a Run that goes wrong stays impossible to miss.

    ```python
    async for delta in psych_runtime.stream_text(store, run_id, scope=scope):
        yield f"data: {delta}\n\n"
    ```

    Args:
        after: the highest sequence already seen, for a reconnect. Text before
            it is not replayed, which is what a client resuming a rendered
            stream wants; a client that needs the whole reply from the start
            passes ``0`` or reads ``answer()`` instead.
        scope: whose read this is. Passing it refuses a Run belonging to
            another tenant, the same as every other read here.

    Raises:
        RunNotFound: no such Run.
        AccessDenied: ``scope`` names a different tenant than the Run's.
        RunAborted: the Run was interrupted or passed its deadline, carrying the
            terminal state so a caller can tell a user's stop from a timeout.
        RunFailed: the Run settled ``FAILED``, carrying the failure from the log.
    """
    async for record in stream(store, run_id, after=after, scope=scope):
        if isinstance(record, ModelCallFinished) and record.text:
            yield record.text
        elif isinstance(record, RunSettled):
            if record.state is TerminalState.COMPLETED:
                return
            if record.state is TerminalState.FAILED:
                raise RunFailed(run_id, record.failure)
            raise RunAborted(run_id, record.state)
