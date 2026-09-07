"""PKCE, canonical resource URIs, and scope-set arithmetic.

Pure functions and small immutable value types, with no IO: everything here
is unit-testable without a network and without a clock (the one place that
looks like it needs a clock, expiry maths, takes ``now`` as an argument
instead of calling one).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from psych_runtime.tools.oauth.errors import InvalidCanonicalUri

__all__ = [
    "PkcePair",
    "canonicalize_resource_uri",
    "generate_pkce_pair",
    "generate_state",
    "s256_challenge",
    "union_scopes",
]

_VERIFIER_BYTES = 64
"""64 random bytes, base64url-encoded without padding, gives a 86-character
verifier: within RFC 7636 §4.1's 43-128 character bound and as much entropy
as the encoding can carry without hitting the upper end pointlessly."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def s256_challenge(verifier: str) -> str:
    """The ``S256`` code challenge for a code verifier: ``BASE64URL-ENCODE
    (SHA256(ASCII(verifier)))``, per RFC 7636 §4.2. Exposed on its own so a
    test can recompute the expected challenge from a known verifier without
    going through ``generate_pkce_pair``'s randomness.
    """
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True, slots=True)
class PkcePair:
    """One PKCE verifier/challenge pair for one authorization request.

    ``method`` is always ``"S256"``: OAuth 2.1's security considerations
    require it "when technically capable", and a client that can compute
    SHA-256 always is, so there is no code path in this package that
    produces ``"plain"``.
    """

    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce_pair() -> PkcePair:
    """A fresh, unpredictable verifier/challenge pair for one authorization
    request. Never reused across requests: reuse would let one leaked
    verifier redeem a code from an unrelated authorization."""
    verifier = _b64url(secrets.token_bytes(_VERIFIER_BYTES))
    return PkcePair(verifier=verifier, challenge=s256_challenge(verifier))


def generate_state() -> str:
    """An unpredictable ``state`` value for one authorization request, used
    to bind the callback back to the request that started it and to defend
    against CSRF-style callback forgery (OAuth 2.1 §7.12)."""
    return _b64url(secrets.token_bytes(32))


def canonicalize_resource_uri(uri: str) -> str:
    """The RFC 8707 canonical URI for an MCP server, for the ``resource``
    parameter sent on every authorization and token request.

    A canonical URI has a scheme, has no fragment, and (per the MCP
    authorization spec's guidance for interoperability) uses a lowercase
    scheme and host with no trailing slash on the path, even though a
    trailing slash is technically a distinct, equally valid URI under
    RFC 3986. Input is accepted with any case in the scheme or host -- the
    spec asks implementations to tolerate that -- and normalised on the way
    out.

    Raises:
        InvalidCanonicalUri: ``uri`` has no scheme, has a fragment, or has no
            host.
    """
    parsed = urlsplit(uri)
    if not parsed.scheme:
        raise InvalidCanonicalUri(uri, "missing a scheme")
    if parsed.fragment:
        raise InvalidCanonicalUri(uri, "contains a fragment")
    if not parsed.hostname:
        raise InvalidCanonicalUri(uri, "missing a host")

    scheme = parsed.scheme.lower()
    netloc = parsed.hostname.lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parsed.path
    if path == "/":
        path = ""
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def union_scopes(previous: str | None, challenged: str | None) -> str | None:
    """The step-up scope set: previously-requested scopes union the scopes a
    challenge just demanded, space-separated per RFC 6749 §3.3, sorted so the
    result is deterministic regardless of which set contributed a name.

    ``None`` in and ``None`` out both mean "no scope restriction expressed",
    which is different from the empty string; an empty result (both inputs
    empty or absent) is also returned as ``None`` for the same reason.
    """
    combined = set((previous or "").split()) | set((challenged or "").split())
    if not combined:
        return None
    return " ".join(sorted(combined))
