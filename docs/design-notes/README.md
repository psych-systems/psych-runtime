# Design notes

DESIGN.md says what Psych is and what gets built. These notes say why the harder
parts are shaped the way they are, and record the failure modes each shape exists
to prevent. Read the relevant one before implementing the component it covers.
Where a note and DESIGN.md disagree, DESIGN.md wins and the note is a bug.

| Note | Covers |
|---|---|
| [durability-and-leases.md](durability-and-leases.md) | DESIGN.md §7, §8: the store port, claiming, leases, deadlines, force settlement, orphaned work, retry budgets |
| [record-log-and-reducer.md](record-log-and-reducer.md) | DESIGN.md §6, §9, §12: the log, the pure reducer, the corruption taxonomy, the agent loop, interrupts and steering, resumable streaming |
| [spec-versioning-and-tool-access.md](spec-versioning-and-tool-access.md) | DESIGN.md §4, §10.4 to §10.6, §10.9, §14: Specs as data, content-hashed Versions, narrowing, pooling, selectors, approvals, the failure-streak guard |
| [tool-disclosure-and-prompt-budget.md](tool-disclosure-and-prompt-budget.md) | DESIGN.md §10.2, §10.3, §10.7, §10.8, §19: deferred disclosure, the catalog cache, large results, prompt assembly and cache stability |
| [metering-and-telemetry.md](metering-and-telemetry.md) | DESIGN.md §13: usage, pricing, latency, the report, the telemetry port and its conformance suite |
| [subagents-and-delegation.md](subagents-and-delegation.md) | DESIGN.md §17: depth, spawn permissions, addressing, listing, projections, cold resume |
| [code-execution-and-sandboxing.md](code-execution-and-sandboxing.md) | DESIGN.md §18: the sandbox contract, the wire protocol, resource limits, teardown |
| [mcp-2026-07-28.md](mcp-2026-07-28.md) | What the current MCP specification revision changed and what Psych has to do about it |
