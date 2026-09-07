"""Agent Cards: built from a Spec, canonicalised per RFC 8785, signed as JWS.

The canonicalisation is the half worth testing hardest. A signature that
verifies is not evidence the bytes are right -- the same wrong bytes on both
sides verify happily -- so the JCS cases here assert the *bytes*, against RFC
8785's own examples, before any signature is involved.
"""

from __future__ import annotations

import json

import pytest
from pydantic import JsonValue

from psych_runtime.a2a.card import agent_card, skills_of
from psych_runtime.a2a.models import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    APIKeySecurityScheme,
    ClientCredentialsOAuthFlow,
    HTTPAuthSecurityScheme,
    MutualTlsSecurityScheme,
    OAuth2SecurityScheme,
    OAuthFlows,
    OpenIdConnectSecurityScheme,
    ProtocolBinding,
    SecurityScheme,
    wire_dict,
)
from psych_runtime.a2a.negotiation import PROTOCOL_VERSION
from psych_runtime.a2a.signing import (
    HmacCardSigner,
    SignatureVerificationError,
    b64url_decode,
    canonicalize,
    card_payload,
    sign_card,
    verify_card,
)
from psych_runtime.core.spec import A2APeer, AgentSpec, HttpTool, McpServer, ModelRef, SubagentRef

pytestmark = pytest.mark.unit


def _spec(**kwargs: object) -> AgentSpec:
    base: dict[str, object] = {
        "name": "support",
        "description": "Answers questions about orders.",
        "model": ModelRef(model="gpt-4o"),
    }
    return AgentSpec.model_validate(base | kwargs)


def _interfaces() -> tuple[AgentInterface, ...]:
    return (
        AgentInterface(
            url="https://agents.example.test/a2a/v1",
            protocol_binding=ProtocolBinding.JSONRPC,
            protocol_version=PROTOCOL_VERSION,
        ),
    )


class TestSkillsFromASpec:
    def test_an_http_tool_becomes_a_skill_with_its_own_description(self) -> None:
        spec = _spec(
            tools=(
                HttpTool(
                    name="lookup_order",
                    description="Look an order up by id.",
                    url="https://api.example.test/orders",
                ),
            )
        )
        skills = skills_of(spec)
        assert [skill.id for skill in skills] == ["lookup_order"]
        assert skills[0].description == "Look an order up by id."

    def test_an_mcp_server_is_one_skill_not_one_per_tool(self) -> None:
        """A card generated from a Spec cannot know what a server currently
        offers without connecting, and advertising a tool that may have been
        withdrawn is the §10.7 failure."""
        spec = _spec(
            mcp_servers=(
                McpServer(name="crm", url="https://crm.example.test/mcp", allow=("search",)),
            )
        )
        skills = skills_of(spec)
        assert [skill.id for skill in skills] == ["mcp:crm"]
        assert "search" in skills[0].description

    def test_a_subagent_is_a_skill(self) -> None:
        spec = _spec(
            subagents=(
                SubagentRef(
                    name="researcher",
                    description="Reads long documents and summarises them.",
                    spec=_spec(name="researcher"),
                ),
            )
        )
        assert [skill.id for skill in skills_of(spec)] == ["subagent:researcher"]

    def test_a_peer_does_not_become_a_skill(self) -> None:
        """What this agent can *reach* is not what it advertises: a peer's
        skills belong on that peer's card."""
        spec = _spec(a2a_peers=(A2APeer(name="research", url="https://peer.example.test"),))
        assert skills_of(spec) == ()

    def test_the_order_is_the_specs_own_sorted_order(self) -> None:
        """RFC 8785 sorts object keys and never array elements, so a skills
        list whose order wandered would break its own signature."""
        spec = _spec(
            tools=(
                HttpTool(name="b_tool", description="b", url="https://x.test"),
                HttpTool(name="a_tool", description="a", url="https://x.test"),
            )
        )
        assert [skill.id for skill in skills_of(spec)] == ["a_tool", "b_tool"]


class TestAgentCard:
    def test_a_card_carries_the_specs_identity_and_the_deployments_urls(self) -> None:
        card = agent_card(_spec(), interfaces=_interfaces(), version="2026.4.1")
        assert card.name == "support"
        assert card.description == "Answers questions about orders."
        assert card.version == "2026.4.1"
        assert card.supported_interfaces[0].protocol_version == "1.0"

    def test_all_five_security_schemes_can_be_declared(self) -> None:
        """§4.5: API key, HTTP auth, OAuth2, OIDC and mutual TLS."""
        schemes = {
            "apiKey": SecurityScheme(
                api_key_security_scheme=APIKeySecurityScheme(location="header", name="X-Key")
            ),
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
            ),
            "oauth": SecurityScheme(
                oauth2_security_scheme=OAuth2SecurityScheme(
                    flows=OAuthFlows(
                        client_credentials=ClientCredentialsOAuthFlow(
                            token_url="https://auth.example.test/token"
                        )
                    )
                )
            ),
            "oidc": SecurityScheme(
                open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                    open_id_connect_url="https://auth.example.test/.well-known/openid-configuration"
                )
            ),
            "mtls": SecurityScheme(mtls_security_scheme=MutualTlsSecurityScheme()),
        }
        card = agent_card(_spec(), interfaces=_interfaces(), version="1", security_schemes=schemes)
        assert set(card.security_schemes) == {"apiKey", "bearer", "oauth", "oidc", "mtls"}
        serialised = wire_dict(card)["securitySchemes"]
        assert isinstance(serialised, dict)
        oauth = serialised["oauth"]
        assert isinstance(oauth, dict)
        assert set(oauth) == {"oauth2SecurityScheme"}

    def test_a_scheme_must_be_exactly_one_of_the_five(self) -> None:
        with pytest.raises(ValueError, match="oneof"):
            SecurityScheme()

    def test_a_card_without_interfaces_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one interface"):
            agent_card(_spec(), interfaces=(), version="1")

    def test_a_card_without_a_description_is_refused(self) -> None:
        """§4.4.1 marks it REQUIRED because it is what a client reads to decide
        whether to call this agent at all."""
        with pytest.raises(ValueError, match="no description"):
            agent_card(_spec(description=""), interfaces=_interfaces(), version="1")

    def test_capabilities_are_declared_explicitly(self) -> None:
        """§3.3.4 gives declaring a capability teeth: an agent that says
        streaming must answer SendStreamingMessage."""
        card = agent_card(
            _spec(),
            interfaces=_interfaces(),
            version="1",
            streaming=False,
            push_notifications=False,
        )
        assert card.capabilities.streaming is False
        assert wire_dict(card)["capabilities"] == {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        }


class TestCanonicalisation:
    """RFC 8785, which §8.4.1 requires before signing."""

    def test_keys_are_sorted_and_whitespace_removed(self) -> None:
        assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_integral_floats_lose_their_fraction(self) -> None:
        """§3.2.2.3 defers to ECMAScript Number::toString, where 1.0 is `1`."""
        assert canonicalize(1.0) == b"1"
        assert canonicalize(-0.5) == b"-0.5"

    def test_exponents_are_not_zero_padded(self) -> None:
        """Python writes 1e-07 and JavaScript writes 1e-7."""
        assert canonicalize(1e-7) == b"1e-7"
        assert canonicalize(1e22) == b"1e+22"

    def test_non_finite_numbers_have_no_canonical_form(self) -> None:
        with pytest.raises(ValueError, match="no JSON representation"):
            canonicalize(float("inf"))

    def test_the_specifications_own_example(self) -> None:
        """§8.4.1's worked example, byte for byte."""
        document: JsonValue = {
            "capabilities": {"pushNotifications": False, "streaming": False},
            "description": "",
            "name": "Example Agent",
            "skills": [],
        }
        assert canonicalize(document) == (
            b'{"capabilities":{"pushNotifications":false,"streaming":false},'
            b'"description":"","name":"Example Agent","skills":[]}'
        )

    def test_the_payload_excludes_the_signature_block(self) -> None:
        """§8.4.1 rule 3, without which signing would be circular."""
        card = agent_card(_spec(), interfaces=_interfaces(), version="1")
        signer = HmacCardSigner(b"secret-key", kid="key-1")
        signed = sign_card(card, signer)
        assert signed.signatures
        assert card_payload(signed) == card_payload(card)
        assert b"signatures" not in card_payload(signed)

    def test_the_signed_bytes_are_the_served_bytes(self) -> None:
        """A card signed over one serialisation and served as another produces
        signatures that never verify anywhere."""
        card = agent_card(_spec(), interfaces=_interfaces(), version="1")
        served = json.loads(json.dumps(wire_dict(card)))
        served.pop("signatures", None)
        assert canonicalize(served) == card_payload(card)


class TestSigning:
    def test_a_signed_card_verifies(self) -> None:
        signer = HmacCardSigner(b"shared-secret", kid="key-1")
        card = sign_card(agent_card(_spec(), interfaces=_interfaces(), version="1"), signer)
        assert verify_card(card, {"key-1": signer}) == ("key-1",)

    def test_the_protected_header_carries_alg_typ_and_kid(self) -> None:
        """§8.4.2: those three are required."""
        signer = HmacCardSigner(b"shared-secret", kid="key-1")
        card = sign_card(
            agent_card(_spec(), interfaces=_interfaces(), version="1"),
            signer,
            jku="https://agents.example.test/jwks.json",
        )
        header = json.loads(b64url_decode(card.signatures[0].protected))
        assert header == {
            "alg": "HS256",
            "typ": "JOSE",
            "kid": "key-1",
            "jku": "https://agents.example.test/jwks.json",
        }

    def test_an_altered_card_fails(self) -> None:
        signer = HmacCardSigner(b"shared-secret", kid="key-1")
        card = sign_card(agent_card(_spec(), interfaces=_interfaces(), version="1"), signer)
        tampered = card.model_copy(update={"description": "Also transfers money."})
        with pytest.raises(SignatureVerificationError, match="altered after signing"):
            verify_card(tampered, {"key-1": signer})

    def test_a_different_key_fails(self) -> None:
        card = sign_card(
            agent_card(_spec(), interfaces=_interfaces(), version="1"),
            HmacCardSigner(b"one-secret", kid="key-1"),
        )
        with pytest.raises(SignatureVerificationError):
            verify_card(card, {"key-1": HmacCardSigner(b"another-secret", kid="key-1")})

    def test_an_algorithm_mismatch_is_refused_rather_than_verified(self) -> None:
        """The algorithm-confusion attack: a document that names its own
        algorithm must not choose how it is checked."""

        class _Es256Verifier:
            alg = "ES256"

            def verify(self, data: bytes, signature: bytes) -> bool:
                return True

        card = sign_card(
            agent_card(_spec(), interfaces=_interfaces(), version="1"),
            HmacCardSigner(b"shared-secret", kid="key-1"),
        )
        with pytest.raises(SignatureVerificationError, match="refusing"):
            verify_card(card, {"key-1": _Es256Verifier()})

    def test_a_card_with_no_signatures_is_not_silently_trusted(self) -> None:
        card = agent_card(_spec(), interfaces=_interfaces(), version="1")
        with pytest.raises(SignatureVerificationError, match="no signatures"):
            verify_card(card, {"key-1": HmacCardSigner(b"k", kid="key-1")})

    def test_an_unknown_kid_is_reported_not_ignored(self) -> None:
        card = sign_card(
            agent_card(_spec(), interfaces=_interfaces(), version="1"),
            HmacCardSigner(b"shared-secret", kid="rotated-out"),
        )
        with pytest.raises(SignatureVerificationError, match="rotated-out"):
            verify_card(card, {"key-1": HmacCardSigner(b"shared-secret", kid="key-1")})

    def test_signing_twice_keeps_both_signatures(self) -> None:
        """A key rotation is signed by both keys for as long as it takes
        clients to catch up (§4.4.7's `signatures` is a list)."""
        old = HmacCardSigner(b"old-secret", kid="key-1")
        new = HmacCardSigner(b"new-secret", kid="key-2")
        card = sign_card(
            sign_card(agent_card(_spec(), interfaces=_interfaces(), version="1"), old), new
        )
        assert len(card.signatures) == 2
        assert set(verify_card(card, {"key-1": old, "key-2": new})) == {"key-1", "key-2"}

    def test_a_provider_and_icon_round_trip_through_signing(self) -> None:
        card = agent_card(
            _spec(),
            interfaces=_interfaces(),
            version="1",
            provider=AgentProvider(url="https://example.test", organization="Example"),
            icon_url="https://example.test/icon.png",
        )
        signer = HmacCardSigner(b"shared-secret", kid="key-1")
        signed = sign_card(card, signer)
        reparsed = AgentCard.model_validate(json.loads(json.dumps(wire_dict(signed))))
        assert verify_card(reparsed, {"key-1": signer}) == ("key-1",)

    def test_a_signer_needs_a_kid(self) -> None:
        with pytest.raises(ValueError, match="kid"):
            HmacCardSigner(b"secret", kid="")

    def test_capabilities_presence_survives_a_round_trip(self) -> None:
        """The reason `AgentCapabilities` keeps `bool | None`: §8.4.1 makes
        "explicitly false" and "not set" different signed bytes, so a card that
        collapsed them would verify at the signer and fail at the verifier."""
        card = AgentCard(
            name="a",
            description="b",
            supported_interfaces=_interfaces(),
            version="1",
            capabilities=AgentCapabilities(streaming=False),
            default_input_modes=("text/plain",),
            default_output_modes=("text/plain",),
            skills=(),
        )
        signer = HmacCardSigner(b"secret", kid="key-1")
        signed = sign_card(card, signer)
        reparsed = AgentCard.model_validate(json.loads(json.dumps(wire_dict(signed))))
        assert verify_card(reparsed, {"key-1": signer}) == ("key-1",)
