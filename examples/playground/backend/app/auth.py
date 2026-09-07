"""Turning a cookie into a `psych_runtime.Scope`, and the routes that mint one.

This is the join between `app.accounts` (who exists, and whether this password
is theirs) and `psych_runtime.Scope` (the isolation boundary every Psych entry point
takes). DESIGN.md §14 says Psych "does not know what a tenant is, whether a
principal may act, or how anyone authenticated", and this module is the answer
to all three for this console.

## The rule that makes the isolation real

`POST /api/runs` used to build its Scope from the request body:

    scope = psych_runtime.Scope(tenant=body.tenant, principal=body.principal)

which let any caller claim any tenant and read anyone's Runs. The tenant now
comes from a verified session and from nothing else, and the request schema no
longer has the field at all. Rejecting a supplied `tenant` would be an
improvement; not accepting one is the fix, because a field that exists is a
field somebody will eventually decide to trust.

## The cookie

`httpOnly`, so script on the page cannot read it and an XSS bug does not
become a stolen session. `SameSite=Lax`, which is what lets the console on
`localhost:3010` call the backend on `localhost:8080`: those are different
*origins* but the same *site*, so the cookie rides along, while a genuine
cross-site request from somewhere else does not carry it. `Secure` is off by
default because the default deployment is plain HTTP on loopback, where a
`Secure` cookie is silently dropped and sign-in presents as immediately
signing out again; `PSYCH_PLAYGROUND_COOKIE_SECURE=1` turns it on behind TLS.

## What a failure says

A sign-in failure says the email or password was wrong, never which. That
distinction is the difference between a login form and an account-enumeration
oracle, and the sign-up form is the one place the difference is unavoidable:
"that address is already registered" is information, but a sign-up that
silently did nothing is worse for the person and no better for anyone else,
since they could learn the same thing by trying to sign in.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Final

from fastapi import Request, Response

import psych_runtime
from app.accounts import (
    SESSION_TTL,
    Account,
    display_name_of,
    email_complaint,
    hash_password,
    hash_session_token,
    new_account_id,
    new_session,
    normalise_email,
    password_complaint,
    verify_password,
)
from app.errors import ApiProblem
from app.settings_store import SettingsStore, StateFile

COOKIE_NAME: Final = "psych_session"

PUBLIC_PATHS: Final = frozenset(
    {
        "/api/health",
        "/api/auth/signup",
        "/api/auth/signin",
        "/api/auth/signout",
        "/api/oauth/callback",
    }
)
"""The only routes that answer without a session, and why each one does.

- `/api/health` reports liveness and whether anybody has signed up yet. It
  says nothing about any account, which is what makes it safe to answer.
- The three `/api/auth` routes are how a session comes to exist. Sign-out is
  here rather than guarded because a sign-out that can fail is one people stop
  trusting.
- `/api/oauth/callback` receives a browser redirected by an authorization
  server, which has no reason to carry this console's cookie. It is guarded
  instead by the unguessable `state` value `OAuthClient` minted, which is the
  only thing that resolves a waiting `authorize()` call.

One set, read by both the middleware that enforces it and the test that audits
it, so the two cannot disagree about what is public.
"""

_SIGNIN_FAILED: Final = "That email and password do not match an account."
"""One message for "no such account" and for "wrong password", deliberately."""


def scope_for(account: Account) -> psych_runtime.Scope:
    """The Scope every call this account makes is admitted under.

    Both fields are the account id rather than the email. The tenant must be
    stable because it is stamped on every Record and filters every store query,
    and an email is a login people change. The principal is stamped on every
    Record too, and an append-only log that nothing can rewrite is the wrong
    place to copy somebody's email address into.
    """
    return psych_runtime.Scope(tenant=account.id, principal=account.id)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", secure=secure, path="/")


async def account_from_request(request: Request, settings: SettingsStore) -> Account | None:
    """Whoever this request is, or `None`. Never raises on a bad cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    file = await settings.load_file()
    return file.accounts.account_for_token(token, datetime.now(UTC))


async def require_account(request: Request, settings: SettingsStore) -> Account:
    """Whoever this request is, or a 401.

    Raises:
        ApiProblem: 401, with a message written for a person rather than for a
            log line, since the console shows it when a session has aged out.
    """
    account = await account_from_request(request, settings)
    if account is None:
        raise ApiProblem(401, "Sign in to continue.")
    return account


async def create_account(
    settings: SettingsStore, email: str, password: str, display_name: str = ""
) -> tuple[Account, str]:
    """Register an account and sign it in. Returns the account and its token.

    Raises:
        ApiProblem: 400 when the address or password is unusable, 409 when the
            address is already registered.
    """
    if (complaint := email_complaint(email)) is not None:
        raise ApiProblem(400, complaint)
    if (complaint := password_complaint(password)) is not None:
        raise ApiProblem(400, complaint)

    normalised = normalise_email(email)
    # Hashed before the lock, not inside it: scrypt costs ~770ms and 128MB by
    # design (`app.accounts`), and doing that while holding the settings lock
    # would stall every other request in the process for the duration. The
    # uniqueness check that matters is the one inside the transaction below.
    password_hash = await asyncio.to_thread(hash_password, password)
    account = Account(
        id=new_account_id(),
        email=normalised,
        password_hash=password_hash,
        created_at=datetime.now(UTC),
        display_name=display_name.strip(),
    )
    token, session = new_session(account.id)

    def register(file: StateFile) -> StateFile:
        # Re-checked here rather than only before hashing: two sign-ups for one
        # address can race between the check and the write, and this is the
        # only point where they cannot.
        if file.accounts.by_email(normalised) is not None:
            raise ApiProblem(409, "That address is already registered. Sign in instead.")
        accounts = file.accounts.model_copy(
            update={
                "accounts": [*file.accounts.accounts, account],
                "sessions": [*file.accounts.sessions, session],
            }
        )
        return file.model_copy(update={"accounts": accounts})

    await settings.update_file(register)
    return account, token


async def sign_in(settings: SettingsStore, email: str, password: str) -> tuple[Account, str]:
    """Verify a password and mint a session.

    Raises:
        ApiProblem: 401, with the same message whether the account is unknown
            or the password is wrong.
    """
    file = await settings.load_file()
    account = file.accounts.by_email(email)
    if account is None:
        # Still spend the time. Returning immediately for an unknown address
        # makes sign-in measurably faster for addresses that do not exist,
        # which turns response latency into an account-enumeration oracle and
        # undoes the shared error message above.
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        raise ApiProblem(401, _SIGNIN_FAILED)
    if not await asyncio.to_thread(verify_password, password, account.password_hash):
        raise ApiProblem(401, _SIGNIN_FAILED)

    token, session = new_session(account.id)

    def add(file: StateFile) -> StateFile:
        now = datetime.now(UTC)
        pruned = file.accounts.without_expired_sessions(now)
        accounts = pruned.model_copy(update={"sessions": [*pruned.sessions, session]})
        return file.model_copy(update={"accounts": accounts})

    await settings.update_file(add)
    return account, token


async def sign_out(settings: SettingsStore, request: Request) -> None:
    """Drop this session. Idempotent: signing out twice is not an error, and
    neither is signing out with no session at all."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return
    token_hash = hash_session_token(token)

    def drop(file: StateFile) -> StateFile:
        accounts = file.accounts.model_copy(
            update={"sessions": [s for s in file.accounts.sessions if s.token_hash != token_hash]}
        )
        return file.model_copy(update={"accounts": accounts})

    await settings.update_file(drop)


async def is_first_account(settings: SettingsStore) -> bool:
    """Whether nobody has signed up yet.

    The console asks so its first screen can say "create your account" instead
    of "sign in", which is the difference between a product and a locked door
    for whoever just ran the launcher.
    """
    file = await settings.load_file()
    return not file.accounts.accounts


def account_summary(account: Account) -> dict[str, str]:
    """What `GET /api/auth/me` returns. Never the password hash, and the
    display name rather than the address, so a console left open on a shared
    screen does not put someone's email in the sidebar."""
    return {
        "id": account.id,
        "email": account.email,
        "display_name": display_name_of(account),
    }


def _make_dummy_hash() -> str:
    """A hash of a value nothing can be, computed once at import.

    Exists only so `sign_in` can spend the same time on an unknown address as
    on a known one. Computing it lazily would put a ~770ms stall on whichever
    request first hit an unknown address, which is the exact signal it exists
    to remove.
    """
    return hash_password("psych.playground.no-such-account")


_DUMMY_HASH: Final = _make_dummy_hash()


__all__ = [
    "COOKIE_NAME",
    "PUBLIC_PATHS",
    "account_from_request",
    "account_summary",
    "clear_session_cookie",
    "create_account",
    "is_first_account",
    "require_account",
    "scope_for",
    "set_session_cookie",
    "sign_in",
    "sign_out",
]
