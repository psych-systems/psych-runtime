"""End to end: an agent loads a skill and remembers a fact.

DESIGN.md §15, §16. Both built-ins go through the same path every other tool
does: recorded before they run (DESIGN.md §8.4), visible in the log, and
readable by ``psych_runtime.report``. This test drives that through a real
``AgentLoop`` against ``FakeModel``, the way ``tests/e2e/test_agent_run.py``
drives ordinary tool calls, rather than asserting against ``load_skill`` or
``remember`` in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from psych_runtime.core.ids import RunId, new_run_id
from psych_runtime.core.records import TerminalState, ToolCallFinished, ToolOutcome
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef, Skill
from psych_runtime.core.version import publish
from psych_runtime.memory.store_backed import StoreBackedMemory
from psych_runtime.runtime.agent import AgentLoop, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.builtins import BuiltinToolResolver, register_builtins
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="support-bot")
END_USER_ID = "end-user-42"

REFUND_SKILL = Skill(
    name="refunds",
    description="How to process a refund for a shipped order.",
    body=(
        "Confirm the order shipped, then issue a full refund to the original "
        "payment method. See also [[skill:escalation]] if the customer disputes it."
    ),
)
ESCALATION_SKILL = Skill(
    name="escalation",
    description="When and how to escalate a refund dispute.",
    body="Escalate to a human if the customer disputes the refund amount.",
)


def build_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer with their refund.",
        "model": ModelRef(model="fake-standard"),
        "skills": (REFUND_SKILL, ESCALATION_SKILL),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def _record(kind: str, **fields: Any) -> Any:
    from psych_runtime.core.records import RECORD_ADAPTER

    return RECORD_ADAPTER.validate_python(
        {"type": kind, "at": datetime.now(UTC), "scope": SCOPE, **fields}
    )


async def start_run(store: InMemoryStore, spec: AgentSpec, message: str) -> RunId:
    version = publish(spec)
    await store.put_version(version)
    run_id = new_run_id()
    now = datetime.now(UTC)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=SCOPE,
            version_hash=version.hash,
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=now + timedelta(seconds=spec.limits.deadline_seconds),
        )
    )
    await store.append(
        run_id,
        1,
        _record(
            "run_admitted",
            run_id=run_id,
            seq=1,
            version_hash=version.hash,
            input={"message": message},
            deadline_at=now + timedelta(seconds=spec.limits.deadline_seconds),
        ),
    )
    return run_id


async def make_loop(
    store: InMemoryStore,
    run_id: RunId,
    spec: AgentSpec,
    model: FakeModel,
    registry: ToolRegistry,
    *,
    resolver: ToolResolver,
    memories: tuple[str, ...] = (),
) -> AgentLoop:
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    return AgentLoop(
        journal,
        spec,
        model,
        resolver,
        ToolExecutor(registry),
        memories=memories,
    )


class TestASkillLoadAndAMemoryWrite:
    async def test_the_agent_loads_a_skill_and_remembers_a_fact(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        registry = ToolRegistry()
        memory = StoreBackedMemory(store)

        definitions = register_builtins(
            registry, spec, SCOPE, memory=memory, end_user_id=END_USER_ID
        )
        resolver = BuiltinToolResolver(ToolResolver(registry), definitions)

        model = (
            FakeModel()
            .turn(
                text="Let me check how to handle this.",
                tool_calls=[("load_skill", {"name": "refunds"})],
            )
            .turn(
                text="Noting that for next time.",
                tool_calls=[
                    (
                        "remember",
                        {"content": "prefers refunds credited to store balance"},
                    )
                ],
            )
            .turn(text="Your refund is on its way.")
        )
        run_id = await start_run(store, spec, "I'd like a refund on order A1")
        loop = await make_loop(store, run_id, spec, model, registry, resolver=resolver)

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        assert outcome.output == {"text": "Your refund is on its way."}

        # Both built-ins are ordinary tool calls in the log: recorded before
        # they run, and readable by anything that projects over the log,
        # including psych_runtime.report (DESIGN.md §8.4).
        state = reduce(await store.read(run_id))
        tool_names = [result.tool for result in state.tool_results]
        assert tool_names == ["load_skill", "remember"]
        assert all(result.outcome is ToolOutcome.OK for result in state.tool_results)

        skill_result = state.tool_results[0].result
        assert skill_result["found"] is True
        assert "issue a full refund" in skill_result["body"]
        assert skill_result["already_loaded"] is False
        # The body links to `escalation`; the model is told it can load it too.
        assert skill_result["linked_skills"] == ["escalation"]

        remember_result = state.tool_results[1].result
        assert remember_result == {
            "remembered": True,
            "content": "prefers refunds credited to store balance",
        }

        # The fact actually landed in the InMemoryStore, isolated to this end user.
        facts = await memory.recall(SCOPE, END_USER_ID)
        assert [fact.content for fact in facts] == ["prefers refunds credited to store balance"]

    async def test_a_second_run_sees_the_memory_injected_into_its_prompt(self) -> None:
        """DESIGN.md §15: memories are injected into the prompt. This proves
        the loop actually receives what a caller recalled, at the position
        psych_runtime.model.prompt fixes, by asserting on what FakeModel recorded it
        was sent, the same mechanism test_agent_run.py uses to prove the tool
        set is recomputed each turn."""
        store = InMemoryStore()
        spec = build_spec()
        memory = StoreBackedMemory(store)
        await memory.remember(SCOPE, END_USER_ID, "prefers refunds credited to store balance")

        registry = ToolRegistry()
        definitions = register_builtins(
            registry, spec, SCOPE, memory=memory, end_user_id=END_USER_ID
        )
        resolver = BuiltinToolResolver(ToolResolver(registry), definitions)

        recalled = tuple(fact.content for fact in await memory.recall(SCOPE, END_USER_ID))
        model = FakeModel().turn(text="Welcome back.")
        run_id = await start_run(store, spec, "hi again")
        loop = await make_loop(
            store, run_id, spec, model, registry, resolver=resolver, memories=recalled
        )

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        system_message = model.last_request.messages[0]
        assert "prefers refunds credited to store balance" in system_message.content
        # Cache-deliberate ordering (DESIGN.md §19): the skills index precedes
        # the memories block in the assembled system prompt.
        skills_at = system_message.content.index("## Skills")
        memories_at = system_message.content.index("## What you remember")
        assert skills_at < memories_at

    async def test_loading_the_same_skill_twice_says_so_and_still_returns_it(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        registry = ToolRegistry()
        definitions = register_builtins(registry, spec, SCOPE)
        resolver = BuiltinToolResolver(ToolResolver(registry), definitions)

        model = (
            FakeModel()
            .turn(tool_calls=[("load_skill", {"name": "refunds"})])
            .turn(tool_calls=[("load_skill", {"name": "refunds"})])
            .turn(text="done")
        )
        run_id = await start_run(store, spec, "help")
        loop = await make_loop(store, run_id, spec, model, registry, resolver=resolver)

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        state = reduce(await store.read(run_id))
        second_call = state.tool_results[1].result
        assert second_call["already_loaded"] is True
        assert "issue a full refund" in second_call["body"]

    async def test_an_unknown_skill_name_is_a_tool_result_not_a_failed_run(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        registry = ToolRegistry()
        definitions = register_builtins(registry, spec, SCOPE)
        resolver = BuiltinToolResolver(ToolResolver(registry), definitions)

        model = (
            FakeModel().turn(tool_calls=[("load_skill", {"name": "nonexistent"})]).turn(text="done")
        )
        run_id = await start_run(store, spec, "help")
        loop = await make_loop(store, run_id, spec, model, registry, resolver=resolver)

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED
        records = await store.read(run_id)
        finished = next(r for r in records if isinstance(r, ToolCallFinished))
        assert finished.outcome is ToolOutcome.OK  # the lookup succeeded; the miss is data
        assert finished.result["found"] is False
        assert finished.result["available_skills"] == ["escalation", "refunds"]
