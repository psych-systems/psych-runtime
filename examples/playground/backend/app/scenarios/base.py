"""The shape every scenario module has, and the small pieces they share.

DESIGN.md §23 is the runtime's own statement of what "done" means, and this
package turns each item into something that actually executes rather than
something read. A scenario module owns one demonstration end to end: it drives
the real runtime (its own ``Store``, its own ``FakeModel`` or a real provider, its own
Spec), and reports what it found through the same kind of typed assertion the
regression gate (``tests/e2e/test_regression_gate.py``) already uses -- a claim,
whether it held, and enough detail that a sceptical reader does not have to take
the ``bool`` on faith.

## The contract a scenario module satisfies

Not a base class -- ``ScenarioModule`` below is a ``Protocol``, because a
scenario is a plain module with three names in it:

- ``INFO: ScenarioInfo`` -- static description, no I/O.
- ``async def check_availability(ctx: ScenarioContext) -> str | None`` --
  ``None`` means it can run right now; any other value is the honest reason it
  cannot, shown to a caller instead of a false pass or a silent skip.
- ``async def run(ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult`` --
  does the real thing, calling ``emit`` as it goes so a caller watching an SSE
  stream sees genuine progress rather than a spinner.

## Why assertions are collected rather than raised

A scenario that fails must report ``passed: false`` naming which assertion
broke, and must never swallow a failure to keep the demo green. A bare
``assert`` stops at the first failure and loses every assertion after it,
which is exactly backwards for a demonstration whose whole
value is showing a sceptical reader what happened. ``Checks.require`` records
every claim, held or not, and lets the scenario keep going to record the rest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.settings_store import ProviderConfig
from psych_runtime.model.egress import HttpTransport

__all__ = [
    "Assertion",
    "Checks",
    "ProgressFn",
    "ScenarioContext",
    "ScenarioInfo",
    "ScenarioModule",
    "ScenarioResult",
]

ProgressFn = Callable[[str, str], Awaitable[None]]
"""``emit(step, detail)``. Called any number of times while a scenario runs;
the endpoint turns each call into one SSE progress event."""


@dataclass(frozen=True, slots=True)
class ScenarioInfo:
    """The static description ``GET /api/scenarios`` lists.

    Attributes:
        id: the path segment ``POST /api/scenarios/{id}/run`` takes.
        title: short, for a list.
        proves: one sentence on what a passing run establishes -- written so it
            stands on its own in a UI, without the reader needing this file's
            docstring too.
        design_ref: where in DESIGN.md this comes from, e.g. ``"§23.2"``.
        requires: what has to be real and reachable for this to run at all:
            any of ``"postgres"``, ``"mysql"``, ``"dynamodb"``,
            ``"model-provider"``, ``"container-runtime"``. Empty means the
            scenario needs nothing beyond the interpreter itself -- a
            ``FakeModel`` and ``InMemoryStore`` scenario, say.
    """

    id: str
    title: str
    proves: str
    design_ref: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioContext:
    """What a scenario may draw on beyond the real runtime it always drives
    directly.

    Deliberately thin. A scenario proves something about Psych, not about this
    example app, so it builds its own ``Store``, its own ``ToolRegistry`` and
    (for every scenario but the two that need a real model) its own
    ``FakeModel`` -- never the playground's own ``AppState``. The two fields
    here are the only things a scenario cannot reasonably construct itself:
    the one egress seam every outbound call in this process shares (DESIGN.md
    §14 -- a scenario opening its own ``httpx`` client would be exactly the
    kind of second path that seam exists to prevent), and whichever model
    provider a person has actually configured through ``/api/settings``,
    which honest-accounting and degenerate-loop need and nothing else does.
    """

    transport: HttpTransport
    active_provider: ProviderConfig | None


@dataclass(slots=True)
class Assertion:
    claim: str
    held: bool
    detail: str


@dataclass(slots=True)
class ScenarioResult:
    passed: bool
    summary: str
    assertions: list[Assertion] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    report: dict[str, Any] | None = None


class Checks:
    """Collects assertions instead of raising on the first one.

    ```python
    checks = Checks()
    checks.require("the hashes match", one.hash == two.hash, f"{one.hash} vs {two.hash}")
    ...
    return checks.result("both authoring forms agree", run_ids=[str(run_id)])
    ```

    ``require`` always returns the condition, so a scenario can still branch on
    it (skip a step that only makes sense if an earlier one held) while the
    record of what was actually checked keeps accumulating either way.
    """

    def __init__(self) -> None:
        self.assertions: list[Assertion] = []

    def require(self, claim: str, held: bool, detail: str) -> bool:
        self.assertions.append(Assertion(claim=claim, held=held, detail=detail))
        return held

    @property
    def all_held(self) -> bool:
        return all(a.held for a in self.assertions)

    def result(
        self,
        summary: str,
        *,
        run_ids: list[str] | None = None,
        report: dict[str, Any] | None = None,
    ) -> ScenarioResult:
        passed = self.all_held
        return ScenarioResult(
            passed=passed,
            summary=summary if passed else f"failed: {summary}",
            assertions=self.assertions,
            run_ids=run_ids if run_ids is not None else [],
            report=report,
        )


class ScenarioModule(Protocol):
    """The structural shape every module under ``app/scenarios/`` has.

    Not inherited from -- a scenario file is a plain module, and this exists
    so ``app/scenarios/__init__.py`` can hold them in one typed tuple rather
    than a ``list[Any]``.
    """

    INFO: ScenarioInfo

    async def check_availability(self, ctx: ScenarioContext) -> str | None: ...

    async def run(self, ctx: ScenarioContext, emit: ProgressFn) -> ScenarioResult: ...
