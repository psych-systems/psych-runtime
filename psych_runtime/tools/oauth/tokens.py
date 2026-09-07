"""``TokenSet``: one grant's access token, its refresh token if it has one,
and the expiry maths ``OAuthClient`` uses to decide when to refresh.

Pure and clock-free by construction: every method here takes ``now`` rather
than reading a clock itself, so the expiry-margin arithmetic is exercised in
``tests/unit/test_oauth.py`` without ``time.monotonic()`` or ``freezegun``
anywhere in sight.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr

__all__ = ["TokenSet"]


class TokenSet(BaseModel):
    """One token response, plus when it was obtained.

    ``obtained_at`` is always a ``time.monotonic()`` reading, never
    wall-clock: the same reasoning ``psych_runtime.tools.mcp``'s catalogue cache
    uses, so a token's freshness cannot jump because the system clock was
    adjusted. Pydantic's ``SecretStr`` keeps both token fields out of this
    model's default repr and str, which matters because this is exactly the
    object most likely to end up in a log line or a debugger's repr of
    in-flight state.
    """

    model_config = ConfigDict(frozen=True)

    access_token: SecretStr
    token_type: str = "Bearer"
    refresh_token: SecretStr | None = None
    scope: str | None = None
    obtained_at: float
    expires_in: float | None = None
    """Seconds, as reported by the token endpoint. ``None`` means the server
    did not say -- treated as "does not expire" for refresh purposes, not as
    "expires immediately"; a token endpoint that omits ``expires_in`` is
    telling us nothing, not telling us to refresh on every call."""

    @property
    def expires_at(self) -> float | None:
        if self.expires_in is None:
            return None
        return self.obtained_at + self.expires_in

    def is_expiring(self, *, now: float, safety_margin_seconds: float) -> bool:
        """Whether this token should be refreshed before use.

        A token with no known expiry is never "expiring": there is nothing
        to race against, and refreshing on a schedule we invented would just
        add a token request the server never asked for.
        """
        expires_at = self.expires_at
        if expires_at is None:
            return False
        return now >= expires_at - safety_margin_seconds
