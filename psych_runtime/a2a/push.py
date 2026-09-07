"""Push notifications: what to send, how to authenticate it, when to retry.

§4.3 and §13.2. A push notification is the third way a client learns a task
moved (§3.5.1), and the only one that works when the client cannot hold a
connection open. The body is the same ``StreamResponse`` an SSE frame carries
(§4.3.3), which is deliberate on the protocol's part and worth preserving on
ours: a client that can parse the stream can parse the webhook.

## What is here and what is not

Here: the payload, the headers, and the retry schedule -- all pure, all
testable without a socket.

Not here: the HTTP call. That belongs to the consumer, through the egress
seam every other outbound call in Psych goes through (DESIGN.md §14), and
that is also where §13.2's SSRF requirement is satisfied. §13.2 asks agents to
"validate webhook URLs to prevent SSRF ... Reject private IP ranges ... Reject
localhost and link-local addresses", and Psych already has exactly one place
where an outbound URL is approved or refused: ``EgressPolicy``. Writing a
second URL check into this module would produce two controls that disagree,
and the one a consumer configured would be the one that did not run. So the
rule is: a push sender calls through ``HttpTransport``, and a deployment that
wants §13.2's restriction implements it in its policy, where it also covers
model calls, HTTP tools and MCP.

``examples/playground/backend/app/a2a/push.py`` is that sender, and it is
about thirty lines because everything protocol-shaped is here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from pydantic import JsonValue

from psych_runtime.a2a.models import (
    A2A_MEDIA_TYPE,
    StreamResponse,
    TaskPushNotificationConfig,
    wire_dict,
)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "NOTIFICATION_TOKEN_HEADER",
    "backoff_delays",
    "push_body",
    "push_headers",
]

NOTIFICATION_TOKEN_HEADER: Final = "X-A2A-Notification-Token"
"""Where ``TaskPushNotificationConfig.token`` travels.

§4.3.1 defines the token ("A token unique for this task or session") and
§13.2 tells receivers to validate notification authenticity, but §4.3.3's
request format shows only ``Authorization`` and ``Content-Type``, so the
specification does not name a transport for it. This header is the convention
A2A 0.3 established and implementations still expect; it is a named constant
rather than a literal so a deployment whose peer wants it elsewhere changes
one line.
"""

DEFAULT_TIMEOUT_SECONDS: Final = 15.0
"""§13.2: "Agents SHOULD implement reasonable timeout values for webhook
requests (recommended: 10-30 seconds)". The middle of the range."""

DEFAULT_MAX_ATTEMPTS: Final = 5
"""§13.2 permits giving up "after a configured number of consecutive
failures". Five attempts over the schedule below is about half a minute of
trying, which outlasts a restart without holding a Run's worth of state open
for an endpoint that has gone away."""


def push_body(event: StreamResponse) -> dict[str, JsonValue]:
    """The webhook body for one event (§4.3.3).

    Identical to the SSE frame's payload, by construction rather than by
    coincidence: both call ``wire_dict`` on the same ``StreamResponse``.
    """
    return wire_dict(event)


def push_headers(config: TaskPushNotificationConfig) -> dict[str, str]:
    """The headers one notification goes out with (§4.3.3, §13.2).

    §13.2: "Agents MUST include authentication credentials in webhook requests
    as specified in ``PushNotificationConfig.authentication``". The scheme and
    credentials are joined the way HTTP authentication always joins them, and
    a scheme with no credentials (mutual TLS, say, where the credential is the
    connection) sends no ``Authorization`` header rather than an empty one,
    which some servers reject outright.
    """
    headers = {"Content-Type": A2A_MEDIA_TYPE}
    auth = config.authentication
    if auth is not None and auth.credentials:
        headers["Authorization"] = f"{auth.scheme} {auth.credentials}"
    if config.token:
        headers[NOTIFICATION_TOKEN_HEADER] = config.token
    return headers


def backoff_delays(
    *,
    attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_seconds: float = 1.0,
    cap_seconds: float = 30.0,
) -> Iterator[float]:
    """How long to wait between delivery attempts (§13.2).

    Exponential, capped, and deterministic. No jitter, and that is a decision
    rather than an oversight: jitter matters when many senders retry the same
    failing endpoint at once, and a push sender retries *one* endpoint for
    *one* task. Determinism is worth more here, because it is what lets the
    schedule be asserted in a test instead of tolerated in one.

    Yields ``attempts - 1`` delays: the first attempt is immediate.
    """
    if attempts < 1:
        raise ValueError("a delivery needs at least one attempt")
    delay = base_seconds
    for _ in range(attempts - 1):
        yield min(delay, cap_seconds)
        delay *= 2
