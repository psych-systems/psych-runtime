"""Every scenario module, in one place.

DESIGN.md §23's ten items, in order, then six more capabilities worth showing
beside them: approvals, large-result offload, delegation, tenant isolation,
the degenerate loop nothing yet stops, and skills loaded on demand.
``ScenarioModule`` (see ``base.py``) is a ``Protocol`` rather than a base
class, so each entry here is just the module itself -- ``SCENARIOS`` is what
turns "a module with three names in it" into something ``app.main`` can
iterate without importing sixteen names by hand.

Adding another scenario means writing the module and adding one line here.
Nothing else discovers scenario modules by scanning the directory:
an unlisted module is not a scenario, on purpose, so a half-written file
left in this package cannot accidentally show up as a real endpoint.
"""

from __future__ import annotations

from app.scenarios import (
    approvals,
    crash_recovery,
    degenerate_loop,
    delegation,
    failure_streak,
    four_stores,
    honest_accounting,
    interrupt_and_steer,
    large_result_offload,
    mcp_mid_run,
    reconnect_without_gaps,
    same_spec_two_authors,
    sandboxed_code,
    skills_on_demand,
    tenant_isolation,
    workflow_resume,
)
from app.scenarios.base import ScenarioModule

__all__ = ["SCENARIOS"]

SCENARIOS: tuple[ScenarioModule, ...] = (
    same_spec_two_authors,
    crash_recovery,
    interrupt_and_steer,
    reconnect_without_gaps,
    workflow_resume,
    mcp_mid_run,
    honest_accounting,
    four_stores,
    sandboxed_code,
    failure_streak,
    approvals,
    large_result_offload,
    delegation,
    tenant_isolation,
    degenerate_loop,
    skills_on_demand,
)
