"""Version: the immutable, content-hashed publication of a Spec.

DESIGN.md §4. A Run pins a Version hash at admission and reads only that Version
for its whole life, so editing an agent never mutates a Run in flight and a trace
read six months later says exactly what the agent was.

## Canonicalisation, and why it is this exact shape

The hash is worthless unless two structurally identical Specs produce the same
one. A publish counter that increments on every call would be easier, and would
mint a new Version for a Spec nobody changed, which puts every unchanged agent
on a fresh hash and makes "what was this agent then" unanswerable. So each rule
below closes one specific way two identical Specs would otherwise hash
differently:

- **Hash the validated model, never the author's input.** A Spec that sets
  ``max_steps`` explicitly to 48 and one that omits it are the same agent, and
  they hash the same because defaults are materialised before serialisation.
- **Sort keys at every level.** Python dicts preserve insertion order, so two
  builder call sequences producing the same logical Spec would otherwise differ.
- **Let the type system settle numbers.** A field declared ``float`` holds a
  float after validation whether the author wrote ``1`` or ``1.0``, so ``1``
  against ``1.0`` never reaches the serialiser.
- **Represent absence one way.** Every field is present, and ``None`` is
  ``null``. Omitting-when-None would make "explicitly null" and "not provided"
  hash differently for the same logical value.
- **Sets sort, sequences do not.** ``psych_runtime.core.spec`` already sorts tools,
  skills, servers and subagents at validation, and leaves workflow steps and
  model fallbacks in the order that carries meaning. The serialiser stays dumb.
- **Nothing time-dependent or server-assigned enters the body.** ``published_at``
  sits beside the hash, never inside it, or republishing the same Spec would
  produce a new Version every time.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, TypeAdapter

from psych_runtime.core.ids import VersionHash
from psych_runtime.core.spec import Spec

__all__ = ["Version", "canonical_bytes", "compute_hash", "publish"]

_HASH_PREFIX: Final = "sha256:"
_SPEC_ADAPTER: Final = TypeAdapter[Spec](Spec)


def canonical_bytes(spec: Spec) -> bytes:
    """The exact bytes that get hashed.

    Exposed rather than kept private because when two Specs that should match
    do not, the first thing anyone needs is a diff of these two byte strings.

    ``sort_keys`` handles nesting recursively. ``separators`` removes the
    whitespace ``json.dumps`` inserts by default, which is not semantic.
    ``ensure_ascii=False`` with an explicit UTF-8 encode keeps non-Latin
    instructions from turning into escape sequences whose exact form is a
    json library implementation detail.
    """
    payload: dict[str, Any] = _SPEC_ADAPTER.dump_python(spec, mode="json", by_alias=False)
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def compute_hash(spec: Spec) -> VersionHash:
    """The content hash of ``spec``, prefixed with the algorithm that made it.

    The prefix is not decoration. Hashes outlive the code that produced them,
    and a stored ``sha256:...`` can be migrated by a future reader that knows
    both algorithms. A bare hex string cannot.
    """
    digest = hashlib.sha256(canonical_bytes(spec)).hexdigest()
    return VersionHash(f"{_HASH_PREFIX}{digest}")


class Version(BaseModel):
    """An immutable publication of a Spec.

    Attributes:
        hash: the content hash. The identity of this Version.
        spec: the Spec exactly as validated.
        published_at: when this Version was first stored. Outside the hashed
            body, because a timestamp inside it would make every republish
            produce a new Version.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hash: VersionHash
    spec: Spec
    published_at: datetime

    @property
    def name(self) -> str:
        """The Spec's name, for logs and reports that need something readable."""
        return self.spec.name

    def verify(self) -> bool:
        """Recompute the hash and check it still matches.

        A stored Version whose bytes were edited under it fails this. Cheap
        enough to call on read, and the alternative is trusting that nothing
        ever went wrong in a database.
        """
        return compute_hash(self.spec) == self.hash

    def export_json(self) -> str:
        """The Version as portable JSON, for a consumer putting an agent built
        by chat into their git repository.

        Carries credential *names* and never credential material: the Spec model
        refuses a literal ``Authorization`` header, and a ``credential`` field
        holds a name the SecretResolver looks up at call time (DESIGN.md §10.4).
        """
        return json.dumps(
            {
                "hash": self.hash,
                "published_at": self.published_at.isoformat(),
                "spec": json.loads(canonical_bytes(self.spec)),
            },
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def import_json(cls, document: str) -> Version:
        """Rebuild a Version from ``export_json`` output.

        The hash is recomputed from the imported Spec rather than trusted from
        the document, and a mismatch raises. An importer that trusted the stated
        hash would let a hand-edited export claim to be a Version it is not.
        """
        parsed = json.loads(document)
        spec = _SPEC_ADAPTER.validate_python(parsed["spec"])
        recomputed = compute_hash(spec)
        stated = parsed.get("hash")
        if stated is not None and stated != recomputed:
            raise ValueError(
                f"imported document claims hash {stated} but its Spec hashes to "
                f"{recomputed}; the document was edited after it was exported"
            )
        published_at = (
            datetime.fromisoformat(parsed["published_at"])
            if "published_at" in parsed
            else datetime.now(UTC)
        )
        return cls(hash=recomputed, spec=spec, published_at=published_at)


def publish(spec: Spec, *, published_at: datetime | None = None) -> Version:
    """Turn a validated Spec into a Version.

    This does not store anything and does not validate. The store round-trip
    that makes republishing idempotent lives in ``psych_runtime.publish()``, which
    validates, calls this, and returns the existing Version when the hash is
    already present. Keeping the hashing pure means a test can check hash
    stability without a database.
    """
    return Version(
        hash=compute_hash(spec),
        spec=spec,
        published_at=published_at if published_at is not None else datetime.now(UTC),
    )
