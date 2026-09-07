"""Deciding what is worth retrying.

DESIGN.md §8.6. 5xx, 429, 408, connection errors and idle timeouts are transient.
400, 401, 403 and schema errors are not. Retrying the second group burns budget
and never succeeds, and worse, it delays the error the caller needs to see.

## The budget is per Run, not per call

Bounding retries per call lets a Run retry forever by spreading failures across
steps: ten calls with three retries each is thirty retries, and nothing notices.
The budget lives on the Run and is derived from the log, so it survives a crash
and a reclaim without a Worker having to carry a counter.
"""

from __future__ import annotations

import asyncio
import errno
import socket
from typing import Final

from psych_runtime.core.errors import TransientError

__all__ = [
    "RETRYABLE_STATUS_CODES",
    "classify_status",
    "is_context_overflow",
    "is_transient",
    "retry_delay_seconds",
]

RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {
        408,  # request timeout
        409,  # conflict: some proxies use it for a transient lock
        425,  # too early
        429,  # rate limited
        500,
        502,
        503,
        504,
        529,  # overloaded by some providers
    }
)
"""Everything else is permanent as far as retrying goes.

403 is deliberately absent even though it is sometimes transient behind a proxy
whose credentials are being rotated. Treating it as retryable means a genuinely
revoked key produces a Run that spends its whole budget before reporting the
problem, and "your key is invalid" arriving eight retries late is worse than
arriving immediately.
"""

_NON_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 413, 422})


def classify_status(status_code: int) -> bool:
    """Whether an HTTP status is worth retrying.

    Anything not explicitly listed either way is judged by class: 5xx retries,
    4xx does not. A provider inventing a new 5xx is having an outage; a provider
    inventing a new 4xx is telling us we asked wrongly, and asking again the same
    way will not help.
    """
    if status_code in RETRYABLE_STATUS_CODES:
        return True
    if status_code in _NON_RETRYABLE_STATUS:
        return False
    return status_code >= 500


_TRANSIENT_OS_ERRORS: Final[frozenset[int]] = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETRESET,
        errno.EAGAIN,
    }
)


_ALWAYS_TRANSIENT: Final[tuple[type[BaseException], ...]] = (
    TransientError,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    socket.gaierror,
)


def is_transient(error: BaseException) -> bool:
    """Whether ``error`` is worth another attempt.

    Handles the cases an adapter cannot always turn into a status code: a socket
    that was reset, a DNS failure, a read that timed out, and our own
    ``TransientError`` raised by the idle-timeout guard.

    ``asyncio.CancelledError`` is never transient. A cancelled call was cancelled
    on purpose, by an abort or a deadline, and retrying it would restart work the
    Run was explicitly told to stop.
    """
    if isinstance(error, asyncio.CancelledError):
        # A cancelled call was cancelled on purpose, by an abort or a deadline.
        # Retrying it would restart work the Run was explicitly told to stop.
        return False

    # socket.gaierror is here because name resolution failing is usually a blip.
    # A genuinely wrong hostname fails the same way every time, and the Run's
    # transient budget is what bounds that rather than this classification.
    if isinstance(error, _ALWAYS_TRANSIENT):
        return True

    if isinstance(error, OSError) and error.errno in _TRANSIENT_OS_ERRORS:
        return True

    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return classify_status(status)

    # An unrecognised error is not retried. Guessing "probably transient" turns
    # every bug in an adapter into a silent retry storm.
    return False


_CONTEXT_OVERFLOW_STATUS: Final[frozenset[int]] = frozenset({400, 413, 422})
"""Statuses a provider uses to say the request itself was too big. All three
are in ``_NON_RETRYABLE_STATUS`` above, which is the point: the same request
will be refused again, and the only useful answer is to send a shorter one."""

_CONTEXT_OVERFLOW_MARKERS: Final[tuple[str, ...]] = (
    "context length",
    "context window",
    "context_length_exceeded",
    "maximum context",
    "prompt is too long",
    "reduce the length of the messages",
    "too many input tokens",
    "too many tokens",
)
"""Substrings, matched case-insensitively against the provider's own message.

Substring matching is not a good way to classify an error and is used here
because there is no better one: no provider in the documented default
deployment returns a machine-readable code for this, they return a 400 whose
body says the prompt was too long in their own words. The list is deliberately
narrow and errs toward saying no, because a false negative leaves the Run
failing exactly as it does today while a false positive would compact a
conversation over an unrelated 400 and hide the real error behind a summary.
"""


def is_context_overflow(error: BaseException) -> bool:
    """Whether ``error`` is a provider refusing a prompt for being too long.

    A third answer beside transient and permanent, and it needs to be: retrying
    is pointless because the request has not changed, and giving up is a waste
    because the conversation can be made shorter and sent again. The agent loop
    uses it to compact instead of settling (``psych_runtime.runtime.compaction``).

    Never true for a transient error: a 429 or a 503 whose body happens to
    mention a context window is an outage, not a request that was too big.
    """
    if is_transient(error):
        return False
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and status not in _CONTEXT_OVERFLOW_STATUS:
        return False
    message = str(error).lower()
    return any(marker in message for marker in _CONTEXT_OVERFLOW_MARKERS)


_BASE_DELAY: Final = 0.5
_MAX_DELAY: Final = 30.0


def retry_delay_seconds(attempt: int, retry_after: float | None = None) -> float:
    """How long to wait before retry ``attempt`` (1-based).

    Exponential with a cap. ``retry_after`` from the provider wins when present,
    because a 429 that says how long to wait is telling the truth and guessing
    shorter just wastes another request.

    There is no jitter here and that is intentional: jitter is randomness, and
    this function is called from paths that are tested for determinism. A caller
    that wants jitter adds it at the call site where the randomness is visible.
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, _MAX_DELAY)
    growth = float(2 ** max(attempt - 1, 0))
    return min(_BASE_DELAY * growth, _MAX_DELAY)
