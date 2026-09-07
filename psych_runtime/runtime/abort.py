"""The abort signal a Worker hands an Attempt, and why it carries a reason.

DESIGN.md §8.4 and §9 name two ways an Attempt is told to stop from outside
its own log: the supervisor firing the deadline, and the user's abort record.
Two more exist in practice and were conflated with the first: the Worker
shutting down gracefully, and the Worker losing its lease to another one.

Treating every one of those as "the deadline fired" wrote a terminal record
reading "the run passed its deadline" over a Run that was merely on a Worker
being redeployed, and, worse, wrote a terminal record into a log another
Worker had just reclaimed. The right response differs per reason:

- ``DEADLINE``: unwind, then settle ``ABORTED`` (the supervisor force-settles
  if unwinding takes longer than the grace period).
- ``SHUTDOWN``: unwind and write nothing terminal. The lease is released as
  ``RUNNABLE`` and the next Worker continues from the log, which is exactly
  the crash-recovery path with the crash left out.
- ``LEASE_LOST``: stop writing immediately. Another Worker owns the log now,
  and anything appended from here is a second writer on a log that permits
  one (§6 rule 3).

The signal is an ``asyncio.Event`` so every existing ``AttemptRunner`` keeps
its shape; ``reason`` is what the Runtime reads once the event is set.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum

__all__ = ["AbortReason", "AbortSignal"]


class AbortReason(StrEnum):
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"
    LEASE_LOST = "lease_lost"


class AbortSignal(asyncio.Event):
    """An ``asyncio.Event`` that remembers why it fired.

    The first reason wins: a Worker that loses its lease while shutting down
    still needs the Attempt to stop *writing*, and a later, gentler reason
    must not talk it back into appending.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason: AbortReason | None = None

    def fire(self, reason: AbortReason) -> None:
        if self.reason is None:
            self.reason = reason
        self.set()

    @property
    def writes_allowed(self) -> bool:
        """Whether this Attempt may still append to the log.

        False once the lease is lost. Every other reason leaves the Attempt
        as the single writer until it unwinds, so it may (and should) record
        what it stopped doing.
        """
        return self.reason is not AbortReason.LEASE_LOST
