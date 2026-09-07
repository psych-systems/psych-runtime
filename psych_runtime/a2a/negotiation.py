"""Version negotiation and extension opt-in: the two service parameters.

§3.2.6 calls these *service parameters* and each binding says how they travel.
Over HTTP -- which is every binding Psych ships -- they are request headers
(§9.2, §11.1), case-insensitive, with multiple values comma-separated in one
field. Both are pure string handling with no IO, which is why they are here
rather than in the playground's routes: the rules are the protocol's, and a
second consumer implementing their own routes should not have to re-derive
them.

## Versions

§3.6 fixes three rules that are easy to get subtly wrong:

1. A version is ``Major.Minor``. Patch numbers "SHOULD NOT be used in
   requests, responses and Agent Cards, and MUST not be considered when
   clients and servers negotiate", so ``1.0.3`` negotiates as ``1.0`` rather
   than being refused.
2. An absent or empty header means ``0.3``, not "latest". §3.6.2: "Agents MUST
   interpret empty value as 0.3 version." A server that treated absence as its
   own newest version would silently serve 1.0 semantics to a 0.3 client,
   which is the exact failure the header exists to prevent.
3. An unsupported version is a ``VersionNotSupportedError``, not a best
   effort.

Psych speaks ``1.0`` and says so. It does **not** claim ``0.3``: 0.3 used
different JSON-RPC method names (``message/send`` and friends) and a different
well-known card path, and declaring a version whose wire shape this package
does not implement would make ``supported_versions`` a lie that only surfaces
as a peer's parse error. The consequence is deliberate and worth stating
plainly: a client that sends no ``A2A-Version`` header is refused, because
what it has told the server, per §3.6.2, is that it speaks 0.3.

## Extensions

§4.6.1: agents declare extensions in the card, and clients opt in through a
binding-specific mechanism -- ``A2A-Extensions`` over HTTP. §4.6.3 adds the
one rule with teeth: an extension the agent marks ``required`` and the client
did not request is an ``ExtensionSupportRequiredError``, and an extension the
agent does not know is ignored rather than approximated ("It MUST NOT fall
back to a previous version of the extension automatically").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Final

from psych_runtime.a2a.errors import ExtensionSupportRequiredError, VersionNotSupportedError
from psych_runtime.a2a.models import AgentExtension

__all__ = [
    "A2A_EXTENSIONS_HEADER",
    "A2A_VERSION_HEADER",
    "A2A_VERSION_QUERY_PARAM",
    "ABSENT_VERSION",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "activated_extensions",
    "format_extensions_header",
    "negotiate_version",
    "parse_extensions_header",
    "require_declared_extensions",
]

A2A_VERSION_HEADER: Final = "A2A-Version"
A2A_VERSION_QUERY_PARAM: Final = "A2A-Version"
"""§3.6.1: "Clients MAY provide the ``A2A-Version`` as a request parameter
instead of a header", spelled exactly like the header."""

A2A_EXTENSIONS_HEADER: Final = "A2A-Extensions"

PROTOCOL_VERSION: Final = "1.0"
"""What this implementation speaks, and what its Agent Cards declare."""

SUPPORTED_VERSIONS: Final = frozenset({PROTOCOL_VERSION})

ABSENT_VERSION: Final = "0.3"
"""§3.6.2: "Agents MUST interpret empty value as 0.3 version"."""

_VERSION_PATTERN: Final = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")


def negotiate_version(raw: str | None, *, supported: frozenset[str] = SUPPORTED_VERSIONS) -> str:
    """The ``Major.Minor`` version to serve this request with.

    Args:
        raw: the ``A2A-Version`` header or query parameter, or ``None`` when
            neither was sent. Whitespace-only counts as absent, since a proxy
            that rewrites headers can turn one into the other.
        supported: the versions this interface offers. A parameter rather than
            a constant because §3.6.2 allows an agent to "expose multiple
            interfaces for the same transport with different versions", and
            each interface answers this question for itself.

    Returns:
        The normalised ``Major.Minor`` version.

    Raises:
        VersionNotSupportedError: the requested version -- including the
            ``0.3`` an absent header implies -- is not in ``supported``. The
            message names what was asked for and what is on offer, because a
            client that has just been refused needs both to decide whether to
            retry.
    """
    requested = (raw or "").strip()
    if not requested:
        requested = ABSENT_VERSION
        asked_for = "no A2A-Version header, which §3.6.2 defines as 0.3"
    else:
        asked_for = f"A2A-Version {requested!r}"

    match = _VERSION_PATTERN.match(requested)
    if match is None:
        raise VersionNotSupportedError(
            f"{asked_for} is not a Major.Minor protocol version; this agent speaks "
            f"{', '.join(sorted(supported))}",
            metadata={"requestedVersion": requested},
        )
    # §3.6: patch numbers "MUST not be considered when clients and servers
    # negotiate protocol versions", so 1.0.7 is 1.0 rather than unknown.
    normalised = f"{match.group(1)}.{match.group(2)}"
    if normalised not in supported:
        raise VersionNotSupportedError(
            f"{asked_for} is not supported by this interface; it speaks "
            f"{', '.join(sorted(supported))}",
            metadata={"requestedVersion": normalised},
        )
    return normalised


def parse_extensions_header(raw: str | None) -> tuple[str, ...]:
    """The extension URIs a client opted into, in the order it listed them.

    §9.2 and §11.1: "Multiple values for the same service parameter (e.g.,
    ``A2A-Extensions``) SHOULD be comma-separated in a single header field".
    Duplicates are collapsed rather than kept, since opting into an extension
    twice is opting into it once, and the order is preserved because a client
    that lists them in preference order should not have that reordered by a
    ``set``.
    """
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for chunk in raw.split(","):
        uri = chunk.strip()
        if uri:
            seen.setdefault(uri, None)
    return tuple(seen)


def format_extensions_header(uris: Iterable[str]) -> str:
    """The header value for ``uris``, comma-separated as §9.2 asks."""
    return ", ".join(uris)


def activated_extensions(
    declared: Sequence[AgentExtension], requested: Sequence[str]
) -> tuple[str, ...]:
    """The extensions that are actually in force for this request.

    The intersection of what the agent declares and what the client asked for,
    in the agent's declared order. §4.6.3: an extension version the agent does
    not support is ignored for the interaction rather than approximated, so
    anything requested and not declared simply does not appear here.
    """
    wanted = set(requested)
    return tuple(extension.uri for extension in declared if extension.uri in wanted)


def require_declared_extensions(
    declared: Sequence[AgentExtension], requested: Sequence[str]
) -> tuple[str, ...]:
    """Check §4.6.3's required-extension rule, then report what is active.

    Raises:
        ExtensionSupportRequiredError: the agent declares an extension with
            ``required: true`` that this client did not opt into. Every
            missing one is named in a single error rather than the first
            alone, so a client fixes its header once.
    """
    active = activated_extensions(declared, requested)
    missing = [
        extension.uri
        for extension in declared
        if extension.required and extension.uri not in active
    ]
    if missing:
        raise ExtensionSupportRequiredError(
            "this agent requires extensions the request did not declare support for: "
            + ", ".join(missing)
            + f"; list them in the {A2A_EXTENSIONS_HEADER} header",
            metadata={"requiredExtensions": ", ".join(missing)},
        )
    return active
