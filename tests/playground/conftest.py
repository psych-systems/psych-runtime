"""Fixtures for the example platform's own tests.

The backend under `examples/playground/backend` is not a library module and is
not on the import path, so this puts it there. It is tested from this suite
rather than from its own because the gate that has to stay green is one gate:
a backend that stops matching the runtime it wraps is exactly the drift the
example exists to prevent, and a second test runner nobody remembers to run
would not catch it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import uvicorn

_BACKEND = Path(__file__).resolve().parents[2] / "examples" / "playground" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytest.importorskip("fastapi", reason="the playground extra is not installed")

import httpx  # noqa: E402

# Module level, not inside the fixture below. `from __future__ import
# annotations` postpones every annotation to a string, and FastAPI resolves a
# handler's annotations against its *module* globals: with `Request` imported
# inside the fixture, `request: Request` was unresolvable, so FastAPI read it
# as a query parameter and the stub answered every completion with a 422.
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402


@pytest_asyncio.fixture
async def anonymous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The backend, in-process, against its own fresh state, nobody signed in.

    Every file it persists goes to `tmp_path`, so a test never reads the state
    a developer's own run left behind and never writes into it. The model's
    base URL points at a port nothing is listening on: no test here dispatches
    a Run that would call it, and a real address would make one that
    accidentally did reach the network, which DESIGN.md §22 forbids outright.
    """
    monkeypatch.setenv("PSYCH_PLAYGROUND_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("PSYCH_PLAYGROUND_MODEL", "test-model")
    monkeypatch.setenv("PSYCH_PLAYGROUND_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_INDEX_FILE", str(tmp_path / "index.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.delenv("PSYCH_PLAYGROUND_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("PSYCH_PLAYGROUND_SECRETS", raising=False)
    monkeypatch.delenv("PSYCH_PLAYGROUND_MCP_SERVERS", raising=False)
    monkeypatch.delenv("PSYCH_PLAYGROUND_API_KEY", raising=False)

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://backend") as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client


@pytest_asyncio.fixture
async def client(anonymous: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """The backend with one account signed in: what almost every test wants.

    Signed in by default because these tests are about the API's behaviour and
    not about its front door, and because a suite where every case begins with
    two lines of sign-up ceremony is a suite people stop adding cases to. The
    front door has its own tests, which take `anonymous` instead.
    """
    await sign_up(anonymous, "owner@example.com")
    yield anonymous


PASSWORD = "correct horse battery staple"
"""One password for every test account. Long enough to pass the length floor
(`app.accounts.MIN_PASSWORD_LENGTH`) and constant so a failing test never turns
out to be about the credential."""


async def sign_up(client: httpx.AsyncClient, email: str) -> str:
    """Register an account on `client` and leave it signed in. Returns its id.

    The cookie lands in `client.cookies` and rides every later request, which
    is what makes two clients against one app two different people.
    """
    response = await client.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest_asyncio.fixture
async def two_accounts(
    anonymous: httpx.AsyncClient,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """Two signed-in people against one backend, which is the whole question.

    A second `httpx.AsyncClient` over the *same* ASGI app rather than a second
    app: one process, one state file, one MCP pool, one Worker. Two apps would
    prove nothing, because two processes are isolated by the operating system
    whatever this code does.
    """
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backend"
    ) as other:
        await sign_up(anonymous, "first@example.com")
        await sign_up(other, "second@example.com")
        yield anonymous, other


@pytest_asyncio.fixture
async def app(anonymous: httpx.AsyncClient) -> AsyncIterator[Any]:
    """The running app object, for a test that needs an adapter the routes use.

    Depends on `anonymous` so the lifespan has run and `app.state.playground`
    exists. Reaching past the HTTP surface is normally the wrong move here, and
    is right for exactly one thing: writing a memory as *another* account, to
    prove the isolation holds against a direct key rather than against the API
    declining to offer one.
    """
    from app.main import app as fastapi_app

    _ = anonymous
    yield fastapi_app


# ---------------------------------------------------------------------------
# A model provider that answers
# ---------------------------------------------------------------------------
#
# Every fixture above points the model at `http://127.0.0.1:1/v1`, a port
# nothing listens on, and no test above dispatches a Run that would reach it.
# That kept the suite off the network, and it left the console's whole reason
# for existing untested: no case drove a model call, a tool execution, usage
# accounting or an approval to a decision. The CI job that boots the image did
# the same thing for the same reason.
#
# It also hid a defect. `load_settings()` used to raise without a provider, so
# the one command `README.md` and `docker-compose.yml` tell a newcomer to run
# exited during startup -- and nothing noticed, because every harness supplied
# the variable whose absence was the bug.
#
# `stub_provider` is the answer `McpStubServer` already is for MCP: a real
# server on loopback, speaking the real protocol, that this process starts and
# scripts. The backend builds an `OpenAICompatibleClient` per Attempt from a
# provider's `base_url`, so there is no seam to inject a `FakeModel` through
# without testing something other than the code that ships.


@dataclass
class Turn:
    """One scripted assistant turn, in the shape the wire carries it."""

    text: str = ""
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    def chunks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for index, (name, arguments) in enumerate(self.tool_calls):
            out.append(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": f"call_{index + 1}",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(arguments),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
        if self.text:
            out.append({"choices": [{"delta": {"content": self.text}, "finish_reason": None}]})
        out.append(
            {
                "choices": [
                    {"delta": {}, "finish_reason": "tool_calls" if self.tool_calls else "stop"}
                ]
            }
        )
        out.append(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
                },
            }
        )
        return out


@dataclass
class StubProvider:
    """A real OpenAI-compatible endpoint, on loopback, answering a script.

    One turn per request. Past the end it repeats the last one rather than
    erroring, so a test that cares about the first two turns does not have to
    predict how many the agent loop will take.
    """

    base_url: str
    script: list[Turn] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def says(self, *turns: Turn) -> StubProvider:
        self.script = list(turns)
        return self

    def _next(self) -> Turn:
        if not self.script:
            return Turn(text="ok")
        return self.script[min(len(self.requests) - 1, len(self.script) - 1)]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def stub_provider() -> Iterator[StubProvider]:
    port = _free_port()
    stub = StubProvider(base_url=f"http://127.0.0.1:{port}/v1")
    api = FastAPI()

    @api.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"data": [{"id": "stub-model"}]}

    @api.post("/v1/chat/completions")
    async def completions(request: Request) -> StreamingResponse:
        stub.requests.append(await request.json())
        turn = stub._next()

        async def body() -> AsyncIterator[str]:
            for chunk in turn.chunks():
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:  # pragma: no cover - a stub that never starts is a broken test run
        raise RuntimeError("the stub provider never started listening")
    try:
        yield stub
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest_asyncio.fixture
async def first_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The backend with no provider in its environment at all.

    The state `docker compose up` starts in, and the one the backend used to
    refuse to boot in.
    """
    async for client in _backend(tmp_path, monkeypatch, provider=None):
        yield client


@pytest_asyncio.fixture
async def talking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> AsyncIterator[httpx.AsyncClient]:
    """One account signed in, against a provider that actually answers."""
    async for client in _backend(tmp_path, monkeypatch, provider=stub_provider.base_url):
        await sign_up(client, "owner@example.com")
        yield client


async def _backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider: str | None
) -> AsyncIterator[httpx.AsyncClient]:
    for name in (
        "PSYCH_PLAYGROUND_BASE_URL",
        "PSYCH_PLAYGROUND_API_KEY",
        "PSYCH_PLAYGROUND_CF_ACCOUNT_ID",
        "PSYCH_PLAYGROUND_POSTGRES_DSN",
        "PSYCH_PLAYGROUND_SECRETS",
        "PSYCH_PLAYGROUND_MCP_SERVERS",
    ):
        monkeypatch.delenv(name, raising=False)
    if provider is not None:
        monkeypatch.setenv("PSYCH_PLAYGROUND_BASE_URL", provider)
        monkeypatch.setenv("PSYCH_PLAYGROUND_API_KEY", "stub-key")
    monkeypatch.setenv("PSYCH_PLAYGROUND_MODEL", "stub-model")
    monkeypatch.setenv("PSYCH_PLAYGROUND_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_INDEX_FILE", str(tmp_path / "index.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_MEMORY_FILE", str(tmp_path / "memory.json"))

    from app.main import app

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest_asyncio.fixture
async def live_playground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_provider: StubProvider
) -> AsyncIterator[tuple[str, httpx.AsyncClient]]:
    """The real playground app, listening on a real loopback port.

    Every other fixture here talks to the app over `httpx.ASGITransport`,
    which is right for the REST contract but cannot exercise A2A: a peer
    connection is a real `httpx` client making a real HTTP request to a URL,
    and there is no URL to give it when the app is not actually listening
    anywhere. This is that one exception, for the one feature that needs it,
    for the same reason `stub_provider` is a real server on loopback rather
    than a substitute for the model: the code under test is the HTTP path
    itself, so a fake of it would test something other than what ships.

    Yields the base URL and a client already talking to it over real TCP, so
    a test can dispatch against it exactly as a deployment's own client would.
    """
    for name in (
        "PSYCH_PLAYGROUND_BASE_URL",
        "PSYCH_PLAYGROUND_API_KEY",
        "PSYCH_PLAYGROUND_CF_ACCOUNT_ID",
        "PSYCH_PLAYGROUND_POSTGRES_DSN",
        "PSYCH_PLAYGROUND_SECRETS",
        "PSYCH_PLAYGROUND_MCP_SERVERS",
        "PSYCH_PLAYGROUND_A2A_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PSYCH_PLAYGROUND_BASE_URL", stub_provider.base_url)
    monkeypatch.setenv("PSYCH_PLAYGROUND_API_KEY", "stub-key")
    monkeypatch.setenv("PSYCH_PLAYGROUND_MODEL", "stub-model")
    monkeypatch.setenv("PSYCH_PLAYGROUND_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_INDEX_FILE", str(tmp_path / "index.json"))
    monkeypatch.setenv("PSYCH_PLAYGROUND_MEMORY_FILE", str(tmp_path / "memory.json"))
    port = _free_port()
    monkeypatch.setenv("PSYCH_PLAYGROUND_HOST", "127.0.0.1")
    monkeypatch.setenv("PSYCH_PLAYGROUND_PORT", str(port))

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:  # pragma: no cover - a server that never starts is a broken test run
        raise RuntimeError("the live playground app never started listening")

    base_url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base_url) as client:
        try:
            yield base_url, client
        finally:
            server.should_exit = True
            thread.join(timeout=10)


async def publish_agent(client: httpx.AsyncClient, **overrides: Any) -> str:
    """Publish a support agent and return its id."""
    body: dict[str, Any] = {
        "name": "support",
        "instructions": "Help the customer with their order.",
        "model": "stub-model",
        "tools": ["lookup_order"],
    }
    body.update(overrides)
    response = await client.post("/api/agents", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["agent_id"])


async def dispatch(client: httpx.AsyncClient, agent_id: str, message: str) -> str:
    response = await client.post("/api/runs", json={"agent_id": agent_id, "message": message})
    assert response.status_code == 201, response.text
    return str(response.json()["run_id"])


async def wait_for(
    client: httpx.AsyncClient, run_id: str, *, settled: bool = True
) -> dict[str, Any]:
    """Poll status until the Run settles, or until it stops for a person."""
    last: dict[str, Any] = {}
    for _ in range(600):
        response = await client.get(f"/api/runs/{run_id}/status")
        assert response.status_code == 200, response.text
        last = dict(response.json())
        lifecycle = last.get("lifecycle")
        if lifecycle in {"done", "failed", "stopped"}:
            return last
        if not settled and lifecycle == "waiting":
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"the Run never reached a resting state: {last}")
