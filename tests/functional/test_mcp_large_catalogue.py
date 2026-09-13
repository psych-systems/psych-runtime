"""A tool catalogue larger than one server-sent event can carry.

A Streamable HTTP server answers each JSON-RPC request by writing the whole
result as a single server-sent event, and the HTTP client underneath the SDK
refuses any one event over a megabyte. A catalogue of a few hundred tools with
full JSON Schemas passes that easily, so the ceiling is really a ceiling on
how many tools a server may offer -- and it was reached silently. The SDK
reads its stream inside a bare ``except Exception``, so the refusal arrived as
"SSE stream ended without a response", which Psych relayed as an unreachable
server. Every layer below reported success, which sent the diagnosis towards
the network, OAuth, and the server, none of which were involved.

Nothing here mocks the client under test: a real catalogue, over a real
socket, through the pool a Run uses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest

import psych_runtime
import psych_runtime.tools.sse_events as sse_events_module
from psych_runtime.core.scope import Scope
from psych_runtime.model.egress import HttpTransport
from psych_runtime.testing.mcp_stub import McpStubServer, make_server
from psych_runtime.tools.mcp import McpPool, McpResponseTooLarge, McpServerUnreachable
from psych_runtime.tools.secrets import InMemorySecretResolver
from psych_runtime.tools.sse_events import (
    DEFAULT_MAX_SSE_EVENT_BYTES,
    BoundedEventSource,
    SseEventTooLarge,
    install_event_source,
    max_sse_event_bytes,
    set_max_sse_event_bytes,
    unpatched_modules,
)

ONE_MEBIBYTE = 1024 * 1024


def fat_tools(total_bytes: int) -> list[dict[str, object]]:
    """A catalogue whose JSON is at least ``total_bytes``.

    Padding goes in the description rather than the tool count so the size is
    a property of the catalogue rather than of how many sockets the stub has
    to write, and so the number stays predictable as the wire shape changes.
    """
    tools: list[dict[str, object]] = []
    written = 0
    index = 0
    while written < total_bytes:
        tool: dict[str, object] = {
            "name": f"report_{index}",
            "description": "d" * 4_000,
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": None,
        }
        tools.append(tool)
        written += len(json.dumps(tool))
        index += 1
    return tools


@pytest.fixture
async def transport() -> AsyncIterator[HttpTransport]:
    http_transport = HttpTransport()
    try:
        yield http_transport
    finally:
        await http_transport.aclose()


@pytest.fixture(autouse=True)
def restore_ceiling() -> Iterator[None]:
    """The ceiling is process-wide, so a test that moves it must put it back."""
    original = max_sse_event_bytes()
    yield
    set_max_sse_event_bytes(original)


class TestACatalogueLargerThanOneMebibyte:
    async def test_the_dependency_default_reproduces_the_reported_failure(
        self, transport: HttpTransport
    ) -> None:
        """The control: the same response through the dependency's one-mebibyte
        source becomes the SDK's -32000 stream-ended error. This proves the
        passing regression below exercises the reported failure, rather than
        merely accepting a large response by some unrelated path.
        """
        import mcp.client.streamable_http as streamable

        tools = fat_tools(2 * ONE_MEBIBYTE)
        assert len(json.dumps(tools)) > ONE_MEBIBYTE
        original_event_source = cast("Any", sse_events_module).EventSource
        cast("Any", streamable).EventSource = original_event_source
        try:
            async with McpStubServer(tools, sse_responses=True) as stub:
                pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
                scope = Scope(tenant="tenant-a", principal="alice")

                with pytest.raises(McpServerUnreachable, match="stream ended"):
                    await pool.get_or_connect(scope, make_server(stub.url))
                await pool.close_all()
        finally:
            install_event_source()

    async def test_it_arrives_instead_of_looking_like_an_unreachable_server(
        self, transport: HttpTransport
    ) -> None:
        """The regression. Under the client's own default this connect failed,
        and failed while naming the wrong cause."""
        tools = fat_tools(2 * ONE_MEBIBYTE)
        assert len(json.dumps(tools)) > ONE_MEBIBYTE
        async with McpStubServer(tools, sse_responses=True) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            connection = await pool.get_or_connect(scope, make_server(stub.url))

            assert len(await connection.list_tools()) == len(tools)
            await pool.close_all()

    async def test_the_same_catalogue_as_json_was_never_affected(
        self, transport: HttpTransport
    ) -> None:
        """Why this went unnoticed: a server answering ``application/json``
        has no event ceiling to reach, and that is what the stub used to do."""
        tools = fat_tools(2 * ONE_MEBIBYTE)
        async with McpStubServer(tools, sse_responses=False) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            connection = await pool.get_or_connect(scope, make_server(stub.url))

            assert len(await connection.list_tools()) == len(tools)
            await pool.close_all()


class TestPastTheCeiling:
    async def test_it_names_the_ceiling_rather_than_blaming_the_server(
        self, transport: HttpTransport
    ) -> None:
        """The ceiling still exists -- a server is a third party, and a reply
        nothing bounds is one that can exhaust this process. What changed is
        that reaching it says so."""
        set_max_sse_event_bytes(64 * 1024)
        tools = fat_tools(256 * 1024)
        async with McpStubServer(tools, sse_responses=True) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            with pytest.raises(McpResponseTooLarge) as caught:
                await pool.get_or_connect(scope, make_server(stub.url))

            message = str(caught.value)
            assert "65536" in message
            assert "server answered correctly" in message
            await pool.close_all()

    async def test_it_is_not_reported_as_an_unreachable_server(
        self, transport: HttpTransport
    ) -> None:
        """The distinction that matters to a caller: ``optional=True`` drops a
        server that is absent. A server that is present and answering, whose
        reply this client will not buffer, must not be quietly dropped -- the
        agent would run without its tools and nobody would be told why.
        """
        set_max_sse_event_bytes(64 * 1024)
        async with McpStubServer(fat_tools(256 * 1024), sse_responses=True) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            with pytest.raises(McpResponseTooLarge) as caught:
                await pool.get_or_connect(scope, make_server(stub.url))

            assert not isinstance(caught.value, McpServerUnreachable)
            await pool.close_all()


class TestWhatTheProbeButtonReports:
    async def test_a_probe_describes_it_instead_of_raising(self, transport: HttpTransport) -> None:
        """A probe is a question, and ``ok=False`` never raises. A new error
        type that the probe does not catch turns the consumer's "test this
        server" button into a 500, which is how this one would first have been
        met: the failure it reports is exactly the one a probe exists to
        describe.
        """
        set_max_sse_event_bytes(64 * 1024)
        async with McpStubServer(fat_tools(256 * 1024), sse_responses=True) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            probe = await pool.probe(scope, make_server(stub.url))

            assert probe.ok is False
            assert probe.error_type == "McpResponseTooLarge"
            assert "65536" in probe.detail
            await pool.close_all()

    async def test_a_catalogue_over_a_mebibyte_probes_clean(self, transport: HttpTransport) -> None:
        async with McpStubServer(fat_tools(2 * ONE_MEBIBYTE), sse_responses=True) as stub:
            pool = McpPool(transport=transport, secrets=InMemorySecretResolver())
            scope = Scope(tenant="tenant-a", principal="alice")

            probe = await pool.probe(scope, make_server(stub.url))

            assert probe.ok is True, probe.detail
            assert probe.error_type is None
            await pool.close_all()


class TestTheCeilingIsInstalledEverywhere:
    def test_the_configuration_and_error_are_on_the_public_surface(self) -> None:
        assert psych_runtime.DEFAULT_MAX_SSE_EVENT_BYTES == DEFAULT_MAX_SSE_EVENT_BYTES
        assert psych_runtime.max_sse_event_bytes() == max_sse_event_bytes()
        assert psych_runtime.set_max_sse_event_bytes is set_max_sse_event_bytes
        assert psych_runtime.McpResponseTooLarge is McpResponseTooLarge

    def test_no_imported_module_still_holds_the_unbounded_source(self) -> None:
        """The guard. Two modules reach the class two different ways: one
        imported the symbol by value, the other looks it up at call time.
        Patching either alone leaves one transport on the old ceiling, which
        is a bug that shows up on one server and not another. A future SDK
        module that captures the symbol fails here rather than in the field.
        """
        import mcp.client.sse
        import mcp.client.streamable_http
        import mcp.shared._httpx_utils  # noqa: F401

        install_event_source()

        assert unpatched_modules() == ()

    def test_importing_the_client_is_enough_to_install_it(self) -> None:
        """What the test above cannot prove, because it installs first.

        The install runs when ``psych_runtime.tools.mcp`` is imported, and
        nothing re-runs it before a connection is opened, so the state that
        matters is the one a fresh process has after that single import with
        no help. This suite has already imported and installed plenty, which
        is exactly why the question has to be put to a subprocess.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import psych_runtime.tools.mcp;"
                "from psych_runtime.tools.sse_events import unpatched_modules, patched_modules;"
                "print(unpatched_modules());print(patched_modules())",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        unpatched, patched, *_ = result.stdout.splitlines()
        assert unpatched == "()", result.stdout
        # Named rather than counted: this is the pair that has to stay covered,
        # and an SDK that stops routing through either should fail here loudly
        # rather than quietly dropping back to the default ceiling.
        assert "httpx2" in patched
        assert "mcp.client.streamable_http" in patched

    def test_the_streamable_http_transport_builds_the_bounded_source(self) -> None:
        import mcp.client.streamable_http as streamable

        # Read with getattr: the SDK does not declare this a public
        # re-export, which is itself why the rebinding has to be
        # asserted rather than assumed.
        assert getattr(streamable, "EventSource") is BoundedEventSource  # noqa: B009

    def test_the_default_is_bounded(self) -> None:
        assert DEFAULT_MAX_SSE_EVENT_BYTES > 0
        assert max_sse_event_bytes() > ONE_MEBIBYTE

    def test_an_explicit_httpx2_limit_is_preserved(self) -> None:
        import httpx2

        request = httpx2.Request("GET", "https://mcp.example.test/events")
        response = httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: " + (b"x" * 64) + b"\n\n",
            request=request,
        )

        source = httpx2.EventSource(response, max_event_size=32)
        with pytest.raises(SseEventTooLarge) as caught:
            list(source)

        assert caught.value.limit == 32

    def test_a_ceiling_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            set_max_sse_event_bytes(0)
