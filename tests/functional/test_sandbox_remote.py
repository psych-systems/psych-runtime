"""``RemoteSandbox`` against a real sandbox service on loopback.

The service is ``psych_runtime.testing.sandbox_service.SandboxService``: a
genuine HTTP/1.1 server on ``127.0.0.1`` speaking the documented protocol,
wrapping either the host's real local backend (so the contract suite runs
end to end through the network) or a scripted double (so every failure a
service can produce is exercised deterministically). No test here reaches
anything but loopback, and every request goes through ``HttpTransport``,
the same egress seam the rest of the library uses.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from psych_runtime.core.code_execution import Enforcement, IsolationLevel
from psych_runtime.core.errors import AccessDenied
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport
from psych_runtime.sandbox.contract import SandboxContractSuite
from psych_runtime.sandbox.local import local_sandbox
from psych_runtime.sandbox.port import SandboxFailure, SandboxLimits, SandboxSetupError
from psych_runtime.sandbox.remote import RemoteSandbox
from psych_runtime.testing.sandbox_service import (
    SandboxService,
    ScriptedSandbox,
    scripted_result,
)

pytestmark = pytest.mark.functional

SCOPE = Scope(tenant="deployment")
_LIMITS = SandboxLimits(
    cpu_seconds=3.0,
    address_space_bytes=256 * 1024 * 1024,
    file_size_bytes=1024 * 1024,
    process_count=16,
    wall_seconds=15.0,
)


def _python_bin() -> str | None:
    if sys.platform != "win32" and os.geteuid() == 0:
        for candidate in (
            f"/usr/bin/python{sys.version_info.major}.{sys.version_info.minor}",
            "/usr/bin/python3",
        ):
            if Path(candidate).is_file():
                return candidate
    return None


async def _token() -> str | None:
    return "test-token"


@pytest_asyncio.fixture
async def real_service() -> AsyncIterator[SandboxService]:
    local = local_sandbox(python_bin=_python_bin(), default_limits=_LIMITS)
    async with SandboxService(local, token="test-token") as service:
        yield service


@pytest_asyncio.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    async with HttpTransport() as http:
        yield http


class TestRemoteSandboxContract(SandboxContractSuite):
    """The full contract, end to end through the wire, against the host's
    real local backend behind the service."""

    @pytest_asyncio.fixture
    async def sandbox(
        self, real_service: SandboxService, transport: HttpTransport
    ) -> RemoteSandbox:
        return RemoteSandbox(
            base_url=real_service.base_url,
            transport=transport,
            scope=SCOPE,
            credential=_token,
            default_limits=_LIMITS,
        )


def _scripted(**kwargs: Any) -> ScriptedSandbox:
    return ScriptedSandbox(**kwargs)


async def _remote(
    service: SandboxService, transport: HttpTransport, **kwargs: Any
) -> RemoteSandbox:
    return RemoteSandbox(
        base_url=service.base_url,
        transport=transport,
        scope=SCOPE,
        credential=_token,
        default_limits=_LIMITS,
        **kwargs,
    )


async def _post_execution(
    transport: HttpTransport, service: SandboxService, body: dict[str, Any]
) -> list[dict[str, Any]]:
    """Speak the protocol by hand, to test the service rather than the client."""
    frames: list[dict[str, Any]] = []
    async with transport.stream(
        "POST",
        f"{service.base_url}/v1/executions",
        scope=SCOPE,
        headers={"authorization": "Bearer test-token"},
        json=body,
    ) as response:
        async for line in response.aiter_lines():
            if line.strip():
                frames.append(json.loads(line))
    return frames


class TestDescribe:
    async def test_the_services_description_is_relayed_and_labelled(
        self, transport: HttpTransport
    ) -> None:
        async with SandboxService(_scripted(), token="test-token") as service:
            remote = await _remote(service, transport)
            description = await remote.describe()
        assert description.ready
        assert description.backend == "remote:scripted"
        assert description.isolation is IsolationLevel.ISOLATED
        assert any("service's own report" in note for note in description.notes)

    async def test_an_unreachable_service_is_not_ready_rather_than_raising(
        self, transport: HttpTransport
    ) -> None:
        remote = RemoteSandbox(base_url="http://127.0.0.1:1", transport=transport, scope=SCOPE)
        description = await remote.describe()
        assert not description.ready
        assert description.problems

    async def test_bad_credentials_are_reported_without_echoing_them(
        self, transport: HttpTransport
    ) -> None:
        async with SandboxService(_scripted(), token="other") as service:
            remote = await _remote(service, transport)
            description = await remote.describe()
            result = await remote.run("return 1", limits=_LIMITS)
        assert not description.ready
        assert "401" in description.problems[0]
        assert result.failure is not None
        assert result.failure.kind == "provider_error"
        assert "test-token" not in result.failure.message
        assert "other" not in result.failure.message


class TestExecutionSemantics:
    async def test_a_binding_travels_out_and_back_over_a_second_connection(
        self, transport: HttpTransport
    ) -> None:
        scripted = _scripted(
            script={"return 7": scripted_result(value=7)},
            bindings_to_call=(("lookup", {"sku": "A"}),),
        )
        calls: list[Mapping[str, Any]] = []

        async def lookup(arguments: Mapping[str, Any]) -> Any:
            calls.append(arguments)
            return {"price": 250}

        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            result = await remote.run("return 7", bindings={"lookup": lookup}, limits=_LIMITS)
        assert result.ok, result.failure
        assert result.value == 7
        assert calls == [{"sku": "A"}]
        assert scripted.binding_results == [{"price": 250}]
        assert any(r.path.endswith("/replies") for r in service.requests)

    async def test_a_binding_failure_reaches_the_program_as_its_own_error(
        self, transport: HttpTransport
    ) -> None:
        scripted = _scripted(bindings_to_call=(("boom", {}),))

        async def boom(_arguments: Mapping[str, Any]) -> Any:
            raise RuntimeError("host tool is down")

        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            result = await remote.run("return 1", bindings={"boom": boom}, limits=_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert "host tool is down" in result.failure.message

    async def test_the_request_carries_limits_network_and_isolation(
        self, transport: HttpTransport
    ) -> None:
        scripted = _scripted()
        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            await remote.run(
                "return 1",
                limits=_LIMITS.model_copy(update={"wall_seconds": 4.0}),
                network=False,
                isolation=IsolationLevel.ISOLATED,
            )
        call = scripted.calls[-1]
        assert call["limits"] is not None
        assert call["limits"].wall_seconds == 4.0
        assert call["network"] is False
        assert call["isolation"] is IsolationLevel.ISOLATED

    async def test_a_service_that_reports_weaker_isolation_has_its_result_withheld(
        self, transport: HttpTransport
    ) -> None:
        """The backstop, for a service that advertised one thing and did
        another. The control is the pre-flight below."""
        scripted = _scripted(script={"return 'secret'": scripted_result(value="secret")})
        async with SandboxService(
            scripted, token="test-token", faults={"weaker_isolation"}
        ) as service:
            remote = await _remote(service, transport)
            result = await remote.run(
                "return 'secret'", limits=_LIMITS, isolation=IsolationLevel.ISOLATED
            )
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "isolation_unavailable"
        assert result.value is None

    async def test_a_service_that_cannot_reach_the_level_is_never_sent_the_program(
        self, transport: HttpTransport
    ) -> None:
        """Refusing after the program ran is not refusing. The adapter reads
        the service's own description first, and a service that says it only
        reaches process-level never receives a program that asked for more --
        which `ScriptedSandbox.calls` proves, because it stays empty."""
        scripted = _scripted(isolation=IsolationLevel.PROCESS)
        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            result = await remote.run(
                "return 'secret'", limits=_LIMITS, isolation=IsolationLevel.ISOLATED
            )
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "isolation_unavailable"
        assert "not sent" in result.failure.message
        assert scripted.calls == [], "the program reached the service"

    async def test_a_service_refuses_before_executing_when_it_cannot_comply(
        self, transport: HttpTransport
    ) -> None:
        """The other side of the same rule, in the reference implementation:
        a service asked for terms it cannot meet answers with an error frame
        and does not run the program."""
        scripted = _scripted(isolation=IsolationLevel.PROCESS)
        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            # Past the adapter's own pre-flight, so the service's refusal is
            # what is under test rather than the client's.
            await remote.describe()
            session_result = await remote.run("return 1", limits=_LIMITS)
        assert session_result.ok
        assert len(scripted.calls) == 1

        scripted_two = _scripted(isolation=IsolationLevel.PROCESS)
        async with SandboxService(scripted_two, token="test-token") as service:
            remote = await _remote(service, transport)
            body = {
                "execution_id": "exec_manual",
                "program": "return 'secret'",
                "bindings": [],
                "limits": _LIMITS.model_dump(mode="json"),
                "network": False,
                "isolation": IsolationLevel.ISOLATED.value,
                "capture": {},
            }
            frames = await _post_execution(transport, service, body)
        assert scripted_two.calls == [], "the service ran a program it had refused"
        assert frames[-1]["type"] == "error"
        assert frames[-1]["kind"] == "isolation_unavailable"

    async def test_a_frame_larger_than_the_ceiling_is_a_provider_error(
        self, transport: HttpTransport
    ) -> None:
        """A service is not trusted to respect the capture limits it was sent:
        one enormous line is bounded here rather than decoded."""
        scripted = _scripted()
        async with SandboxService(scripted, token="test-token", faults={"giant_frame"}) as service:
            remote = await _remote(service, transport)
            result = await remote.run("return 1", limits=_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "provider_error"
        assert "ceiling" in result.failure.message, result.failure.message

    async def test_a_credential_is_not_sent_over_plaintext_http(
        self, transport: HttpTransport
    ) -> None:
        """A bearer token in clear is a bearer token for the network. Loopback
        is the exception, because there is no network to be on."""
        with pytest.raises(SandboxSetupError, match="plaintext"):
            RemoteSandbox(
                base_url="http://sandboxes.example.com",
                transport=transport,
                scope=SCOPE,
                credential=_token,
            )
        # Same URL, opted into deliberately.
        RemoteSandbox(
            base_url="http://sandboxes.example.com",
            transport=transport,
            scope=SCOPE,
            credential=_token,
            allow_insecure_http=True,
        )
        # Loopback needs no opt-in.
        RemoteSandbox(
            base_url="http://127.0.0.1:8931",
            transport=transport,
            scope=SCOPE,
            credential=_token,
        )
        # No credential, nothing to leak.
        RemoteSandbox(base_url="http://sandboxes.example.com", transport=transport, scope=SCOPE)

    async def test_a_scripted_failure_round_trips_as_data(self, transport: HttpTransport) -> None:
        failing = scripted_result(
            failure=SandboxFailure(kind="exception", message="boom", traceback="Traceback..."),
        )
        scripted = _scripted(script={"raise": failing})
        async with SandboxService(scripted, token="test-token") as service:
            remote = await _remote(service, transport)
            result = await remote.run("raise", limits=_LIMITS)
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "exception"
        assert result.failure.traceback == "Traceback..."


class TestProviderFailures:
    """Each fault the service can produce, and that none of them is retried."""

    @pytest.mark.parametrize(
        ("fault", "expect"),
        [
            ("server_error", "500"),
            ("disconnect_after_ready", "ended before a done frame"),
            ("malformed_frame", "not valid JSON"),
            ("error_frame", "capacity"),
        ],
    )
    async def test_each_fault_is_a_provider_error_and_not_retried(
        self, transport: HttpTransport, fault: str, expect: str
    ) -> None:
        scripted = _scripted()
        async with SandboxService(scripted, token="test-token", faults={fault}) as service:
            remote = await _remote(service, transport)
            result = await remote.run("return 1", limits=_LIMITS)
            starts = [r for r in service.requests if r.path == "/v1/executions"]
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "provider_error"
        assert expect in result.failure.message
        assert len(starts) == 1, "an execution is never retried automatically"

    async def test_a_hung_service_is_cancelled_at_the_wall_clock(
        self, transport: HttpTransport
    ) -> None:
        async with SandboxService(
            _scripted(), token="test-token", faults={"hang_after_ready"}
        ) as service:
            remote = RemoteSandbox(
                base_url=service.base_url,
                transport=transport,
                scope=SCOPE,
                credential=_token,
                connect_timeout=1.0,
            )
            result = await remote.run(
                "return 1", limits=_LIMITS.model_copy(update={"wall_seconds": 1.0})
            )
            assert service.cancelled, "the service was told to cancel"
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind == "timeout"

    async def test_a_callers_cancellation_reaches_the_service(
        self, transport: HttpTransport
    ) -> None:
        cancel = asyncio.Event()
        async with SandboxService(
            _scripted(), token="test-token", faults={"hang_after_ready"}
        ) as service:
            remote = await _remote(service, transport)

            async def fire() -> None:
                await asyncio.sleep(0.3)
                cancel.set()

            asyncio.get_running_loop().create_task(fire())
            result = await remote.run("return 1", limits=_LIMITS, cancel=cancel)
            assert service.cancelled
        assert result.cancelled
        assert result.failure is not None
        assert result.failure.kind == "cancelled"

    async def test_the_egress_policy_gates_the_service(self) -> None:
        class Deny:
            async def allow(self, scope: Scope, url: str) -> bool:
                return False

        async with (
            SandboxService(_scripted(), token="test-token") as service,
            HttpTransport(policy=Deny()) as denied,
        ):
            remote = await _remote(service, denied)
            description = await remote.describe()
            result = await remote.run("return 1", limits=_LIMITS)
        assert not description.ready
        assert "AccessDenied" in description.problems[0]
        assert result.failure is not None
        assert result.failure.kind == "provider_error"
        assert not [r for r in service.requests if r.path == "/v1/executions"]
        assert AccessDenied.__name__ in result.failure.message


class TestScriptedSandboxAsATestDouble:
    """The double a consumer scripts in their own tests, used directly."""

    async def test_it_records_calls_and_refuses_a_level_it_does_not_reach(self) -> None:
        scripted = _scripted(
            isolation=IsolationLevel.PROCESS,
            default=scripted_result(value=1, isolation=IsolationLevel.PROCESS),
        )
        ok = await scripted.run("return 1", isolation=IsolationLevel.PROCESS)
        refused = await scripted.run("return 1", isolation=IsolationLevel.ISOLATED)
        assert ok.value == 1
        assert refused.failure is not None
        assert refused.failure.kind == "isolation_unavailable"
        assert [c["isolation"] for c in scripted.calls] == [
            IsolationLevel.PROCESS,
            IsolationLevel.ISOLATED,
        ]
        assert ok.guarantees.process_tree is Enforcement.ENFORCED
