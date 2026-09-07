"""End to end: a large tool result, elided, and read back by handle.

DESIGN.md §10.8: a result over the threshold is written to the log in full and
elided in the model's context; ``read_tool_output`` reads it back. Driven
through ``AgentLoop`` and ``FakeModel``, the same shape as
``tests/e2e/test_agent_run.py``, so the assertions go through the log rather
than through internal state.

## A historical note on the classes below

``TestReadingALargeResultByHandle`` and ``TestReadingAHandleFromAnotherRun``
wire ``read_tool_output`` in locally, through a ``ToolResolver`` subclass and a
``ToolExecutor`` ``mcp_caller``, from a time when ``AgentLoop`` did not yet do
this natively. It now does (``_turn`` already offers
``read_tool_output_tools(state)`` and ``_execute_and_record`` already routes
``READ_TOOL_OUTPUT`` calls to ``psych_runtime.tools.large_results`` directly), so the
local wiring below is redundant rather than load-bearing; it is left in place
because it still passes and rewriting passing tests is not this ticket's job.
``TestOffloadAcrossStoreAdapters`` below, added later, uses the native path
with no local wiring, which is now the shape every new test should use.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.ids import RunId, ToolCallId, new_run_id
from psych_runtime.core.messages import ToolResultMessage
from psych_runtime.core.records import RECORD_ADAPTER, TerminalState, ToolCallFinished
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, CodeTool, Limits, ModelRef
from psych_runtime.core.version import publish
from psych_runtime.runtime.agent import DEFAULT_OFFLOAD_BYTES, AgentLoop, ToolExecutor
from psych_runtime.runtime.journal import Journal
from psych_runtime.store.blob import blob_key
from psych_runtime.store.blob_memory import InMemoryBlobStore
from psych_runtime.store.dynamodb import DynamoDBStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.mysql import MySQLStore
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.store.postgres import PostgresStore
from psych_runtime.testing.fake_model import FakeModel, ToolCallScript
from psych_runtime.tools.large_results import TOOL_NAME, read_tool_output, read_tool_output_tools
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ResolvedTools, ToolResolver

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

_BIG_CALL_ID = ToolCallId("call-big-1")
_HANDLE = "res_call-big-1"


def build_spec(**kwargs: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer read a long report.",
        "model": ModelRef(model="fake-standard"),
        "tools": (CodeTool(name="big_report"),),
        # Between the read_tool_output response for a small window (~280
        # bytes) and a 500-row big_report result (~13,000 bytes): the report
        # elides, a small read of it does not, so a turn later in this test
        # can assert on real content rather than on a second handle.
        "limits": Limits(large_result_bytes=500),
    }
    base.update(kwargs)
    return AgentSpec(**base)


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def big_report(rows: int) -> str:
        """Return a long, line-oriented report."""
        return "\n".join(f"line {i} of the big result" for i in range(rows))

    return registry


class _ResolverWithReader(ToolResolver):
    """Adds ``read_tool_output`` to the offered set when the Run has one to
    read, the way ``AgentLoop`` should call ``read_tool_output_tools`` and
    pass it as ``extra=`` (see the module docstring)."""

    def __init__(self, registry: ToolRegistry, journal: Journal) -> None:
        super().__init__(registry)
        self._journal = journal

    async def resolve(
        self,
        spec: Any,
        scope: Scope,
        *,
        failure_streaks: Any = None,
        extra: Any = (),
    ) -> ResolvedTools:
        combined = (*extra, *read_tool_output_tools(self._journal.state))
        return await super().resolve(spec, scope, failure_streaks=failure_streaks, extra=combined)


def _mcp_caller(journal: Journal) -> Any:
    """Routes ``read_tool_output`` to this module; anything else is an error,
    the same as a Run with no MCP servers configured at all."""

    async def caller(name: str, arguments: dict[str, Any]) -> Any:
        if name == TOOL_NAME:
            return read_tool_output(journal.state, arguments)
        raise AccessDenied(f"tool {name!r}", "no mcp server is configured for this run")

    return caller


async def start_run(store: Store, spec: AgentSpec, message: str = "read me the report") -> RunId:
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
        RECORD_ADAPTER.validate_python(
            {
                "type": "run_admitted",
                "run_id": run_id,
                "seq": 1,
                "at": now,
                "scope": SCOPE,
                "version_hash": version.hash,
                "input": {"message": message},
                "deadline_at": now + timedelta(seconds=spec.limits.deadline_seconds),
            }
        ),
    )
    return run_id


async def make_loop(
    store: Store, run_id: RunId, spec: AgentSpec, model: FakeModel, registry: ToolRegistry
) -> AgentLoop:
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    return AgentLoop(
        journal,
        spec,
        model,
        _ResolverWithReader(registry, journal),
        ToolExecutor(registry, mcp_caller=_mcp_caller(journal)),
    )


class TestReadingALargeResultByHandle:
    async def test_the_model_reads_a_window_of_a_result_it_could_not_see_whole(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(
                text="Let me pull that up.",
                tool_calls=[
                    ToolCallScript(name="big_report", arguments={"rows": 500}, call_id=_BIG_CALL_ID)
                ],
            )
            .turn(
                text="Let me read the middle of it.",
                tool_calls=[
                    ("read_tool_output", {"handle": _HANDLE, "offset": 10, "limit": 2}),
                ],
            )
            .turn(text="Lines 10 and 11 say what you asked about.")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())

        outcome = await loop.run()

        assert outcome.state is TerminalState.COMPLETED

        # The log holds the whole report, in full, regardless of the handle.
        records = await store.read(run_id)
        big_finished = next(
            r for r in records if r.type == "tool_call_finished" and r.call_id == _BIG_CALL_ID
        )
        full_report = "\n".join(f"line {i} of the big result" for i in range(500))
        assert big_finished.result == full_report
        assert big_finished.result_handle == _HANDLE

        # The model's second-turn context never held the full report: it saw
        # the handle and a short preview, not 500 lines.
        second_request_tool_message = next(
            m for m in model.requests[1].messages if isinstance(m, ToolResultMessage)
        )
        assert _HANDLE in second_request_tool_message.content
        assert len(second_request_tool_message.content) < len(full_report)
        assert "line 499 of the big result" not in second_request_tool_message.content

        # The third turn's context is what read_tool_output actually returned:
        # the two requested lines, not the whole report and not another handle.
        third_request_tool_message = next(
            m
            for m in model.requests[2].messages
            if isinstance(m, ToolResultMessage) and m.name == TOOL_NAME
        )
        assert "line 10 of the big result" in third_request_tool_message.content
        assert "line 11 of the big result" in third_request_tool_message.content
        assert "line 0 of the big result" not in third_request_tool_message.content
        assert "line 499 of the big result" not in third_request_tool_message.content
        assert len(third_request_tool_message.content) < len(full_report)

    async def test_the_tool_is_not_offered_before_any_result_is_stored(self) -> None:
        """The very first turn has nothing to read yet."""
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(
                tool_calls=[
                    ToolCallScript(name="big_report", arguments={"rows": 5}, call_id=_BIG_CALL_ID)
                ]
            )
            .turn(text="done")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        first_request = model.requests[0]
        assert TOOL_NAME not in {tool.name for tool in first_request.tools}

    async def test_the_tool_is_offered_once_a_large_result_exists(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(
                tool_calls=[
                    ToolCallScript(name="big_report", arguments={"rows": 500}, call_id=_BIG_CALL_ID)
                ]
            )
            .turn(text="done")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        second_request = model.requests[1]
        assert TOOL_NAME in {tool.name for tool in second_request.tools}

    async def test_a_small_result_is_never_elided_and_never_offers_the_reader(self) -> None:
        store = InMemoryStore()
        spec = build_spec()
        model = (
            FakeModel()
            .turn(tool_calls=[("big_report", {"rows": 1})])
            .turn(text="short report, thanks")
        )
        run_id = await start_run(store, spec)
        loop = await make_loop(store, run_id, spec, model, build_registry())
        await loop.run()

        records = await store.read(run_id)
        finished = next(r for r in records if r.type == "tool_call_finished")
        assert finished.result_handle is None

        second_request = model.requests[1]
        assert TOOL_NAME not in {tool.name for tool in second_request.tools}


class TestReadingAHandleFromAnotherRun:
    async def test_a_run_cannot_read_a_handle_it_never_issued(self) -> None:
        """A handle another Run minted, called against this Run's state, fails
        as an access problem: it is data this Run's log never produced."""
        store = InMemoryStore()
        spec = build_spec()
        run_id = await start_run(store, spec)
        journal = await Journal.open(store, run_id, SCOPE)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)

        with pytest.raises(AccessDenied, match="another Run"):
            read_tool_output(journal.state, {"handle": "res_from_a_different_run"})


# ---------------------------------------------------------------------------
# A large tool result must not kill the Run on DynamoDB.
# ---------------------------------------------------------------------------


def _big_csv_spec() -> AgentSpec:
    return AgentSpec(
        name="report-reader",
        instructions="Fetch a big export and answer a question about it.",
        model=ModelRef(model="fake-standard"),
        tools=(CodeTool(name="big_export"),),
        # Left at the default (32,768): the offload threshold this test cares
        # about is far above it, and this Spec elides long before it offloads,
        # exactly as any real Spec would.
        limits=Limits(),
    )


def _big_csv_content(rows: int) -> str:
    return "\n".join(f"row-{i},value-{i}" for i in range(rows))


def _big_csv_registry(content: str) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register
    def big_export() -> str:
        """Return a large CSV-shaped export."""
        return content

    return registry


async def _make_native_loop(
    store: Store,
    run_id: RunId,
    spec: AgentSpec,
    model: FakeModel,
    registry: ToolRegistry,
    *,
    blob_store: InMemoryBlobStore,
) -> AgentLoop:
    """An ``AgentLoop`` with no local ``read_tool_output`` wiring: this is the
    shape the native wiring makes possible, using exactly what ``psych_runtime.runtime.execute``
    wires up for a real Worker rather than the pre-native-wiring seams the
    other test classes in this file use.
    """
    journal = await Journal.open(store, run_id, SCOPE)
    await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
    return AgentLoop(
        journal,
        spec,
        model,
        ToolResolver(registry),
        ToolExecutor(registry),
        blob=blob_store,
        blob_offload_bytes=DEFAULT_OFFLOAD_BYTES,
    )


@pytest_asyncio.fixture(params=["memory", "postgres", "mysql", "dynamodb"])
async def real_store(request: pytest.FixtureRequest) -> AsyncIterator[Store]:
    """The same headline scenario against all four store adapters.

    Each non-memory backend is requested through the same skip-guarded
    fixtures the store contract suites use (``postgres_dsn``, ``mysql_dsn``,
    ``dynamodb_endpoint`` in ``tests/conftest.py``), fetched with
    ``getfixturevalue`` rather than as a normal parameter so this one fixture
    can dispatch on ``request.param`` instead of pytest needing four separate
    fixtures wired into four separate test functions for what is one scenario.
    """
    kind = request.param
    if kind == "memory":
        yield InMemoryStore()
        return

    if kind == "postgres":
        dsn = request.getfixturevalue("postgres_dsn")
        pg_store = PostgresStore(dsn=dsn)
        await pg_store.migrate()
        try:
            yield pg_store
        finally:
            await pg_store.close()
        return

    if kind == "mysql":
        dsn = request.getfixturevalue("mysql_dsn")
        mysql_store = MySQLStore(dsn=dsn)
        await mysql_store.migrate()
        try:
            yield mysql_store
        finally:
            await mysql_store.close()
        return

    endpoint = request.getfixturevalue("dynamodb_endpoint")
    prefix = f"psych-e2e-{uuid.uuid4().hex[:16]}"
    dynamo_store = DynamoDBStore(
        endpoint_url=endpoint,
        table_prefix=prefix,
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )
    await dynamo_store.ensure_tables()
    try:
        yield dynamo_store
    finally:
        await dynamo_store.drop_tables()
        await dynamo_store.close()


class TestOffloadAcrossStoreAdapters:
    """The literal acceptance criterion: a 5MB tool result round-trips through
    a Run on all four store adapters, including DynamoDB, without the item
    size that kills a raw write ever reaching the Store at all.
    """

    async def test_a_five_megabyte_result_round_trips(self, real_store: Store) -> None:
        call_id = ToolCallId("call-big")
        rows = 250_000  # "row-i,value-i\n" averages ~24 bytes; ~6MB total.
        full_content = _big_csv_content(rows)
        assert len(full_content.encode("utf-8")) > 5 * 1024 * 1024, "fixture must exceed 5MB"
        registry = _big_csv_registry(full_content)
        blob_store = InMemoryBlobStore()

        spec = _big_csv_spec()
        model = (
            FakeModel()
            .turn(
                text="Pulling the export now.",
                tool_calls=[ToolCallScript(name="big_export", arguments={}, call_id=call_id)],
            )
            .turn(
                text="Reading the middle of it.",
                tool_calls=[
                    ("read_tool_output", {"handle": "res_call-big", "offset": 5, "limit": 2})
                ],
            )
            .turn(text="Rows 5 and 6 are what you asked about.")
        )

        run_id = await start_run(real_store, spec)
        loop = await _make_native_loop(
            real_store, run_id, spec, model, registry, blob_store=blob_store
        )

        outcome = await loop.run()
        assert outcome.state is TerminalState.COMPLETED

        # The Store's own record never held the payload: exactly the write
        # that fails against real DynamoDB above ~400KB never happened.
        records = await real_store.read(run_id)
        finished = next(
            r for r in records if isinstance(r, ToolCallFinished) and r.call_id == call_id
        )
        assert finished.result is None
        assert finished.result_blob_key is not None
        assert finished.result_content_type == "text/plain; charset=utf-8"
        assert finished.result_bytes == len(full_content.encode("utf-8"))
        # A record this small comfortably clears DynamoDB's 400KB item cap,
        # which is the whole point: the reference is tiny even though the
        # result it points at is not.
        assert len(RECORD_ADAPTER.dump_json(finished)) < 10_000

        # Nothing was lost: the blob holds exactly what the tool returned,
        # fetched by rebuilding the key the same way read_tool_output_async
        # does (from this Run's own scope and run id), never by trusting the
        # stored reference string as an address.
        key = blob_key(SCOPE, run_id, call_id)
        stored_bytes = await blob_store.get(key)
        assert stored_bytes.decode("utf-8") == full_content

        # The model's own context never saw the whole thing either.
        second_request_tool_message = next(
            m for m in model.requests[1].messages if isinstance(m, ToolResultMessage)
        )
        assert len(second_request_tool_message.content) < 10_000
        assert "res_call-big" in second_request_tool_message.content

        # But read_tool_output, reading transparently through the blob store,
        # answers the exact window asked for.
        third_request_tool_message = next(
            m
            for m in model.requests[2].messages
            if isinstance(m, ToolResultMessage) and m.name == TOOL_NAME
        )
        assert "row-5,value-5" in third_request_tool_message.content
        assert "row-6,value-6" in third_request_tool_message.content
        assert "row-4,value-4" not in third_request_tool_message.content
        assert "row-7,value-7" not in third_request_tool_message.content
