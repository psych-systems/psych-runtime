"""The transient classifier.

DESIGN.md §8.6: 5xx, 429, 408, connection errors and idle timeouts are worth
retrying; 400, 401, 403 and schema errors are not. This is one of the named
unit-test targets in DESIGN.md §22, and it is pure: no IO, no clock beyond what
the caller passes in, table-driven against real provider error shapes.
"""

from __future__ import annotations

import asyncio
import errno
import socket

import pytest

from psych_runtime.core.errors import TransientError
from psych_runtime.model.transient import (
    RETRYABLE_STATUS_CODES,
    classify_status,
    is_transient,
    retry_delay_seconds,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# classify_status: every status code branch
# ---------------------------------------------------------------------------


class TestClassifyStatus:
    @pytest.mark.parametrize(
        "status",
        sorted(RETRYABLE_STATUS_CODES),
        ids=lambda status: f"http_{status}",
    )
    def test_explicitly_retryable_codes_are_transient(self, status: int) -> None:
        assert classify_status(status) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_explicitly_non_retryable_codes_are_permanent(self, status: int) -> None:
        assert classify_status(status) is False

    @pytest.mark.parametrize("status", [501, 505, 599])
    def test_unlisted_5xx_falls_back_to_transient(self, status: int) -> None:
        """A provider inventing a new 5xx is having an outage, not asking us to
        stop, so class-based fallback treats it the same as the ones we know."""
        assert classify_status(status) is True

    @pytest.mark.parametrize("status", [402, 405, 406, 410, 415, 418, 451])
    def test_unlisted_4xx_falls_back_to_permanent(self, status: int) -> None:
        """A provider inventing a new 4xx is telling us we asked wrongly, and
        asking again the same way will not help."""
        assert classify_status(status) is False

    def test_403_is_deliberately_not_retryable(self) -> None:
        """A revoked key must be reported immediately, not after the Run has
        spent its retry budget pretending the credential might come back."""
        assert classify_status(403) is False
        assert 403 not in RETRYABLE_STATUS_CODES

    @pytest.mark.parametrize("status", [200, 201, 204, 301, 304])
    def test_success_and_redirect_codes_are_permanent(self, status: int) -> None:
        assert classify_status(status) is False


# ---------------------------------------------------------------------------
# is_transient: exception-shaped failures, real provider error shapes
# ---------------------------------------------------------------------------


class _HttpStatusError(Exception):
    """Shaped like the status-carrying exceptions real HTTP clients raise
    (httpx.HTTPStatusError, requests.HTTPError, aiohttp.ClientResponseError all
    expose a status code as an attribute), without importing any of them."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"http {status_code}")


class TestIsTransientExceptions:
    def test_cancelled_error_is_never_transient(self) -> None:
        """A cancelled call was cancelled on purpose, by an abort or a
        deadline; retrying it would restart work the Run was told to stop."""
        assert is_transient(asyncio.CancelledError()) is False

    def test_transient_error_is_always_transient(self) -> None:
        assert is_transient(TransientError("idle timeout")) is True

    def test_builtin_timeout_error_is_transient(self) -> None:
        assert is_transient(TimeoutError("timed out")) is True

    def test_socket_timeout_is_transient(self) -> None:
        assert is_transient(TimeoutError()) is True

    def test_connection_error_is_transient(self) -> None:
        assert is_transient(ConnectionError("connection reset")) is True
        assert is_transient(ConnectionResetError()) is True
        assert is_transient(ConnectionRefusedError()) is True
        assert is_transient(BrokenPipeError()) is True

    def test_dns_failure_is_transient(self) -> None:
        assert is_transient(socket.gaierror("Name or service not known")) is True

    @pytest.mark.parametrize(
        "code",
        [
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.ECONNABORTED,
            errno.EPIPE,
            errno.ETIMEDOUT,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ENETRESET,
            errno.EAGAIN,
        ],
    )
    def test_known_transient_os_errors(self, code: int) -> None:
        assert is_transient(OSError(code, "transient")) is True

    def test_unlisted_os_error_is_not_transient(self) -> None:
        assert is_transient(OSError(errno.ENOENT, "no such file or directory")) is False

    def test_status_carrying_exception_defers_to_classify_status(self) -> None:
        assert is_transient(_HttpStatusError(429)) is True
        assert is_transient(_HttpStatusError(500)) is True
        assert is_transient(_HttpStatusError(400)) is False
        assert is_transient(_HttpStatusError(403)) is False

    def test_unrecognised_error_is_not_transient(self) -> None:
        """Guessing "probably transient" on an unrecognised error turns every
        bug in an adapter into a silent retry storm."""
        assert is_transient(ValueError("some unrelated bug")) is False

    def test_plain_exception_with_no_status_is_not_transient(self) -> None:
        assert is_transient(RuntimeError("boom")) is False


# ---------------------------------------------------------------------------
# retry_delay_seconds
# ---------------------------------------------------------------------------


class TestRetryDelaySeconds:
    def test_exponential_backoff_without_retry_after(self) -> None:
        assert retry_delay_seconds(1) == pytest.approx(0.5)
        assert retry_delay_seconds(2) == pytest.approx(1.0)
        assert retry_delay_seconds(3) == pytest.approx(2.0)
        assert retry_delay_seconds(4) == pytest.approx(4.0)

    def test_zero_and_negative_attempt_treated_as_first(self) -> None:
        assert retry_delay_seconds(0) == pytest.approx(0.5)
        assert retry_delay_seconds(-5) == pytest.approx(0.5)

    def test_backoff_is_capped(self) -> None:
        assert retry_delay_seconds(10) == pytest.approx(30.0)
        assert retry_delay_seconds(100) == pytest.approx(30.0)

    def test_retry_after_wins_over_backoff(self) -> None:
        """A 429 that names its own wait is telling the truth; guessing
        shorter from the backoff table just wastes another request."""
        assert retry_delay_seconds(1, retry_after=5.0) == pytest.approx(5.0)
        assert retry_delay_seconds(10, retry_after=5.0) == pytest.approx(5.0)

    def test_retry_after_is_still_capped(self) -> None:
        assert retry_delay_seconds(1, retry_after=9999.0) == pytest.approx(30.0)

    def test_retry_after_zero_is_honoured(self) -> None:
        """Zero is a valid instruction to retry immediately, distinct from
        ``None`` meaning no instruction was given at all."""
        assert retry_delay_seconds(5, retry_after=0.0) == pytest.approx(0.0)

    def test_negative_retry_after_is_ignored(self) -> None:
        """A malformed or negative Retry-After falls back to backoff rather
        than being taken literally."""
        assert retry_delay_seconds(1, retry_after=-1.0) == pytest.approx(0.5)

    def test_retry_delay_is_deterministic(self) -> None:
        """No jitter: this function is called from paths tested for
        determinism, and a caller that wants jitter adds it at the call site."""
        assert retry_delay_seconds(3) == retry_delay_seconds(3)
