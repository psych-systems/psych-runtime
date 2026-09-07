"""The error hierarchy.

One root so a consumer can catch everything Psych raises with a single except,
and typed leaves so they can catch exactly what they mean to handle.

Corruption errors live in ``psych_runtime.core.corruption`` because there are eleven of
them and they share behaviour the rest of these do not.
"""

from __future__ import annotations

__all__ = [
    "AccessDenied",
    "DeadlineExceeded",
    "LeaseLost",
    "PsychError",
    "RunAborted",
    "RunAlreadySettled",
    "RunFailed",
    "RunNotFound",
    "RunNotSuspended",
    "SeqConflict",
    "SpecValidationError",
    "StoreError",
    "SuspensionExpired",
    "TransientError",
    "ValidationIssue",
    "VersionNotFound",
]


class PsychError(Exception):
    """Root of everything Psych raises."""


# ---------------------------------------------------------------------------
# Publish-time validation
# ---------------------------------------------------------------------------


class ValidationIssue:
    """One problem with a Spec, naming exactly what is wrong and where.

    Not an exception: a publish reports every issue at once rather than making
    the author fix one typo per attempt.

    Attributes:
        path: where in the Spec the problem is, dotted from the root.
        message: what is wrong, in a sentence an author can act on.
    """

    __slots__ = ("message", "path")

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"

    def __repr__(self) -> str:
        return f"ValidationIssue(path={self.path!r}, message={self.message!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValidationIssue):
            return NotImplemented
        return (self.path, self.message) == (other.path, other.message)

    def __hash__(self) -> int:
        return hash((self.path, self.message))


class SpecValidationError(PsychError):
    """A Spec was refused at publish.

    DESIGN.md §4: validation happens at publish, never at run. A customer
    waiting on a response is not the right place to discover a typo, so this is
    raised by ``publish()`` and never by the agent loop.
    """

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        listed = "\n  ".join(str(issue) for issue in issues)
        count = len(issues)
        noun = "problem" if count == 1 else "problems"
        super().__init__(f"the Spec has {count} {noun}:\n  {listed}")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class StoreError(PsychError):
    """Something went wrong at the Store port."""


class SeqConflict(StoreError):
    """Appending Record ``seq`` found one already there.

    The single-writer rule (DESIGN.md §6 rule 3) says exactly one Attempt holds
    the lease and may append, so this means another writer exists. The losing
    Attempt aborts immediately rather than retrying at the next sequence, which
    would interleave two Attempts' records into one log.
    """

    def __init__(self, run_id: str, seq: int) -> None:
        self.run_id = run_id
        self.seq = seq
        super().__init__(
            f"record {seq} of run {run_id} already exists. Another Attempt holds "
            "the lease and is writing; this Attempt must abort rather than retry."
        )


class RunNotFound(StoreError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"no run {run_id}")


class VersionNotFound(StoreError):
    def __init__(self, version_hash: str) -> None:
        self.version_hash = version_hash
        super().__init__(f"no published Version with hash {version_hash}")


class RunEndedWithoutAnswer(PsychError):
    """A Run a caller was reading ended without producing one.

    Raised only by ``psych_runtime.stream_text()``, which is a projection that
    yields words and therefore has no way to *show* an ending that produced
    none. Returning quietly would leave a UI with a blank reply and no error for
    a Run whose log says exactly what went wrong, so this is the one place a
    projection raises rather than renders.

    Every other read path returns the ending as data instead, because they can:
    ``status()`` names the lifecycle, ``answer()`` sets ``finished`` to False,
    and ``report()`` carries the terminal state and the failure.
    """


class RunAborted(RunEndedWithoutAnswer):
    """The Run stopped before answering: interrupted, or past its deadline.

    ``state`` distinguishes them, which a caller usually needs: a user pressing
    stop is not an incident and a deadline is.
    """

    def __init__(self, run_id: str, state: str) -> None:
        self.run_id = run_id
        self.state = state
        super().__init__(f"run {run_id} ended {state} without producing an answer")


class RunFailed(RunEndedWithoutAnswer):
    """The Run settled FAILED.

    ``failure`` is the one recorded in the log, so a caller reports what
    happened rather than a generic message about streaming.
    """

    def __init__(self, run_id: str, failure: object | None = None) -> None:
        self.run_id = run_id
        self.failure = failure
        detail = f": {failure}" if failure is not None else ""
        super().__init__(f"run {run_id} failed{detail}")


class RunNotSuspended(PsychError):
    """``resume()`` was called on a Run that is not waiting for anything.

    Typed rather than a bare ``ValueError`` so an HTTP layer can map it to a
    status without matching on message text.
    """

    def __init__(self, run_id: str, state: str) -> None:
        self.run_id = run_id
        self.state = state
        super().__init__(
            f"run {run_id} is not suspended, so there is nothing to resume. Its state is {state}."
        )


class RunAlreadySettled(PsychError):
    """A Run that has reached its terminal record was asked to do more.

    Raised by ``send()``; ``interrupt()`` on a settled Run is a no-op by
    contract, since the first abort is the one that counts.
    """

    def __init__(self, run_id: str, what: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"run {run_id} is already settled; {what}. Continue the conversation with "
            "dispatch(continues=run_id, ...) instead."
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class LeaseLost(PsychError):
    """This Attempt no longer holds the lease it was working under.

    Raised when a renewal is refused because another Worker reclaimed an expired
    lease. The Attempt stops immediately: anything it writes from here would be
    a second writer on a log that permits one.
    """

    def __init__(self, run_id: str, worker_id: str) -> None:
        self.run_id = run_id
        self.worker_id = worker_id
        super().__init__(
            f"worker {worker_id} lost the lease on run {run_id}; another worker "
            "reclaimed it, so this attempt must stop writing"
        )


class DeadlineExceeded(PsychError):
    """A Run passed the deadline every Run has (DESIGN.md §8.4)."""

    def __init__(self, run_id: str, deadline_seconds: float) -> None:
        self.run_id = run_id
        self.deadline_seconds = deadline_seconds
        super().__init__(f"run {run_id} exceeded its {deadline_seconds}s deadline")


class SuspensionExpired(PsychError):
    """A suspended Run waited past its expiry and is settled as abandoned.

    DESIGN.md §11: suspensions expire rather than waiting forever on a user who
    left.
    """

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"run {run_id} was suspended for {reason} and waited past its expiry")


class AccessDenied(PsychError):
    """The consumer's Policy port refused, or narrowing removed the tool.

    Carries which, because "you may not" and "that no longer exists for you" are
    different problems for whoever is reading the log.
    """

    def __init__(self, subject: str, reason: str) -> None:
        self.subject = subject
        self.reason = reason
        super().__init__(f"access to {subject} denied: {reason}")


class TransientError(PsychError):
    """A failure the classifier judged worth retrying (DESIGN.md §8.6).

    Retries draw on a per-Run budget rather than a per-call one, so a Run cannot
    retry forever by spreading failures across steps.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)
