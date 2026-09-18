"""Regression pins for the bounded, medium and low findings of the runtime review.

Each class names the finding it closes. Small on purpose: these are the
cheapest possible checks that the specific behaviour cannot quietly return.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import psych_runtime
from psych_runtime.core.code_execution import Enforcement
from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.errors import (
    DeadlineExceeded,
    LeaseLost,
    RunNotFound,
    StoreError,
    SuspensionExpired,
)
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import ModelCallFinished, ToolCallStarted
from psych_runtime.core.reducer import reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, ModelRef
from psych_runtime.model.transient import RETRYABLE_STATUS_CODES, retry_delay_seconds
from psych_runtime.runtime.stream import subscribe
from psych_runtime.sandbox.protocol import ReadyFrame
from psych_runtime.store.blob import BlobKey
from psych_runtime.store.blob_fs import FilesystemBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.logs import LogBuilder
from psych_runtime.tools.a2a import a2a_task_ids
from psych_runtime.tools.oauth.errors import InvalidCanonicalUri
from psych_runtime.tools.oauth.pkce import canonicalize_resource_uri, is_tls_or_loopback

pytestmark = pytest.mark.unit


class TestH3TheReducerChecksTurnAndScopeStamps:
    def test_a_model_call_finished_in_another_turn_is_corruption(self) -> None:
        records = LogBuilder().admitted().attempt().turn().model_started().model_finished().records
        finished = next(r for r in records if isinstance(r, ModelCallFinished))
        records[records.index(finished)] = finished.model_copy(update={"turn": 2})
        with pytest.raises(CorruptLog) as caught:
            reduce(records)
        assert caught.value.reason is CorruptionReason.UNKNOWN_OPERATION
        assert "turn 2" in caught.value.detail

    def test_a_tool_call_stamped_with_the_wrong_turn_is_corruption(self) -> None:
        records = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", "refund")
            .records
        )
        started = next(r for r in records if isinstance(r, ToolCallStarted))
        records[records.index(started)] = started.model_copy(update={"turn": 7})
        with pytest.raises(CorruptLog):
            reduce(records)

    def test_a_record_under_another_tenants_scope_is_corruption(self) -> None:
        records = LogBuilder().admitted().attempt().turn().model_started().records
        last = records[-1]
        records[-1] = last.model_copy(update={"scope": Scope(tenant="globex", principal="p")})
        with pytest.raises(CorruptLog) as caught:
            reduce(records)
        assert "scope" in caught.value.detail

    def test_a_consistent_log_still_folds(self) -> None:
        records = (
            LogBuilder()
            .admitted()
            .attempt()
            .turn()
            .model_started()
            .model_finished()
            .tool_started("call-1", "refund")
            .tool_finished("call-1")
            .records
        )
        assert reduce(records).turn == 1


class TestH5TheWindowsBackendNeverClaimsFilesystemContainment:
    def test_an_unreadable_canary_is_not_enforcement(self) -> None:
        windows = pytest.importorskip("psych_runtime.sandbox.windows")
        graded = windows._grade(
            ReadyFrame(network_denied=True, canary_readable=False), network_granted=False
        )
        assert graded.filesystem is Enforcement.UNAVAILABLE
        assert graded.network is Enforcement.ENFORCED


class TestM5ProcessCountFollowsTheIdentityGrade:
    def test_an_unverified_privilege_drop_leaves_process_count_unverified(self) -> None:
        subprocess = pytest.importorskip("psych_runtime.sandbox.subprocess")
        graded = subprocess._grade(
            ReadyFrame(network_denied=True, canary_readable=None, uid=None),
            run_as=(65534, 65534),
            network_granted=False,
            seatbelt=False,
        )
        assert graded.identity is Enforcement.UNVERIFIED
        assert graded.process_count is Enforcement.UNVERIFIED

    def test_a_verified_drop_keeps_process_count_enforced(self) -> None:
        subprocess = pytest.importorskip("psych_runtime.sandbox.subprocess")
        graded = subprocess._grade(
            ReadyFrame(network_denied=True, canary_readable=None, uid=65534),
            run_as=(65534, 65534),
            network_granted=False,
            seatbelt=False,
        )
        assert graded.identity is Enforcement.ENFORCED
        assert graded.process_count is Enforcement.ENFORCED


class TestM7OnlyThisRunsTasksAreContinued:
    def test_task_ids_are_read_from_a2a_results_only(self) -> None:
        results = [
            SimpleNamespace(result={"peer": "research", "task_id": "t-1", "text": "x"}),
            SimpleNamespace(result={"peer": "research", "task_id": None}),
            SimpleNamespace(result={"task_id": "not-a2a"}),
            SimpleNamespace(result="plain text"),
            SimpleNamespace(result={"peer": "other", "task_id": "t-2"}),
        ]
        assert a2a_task_ids(results) == frozenset({"t-1", "t-2"})


class TestM8OAuthRequiresTls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://as.example.com/token",
            "http://localhost:8080/token",
            "http://127.0.0.1:9000/mcp",
            "http://[::1]:9000/mcp",
            "http://dev.localhost/x",
        ],
    )
    def test_https_and_loopback_are_accepted(self, url: str) -> None:
        assert is_tls_or_loopback(url)

    @pytest.mark.parametrize(
        "url", ["http://as.example.com/token", "http://10.0.0.5/mcp", "ftp://x.example.com/"]
    )
    def test_plain_http_to_a_remote_host_is_not(self, url: str) -> None:
        assert not is_tls_or_loopback(url)

    def test_a_plain_http_resource_is_refused_at_canonicalisation(self) -> None:
        with pytest.raises(InvalidCanonicalUri, match="TLS"):
            canonicalize_resource_uri("http://mcp.example.com/server")

    def test_a_loopback_resource_still_canonicalises(self) -> None:
        assert (
            canonicalize_resource_uri("HTTP://127.0.0.1:8000/mcp/") == "http://127.0.0.1:8000/mcp"
        )


class TestM9BlobWritesLeaveNoPartialFiles:
    async def test_no_temporary_files_survive_a_put(self, tmp_path: Path) -> None:
        store = FilesystemBlobStore(tmp_path)
        key = BlobKey(tenant="acme", run_id="run_1", call_id="call_1")  # type: ignore[arg-type]
        await store.put(key, b"payload", content_type="text/plain", metadata={"k": "v"})
        names = sorted(p.name for p in (tmp_path / "acme" / "run_1").iterdir())
        assert names == ["call_1", "call_1.meta.json"]
        assert await store.get(key) == b"payload"
        head = await store.head(key)
        assert head is not None
        assert head.content_type == "text/plain"

    async def test_a_second_put_replaces_atomically(self, tmp_path: Path) -> None:
        store = FilesystemBlobStore(tmp_path)
        key = BlobKey(tenant="acme", run_id="run_1", call_id="call_1")  # type: ignore[arg-type]
        await store.put(key, b"one", content_type="text/plain")
        await store.put(key, b"two", content_type="text/plain")
        assert await store.get(key) == b"two"
        assert sorted(p.name for p in (tmp_path / "acme" / "run_1").iterdir()) == [
            "call_1",
            "call_1.meta.json",
        ]


class TestL2ASubscriptionThatDiesIsLogged:
    async def test_the_failure_is_logged_when_the_task_ends(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryStore()
        version = await psych_runtime.publish(
            store,
            AgentSpec(name="s", instructions="Help.", model=ModelRef(model="fake-standard")),
        )
        run = await psych_runtime.dispatch(store, version, Scope(tenant="acme", principal="p"))

        async def on_record(record: object) -> None:
            raise ValueError("the bridge broke")

        with caplog.at_level(logging.ERROR, logger="psych.runtime.stream"):
            task = subscribe(store, run.run_id, on_record)
            await asyncio.wait([task], timeout=10)
            # The done callback runs after the task settles; let the loop deliver it.
            await asyncio.sleep(0)
        assert task.done()
        assert isinstance(task.exception(), ValueError)
        assert any("ended with an error" in r.message for r in caplog.records)


class TestL9PublicNamesAreConstructible:
    def test_the_error_types_carry_what_their_docstrings_promise(self) -> None:
        run_id = RunId("run_1")
        assert "run_1" in str(SuspensionExpired(run_id, "approval"))
        assert isinstance(LeaseLost(run_id, "wrk_1"), Exception)
        assert isinstance(DeadlineExceeded(run_id, 30.0), Exception)
        assert isinstance(StoreError("boom"), Exception)
        assert issubclass(RunNotFound, StoreError)

    def test_the_code_execution_enums_and_protocols_are_importable(self) -> None:
        from psych_runtime.core.code_execution import ArtifactCollection, WorkspacePolicy
        from psych_runtime.sandbox.port import HostBinding
        from psych_runtime.sandbox.profiles import CodeExecutionPolicy

        assert list(WorkspacePolicy)
        assert list(ArtifactCollection)
        assert HostBinding is not None
        assert CodeExecutionPolicy is not None


class TestL10AndL11Transient:
    def test_409_is_not_retried(self) -> None:
        assert 409 not in RETRYABLE_STATUS_CODES
        assert 429 in RETRYABLE_STATUS_CODES

    def test_the_backoff_exponent_is_clamped(self) -> None:
        huge = retry_delay_seconds(10_000)
        assert huge == retry_delay_seconds(100)
        assert huge <= 30.0
