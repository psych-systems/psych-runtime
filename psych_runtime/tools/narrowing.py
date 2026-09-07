"""Access narrows and never widens.

DESIGN.md §10.5:

    what the server offers  ⊇  what the tenant permits  ⊇  what the Spec grants
                            ⊇  what is callable now

**There is exactly one function computing this intersection, and both the
validator and the runtime call it.** That is the whole point of the module. Two
implementations of "what may this Run call" drift, and the day they drift is the
day the validator says yes to something the runtime says no to, or worse, the
other way round.

## Empty means everything, not nothing

This is the rule most likely to be inverted by someone reading the code fresh.
An empty allow list at any plane means "everything the plane above permits". It
never means "deny all".

Getting this backwards fails safe-looking but is worse than it appears: every
agent with no explicit tool list silently loses all its tools, which looks like a
broken integration rather than a policy decision, and the fix people reach for is
to grant `*` everywhere.

## Composition is one-directional

The tenant plane narrows what the server offers. The Spec plane narrows the
already-narrowed tenant result, never the raw catalogue. Each step's output is
provably a subset of its input, so composing them can only shrink. Narrowing
against the raw catalogue at the second step would let a Spec grant re-add a tool
the tenant revoked, which is the exact failure this ordering exists to prevent.

## A revoked tool is silently absent

A Spec granting a tool the tenant no longer permits simply does not get it. No
warning from this function, no exception. Asking for something you do not have
access to just does not get it, and the caller decides whether that is worth
telling anyone about.

## Why prefix patterns are safe here

Names match exactly, with one addition: a trailing ``*``. ``read_*`` against an
MCP server with forty tools is a real need, and the alternative is a Spec that
lists forty names and goes stale the first time the server adds one. The
pattern cannot widen anything, because it only ever *selects from* the set
above; it can never introduce a name that set did not contain.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

__all__ = ["matches_pattern", "narrow", "tenant_permitted"]


def matches_pattern(name: str, pattern: str) -> bool:
    """Whether ``name`` is selected by ``pattern``.

    ``*`` alone matches everything. A trailing ``*`` is a prefix match. Anything
    else is an exact match.

    Deliberately not ``fnmatch``: a full glob brings ``?``, character classes and
    an escaping story, and every one of those is a way for a security-relevant
    pattern to mean something other than what its author read. Prefix or exact is
    enough for tool names and cannot surprise anyone.
    """
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern


def _select(available: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """Names in ``available`` selected by any of ``patterns``, order preserved.

    Iterating ``available`` rather than ``patterns`` is what keeps this
    monotone: the result is a subsequence of the input by construction, so no
    pattern can produce a name the plane above did not offer. It is also why a
    stale pattern naming a tool the server dropped disappears quietly instead of
    erroring.
    """
    return [name for name in available if any(matches_pattern(name, p) for p in patterns)]


def tenant_permitted(
    server_offers: Iterable[str],
    tenant_allows: Sequence[str],
) -> list[str]:
    """What the tenant permits, out of what the server offers.

    Args:
        server_offers: every tool the server currently advertises.
        tenant_allows: the tenant's allow list. **Empty means everything.**

    Returns:
        The permitted names, in the order the server offered them.
    """
    offered = list(server_offers)
    if not tenant_allows:
        return offered
    return _select(offered, tenant_allows)


def narrow(
    server_offers: Iterable[str],
    tenant_allows: Sequence[str] = (),
    spec_grants: Sequence[str] = (),
    callable_now: Sequence[str] | None = None,
) -> list[str]:
    """The whole chain, in one call.

    This is the function DESIGN.md §10.5 says must be the only one. Both
    ``psych_runtime.core.validation`` (at publish) and the tool resolver (at every turn)
    reach it, so the two cannot disagree about what a Spec may call.

    Args:
        server_offers: what the server currently advertises.
        tenant_allows: the tenant's allow list. Empty means everything offered.
        spec_grants: the Spec's allow list. Empty means everything permitted.
        callable_now: a final restriction for things true only at this moment,
            such as a tool whose failure streak has tripped the guard, or one the
            consumer's Policy port just refused. ``None`` means no restriction.
            Passing an empty list means nothing is callable, which is different
            from ``None`` and is deliberately not folded into the
            empty-means-everything rule: the earlier planes are static policy
            where empty is a sensible default, and this one is a live answer
            where "nothing" is a real answer.

    Returns:
        Callable tool names, in the order the server offered them.
    """
    permitted = tenant_permitted(server_offers, tenant_allows)
    granted = permitted if not spec_grants else _select(permitted, spec_grants)
    if callable_now is None:
        return granted
    live = set(callable_now)
    return [name for name in granted if name in live]
