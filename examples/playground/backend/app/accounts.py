"""Accounts and sessions: the identity Psych refuses to own.

DESIGN.md §1 refuses "authentication, identity, users, sessions-as-login" and
"organizations, teams, roles or permissions" by name. This module is what a
consumer writes instead, and it is the whole reason `psych_runtime.Scope` exists: §14
threads a Scope through every entry point, stamps it on every Record and
filters every store query by it, while deliberately knowing nothing about
"whether a principal may act, or how anyone authenticated". That is the seam.
This file fills it.

## Why an account and a tenant are the same thing here

`Scope.tenant` is the isolation boundary. In this console one account is one
boundary, so `Scope(tenant=account.id)`. Not the email: an email is a login,
people change theirs, and a tenant that changes underneath a Run would orphan
every Record already stamped with the old one. The id is minted once and never
moves.

`Scope.principal` is also the account id rather than the email, because a
principal is stamped on every Record and an email is personal data that has no
business being copied into an append-only log that nothing can rewrite.

## Passwords, with no new dependency

`hashlib.scrypt` is in the standard library and is a real memory-hard password
KDF, so this needs neither `argon2-cffi` nor `bcrypt`. That matters a little
for a project whose library half has exactly two runtime dependencies, and it
matters more for the one-command launcher: a pure-stdlib hash is one fewer
wheel to build on someone's laptop.

The parameters below are OWASP's scrypt floor (N=2^17, r=8, p=1). They are
deliberately slow: measured here, hashing costs ~770ms and verifying ~430ms,
using 128MB. That is the point of a password hash rather than a cost to tune
away, and it is why `verify_password` runs once per sign-in and never per
request. Sessions are what keep the expensive thing rare, and `SESSION_TTL` is
30 days so it stays rare.

If that latency ever becomes the wrong trade, lower N rather than switching
algorithm: the stored hash carries its own parameters, so old hashes keep
verifying either way.

## What this is not

No password reset, no email verification, no OAuth sign-in, no roles, no
invitations, no shared workspaces. Each of those is real work with real
security corners, and a console that pretends to have them is worse than one
that plainly does not. What is here is enough to make isolation true, which is
the property that was missing.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "SESSION_TTL",
    "Account",
    "Session",
    "hash_password",
    "hash_session_token",
    "new_account_id",
    "new_session_token",
    "normalise_email",
    "password_complaint",
    "verify_password",
]

# OWASP's scrypt floor: N=2^17, r=8, p=1. `maxmem` has to be raised past
# CPython's 32MB default or `hashlib.scrypt` refuses these parameters outright
# with "memory limit exceeded", which reads like a bug in the caller.
_SCRYPT_N: Final = 2**17
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_MAXMEM: Final = 2**28  # 256MB, comfortably above N * r * 128.
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16

MIN_PASSWORD_LENGTH: Final = 12
"""Length is the only password rule here worth having.

Composition rules ("one digit, one symbol") push people towards `Passw0rd!`
and buy nothing measurable. A floor on length is the one constraint that does.
"""

MAX_PASSWORD_BYTES: Final = 1024
"""An upper bound, because scrypt's cost is paid by this process.

Without it, a single sign-in request carrying a megabyte passphrase is a free
64MB-and-100ms of work per attempt, repeatable by anyone who can reach the
port. 1024 bytes is far past any real passphrase.
"""

SESSION_TTL: Final = timedelta(days=30)
"""How long a session lasts.

Long, on purpose. This is a tool someone leaves open across days, and the
sibling store-configuration work restarts the backend to adopt a new
database, which must not sign everybody out.
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Account(BaseModel):
    """One person, and one `Scope.tenant`.

    Frozen, so an account object cannot be mutated between the moment a
    session is verified against it and the moment its id becomes the Scope a
    Run is admitted under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    email: str
    """Normalised (see `normalise_email`). Unique across accounts, which is
    what makes it usable as the thing typed into a sign-in form."""
    password_hash: str
    """`scrypt$<n>$<r>$<p>$<salt hex>$<derived hex>`. Self-describing so a
    future parameter increase can re-hash on next sign-in instead of locking
    everyone out, and so a hash written by an older build is still verifiable
    by a newer one."""
    created_at: datetime
    display_name: str = ""
    """What the console shows. Empty falls back to the email's local part;
    there is no separate profile page and this is not worth one."""


class Session(BaseModel):
    """One signed-in browser.

    Only the *hash* of the token is stored. The token itself exists in the
    cookie and nowhere else, so a leaked state file does not hand someone a
    working session the way a leaked table of raw tokens would. This is the
    same reasoning as the password hash beside it, applied to a bearer value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_hash: str
    account_id: str
    created_at: datetime
    expires_at: datetime

    def is_live(self, now: datetime) -> bool:
        return now < self.expires_at


def new_account_id() -> str:
    """A tenant id. Opaque, so nothing can be inferred from it, and short
    enough to read in a log line beside a run id."""
    return f"acct_{uuid.uuid4().hex[:16]}"


def new_session_token() -> str:
    """256 bits from the OS. This is a bearer credential; nothing about it is
    derived from the account, so it leaks nothing if seen."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """A plain SHA-256, deliberately, where the password uses scrypt.

    The two are different problems. A password is low-entropy and guessable,
    so its hash must be *slow*. A session token is 256 random bits, so there
    is nothing to guess and slowness would only tax every authenticated
    request. What is needed here is preimage resistance, which SHA-256 has.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(raw: str) -> str:
    """Lowercase, trimmed, NFKC.

    Unicode normalisation matters more than it looks: without it two byte
    sequences that render identically are two different accounts, and a person
    who signs up on one keyboard cannot sign in from another. Lowercasing the
    whole address is technically wrong about the local part, which the RFC
    calls case-sensitive, and right about every mail provider anyone actually
    uses. Predictability wins here.
    """
    return unicodedata.normalize("NFKC", raw).strip().lower()


def password_complaint(password: str) -> str | None:
    """What is wrong with this password, addressed to the person typing it.

    Returns `None` when it is acceptable. The messages say what to do rather
    than what failed, because a sign-up form is the worst place to be terse.
    """
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return f"That password is too long. Keep it under {MAX_PASSWORD_BYTES} bytes."
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"Use at least {MIN_PASSWORD_LENGTH} characters. "
            "A short phrase you can remember beats a short password you cannot."
        )
    if not password.strip():
        return "That password is only whitespace."
    return None


def email_complaint(raw: str) -> str | None:
    """Whether this looks like an address, without pretending to validate one.

    Full RFC 5322 validation is a famous waste of time and this console sends
    no mail, so the only real question is whether a person can type it back
    tomorrow. Shape-checking catches the typo that matters (a missing `@`) and
    stops there.
    """
    email = normalise_email(raw)
    if not email:
        return "Enter an email address."
    if len(email) > 320:  # RFC 3696 erratum: 64 local + @ + 255 domain.
        return "That address is too long."
    if not _EMAIL.match(email):
        return "That does not look like an email address."
    return None


def hash_password(password: str) -> str:
    """Derive a storable hash, with a fresh salt.

    Raises:
        ValueError: the password is longer than `MAX_PASSWORD_BYTES`. Checked
            here as well as in `password_complaint` so no caller can reach
            scrypt with an unbounded input by forgetting the other one.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password exceeds {MAX_PASSWORD_BYTES} bytes")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        encoded,
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` produces `stored`.

    Returns `False` rather than raising on a malformed or unknown-scheme hash.
    A state file someone hand-edited should fail the sign-in it was edited to
    allow, not crash the endpoint into a 500 that says more about the internals
    than a 401 would.

    The parameters come from the stored string rather than from the constants
    above, so raising the cost later does not invalidate existing hashes.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return False
    # A hash claiming absurd parameters is a denial-of-service against this
    # process, not a credential to check: reject it rather than allocating
    # whatever it asks for.
    if not (0 < n <= _SCRYPT_N) or not (0 < r <= 32) or not (0 < p <= 16):
        return False
    try:
        derived = hashlib.scrypt(
            encoded,
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected) or _SCRYPT_DKLEN,
        )
    except ValueError:
        return False
    return hmac.compare_digest(derived, expected)


def new_session(account_id: str, now: datetime | None = None) -> tuple[str, Session]:
    """Mint a session. Returns the token to hand the browser and the record to
    store, in that order, because only the first should ever be logged and
    neither should be confused for the other."""
    moment = now or datetime.now(UTC)
    token = new_session_token()
    return token, Session(
        token_hash=hash_session_token(token),
        account_id=account_id,
        created_at=moment,
        expires_at=moment + SESSION_TTL,
    )


def display_name_of(account: Account) -> str:
    """What to show for this account. Never the full address by default: a
    console left open on a shared screen should not put someone's email in the
    sidebar."""
    if account.display_name.strip():
        return account.display_name.strip()
    local = account.email.split("@", 1)[0]
    return local or account.email


class AccountsFile(BaseModel):
    """The accounts half of the playground's state file.

    Separate model from the per-account workspace so `app.settings_store` can
    hold both in one file under one lock, and so the type that carries password
    hashes is small enough to audit at a glance.
    """

    model_config = ConfigDict(extra="forbid")

    accounts: list[Account] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)

    def by_email(self, email: str) -> Account | None:
        wanted = normalise_email(email)
        return next((a for a in self.accounts if a.email == wanted), None)

    def by_id(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def account_for_token(self, token: str, now: datetime) -> Account | None:
        """Resolve a cookie value to an account, or `None`.

        Expiry is checked here rather than by a sweep, so a session that has
        aged out stops working the moment it is used even if nothing has
        pruned it yet.
        """
        token_hash = hash_session_token(token)
        session = next((s for s in self.sessions if s.token_hash == token_hash), None)
        if session is None or not session.is_live(now):
            return None
        return self.by_id(session.account_id)

    def without_expired_sessions(self, now: datetime) -> AccountsFile:
        live = [s for s in self.sessions if s.is_live(now)]
        if len(live) == len(self.sessions):
            return self
        return self.model_copy(update={"sessions": live})
