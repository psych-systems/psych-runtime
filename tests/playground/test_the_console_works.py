"""The console doing the thing it exists to do, against a provider that answers.

`test_backend.py` beside this covers the API's shape: what publishing carries,
what a branch shares, what a stranger is refused. Every fixture it uses points
the model at a port nothing listens on, so nothing there drives a Run to an
answer. This file does, against `stub_provider`.

That gap was not academic. `load_settings()` raised without a model provider,
so the command `README.md` and `docker-compose.yml` tell a newcomer to run
exited during startup -- and no test caught it, because every harness in the
repository, this suite and the CI image job alike, set the variable whose
absence was the defect.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.playground.conftest import (
    StubProvider,
    Turn,
    dispatch,
    publish_agent,
    sign_up,
    wait_for,
)

pytestmark = pytest.mark.functional


class TestTheDocumentedFirstRun:
    """With nothing configured at all, which is how the documents say to start."""

    async def test_the_backend_boots_and_answers(self, first_boot: httpx.AsyncClient) -> None:
        response = await first_boot.get("/api/health")

        assert response.status_code == 200
        assert response.json()["ok"] is True

    async def test_it_reports_no_provider_rather_than_a_broken_one(
        self, first_boot: httpx.AsyncClient
    ) -> None:
        await sign_up(first_boot, "owner@example.com")

        config = (await first_boot.get("/api/config")).json()

        assert config["provider_label"] is None
        assert config["has_api_key"] is False
        # Not one whose base_url is "": the console would list it as configured
        # and the first Run would fail at the transport rather than saying that
        # nothing is set up yet.
        assert (await first_boot.get("/api/settings")).json()["providers"] == []

    async def test_adding_the_first_provider_makes_it_active(
        self, first_boot: httpx.AsyncClient
    ) -> None:
        await sign_up(first_boot, "owner@example.com")

        response = await first_boot.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {
                        "id": "mine",
                        "label": "mine",
                        "base_url": "http://127.0.0.1:1/v1",
                        "model": "some-model",
                    }
                ]
            },
        )

        assert response.status_code == 200, response.text
        # Settings is where the documents send a person with no provider. It
        # used to leave them with one configured and none active, and nothing
        # anywhere said to activate it as a second step.
        assert (await first_boot.get("/api/config")).json()["provider_label"] == "mine"

    async def test_removing_the_active_one_leaves_nothing_active(
        self, first_boot: httpx.AsyncClient
    ) -> None:
        await sign_up(first_boot, "owner@example.com")
        await first_boot.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {"id": "a", "label": "a", "base_url": "http://127.0.0.1:1/v1", "model": "m"},
                    {"id": "b", "label": "b", "base_url": "http://127.0.0.1:2/v1", "model": "m"},
                ]
            },
        )
        await first_boot.post("/api/settings/providers/b/activate")

        await first_boot.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {"id": "a", "label": "a", "base_url": "http://127.0.0.1:1/v1", "model": "m"}
                ]
            },
        )

        # Deliberately not "a": an operator who removed their active provider
        # gets an explicit nothing rather than a silent switch to an endpoint
        # and a key they did not choose.
        assert (await first_boot.get("/api/config")).json()["provider_label"] is None


class TestAConversation:
    async def test_the_agent_calls_its_tool_and_answers(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("lookup_order", {"order_id": "A1"}),), prompt_tokens=412),
            Turn(text="A1 has shipped.", prompt_tokens=461, completion_tokens=9),
        )
        run_id = await dispatch(talking, await publish_agent(talking), "where is order A1?")

        status = await wait_for(talking, run_id)

        assert status["terminal_state"] == "completed", status
        assert (await talking.get(f"/api/runs/{run_id}/answer")).json()["text"] == "A1 has shipped."

        report = (await talking.get(f"/api/runs/{run_id}/report")).json()
        assert [(call["tool"], call["outcome"]) for call in report["tool_calls"]] == [
            ("lookup_order", "ok")
        ]

    async def test_usage_keeps_cached_reads_disjoint_from_input(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(
                tool_calls=(("lookup_order", {"order_id": "A1"}),),
                prompt_tokens=412,
                completion_tokens=23,
            ),
            Turn(text="Shipped.", prompt_tokens=461, completion_tokens=9, cached_tokens=400),
        )
        run_id = await dispatch(talking, await publish_agent(talking), "where is order A1?")
        await wait_for(talking, run_id)

        usage = (await talking.get(f"/api/runs/{run_id}/report")).json()["totals"]["usage"]

        # `prompt_tokens` counts cached tokens too, so input is the difference
        # (DESIGN.md 13.1). Folding them together is how a cache-heavy
        # workload gets billed wrong.
        assert usage["input"] == 412 + 61
        assert usage["output"] == 23 + 9
        assert usage["cache_read"] == 400

    async def test_a_model_with_no_price_costs_none_rather_than_zero(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(Turn(text="Done.", prompt_tokens=10, completion_tokens=2))
        run_id = await dispatch(talking, await publish_agent(talking), "hello")
        await wait_for(talking, run_id)

        totals = (await talking.get(f"/api/runs/{run_id}/report")).json()["totals"]

        assert totals["cost"] is None
        assert totals["unpriced_model_calls"] >= 1

    async def test_the_conversation_reads_back_for_a_chat_screen(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("lookup_order", {"order_id": "A1"}),)),
            Turn(text="A1 has shipped."),
        )
        run_id = await dispatch(talking, await publish_agent(talking), "where is order A1?")
        await wait_for(talking, run_id)

        messages = (await talking.get(f"/api/runs/{run_id}/messages")).json()

        assert messages[0]["role"] == "user"
        assert any("A1 has shipped." in message["content"] for message in messages)

    async def test_reconnecting_with_after_delivers_exactly_the_remainder(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("lookup_order", {"order_id": "A1"}),)),
            Turn(text="A1 has shipped."),
        )
        run_id = await dispatch(talking, await publish_agent(talking), "where is order A1?")
        await wait_for(talking, run_id)

        whole = await _seqs(talking, run_id, after=0)
        assert whole, "the stream yielded no records at all"

        midpoint = whole[len(whole) // 2]
        assert await _seqs(talking, run_id, after=midpoint) == [s for s in whole if s > midpoint]


class TestAnApprovalHeldForAPerson:
    async def test_it_suspends_before_the_destructive_call(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("issue_refund", {"order_id": "A1", "cents": 4200}),)),
            Turn(text="Refunded."),
        )
        run_id = await dispatch(talking, await _refunder(talking), "refund A1")

        status = await wait_for(talking, run_id, settled=False)

        assert status["lifecycle"] == "waiting", status
        pending = status["pending_approval"]
        assert pending is not None, "the Run stopped without saying what for"
        assert pending["tool"] == "issue_refund"
        # The exact arguments, so a console shows what it is approving rather
        # than only which tool.
        assert pending["arguments"] == {"order_id": "A1", "cents": 4200}

        report = (await talking.get(f"/api/runs/{run_id}/report")).json()
        assert [c for c in report["tool_calls"] if c["outcome"] is not None] == []

    async def test_approving_lets_it_through(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("issue_refund", {"order_id": "A1", "cents": 4200}),)),
            Turn(text="Refunded 4200 cents on order A1."),
        )
        run_id = await dispatch(talking, await _refunder(talking), "refund A1")
        await wait_for(talking, run_id, settled=False)

        response = await talking.post(
            f"/api/runs/{run_id}/resume", json={"approved": True, "by": "manager-7"}
        )
        assert response.status_code == 200, response.text

        assert (await wait_for(talking, run_id))["terminal_state"] == "completed"
        report = (await talking.get(f"/api/runs/{run_id}/report")).json()
        refund = next(c for c in report["tool_calls"] if c["tool"] == "issue_refund")
        assert refund["outcome"] == "ok"

    async def test_denying_settles_the_call_as_an_error_and_never_runs_it(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("issue_refund", {"order_id": "A1", "cents": 4200}),)),
            Turn(text="I was not able to issue that refund."),
        )
        run_id = await dispatch(talking, await _refunder(talking), "refund A1")
        await wait_for(talking, run_id, settled=False)

        await talking.post(
            f"/api/runs/{run_id}/resume", json={"approved": False, "by": "manager-7"}
        )
        status = await wait_for(talking, run_id)

        # A refusal is a settlement, not a failure: the Run did everything it
        # was allowed to do and said so.
        assert status["terminal_state"] == "completed"

        report = (await talking.get(f"/api/runs/{run_id}/report")).json()
        refund = next(c for c in report["tool_calls"] if c["tool"] == "issue_refund")
        assert refund["outcome"] == "error"
        assert refund["failure"]["kind"] == "denied"
        assert refund["result"] is None, "a refused call produced a result"

    async def test_a_stranger_cannot_decide_it(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(tool_calls=(("issue_refund", {"order_id": "A1", "cents": 4200}),)),
            Turn(text="Refunded."),
        )
        run_id = await dispatch(talking, await _refunder(talking), "refund A1")
        await wait_for(talking, run_id, settled=False)

        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as stranger:
            await sign_up(stranger, "stranger@example.com")

            response = await stranger.post(f"/api/runs/{run_id}/resume", json={"approved": True})

        assert response.status_code == 404, "another account decided somebody else's refund"


async def _refunder(client: httpx.AsyncClient) -> str:
    return await publish_agent(
        client,
        name="refunds",
        tools=["lookup_order", "issue_refund"],
        approval_selectors=["@destructive"],
    )


async def _seqs(client: httpx.AsyncClient, run_id: str, *, after: int) -> list[int]:
    """Every record sequence the SSE stream yields from `after`."""
    seqs: list[int] = []
    async with client.stream("GET", f"/api/runs/{run_id}/stream?after={after}") as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if not payload:
                continue
            record: Any = json.loads(payload)
            if isinstance(record, dict) and "seq" in record:
                seqs.append(int(record["seq"]))
    return seqs
