"""Building record logs, for tests and for consumers testing their own agents.

A log is tedious to build by hand: every record needs a run id, a gapless
sequence, a timestamp and a scope, and getting the sequence wrong makes a test
fail for a reason that has nothing to do with what it meant to check.

``LogBuilder`` handles the bookkeeping so a test reads as the sequence of events
it is actually about.

This is exported rather than kept in the test tree because a consumer writing
tests against their own agents needs exactly the same thing, and DESIGN.md §21
puts consumer-facing helpers here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Self

from psych_runtime.core.ids import AttemptId, RunId, StepId, ToolCallId, VersionHash, WorkerId
from psych_runtime.core.records import (
    AbortRequested,
    AttemptStarted,
    CompactionApplied,
    ModelCallFailed,
    ModelCallFinished,
    ModelCallStarted,
    ModelTimings,
    QueueCancelled,
    QueueConsumed,
    QueueEnqueued,
    QueueKind,
    Record,
    Resumed,
    RunAdmitted,
    RunSettled,
    StepCompleted,
    StepStarted,
    SubagentFinished,
    SubagentMessaged,
    SubagentSpawned,
    Suspended,
    SuspendReason,
    TerminalState,
    ToolCallFinished,
    ToolCallStarted,
    ToolFailure,
    ToolOutcome,
    TurnStarted,
)
from psych_runtime.core.scope import Scope
from psych_runtime.core.usage import Cost, Usage

__all__ = ["LogBuilder"]

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class LogBuilder:
    """Assemble a valid record log one event at a time.

    Sequence numbers, run id, scope and timestamps are filled in. Timestamps
    advance one second per record, which keeps them ordered and readable without
    a test having to care.

    Every method returns ``self``, so a log reads as a script:

    ```python
    log = (
        LogBuilder()
        .admitted()
        .attempt()
        .turn()
        .tool_started("call-1", "refund")
        .tool_finished("call-1")
        .settled()
        .records
    )
    ```
    """

    def __init__(
        self,
        run_id: str = "run_test",
        scope: Scope | None = None,
        version_hash: str = "sha256:test",
    ) -> None:
        self.run_id = RunId(run_id)
        self.scope = scope if scope is not None else Scope(tenant="acme")
        self.version_hash = VersionHash(version_hash)
        self.records: list[Record] = []
        self._attempts = 0
        self._turns = 0
        self._attempt_id: AttemptId | None = None

    # -- bookkeeping --------------------------------------------------------

    @property
    def _next_seq(self) -> int:
        return len(self.records) + 1

    def _at(self) -> datetime:
        return _EPOCH + timedelta(seconds=len(self.records))

    def _common(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self._next_seq,
            "at": self._at(),
            "scope": self.scope,
            "attempt_id": self._attempt_id,
        }

    def _add(self, record: Record) -> Self:
        self.records.append(record)
        return self

    # -- lifecycle ----------------------------------------------------------

    def admitted(self, **kwargs: Any) -> Self:
        return self._add(
            RunAdmitted(
                **self._common(),
                version_hash=kwargs.pop("version_hash", self.version_hash),
                deadline_at=kwargs.pop("deadline_at", _EPOCH + timedelta(hours=1)),
                **kwargs,
            )
        )

    def attempt(self, *, reclaimed: bool = False) -> Self:
        self._attempts += 1
        self._attempt_id = AttemptId(f"att_{self._attempts}")
        common = self._common()
        common["attempt_id"] = self._attempt_id
        return self._add(
            AttemptStarted(
                **common,
                worker_id=WorkerId(f"wrk_{self._attempts}"),
                attempt_number=self._attempts,
                reclaimed_expired_lease=reclaimed,
            )
        )

    def settled(self, state: TerminalState = TerminalState.COMPLETED, **kwargs: Any) -> Self:
        return self._add(RunSettled(**self._common(), state=state, **kwargs))

    # -- turns and model calls ---------------------------------------------

    def turn(self) -> Self:
        self._turns += 1
        return self._add(TurnStarted(**self._common(), turn=self._turns))

    def model_started(self, model: str = "gpt-4o", **kwargs: Any) -> Self:
        return self._add(
            ModelCallStarted(**self._common(), turn=max(self._turns, 1), model=model, **kwargs)
        )

    def model_finished(
        self,
        model: str = "gpt-4o",
        usage: Usage | None = None,
        cost: Cost | None = None,
        **kwargs: Any,
    ) -> Self:
        return self._add(
            ModelCallFinished(
                **self._common(),
                turn=max(self._turns, 1),
                model=model,
                usage=usage if usage is not None else Usage(input=10, output=5),
                cost=cost,
                timings=kwargs.pop("timings", ModelTimings()),
                finish_reason=kwargs.pop("finish_reason", "stop"),
                **kwargs,
            )
        )

    def model_failed(self, model: str = "gpt-4o", **kwargs: Any) -> Self:
        return self._add(
            ModelCallFailed(
                **self._common(),
                turn=max(self._turns, 1),
                model=model,
                failure=kwargs.pop("failure", ToolFailure(kind="http_500", message="upstream")),
                **kwargs,
            )
        )

    # -- tools --------------------------------------------------------------

    def tool_started(self, call_id: str, tool: str = "refund", **kwargs: Any) -> Self:
        return self._add(
            ToolCallStarted(
                **self._common(),
                call_id=ToolCallId(call_id),
                tool=tool,
                turn=max(self._turns, 1),
                **kwargs,
            )
        )

    def tool_finished(
        self, call_id: str, outcome: ToolOutcome = ToolOutcome.OK, **kwargs: Any
    ) -> Self:
        if outcome is ToolOutcome.ERROR and "failure" not in kwargs:
            kwargs["failure"] = ToolFailure(kind="tool_error", message="it did not work")
        return self._add(
            ToolCallFinished(
                **self._common(), call_id=ToolCallId(call_id), outcome=outcome, **kwargs
            )
        )

    # -- steps --------------------------------------------------------------

    def step_started(
        self, step_id: str, name: str = "step", kind: str = "tool", attempt_number: int = 1
    ) -> Self:
        return self._add(
            StepStarted(
                **self._common(),
                step_id=StepId(step_id),
                name=name,
                kind=kind,  # type: ignore[arg-type]
                attempt_number=attempt_number,
            )
        )

    def step_completed(self, step_id: str, **kwargs: Any) -> Self:
        return self._add(StepCompleted(**self._common(), step_id=StepId(step_id), **kwargs))

    # -- composed subagents -------------------------------------------------

    def spawned(self, child_run_id: str, name: str = "researcher", **kwargs: Any) -> Self:
        """A background subagent this Run composed and started (DESIGN.md §17)."""
        return self._add(
            SubagentSpawned(
                **self._common(),
                child_run_id=RunId(child_run_id),
                name=name,
                call_id=ToolCallId(kwargs.pop("call_id", "call-spawn")),
                child_version_hash=VersionHash(kwargs.pop("child_version_hash", "sha256:child")),
                purpose=kwargs.pop("purpose", "Research supplier pricing for the quote."),
                task=kwargs.pop("task", "Find the list price of part 88-B from every supplier."),
                deliverable=kwargs.pop("deliverable", "A list of supplier and price."),
                model=kwargs.pop("model", "gpt-4o"),
                delegation_depth=kwargs.pop("delegation_depth", 1),
                **kwargs,
            )
        )

    def messaged_child(self, child_run_id: str, name: str = "researcher", **kwargs: Any) -> Self:
        return self._add(
            SubagentMessaged(
                **self._common(),
                child_run_id=RunId(child_run_id),
                name=name,
                call_id=ToolCallId(kwargs.pop("call_id", "call-message")),
                entry_id=kwargs.pop("entry_id", "entry-1"),
                message=kwargs.pop("message", "Only suppliers we have a contract with."),
                **kwargs,
            )
        )

    def child_finished(self, child_run_id: str, name: str = "researcher", **kwargs: Any) -> Self:
        return self._add(
            SubagentFinished(
                **self._common(),
                child_run_id=RunId(child_run_id),
                name=name,
                state=kwargs.pop("state", TerminalState.COMPLETED),
                **kwargs,
            )
        )

    # -- interrupts and queues ---------------------------------------------

    def abort(self, reason: str = "user stopped it") -> Self:
        return self._add(AbortRequested(**self._common(), reason=reason))

    def enqueued(self, entry_id: str, queue: QueueKind = QueueKind.STEER, **kwargs: Any) -> Self:
        return self._add(QueueEnqueued(**self._common(), entry_id=entry_id, queue=queue, **kwargs))

    def cancelled(self, entry_id: str) -> Self:
        return self._add(QueueCancelled(**self._common(), entry_id=entry_id))

    def consumed(self, entry_id: str) -> Self:
        return self._add(QueueConsumed(**self._common(), entry_id=entry_id))

    # -- suspension ---------------------------------------------------------

    def suspended(self, reason: SuspendReason = SuspendReason.APPROVAL, **kwargs: Any) -> Self:
        return self._add(
            Suspended(
                **self._common(),
                reason=reason,
                expires_at=kwargs.pop("expires_at", _EPOCH + timedelta(days=1)),
                **kwargs,
            )
        )

    def resumed(self, **kwargs: Any) -> Self:
        return self._add(Resumed(**self._common(), **kwargs))

    def compacted(self, from_seq: int, to_seq: int, **kwargs: Any) -> Self:
        return self._add(
            CompactionApplied(
                **self._common(),
                reason=kwargs.pop("reason", "threshold"),
                replaced_from_seq=from_seq,
                replaced_to_seq=to_seq,
                summary=kwargs.pop("summary", "the conversation so far"),
                **kwargs,
            )
        )
