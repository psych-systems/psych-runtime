"""One exception type for every non-2xx response this API returns.

FastAPI's own ``HTTPException`` puts whatever you pass as ``detail`` under a
``{"detail": ...}`` envelope, which means a caller wanting the per-path
problems from a ``SpecValidationError`` (a list) alongside a human-readable
summary (a string) ends up with one or the other, not both, without a second
envelope layer. ``ApiProblem`` carries both explicitly and is rendered by one
handler in ``app.main`` into ``schemas.ProblemResponse``, so every error this
API produces -- a bad Spec, an unknown run, an unresumable one -- has the same
shape on the wire.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.schemas import ValidationProblem
from psych_runtime.core.errors import SpecValidationError


class ApiProblem(Exception):
    def __init__(
        self, status_code: int, detail: str, issues: list[ValidationProblem] | None = None
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.issues = issues if issues is not None else []
        super().__init__(detail)


def from_pydantic(detail: str, err: ValidationError) -> ApiProblem:
    """A request body, or a Spec field built from one, failed pydantic
    validation -- a bad ``limits`` key, a tool name that fails the pattern
    every Spec name is checked against, and so on."""
    issues = [
        ValidationProblem(
            path=".".join(str(part) for part in e["loc"]) or "(root)", message=e["msg"]
        )
        for e in err.errors()
    ]
    return ApiProblem(400, detail, issues)


def from_spec_validation(err: SpecValidationError) -> ApiProblem:
    """DESIGN.md §4: validation happens at publish, never at run, exactly so
    it can be reported like this -- every problem at once, each naming its
    path, instead of a customer discovering one typo per attempt."""
    issues = [ValidationProblem(path=i.path, message=i.message) for i in err.issues]
    return ApiProblem(400, "the agent's Spec was refused at publish", issues)
