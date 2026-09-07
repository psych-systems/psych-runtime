"""The read_tool_output reader against a real Store and a real log.

DESIGN.md §22: a functional test runs a component against a real adapter, never
a mock. Every state here is derived by actually appending records through
``psych_runtime.runtime.journal.Journal`` onto a real ``psych_runtime.store.memory.InMemoryStore``
and reading them back, rather than constructed in memory and handed to the
reducer directly (that is what the unit tests do).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId, ToolCallId, VersionHash, new_run_id
from psych_runtime.core.records import RECORD_ADAPTER
from psych_runtime.core.reducer import RunStateView
from psych_runtime.core.scope import Scope
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState
from psych_runtime.tools.large_results import (
    read_tool_output,
    read_tool_output_tools,
)

pytestmark = pytest.mark.functional

_SCOPE = Scope(tenant="acme", principal="user-1")


async def _new_run(store: InMemoryStore, *, scope: Scope = _SCOPE) -> RunId:
    """Admit a Run directly onto the store, the way ``psych_runtime.runtime.dispatch``
    does: ``run_admitted`` has to exist before a Journal can be opened, because
    ``reduce()`` refuses an empty log with no prior state to continue from."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    await store.create_run(
        RunHeader(
            run_id=run_id,
            scope=scope,
            version_hash=VersionHash("sha256:test"),
            state=RunState.RUNNABLE,
            created_at=now,
            deadline_at=deadline,
        )
    )
    await store.append(
        run_id,
        1,
        RECORD_ADAPTER.validate_python(
            {
                "type": "run_admitted",
                "run_id": run_id,
                "seq": 1,
                "at": now,
                "scope": scope,
                "version_hash": VersionHash("sha256:test"),
                "input": {"message": "go"},
                "deadline_at": deadline,
            }
        ),
    )
    journal = await Journal.open(store, run_id, scope)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    return run_id


async def _store_large_result(
    store: InMemoryStore, run_id: RunId, *, call_id: str, result: object, scope: Scope = _SCOPE
) -> str:
    """Append a tool call whose result is elided, the way AgentLoop would."""
    journal = await Journal.open(store, run_id, scope)
    await journal.append(type="turn_started", turn=journal.state.turn + 1)
    await journal.append(
        type="tool_call_started",
        call_id=ToolCallId(call_id),
        tool="big",
        turn=journal.state.turn,
    )
    handle = f"res_{call_id}"
    rendered = result if isinstance(result, str) else str(result)
    await journal.append(
        type="tool_call_finished",
        call_id=ToolCallId(call_id),
        outcome="ok",
        result=result,
        result_bytes=len(rendered),
        result_handle=handle,
        preview=rendered[:2000],
    )
    return handle


async def _current_state(
    store: InMemoryStore, run_id: RunId, scope: Scope = _SCOPE
) -> RunStateView:
    journal = await Journal.open(store, run_id, scope)
    return journal.state


class TestReadingFromARealLog:
    async def test_a_stored_result_reads_back_through_a_real_journal_and_store(self) -> None:
        store = InMemoryStore()
        run_id = await _new_run(store)
        text = "\n".join(f"row {i}" for i in range(20))
        handle = await _store_large_result(store, run_id, call_id="call-1", result=text)

        state = await _current_state(store, run_id)
        out = read_tool_output(state, {"handle": handle, "offset": 2, "limit": 3})

        assert out["content"] == "row 2\nrow 3\nrow 4"
        assert out["total_lines"] == 20

    async def test_the_log_still_holds_the_whole_result_after_reading_a_slice(self) -> None:
        """Reading a slice must not mutate or truncate what the store holds:
        the reader only ever reads, and the whole result stays recoverable."""
        store = InMemoryStore()
        run_id = await _new_run(store)
        text = "z" * 50_000
        handle = await _store_large_result(store, run_id, call_id="call-1", result=text)

        state = await _current_state(store, run_id)
        read_tool_output(state, {"handle": handle, "limit": 1})

        records = await store.read(run_id)
        finished = next(r for r in records if r.type == "tool_call_finished")
        assert finished.result == text
        assert len(finished.result) == 50_000

    async def test_a_second_worker_reclaiming_the_run_can_still_read_the_handle(self) -> None:
        """Crash recovery: a fresh Journal.open (a different Worker's view) folds
        the same log and resolves the same handle, because the handle lives in
        the store's log, not in one process's memory."""
        store = InMemoryStore()
        run_id = await _new_run(store)
        handle = await _store_large_result(store, run_id, call_id="call-1", result="a" * 5000)

        # A different Journal.open, standing in for a Worker that reclaimed
        # this Run after a crash and never saw the first Worker's in-memory
        # state at all.
        reclaiming_journal = await Journal.open(store, run_id, _SCOPE)
        out = read_tool_output(reclaiming_journal.state, {"handle": handle})

        assert out["binary"] is False
        assert out["total_size_bytes"] == 5000


class TestOfferingAgainstARealLog:
    async def test_a_fresh_run_with_no_large_result_offers_nothing(self) -> None:
        store = InMemoryStore()
        run_id = await _new_run(store)
        state = await _current_state(store, run_id)
        assert read_tool_output_tools(state) == ()

    async def test_a_run_with_a_stored_result_offers_the_tool(self) -> None:
        store = InMemoryStore()
        run_id = await _new_run(store)
        await _store_large_result(store, run_id, call_id="call-1", result="x" * 5000)
        state = await _current_state(store, run_id)
        assert [t.name for t in read_tool_output_tools(state)] == ["read_tool_output"]


class TestCrossRunIsolation:
    """DESIGN.md §6: a handle must never resolve across Runs. Two real Runs in
    the same real store, not a hand-built state, so this proves the isolation
    holds through an actual store read rather than through how a test happens
    to construct its fixtures."""

    async def test_a_handle_from_another_run_in_the_same_store_is_access_denied(self) -> None:
        store = InMemoryStore()
        run_a = await _new_run(store)
        run_b = await _new_run(store)

        handle_a = await _store_large_result(store, run_a, call_id="call-a-1", result="a" * 5000)
        await _store_large_result(store, run_b, call_id="call-b-1", result="b" * 5000)

        state_b = await _current_state(store, run_b)
        with pytest.raises(AccessDenied, match="another Run"):
            read_tool_output(state_b, {"handle": handle_a})

    async def test_the_same_call_id_in_two_runs_still_does_not_cross_resolve(self) -> None:
        """Both runs use call_id "call-1", so both mint the literal handle
        "res_call-1". Resolution must still be scoped to the state's own Run,
        not to the handle string coincidentally matching."""
        store = InMemoryStore()
        run_a = await _new_run(store)
        run_b = await _new_run(store)

        await _store_large_result(store, run_a, call_id="call-1", result="from a")
        await _store_large_result(store, run_b, call_id="call-1", result="from b, longer text")

        state_a = await _current_state(store, run_a)
        state_b = await _current_state(store, run_b)

        out_a = read_tool_output(state_a, {"handle": "res_call-1"})
        out_b = read_tool_output(state_b, {"handle": "res_call-1"})
        assert out_a["content"] == "from a"
        assert out_b["content"] == "from b, longer text"

    async def test_a_different_tenants_run_cannot_be_read_through_another_scope(self) -> None:
        store = InMemoryStore()
        tenant_a_scope = Scope(tenant="tenant-a")
        tenant_b_scope = Scope(tenant="tenant-b")
        run_a = await _new_run(store, scope=tenant_a_scope)
        run_b = await _new_run(store, scope=tenant_b_scope)

        handle_a = await _store_large_result(
            store, run_a, call_id="call-1", result="secret", scope=tenant_a_scope
        )
        await _store_large_result(
            store, run_b, call_id="call-2", result="other", scope=tenant_b_scope
        )

        state_b = await _current_state(store, run_b, tenant_b_scope)
        with pytest.raises(AccessDenied):
            read_tool_output(state_b, {"handle": handle_a})


class TestBinaryAndStructuredResultsThroughAStore:
    async def test_bytes_survive_a_real_store_round_trip_and_report_as_binary(self) -> None:
        store = InMemoryStore()
        run_id = await _new_run(store)
        payload = bytes(range(256)) * 20
        journal = await Journal.open(store, run_id, _SCOPE)
        await journal.append(type="turn_started", turn=1)
        await journal.append(
            type="tool_call_started", call_id=ToolCallId("call-1"), tool="download", turn=1
        )
        await journal.append(
            type="tool_call_finished",
            call_id=ToolCallId("call-1"),
            outcome="ok",
            result=payload,
            result_bytes=len(payload),
            result_handle="res_call-1",
            preview="",
        )

        state = await _current_state(store, run_id)
        out = read_tool_output(state, {"handle": "res_call-1"})

        assert out["binary"] is True
        assert out["total_size_bytes"] == len(payload)

    async def test_a_structured_result_survives_a_real_store_round_trip(self) -> None:
        store = InMemoryStore()
        run_id = await _new_run(store)
        result = {"orders": [{"id": i, "status": "shipped"} for i in range(100)]}
        handle = await _store_large_result(store, run_id, call_id="call-1", result=result)

        state = await _current_state(store, run_id)
        out = read_tool_output(state, {"handle": handle, "limit": 5})

        assert out["binary"] is False
        assert out["content"].startswith("{")
