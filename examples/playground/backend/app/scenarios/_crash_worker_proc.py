"""Not a scenario module. A doomed Worker, run as a real OS process.

``app.scenarios.crash_recovery`` spawns this with ``subprocess_exec`` and then
sends it ``SIGKILL`` once its log shows the tool call it is mid-execution on.
Everything DESIGN.md §23 item 2 asks for -- "a Run survives its Worker being
killed" -- is only actually tested if the killing is real: a thread cannot be
SIGKILLed without taking the parent down with it, and an ``asyncio.Task``
cancelled from the same event loop never stops heartbeating, which is a
different (and already-covered, by the deadline mechanism) failure than a
Worker's whole process disappearing. So this runs in its own process, with its
own event loop, connected to the same real Postgres the parent is, and dies
exactly the way DESIGN.md §8.2's lease is built to notice: mid-call, with no
graceful unwind.

Usage: ``python -m app.scenarios._crash_worker_proc <dsn> <run_id> <lease_seconds> <marker_path>``
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from psych_runtime.core.ids import WorkerId
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

_SLEEP_SECONDS = 30.0
"""Comfortably longer than the parent needs to notice the tool started and
send SIGKILL. If this process is somehow never killed (a bug in the parent's
own polling), it settles the Run itself after this long rather than hanging
the demo forever."""


def _build_registry(marker_path: str) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    def slow_carrier_lookup(order_id: str) -> str:
        """Check a shipment's status with a slow carrier API."""
        # Written before the sleep: the parent's assertion that this ran
        # *exactly once* depends on this line executing in the doomed
        # process and never again in whichever process finishes the Run.
        with Path(marker_path).open("a", encoding="utf-8") as fh:
            fh.write(f"executed in doomed process at {time.time()}\n")
        time.sleep(_SLEEP_SECONDS)
        return f"{order_id}: in transit"  # never reached: this process is killed first

    return registry


async def _main() -> None:
    dsn, run_id, lease_seconds_raw, marker_path = sys.argv[1:5]
    store = PostgresStore(dsn=dsn)
    model = FakeModel().turn(
        text="Let me check on that shipment.",
        tool_calls=[("slow_carrier_lookup", {"order_id": "A1"})],
    )
    runtime = Runtime(store=store, model=model, registry=_build_registry(marker_path))
    worker = Worker(
        store,
        runtime,
        worker_id=WorkerId("wrk-doomed"),
        lease_seconds=float(lease_seconds_raw),
        heartbeat_seconds=float(lease_seconds_raw) / 3,
        poll_interval=0.05,
        supervisor_interval=0.2,
    )
    _ = run_id  # the Worker claims whatever is runnable; nothing to scope by here
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
