"""Errors raised before a Spec exists at all.

Distinct from ``psych_runtime.core.errors.SpecValidationError``: that one is raised by
``psych_runtime.core.validation`` against a Spec that is already structurally sound and
refers to something missing (an unregistered tool, an unreachable server).
``BuilderError`` is raised earlier, by a call sequence that could never produce
a Spec in the first place, such as ``.build()`` with no model set.
"""

from __future__ import annotations

from psych_runtime.core.errors import PsychError

__all__ = ["BuilderError"]


class BuilderError(PsychError):
    """The builder was asked to produce a Spec it cannot construct."""
