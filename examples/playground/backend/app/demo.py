"""Review data for the code-execution screens: real Runs, no model provider.

``POST /api/demo/code-execution`` publishes a review agent for the calling
account and drives four conversations through the real runtime with a
scripted model and this host's own sandbox, so every screen that shows a
program running -- the chat card, Activity, Trace, the attachment reader --
can be looked at without a provider configured or a network to reach:

1. a program that calls a host tool and returns a value, writing a file the
   console can open as an artifact;
2. a program that raises, with its traceback handed back as data;
3. a program that runs past its wall clock and is killed;
4. a program whose output is far larger than the model's preview, kept in
   the blob store and readable in windows.

Deterministic on purpose. The programs are fixed, the model's turns are
scripted, and the sandbox is the one the account's ``default`` profile
resolves to, so what the screens show is what a real Run produces here,
graded by the backend's own report rather than by anything invented for
the demo. Nothing is faked and nothing reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psych_runtime
from app.accounts import Account
from app.settings_store import SettingsStore
from app.store_index import (
    AgentEntry,
    PlaygroundIndex,
    RunEntry,
    new_agent_id,
    new_branch_id,
    new_conversation_id,
)
from psych_runtime.core.ids import RunId
from psych_runtime.core.scope import Scope
from psych_runtime.runtime.abort import AbortSignal
from psych_runtime.runtime.dispatch import dispatch as _dispatch
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.journal import Journal
from psych_runtime.sandbox.profiles import SandboxProfiles
from psych_runtime.store.blob import BlobStore
from psych_runtime.store.port import Store
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["DEMO_AGENT_NAME", "seed_code_execution_demo"]

DEMO_AGENT_NAME = "code-execution-review"

_INSTRUCTIONS = (
    "You are a data analyst. Work things out by writing short Python programs, "
    "and use the host tools for anything that needs live data."
)

_SUCCESS = (
    "prices = {}\n"
    "for order in ('A-100', 'B-200'):\n"
    "    status = await lookup_order(order_id=order)\n"
    "    prices[order] = status['status']\n"
    "rows = [f'{order},{status}' for order, status in prices.items()]\n"
    "with open('orders.csv', 'w') as f:\n"
    "    f.write('order,status\\n' + '\\n'.join(rows) + '\\n')\n"
    "print(f'checked {len(prices)} orders')\n"
    "shipped = sum(1 for s in prices.values() if s == 'shipped')\n"
    "return {'checked': len(prices), 'shipped': shipped}\n"
)

_FAILURE = (
    "totals = {'A-100': 250, 'B-200': 400}\n"
    "average = sum(totals.values()) / len(totals)\n"
    "per_item = average / totals.get('C-300', 0)\n"
    "return per_item\n"
)

_LIMITED = "import time\nwhile True:\n    time.sleep(0.1)\n"

_SPILL = (
    "import json\n"
    "def row(i):\n"
    "    return {'id': i, 'sku': f'SKU-{i:05d}', 'qty': i % 7, 'note': 'x' * 40}\n"
    "rows = [row(i) for i in range(4000)]\n"
    "for row in rows:\n"
    "    print(json.dumps(row))\n"
    "return {'rows': len(rows), 'total_qty': sum(r['qty'] for r in rows)}\n"
)

_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "Check the shipping status of orders A-100 and B-200 and save a CSV.",
        _SUCCESS,
        "Both orders are shipped. I saved the list as orders.csv.",
        "success",
    ),
    (
        "Work out the average order value per item for C-300.",
        _FAILURE,
        "That divides by zero because C-300 has no recorded total. "
        "I need a total for it before I can compute a per-item figure.",
        "failure",
    ),
    (
        "Poll until the warehouse feed updates.",
        _LIMITED,
        "The program hit its time limit before the feed changed. "
        "A polling loop is not the right shape here; tell me what to check.",
        "limited",
    ),
    (
        "List every SKU in the catalogue with its quantity.",
        _SPILL,
        "There are 4,000 SKUs with 12,000 units in total. The full listing is "
        "stored; I can pull any range or search it.",
        "spill",
    ),
)


async def seed_code_execution_demo(
    *,
    store: Store,
    index: PlaygroundIndex,
    settings: SettingsStore,
    registry: ToolRegistry,
    profiles: SandboxProfiles,
    blob: BlobStore,
    account: Account,
    approval_selectors: tuple[str, ...],
) -> tuple[str, list[RunId]]:
    """Publish the review agent and run its four conversations. Returns the
    agent id and the Run ids, oldest first."""
    scope = Scope(tenant=account.id, principal=account.id)
    spec = psych_runtime.AgentSpec(
        name=DEMO_AGENT_NAME,
        description="A review agent whose conversations show every code-execution state.",
        instructions=_INSTRUCTIONS,
        model=psych_runtime.ModelRef(model="review-fixture"),
        tools=(psych_runtime.CodeTool(name="lookup_order"),),
        code_execution=psych_runtime.CodeExecution(
            isolation=psych_runtime.IsolationLevel.PROCESS,
            limits=psych_runtime.CodeExecutionLimits(wall_seconds=2.0, cpu_seconds=2.0),
            output=psych_runtime.OutputPolicy(preview_bytes=1_500),
        ),
    )
    version = await psych_runtime.publish(
        store,
        spec,
        context=psych_runtime.ValidationContext(
            registered_tools=registry.names, sandbox_profiles=profiles.names
        ),
    )
    existing = next(
        (
            pointer.agent_id
            for pointer, _entry in await index.list_agents(account.id)
            if pointer.name == DEMO_AGENT_NAME
        ),
        None,
    )
    agent_id = existing or new_agent_id()
    now = datetime.now(UTC)
    await index.record_publish(
        AgentEntry(
            version_hash=version.hash,
            agent_id=agent_id,
            owner=account.id,
            name=spec.name,
            description=spec.description,
            instructions=spec.instructions,
            model=spec.model.model,
            tools=("lookup_order",),
            mcp_servers=(),
            limits=spec.limits.model_dump(),
            suspension=spec.suspension.model_dump(exclude={"may_ask_questions"}),
            code_execution=(
                spec.code_execution.model_dump(mode="json") if spec.code_execution else None
            ),
            published_at=version.published_at,
            approval_selectors=approval_selectors,
        ),
        now=now,
    )
    _ = settings

    run_ids: list[RunId] = []
    for message, program, answer, _label in _CASES:
        model = (
            FakeModel()
            .turn(text="Let me work that out.", tool_calls=[("run_code", {"program": program})])
            .turn(text=answer)
        )
        # Admitted the way a subagent is: the seeder drives this Run itself,
        # immediately and with its own scripted model, so the Worker polling
        # the same store must not also claim it. Two Attempts writing one log
        # is exactly what the Journal refuses, and a demo that races the
        # Worker would fail that way roughly whenever the machine was busy.
        dispatched = await _dispatch(
            store,
            version.hash,
            scope,
            input={"message": message, "end_user_id": account.id},
            nested=True,
        )
        await index.put_run(
            RunEntry(
                run_id=dispatched.run_id,
                agent_id=agent_id,
                branch_id=new_branch_id(),
                conversation_id=new_conversation_id(),
                name=spec.name,
                tenant=scope.tenant,
                started_at=datetime.now(UTC),
                version_hash=version.hash,
                approval_selectors=approval_selectors,
                message=message,
            )
        )
        journal = await Journal.open(store, dispatched.run_id, scope)
        await journal.append(type="attempt_started", worker_id="review-fixture", attempt_number=1)
        runtime = Runtime(
            store=store,
            model=model,
            registry=registry,
            sandboxes=profiles,
            blob=blob,
            approval_selectors=approval_selectors,
        )
        header = await store.get_run(dispatched.run_id)
        assert header is not None
        await runtime(journal, header, AbortSignal())
        # Settled the way any inline executor settles what it drove. Nothing
        # holds a lease on a nested Run, so `Store.release` would match no row
        # and leave a header that disagrees with a log saying this finished;
        # `settle_inline` is addressed by Run and admits only the NESTED state
        # for that reason.
        settled = await store.settle_inline(dispatched.run_id)
        assert settled, "the seeded Run should have been nested and unsettled"
        run_ids.append(dispatched.run_id)
    return agent_id, run_ids
