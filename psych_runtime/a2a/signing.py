"""Signed Agent Cards: RFC 8785 canonicalisation, and a JWS over it.

§8.4, and v1.0's headline addition. A card says who an agent is, what it can
do and where to send credentials; an unsigned one says all of that on the word
of whoever answered the DNS lookup. The signature is what makes a card
evidence rather than an assertion.

## Two halves, and only one of them is cryptography

The hard half is agreeing on the bytes. §8.4.1 requires the card be
canonicalised with JCS (RFC 8785) *after* proto field-presence rules are
applied (§5.7) and *without* the ``signatures`` field, so that a verifier who
re-serialises a card they parsed gets exactly the bytes the signer signed. Get
that wrong and every signature fails for reasons no error message will
explain. That half is implemented here in full, and it is the half that is
worth testing hard.

The easy half is the signature itself: base64url the header, base64url the
payload, join with a dot, sign the result (§8.4.2's four steps).

## Why HMAC ships and RSA/ECDSA does not

``CardSigner`` and ``CardVerifier`` are Protocols, and ``HmacCardSigner`` is a
complete HS256 implementation over ``hmac`` from the standard library. It is
not a placeholder: it signs, it verifies, and it round-trips against the
canonicalisation above.

What it is not is *asymmetric*, and §8.4 exists for a reason that HMAC only
half serves: a symmetric key can be verified only by someone who could also
have forged the card, so HS256 is right for two systems that already share a
secret and wrong for publishing a card to the open web. ES256 and RS256 need
``cryptography``, which Psych does not depend on and will not add to its core
for one optional protocol feature -- DESIGN.md's dependency floor is pydantic
and httpx. A consumer who needs a publicly verifiable card writes a fifteen
line ``CardSigner`` over their existing key material, and gets the
canonicalisation, the header assembly and the verification loop from here.
That is the seam this module is shaped around, and ``jku`` in the protected
header (§8.4.2) is supported precisely so such a signer can point at a JWKS.

## The two places this implementation is deliberately narrow

- **Key ordering is by UTF-16 code unit**, which is what RFC 8785 §3.2.3
  specifies, not Python's native code-point ordering. They differ only for
  keys containing characters outside the Basic Multilingual Plane, which is
  vanishingly rare in an Agent Card and would still be a silent signature
  mismatch, so it is done properly rather than approximated.
- **Numbers are formatted per ECMAScript ``Number::toString``**, RFC 8785
  §3.2.2.3, which is where a naive implementation diverges: ``1.0`` must
  serialise as ``1`` and ``1e-7`` as ``1e-7`` rather than Python's
  ``1e-07``. Non-finite numbers have no JSON form at all and raise.
"""

from __future__ import annotations

import base64
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Final, Protocol, runtime_checkable

from pydantic import JsonValue

from psych_runtime.a2a.models import AgentCard, AgentCardSignature, wire_dict

__all__ = [
    "CardSigner",
    "CardVerifier",
    "HmacCardSigner",
    "SignatureVerificationError",
    "b64url_decode",
    "b64url_encode",
    "canonicalize",
    "card_payload",
    "sign_card",
    "signing_input",
    "verify_card",
]

_JOSE_TYP: Final = "JOSE"
"""§8.4.2: the protected header's ``typ`` "SHOULD be set to 'JOSE' for JWS"."""


class SignatureVerificationError(ValueError):
    """A card's signature did not verify, and why.

    A ``ValueError`` rather than a ``PsychError``: the caller is deciding
    whether to trust a document, and the answer to a failure is always to
    refuse the document, never to catch this and continue.
    """


# ---------------------------------------------------------------------------
# RFC 8785 (JCS)
# ---------------------------------------------------------------------------


def _format_number(value: float | int) -> str:
    """RFC 8785 §3.2.2.3: ECMAScript ``Number::toString``.

    Python and JavaScript agree on shortest round-trip digits and disagree on
    presentation in exactly two ways, both handled here: Python writes ``1.0``
    where JavaScript writes ``1``, and Python pads exponents to two digits
    (``1e-07``) where JavaScript does not (``1e-7``).
    """
    if isinstance(value, bool):
        raise TypeError("a boolean is not a number in JSON")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(
            f"{value!r} has no JSON representation, so it cannot appear in a canonical document"
        )
    if value == int(value) and abs(value) < 1e21:
        # ECMAScript prints an integral double without a fraction, and below
        # 1e21 without an exponent.
        return str(int(value))
    text = repr(value)
    if "e" in text:
        mantissa, _, exponent = text.partition("e")
        sign = "+" if not exponent.startswith("-") else "-"
        digits = exponent.lstrip("+-").lstrip("0") or "0"
        return f"{mantissa}e{sign}{digits}"
    return text


def _utf16_key(key: str) -> tuple[int, ...]:
    """RFC 8785 §3.2.3: sort object keys by their UTF-16 code units."""
    encoded = key.encode("utf-16-be")
    return tuple(
        int.from_bytes(encoded[index : index + 2], "big") for index in range(0, len(encoded), 2)
    )


def canonicalize(value: JsonValue) -> bytes:
    """The RFC 8785 canonical form of a JSON value, as UTF-8 bytes.

    Strings are escaped by ``json.dumps``, which uses exactly the escape set
    RFC 8785 §3.2.2.2 inherits from ``JSON.stringify``: the six short forms
    plus ``\\u00XX`` for the remaining control characters, and no escaping of
    anything else.
    """
    if value is None:
        return b"null"
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int | float):
        return _format_number(value).encode("utf-8")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(canonicalize(item) for item in value) + b"]"
    if isinstance(value, dict):
        parts = [
            canonicalize(key) + b":" + canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: _utf16_key(pair[0]))
        ]
        return b"{" + b",".join(parts) + b"}"
    raise TypeError(f"{type(value).__name__} is not JSON and cannot be canonicalised")


def b64url_encode(data: bytes) -> str:
    """Base64url without padding, which is what JWS uses throughout."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """The inverse, restoring the padding JWS strips."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ---------------------------------------------------------------------------
# The signing input (§8.4.2)
# ---------------------------------------------------------------------------


def card_payload(card: AgentCard) -> bytes:
    """The exact bytes a card's signature covers.

    §8.4.1's three rules, applied in order: field-presence semantics (which
    ``wire_dict`` owns, so the signed bytes and the served bytes cannot
    differ), the ``signatures`` field excluded "to avoid circular
    dependencies", and RFC 8785 over what is left.
    """
    document = wire_dict(card)
    document.pop("signatures", None)
    return canonicalize(document)


def signing_input(protected: str, payload: bytes) -> bytes:
    """RFC 7515's JWS Signing Input, per §8.4.2 step 3.

    ``ASCII(BASE64URL(UTF8(protected)) || '.' || BASE64URL(payload))``.
    """
    return f"{protected}.{b64url_encode(payload)}".encode("ascii")


@runtime_checkable
class CardSigner(Protocol):
    """Whatever can produce a signature over bytes, with a key A2A can name.

    Three members and no key material in the interface: an implementation
    backed by a KMS or an HSM never has the private key in the process, and a
    protocol that required one would exclude exactly the deployments that care
    most about signing.
    """

    @property
    def alg(self) -> str:
        """The JWS ``alg`` for the protected header (§8.4.2)."""
        ...

    @property
    def kid(self) -> str:
        """The key id, so a verifier knows which key to fetch."""
        ...

    def sign(self, data: bytes) -> bytes:
        """Sign the JWS Signing Input, returning raw signature bytes."""
        ...


@runtime_checkable
class CardVerifier(Protocol):
    """The other half. Separate from ``CardSigner`` because a verifier holds
    only public material and the two are usually different processes."""

    @property
    def alg(self) -> str: ...

    def verify(self, data: bytes, signature: bytes) -> bool:
        """Whether ``signature`` is valid over ``data``. Must not raise for a
        bad signature: that is an answer, not an error."""
        ...


class HmacCardSigner:
    """HS256 over a shared secret: a complete signer, for a bounded case.

    Right when both agents are operated by people who already share a secret
    (a private mesh, an internal deployment, a test). Wrong for a card served
    on the open web, because anyone who can verify it could have signed it --
    see this module's docstring for why that is a deliberate boundary rather
    than an omission.
    """

    alg: Final = "HS256"

    def __init__(self, key: bytes, *, kid: str) -> None:
        if not key:
            raise ValueError("an HMAC signing key cannot be empty")
        if not kid:
            raise ValueError(
                "a signature needs a kid: §8.4.2 requires one in the protected header so a "
                "verifier can tell which key to check against"
            )
        self._key = key
        self._kid = kid

    @property
    def kid(self) -> str:
        return self._kid

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, sha256).digest()

    def verify(self, data: bytes, signature: bytes) -> bool:
        # compare_digest rather than ==: signature comparison is the textbook
        # timing-attack target, and the fix costs nothing.
        return hmac.compare_digest(self.sign(data), signature)


def sign_card(
    card: AgentCard,
    signer: CardSigner,
    *,
    jku: str | None = None,
    unprotected: Mapping[str, JsonValue] | None = None,
) -> AgentCard:
    """Return ``card`` with one more signature on it (§8.4.2).

    Appends rather than replaces: §4.4.7's ``signatures`` is a list because a
    card may be signed by several keys -- during a key rotation, both. A
    caller re-signing after a rotation drops the old entry themselves, which
    is a decision this function should not make for them.

    Args:
        jku: a JWKS URL for the protected header. Optional per §8.4.2, and the
            thing that makes an asymmetric signature verifiable by a stranger.
        unprotected: JWS unprotected header values. Not covered by the
            signature, which is why nothing security-relevant should go here.
    """
    header: dict[str, JsonValue] = {"alg": signer.alg, "typ": _JOSE_TYP, "kid": signer.kid}
    if jku is not None:
        header["jku"] = jku
    # Canonicalised rather than merely compact: the protected header is
    # base64url-encoded and signed as-is, so a verifier never re-serialises
    # it, but a stable encoding means signing the same card twice with the
    # same key produces the same bytes, which makes a signature diffable.
    protected = b64url_encode(canonicalize(header))
    payload = card_payload(card)
    signature = signer.sign(signing_input(protected, payload))
    entry = AgentCardSignature(
        protected=protected,
        signature=b64url_encode(signature),
        header=dict(unprotected) if unprotected else None,
    )
    return card.model_copy(update={"signatures": (*card.signatures, entry)})


def verify_card(
    card: AgentCard,
    verifiers: Mapping[str, CardVerifier],
    *,
    require_all: bool = False,
) -> tuple[str, ...]:
    """Check a card's signatures, returning the key ids that verified.

    §8.4.3's steps, with the ordering that matters: the payload is rebuilt
    from the card as parsed, so a card whose content was altered in transit
    produces different bytes and fails even though the signature block is
    untouched.

    Args:
        verifiers: by ``kid``. A signature naming a key not in here is
            skipped, not failed: a card may carry signatures from several
            issuers and a verifier is only asked about the ones it knows.
        require_all: fail unless every signature verified, rather than at
            least one. For a caller who parsed a card from a source they trust
            to send only signatures they can check.

    Raises:
        SignatureVerificationError: the card carries no signature this caller
            can check, a signature over a known key did not verify, or a
            protected header is not a JSON object with an ``alg`` matching its
            verifier. An ``alg`` mismatch is a failure rather than a skip: a
            card that says HS256 where the verifier expects ES256 is the
            classic algorithm-confusion attack, and the answer is to refuse.
    """
    if not card.signatures:
        raise SignatureVerificationError("the card carries no signatures")

    payload = card_payload(card)
    verified: list[str] = []
    checked = 0

    for entry in card.signatures:
        header = _protected_header(entry)
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise SignatureVerificationError(
                "a signature's protected header has no string kid, so no key can be chosen"
            )
        verifier = verifiers.get(kid)
        if verifier is None:
            continue
        checked += 1
        if header.get("alg") != verifier.alg:
            raise SignatureVerificationError(
                f"signature {kid!r} declares alg {header.get('alg')!r} but the key for it "
                f"is {verifier.alg!r}; refusing rather than verifying under the algorithm "
                "the document asked for"
            )
        signed = signing_input(entry.protected, payload)
        if not verifier.verify(signed, b64url_decode(entry.signature)):
            raise SignatureVerificationError(
                f"signature {kid!r} does not match the card's canonical payload; the card "
                "was altered after signing, or it was signed over different bytes"
            )
        verified.append(kid)

    if checked == 0:
        raise SignatureVerificationError(
            "none of the card's signatures name a key this caller holds: "
            + ", ".join(sorted(_kids(card.signatures)))
        )
    if require_all and len(verified) != len(card.signatures):
        raise SignatureVerificationError(
            f"{len(verified)} of {len(card.signatures)} signatures verified and require_all "
            "was asked for"
        )
    return tuple(verified)


def _protected_header(entry: AgentCardSignature) -> dict[str, JsonValue]:
    try:
        decoded = json.loads(b64url_decode(entry.protected))
    except Exception as err:
        raise SignatureVerificationError(
            f"a signature's protected header is not base64url-encoded JSON: {err}"
        ) from err
    if not isinstance(decoded, dict):
        raise SignatureVerificationError("a signature's protected header is not a JSON object")
    return decoded


def _kids(signatures: Sequence[AgentCardSignature]) -> tuple[str, ...]:
    out: list[str] = []
    for entry in signatures:
        try:
            header = _protected_header(entry)
        except SignatureVerificationError:
            continue
        kid = header.get("kid")
        if isinstance(kid, str):
            out.append(kid)
    return tuple(out)
