"""The OAuth error hierarchy.

Every leaf here is a ``psych_runtime.core.errors.PsychError``, so a consumer catching
that root still catches everything this package raises. None of these ever
carries a token, a refresh token, or a client secret in its message: several
docstrings below say exactly why, at the call site where the temptation to
interpolate one is greatest.
"""

from __future__ import annotations

from psych_runtime.core.errors import PsychError

__all__ = [
    "AuthorizationDenied",
    "ClientRegistrationError",
    "DiscoveryError",
    "InvalidCanonicalUri",
    "IssuerMismatch",
    "NoActiveSession",
    "OAuthError",
    "PkceRequired",
    "ReauthorizationRequired",
    "StepUpExhausted",
    "TokenRequestFailed",
    "UnsupportedGrant",
]


class OAuthError(PsychError):
    """Root of everything ``psych_runtime.tools.oauth`` raises."""


class InvalidCanonicalUri(OAuthError):
    """A resource URI is not a valid RFC 8707 canonical server URI.

    Raised by ``canonicalize_resource_uri`` for a URI missing a scheme,
    carrying a fragment, or otherwise unable to identify one MCP server
    unambiguously.
    """

    def __init__(self, uri: str, reason: str) -> None:
        self.uri = uri
        self.reason = reason
        super().__init__(f"{uri!r} is not a valid canonical resource URI: {reason}")


class DiscoveryError(OAuthError):
    """Protected Resource Metadata or authorization server metadata could not
    be obtained or did not validate.

    Covers: no reachable metadata document at any of the URLs a discovery
    step is required to try, a document that is not valid JSON, a document
    missing a field this client requires, and the RFC 8414 §3.3 /
    OpenID Connect Discovery §4.3 anti-mix-up check -- a document whose
    ``issuer`` field does not match the issuer identifier used to construct
    the well-known URL it was fetched from.
    """


class UnsupportedGrant(OAuthError):
    """A caller asked for something OAuth 2.1 does not offer.

    The one instance in this package: the authorization code grant without
    PKCE. PKCE is mandatory in OAuth 2.1 (there is no "authorization code,
    no PKCE" variant to fall back to -- that combination is OAuth 2.0, which
    this client does not implement), so a caller passing ``require_pkce=False``
    to ``OAuthClient.start`` gets this raised immediately, before any network
    call, rather than the request being quietly upgraded to use PKCE anyway
    or, worse, actually sent without it.
    """


class PkceRequired(OAuthError):
    """The authorization server does not advertise PKCE support.

    OAuth 2.1 makes PKCE mandatory (DESIGN.md-adjacent reasoning: there is no
    "without PKCE" variant of the authorization code grant to fall back to),
    and the MCP authorization spec's security considerations page requires a
    client to verify PKCE support from authorization server metadata before
    proceeding, refusing when ``code_challenge_methods_supported`` is absent
    or does not list ``S256``. This is raised instead of silently sending an
    authorization request the server would only reject less clearly, or
    proceeding without PKCE, which OAuth 2.1 does not permit at all.
    """


class ClientRegistrationError(OAuthError):
    """No client identity could be obtained for an authorization server.

    Raised when none of the three registration mechanisms (Client ID
    Metadata Document, pre-registration, Dynamic Client Registration) apply:
    no pre-registered client id was configured, the authorization server
    does not advertise ``client_id_metadata_document_supported`` (or none
    was configured), and either Dynamic Client Registration is disabled or
    the server has no ``registration_endpoint``. Also raised when Dynamic
    Client Registration is attempted and the server refuses it.
    """


class IssuerMismatch(OAuthError):
    """The RFC 9207 §2.4 validation table rejected an authorization response.

    Raised for a missing ``iss`` when the authorization server advertises
    ``authorization_response_iss_parameter_supported: true``, and for an
    ``iss`` present but not identical (by RFC 3986 §6.2.1 simple string
    comparison, no normalisation) to the issuer recorded when the
    authorization request was built.

    This is raised *before* the client ever looks at ``error``,
    ``error_description`` or ``error_uri`` on the callback, and this
    exception's own message never repeats them: the MCP authorization spec
    is explicit that on a mismatch a client must not act on or display those
    fields, and an exception message is a display surface.
    """


class AuthorizationDenied(OAuthError):
    """The authorization step failed for a reason short of an issuer
    mismatch: the user (or the authorization server) declined, the
    ``state`` on the callback did not match the one sent, or the
    ``AuthorizationRedirectPort`` implementation could not complete the
    round trip.
    """


class TokenRequestFailed(OAuthError):
    """The token endpoint returned an error response.

    Carries the OAuth ``error`` code from the response, which is safe to
    surface (it is a fixed enum member such as ``invalid_grant``, not a
    credential), but never the request or response body verbatim: a
    malformed client could echo a client secret or code verifier back in an
    error description, and this type has no way to know it is not looking at
    exactly that.
    """

    def __init__(self, error: str, description: str | None = None) -> None:
        self.error = error
        self.description = description
        message = f"token request failed: {error}"
        if description:
            message = f"{message} ({description})"
        super().__init__(message)


class NoActiveSession(OAuthError):
    """``bearer_token`` was called for a ``(scope, resource)`` that has never
    completed ``OAuthClient.start`` or ``OAuthClient.step_up`` successfully.

    Distinct from ``ReauthorizationRequired``: this means "never authorized",
    that means "was authorized, no longer is."
    """

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(
            f"no OAuth session for resource {resource!r}; call OAuthClient.start "
            "with the 401 challenge before requesting a bearer token"
        )


class ReauthorizationRequired(OAuthError):
    """A stored session's access token expired and its refresh attempt
    failed (no refresh token, or the authorization server refused it).

    The failed session is evicted before this is raised, deliberately: the
    fix is a full re-authorization from a fresh challenge, driven by
    whatever 401 the caller's next unauthenticated request receives, rather
    than retrying the same refresh in a loop.
    """

    def __init__(self, resource: str, reason: str) -> None:
        self.resource = resource
        self.reason = reason
        super().__init__(
            f"refresh failed for resource {resource!r} ({reason}); the session was "
            "discarded and a full re-authorization is required"
        )


class StepUpExhausted(OAuthError):
    """A resource has been stepped up ``max_step_up_attempts`` times already.

    The MCP authorization spec requires bounded retries here and treats
    continued failure as permanent rather than a reason to keep trying.
    """

    def __init__(self, resource: str, attempts: int) -> None:
        self.resource = resource
        self.attempts = attempts
        super().__init__(
            f"resource {resource!r} has been stepped up {attempts} time(s) already; "
            "treating repeated insufficient_scope as a permanent authorization failure"
        )
