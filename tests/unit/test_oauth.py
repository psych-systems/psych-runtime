"""Pure OAuth 2.1 logic: PKCE, canonical URIs, challenge parsing, the RFC
9207 issuer table, scope-set arithmetic, discovery URL construction, and
token expiry maths.

No IO anywhere in this file: every function under test takes its inputs as
arguments (including "now", for expiry) and returns a value, which is what
makes a whole afternoon of MCP authorization spec fiddliness checkable
without a server in the loop.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import SecretStr

from psych_runtime.core.scope import Scope
from psych_runtime.tools.oauth import (
    ClientIdentityConfig,
    InvalidCanonicalUri,
    IssuerMismatch,
    NoActiveSession,
    OAuthClient,
    ReauthorizationRequired,
    TokenSet,
    UnsupportedGrant,
    authorization_server_metadata_urls,
    canonicalize_resource_uri,
    find_bearer_challenge,
    generate_pkce_pair,
    generate_state,
    parse_www_authenticate,
    protected_resource_metadata_urls,
    s256_challenge,
    union_scopes,
    validate_authorization_response_issuer,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPkce:
    def test_the_challenge_is_s256_of_the_verifier(self) -> None:
        pair = generate_pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode()).digest())
        assert pair.challenge == expected.rstrip(b"=").decode("ascii")

    def test_the_method_is_always_s256(self) -> None:
        assert generate_pkce_pair().method == "S256"

    def test_the_challenge_has_no_padding(self) -> None:
        assert "=" not in generate_pkce_pair().challenge

    def test_two_pairs_are_never_the_same(self) -> None:
        one = generate_pkce_pair()
        other = generate_pkce_pair()
        assert one.verifier != other.verifier
        assert one.challenge != other.challenge

    def test_the_verifier_length_is_within_rfc_7636_bounds(self) -> None:
        pair = generate_pkce_pair()
        assert 43 <= len(pair.verifier) <= 128

    def test_s256_challenge_is_a_pure_function_of_the_verifier(self) -> None:
        assert s256_challenge("a-known-verifier") == s256_challenge("a-known-verifier")

    def test_a_known_verifier_produces_the_documented_rfc_7636_challenge(self) -> None:
        # The worked example from RFC 7636 Appendix B.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        assert s256_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_state_values_are_unpredictable_and_distinct(self) -> None:
        assert generate_state() != generate_state()


# ---------------------------------------------------------------------------
# Canonical resource URI (RFC 8707)
# ---------------------------------------------------------------------------


class TestCanonicalResourceUri:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("https://mcp.example.com/mcp", "https://mcp.example.com/mcp"),
            ("https://mcp.example.com", "https://mcp.example.com"),
            ("https://mcp.example.com:8443", "https://mcp.example.com:8443"),
            ("https://mcp.example.com/server/mcp", "https://mcp.example.com/server/mcp"),
            ("https://mcp.example.com/", "https://mcp.example.com"),
            ("https://MCP.Example.COM/mcp", "https://mcp.example.com/mcp"),
            ("HTTPS://mcp.example.com/mcp", "https://mcp.example.com/mcp"),
        ],
    )
    def test_valid_uris_canonicalise_as_expected(self, given: str, expected: str) -> None:
        assert canonicalize_resource_uri(given) == expected

    def test_missing_scheme_is_rejected(self) -> None:
        with pytest.raises(InvalidCanonicalUri, match="scheme"):
            canonicalize_resource_uri("mcp.example.com")

    def test_a_fragment_is_rejected(self) -> None:
        with pytest.raises(InvalidCanonicalUri, match="fragment"):
            canonicalize_resource_uri("https://mcp.example.com#fragment")

    def test_missing_host_is_rejected(self) -> None:
        with pytest.raises(InvalidCanonicalUri, match="host"):
            canonicalize_resource_uri("https:///path")

    def test_the_root_path_becomes_no_path(self) -> None:
        assert canonicalize_resource_uri("https://mcp.example.com/") == "https://mcp.example.com"


# ---------------------------------------------------------------------------
# WWW-Authenticate parsing
# ---------------------------------------------------------------------------


class TestParseWwwAuthenticate:
    def test_a_single_param(self) -> None:
        challenges = parse_www_authenticate('Bearer realm="example"')
        assert challenges == (("Bearer", {"realm": "example"}),)

    def test_multiple_params(self) -> None:
        header = (
            'Bearer resource_metadata="https://mcp.example.com/.well-known/'
            'oauth-protected-resource", scope="files:read"'
        )
        challenges = parse_www_authenticate(header)
        assert len(challenges) == 1
        scheme, params = challenges[0]
        assert scheme == "Bearer"
        assert (
            params["resource_metadata"]
            == "https://mcp.example.com/.well-known/oauth-protected-resource"
        )
        assert params["scope"] == "files:read"

    def test_a_quoted_value_may_contain_a_comma(self) -> None:
        header = 'Bearer error="insufficient_scope", error_description="need a, b and c"'
        challenges = parse_www_authenticate(header)
        _, params = challenges[0]
        assert params["error_description"] == "need a, b and c"

    def test_a_quoted_value_may_contain_an_escaped_quote(self) -> None:
        header = r'Bearer error_description="she said \"hello\""'
        _, params = parse_www_authenticate(header)[0]
        assert params["error_description"] == 'she said "hello"'

    def test_an_unquoted_token_value_is_accepted(self) -> None:
        _, params = parse_www_authenticate("Bearer error=insufficient_scope")[0]
        assert params["error"] == "insufficient_scope"

    def test_param_names_are_lowercased(self) -> None:
        _, params = parse_www_authenticate('Bearer Scope="a"')[0]
        assert params["scope"] == "a"

    def test_two_challenges_are_both_found(self) -> None:
        header = 'Basic realm="x", Bearer error="insufficient_scope"'
        challenges = parse_www_authenticate(header)
        assert [scheme for scheme, _ in challenges] == ["Basic", "Bearer"]
        assert challenges[1][1]["error"] == "insufficient_scope"

    def test_a_scheme_with_no_params_is_still_returned(self) -> None:
        challenges = parse_www_authenticate("Bearer")
        assert challenges == (("Bearer", {}),)

    def test_find_bearer_challenge_is_case_insensitive_on_scheme(self) -> None:
        challenge = find_bearer_challenge('bearer scope="x"')
        assert challenge is not None
        assert challenge.scope == "x"

    def test_find_bearer_challenge_returns_none_when_absent(self) -> None:
        assert find_bearer_challenge('Basic realm="x"') is None

    def test_bearer_challenge_named_properties(self) -> None:
        header = (
            'Bearer error="insufficient_scope", scope="a b", error_description="need more", '
            'resource_metadata="https://example.com/.well-known/oauth-protected-resource"'
        )
        challenge = find_bearer_challenge(header)
        assert challenge is not None
        assert challenge.error == "insufficient_scope"
        assert challenge.scope == "a b"
        assert challenge.error_description == "need more"
        assert (
            challenge.resource_metadata
            == "https://example.com/.well-known/oauth-protected-resource"
        )

    def test_an_auth_param_with_no_value_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="no value"):
            parse_www_authenticate("Bearer realm=")

    def test_a_bare_token_with_no_equals_is_parsed_as_token68_not_an_error(self) -> None:
        """ "Bearer sometoken" is a legal challenge under RFC 7235's grammar
        (auth-scheme SP token68); it just carries no named params."""
        assert parse_www_authenticate("Bearer opaquetoken") == (("Bearer", {}),)

    def test_an_unterminated_quoted_string_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="unterminated"):
            parse_www_authenticate('Bearer realm="unterminated')


# ---------------------------------------------------------------------------
# RFC 9207 §2.4: the exact four-row table
# ---------------------------------------------------------------------------


class TestAuthorizationResponseIssuerValidation:
    def test_row_1_supported_and_present_and_matching_proceeds(self) -> None:
        validate_authorization_response_issuer(
            iss_parameter_supported=True,
            iss="https://as.example.com",
            expected_issuer="https://as.example.com",
        )

    def test_row_1_supported_and_present_but_mismatched_is_rejected(self) -> None:
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=True,
                iss="https://attacker.example.com",
                expected_issuer="https://as.example.com",
            )

    def test_row_2_supported_and_absent_is_rejected(self) -> None:
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=True, iss=None, expected_issuer="https://as.example.com"
            )

    def test_row_3_unsupported_and_present_and_matching_proceeds(self) -> None:
        validate_authorization_response_issuer(
            iss_parameter_supported=False,
            iss="https://as.example.com",
            expected_issuer="https://as.example.com",
        )

    def test_row_3_unsupported_and_present_but_mismatched_is_rejected(self) -> None:
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=False,
                iss="https://attacker.example.com",
                expected_issuer="https://as.example.com",
            )

    def test_row_4_unsupported_and_absent_proceeds(self) -> None:
        validate_authorization_response_issuer(
            iss_parameter_supported=False, iss=None, expected_issuer="https://as.example.com"
        )

    def test_no_normalisation_case(self) -> None:
        """RFC 3986 §6.2.1 simple string comparison: no case folding."""
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=False,
                iss="HTTPS://AS.EXAMPLE.COM",
                expected_issuer="https://as.example.com",
            )

    def test_no_normalisation_trailing_slash(self) -> None:
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=False,
                iss="https://as.example.com/",
                expected_issuer="https://as.example.com",
            )

    def test_no_normalisation_default_port(self) -> None:
        with pytest.raises(IssuerMismatch):
            validate_authorization_response_issuer(
                iss_parameter_supported=False,
                iss="https://as.example.com:443",
                expected_issuer="https://as.example.com",
            )


# ---------------------------------------------------------------------------
# Scope union
# ---------------------------------------------------------------------------


class TestUnionScopes:
    def test_the_union_of_two_disjoint_sets(self) -> None:
        assert union_scopes("files:read", "files:write") == "files:read files:write"

    def test_overlapping_scopes_are_not_duplicated(self) -> None:
        assert union_scopes("files:read files:write", "files:write") == "files:read files:write"

    def test_the_result_is_deterministically_ordered(self) -> None:
        assert union_scopes("z y", "a b") == "a b y z"

    def test_both_none_is_none(self) -> None:
        assert union_scopes(None, None) is None

    def test_one_none_returns_the_other(self) -> None:
        assert union_scopes(None, "files:read") == "files:read"
        assert union_scopes("files:read", None) == "files:read"

    def test_empty_strings_behave_like_none(self) -> None:
        assert union_scopes("", "") is None


# ---------------------------------------------------------------------------
# Discovery URL construction
# ---------------------------------------------------------------------------


class TestProtectedResourceMetadataUrls:
    def test_root_resource_has_one_candidate(self) -> None:
        urls = protected_resource_metadata_urls("https://mcp.example.com")
        assert urls == ("https://mcp.example.com/.well-known/oauth-protected-resource",)

    def test_a_resource_with_a_path_tries_path_aware_first(self) -> None:
        urls = protected_resource_metadata_urls("https://example.com/public/mcp")
        assert urls == (
            "https://example.com/.well-known/oauth-protected-resource/public/mcp",
            "https://example.com/.well-known/oauth-protected-resource",
        )


class TestAuthorizationServerMetadataUrls:
    def test_an_issuer_with_no_path(self) -> None:
        urls = authorization_server_metadata_urls("https://auth.example.com")
        assert urls == (
            "https://auth.example.com/.well-known/oauth-authorization-server",
            "https://auth.example.com/.well-known/openid-configuration",
        )

    def test_an_issuer_with_a_path_tries_all_three_in_priority_order(self) -> None:
        urls = authorization_server_metadata_urls("https://auth.example.com/tenant1")
        assert urls == (
            "https://auth.example.com/.well-known/oauth-authorization-server/tenant1",
            "https://auth.example.com/.well-known/openid-configuration/tenant1",
            "https://auth.example.com/tenant1/.well-known/openid-configuration",
        )


# ---------------------------------------------------------------------------
# Token expiry maths
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    def _token(self, *, obtained_at: float, expires_in: float | None) -> TokenSet:
        return TokenSet(
            access_token=SecretStr("access-token-value"),
            obtained_at=obtained_at,
            expires_in=expires_in,
        )

    def test_a_token_with_no_expires_in_never_expires(self) -> None:
        token = self._token(obtained_at=0.0, expires_in=None)
        assert token.is_expiring(now=10_000.0, safety_margin_seconds=30.0) is False

    def test_a_fresh_token_is_not_expiring(self) -> None:
        token = self._token(obtained_at=1_000.0, expires_in=3600.0)
        assert token.is_expiring(now=1_100.0, safety_margin_seconds=30.0) is False

    def test_a_token_inside_the_safety_margin_is_expiring(self) -> None:
        token = self._token(obtained_at=1_000.0, expires_in=60.0)
        # expires_at = 1060; safety margin 30 means "expiring" from 1030 on.
        assert token.is_expiring(now=1_031.0, safety_margin_seconds=30.0) is True
        assert token.is_expiring(now=1_029.0, safety_margin_seconds=30.0) is False

    def test_an_already_expired_token_is_expiring(self) -> None:
        token = self._token(obtained_at=1_000.0, expires_in=10.0)
        assert token.is_expiring(now=2_000.0, safety_margin_seconds=30.0) is True

    def test_expires_at_is_none_when_expires_in_is_none(self) -> None:
        assert self._token(obtained_at=5.0, expires_in=None).expires_at is None

    def test_expires_at_adds_expires_in_to_obtained_at(self) -> None:
        assert self._token(obtained_at=5.0, expires_in=60.0).expires_at == 65.0


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


class TestSecretsNeverLeak:
    def test_token_set_repr_and_str_omit_the_access_token(self) -> None:
        secret_value = "sk-do-not-print-me-abcdef123456"
        token = TokenSet(access_token=SecretStr(secret_value), obtained_at=0.0)
        assert secret_value not in repr(token)
        assert secret_value not in str(token)

    def test_token_set_repr_and_str_omit_the_refresh_token(self) -> None:
        secret_value = "refresh-do-not-print-me-abcdef123456"
        token = TokenSet(
            access_token=SecretStr("access"), refresh_token=SecretStr(secret_value), obtained_at=0.0
        )
        assert secret_value not in repr(token)
        assert secret_value not in str(token)

    def test_token_set_is_frozen(self) -> None:
        token = TokenSet(access_token=SecretStr("x"), obtained_at=0.0)
        with pytest.raises(ValueError, match="frozen"):
            token.scope = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Errors raised without ever touching the network
# ---------------------------------------------------------------------------


class TestOAuthClientRefusesWithoutPkce:
    async def test_authorization_code_without_pkce_is_refused_before_any_io(self) -> None:
        class _ExplodingTransport:
            async def request(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("must not make any request when PKCE is refused")

        client = OAuthClient(transport=_ExplodingTransport())  # type: ignore[arg-type]
        with pytest.raises(UnsupportedGrant, match="PKCE"):
            await client.start(
                Scope(tenant="acme"),
                resource="https://mcp.example.com",
                challenge='Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"',
                identity=ClientIdentityConfig(preregistered_client_id="client-1"),
                require_pkce=False,
            )


class TestOAuthClientSessionLookupErrors:
    async def test_bearer_token_with_no_session_raises_no_active_session(self) -> None:
        class _ExplodingTransport:
            async def request(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("must not make any request with no session")

        client = OAuthClient(transport=_ExplodingTransport())  # type: ignore[arg-type]
        with pytest.raises(NoActiveSession):
            await client.bearer_token(Scope(tenant="acme"), resource="https://mcp.example.com")

    async def test_step_up_with_no_session_raises_no_active_session(self) -> None:
        class _ExplodingTransport:
            async def request(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("must not make any request with no session")

        client = OAuthClient(transport=_ExplodingTransport())  # type: ignore[arg-type]
        with pytest.raises(NoActiveSession):
            await client.step_up(
                Scope(tenant="acme"),
                resource="https://mcp.example.com",
                challenge='Bearer error="insufficient_scope", scope="files:write"',
            )

    async def test_evict_of_an_unknown_resource_is_a_silent_no_op(self) -> None:
        class _ExplodingTransport:
            async def request(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("must not make any request")

        client = OAuthClient(transport=_ExplodingTransport())  # type: ignore[arg-type]
        client.evict(Scope(tenant="acme"), resource="https://mcp.example.com")


def test_reauthorization_required_is_distinct_from_no_active_session() -> None:
    """The two are different failures a caller must be able to tell apart:
    "never authorized" versus "was authorized, refresh just failed."""
    assert not issubclass(ReauthorizationRequired, NoActiveSession)
    assert not issubclass(NoActiveSession, ReauthorizationRequired)
