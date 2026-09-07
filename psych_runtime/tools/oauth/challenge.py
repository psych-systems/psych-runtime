"""Parsing a ``WWW-Authenticate`` header, per RFC 7235's auth-param grammar.

A 401 or 403 from an MCP server carries a ``WWW-Authenticate`` header built
from one or more challenges, each an auth-scheme followed by either a
comma-separated list of ``name=value`` auth-params (values optionally
quoted, with backslash-escaping inside the quotes) or a bare token68. A
single regex over the whole header cannot tell a comma that separates two
auth-params from one that separates two challenges, and cannot handle a
comma or an ``=`` sign that appears *inside* a quoted value (an
``error_description`` is free text and both are legal there) -- both are
exactly the case this module exists to get right, so it walks the header
character by character instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["BearerChallenge", "find_bearer_challenge", "parse_www_authenticate"]

_TCHAR = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TOKEN68 = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


def _skip_ows(header: str, i: int) -> int:
    n = len(header)
    while i < n and header[i] in " \t":
        i += 1
    return i


def _match_token(header: str, i: int) -> tuple[str, int]:
    match = _TCHAR.match(header, i)
    if match is None:
        return "", i
    return match.group(0), match.end()


def _match_quoted_string(header: str, i: int) -> tuple[str, int]:
    """``header[i]`` is the opening ``"``. Returns the unescaped value and
    the index just past the closing quote."""
    n = len(header)
    j = i + 1
    out: list[str] = []
    while j < n:
        char = header[j]
        if char == "\\" and j + 1 < n:
            out.append(header[j + 1])
            j += 2
            continue
        if char == '"':
            return "".join(out), j + 1
        out.append(char)
        j += 1
    raise ValueError(f"unterminated quoted-string in WWW-Authenticate header: {header!r}")


def _starts_auth_param(header: str, i: int) -> bool:
    """Whether ``header[i:]`` begins with ``token BWS "="``, the shape that
    distinguishes an auth-param list from a bare token68 challenge and, at a
    comma inside an auth-param list, distinguishes "one more param" from
    "the next challenge starts here"."""
    token, after = _match_token(header, i)
    if not token:
        return False
    after = _skip_ows(header, after)
    return after < len(header) and header[after] == "="


@dataclass(frozen=True, slots=True)
class BearerChallenge:
    """One parsed ``Bearer`` challenge from a ``WWW-Authenticate`` header.

    ``params`` holds every auth-param the server sent, keys lowercased
    (auth-param names are conventionally lowercase and MCP's are always
    lowercase; lowercasing here means a caller need not guess a server's
    casing). The four named properties are the ones this package acts on;
    anything else a server adds is still reachable through ``params``.
    """

    params: Mapping[str, str]

    @property
    def resource_metadata(self) -> str | None:
        return self.params.get("resource_metadata")

    @property
    def scope(self) -> str | None:
        return self.params.get("scope")

    @property
    def error(self) -> str | None:
        return self.params.get("error")

    @property
    def error_description(self) -> str | None:
        return self.params.get("error_description")


def _parse_auth_param_list(header: str, i: int) -> tuple[dict[str, str], int]:
    """``header[i:]`` begins with an auth-param list (the caller already
    confirmed this via ``_starts_auth_param``). Parses ``name=value`` pairs
    separated by commas until a comma is found that starts the next
    challenge instead of another param, or the header ends. Returns the
    params and the index just past the list.
    """
    n = len(header)
    params: dict[str, str] = {}
    while True:
        key, i = _match_token(header, i)
        if not key:
            raise ValueError(f"malformed auth-param in WWW-Authenticate header: {header!r}")
        i = _skip_ows(header, i)
        if i >= n or header[i] != "=":
            raise ValueError(
                f"auth-param {key!r} has no '=' in WWW-Authenticate header: {header!r}"
            )
        i = _skip_ows(header, i + 1)
        if i < n and header[i] == '"':
            value, i = _match_quoted_string(header, i)
        else:
            value, i = _match_token(header, i)
            if not value:
                raise ValueError(
                    f"auth-param {key!r} has no value in WWW-Authenticate header: {header!r}"
                )
        params[key.lower()] = value
        i = _skip_ows(header, i)
        if i >= n or header[i] != ",":
            return params, i
        lookahead = _skip_ows(header, i + 1)
        if not (lookahead < n and _starts_auth_param(header, lookahead)):
            # This comma separates challenges, not params: leave it for the
            # caller to consume.
            return params, i
        i = lookahead


def _parse_one_challenge(header: str, i: int) -> tuple[str, dict[str, str], int]:
    """Parse one challenge starting at ``header[i]``: its scheme, and either
    an auth-param list, a token68 (discarded), or nothing at all. Returns
    ``(scheme, params, next_index)``."""
    n = len(header)
    scheme, i = _match_token(header, i)
    if not scheme:
        raise ValueError(f"expected an auth-scheme in WWW-Authenticate header: {header!r}")

    before_gap = i
    i = _skip_ows(header, i)
    has_gap = i > before_gap
    if not has_gap or i >= n or header[i] == ",":
        return scheme, {}, i

    if _starts_auth_param(header, i):
        params, i = _parse_auth_param_list(header, i)
        return scheme, params, i

    # A token68, present but not one of our named params.
    match = _TOKEN68.match(header, i)
    if match is not None:
        i = match.end()
    return scheme, {}, i


def parse_www_authenticate(header: str) -> tuple[tuple[str, Mapping[str, str]], ...]:
    """Every challenge in a ``WWW-Authenticate`` header, as
    ``(scheme, params)`` pairs in the order they appeared.

    A challenge using token68 syntax (no ``name=value`` pairs -- rare for
    ``Bearer``, but common for e.g. ``Basic`` on a server that offers both)
    is still returned, with an empty ``params`` mapping: this function's job
    is to find challenge boundaries correctly, not to judge which schemes
    carry params.

    Raises:
        ValueError: the header is not a syntactically valid challenge list
            (an auth-param with no ``=``, or an unterminated quoted-string).
    """
    challenges: list[tuple[str, Mapping[str, str]]] = []
    i = 0
    n = len(header)
    while True:
        i = _skip_ows(header, i)
        while i < n and header[i] == ",":
            i += 1
            i = _skip_ows(header, i)
        if i >= n:
            break
        scheme, params, i = _parse_one_challenge(header, i)
        challenges.append((scheme, params))
    return tuple(challenges)


def find_bearer_challenge(header: str) -> BearerChallenge | None:
    """The first ``Bearer`` challenge in a ``WWW-Authenticate`` header, or
    ``None`` if the header has none. Scheme comparison is case-insensitive,
    per RFC 7235's auth-scheme grammar."""
    for scheme, params in parse_www_authenticate(header):
        if scheme.lower() == "bearer":
            return BearerChallenge(params=params)
    return None
