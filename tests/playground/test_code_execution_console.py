"""The console's code-execution surface: settings, agent terms, health, data.

Everything the Playground adds over the library for running programs:
sandbox profiles in Settings, ``code_execution`` on an agent, the profile
health check, the review data seeder, and reading an output back by handle.
Driven through the HTTP API against the backend in-process, with this host's
own sandbox where a program actually runs.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.functional

AGENT = {
    "name": "coder",
    "instructions": "Work things out with code.",
    "model": "test-model",
    "tools": ["lookup_order"],
}


async def _publish(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await client.post("/api/agents", json={**AGENT, **overrides})
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestSandboxSettings:
    async def test_the_settings_report_backends_and_profile_names(
        self, client: httpx.AsyncClient
    ) -> None:
        runtime = (await client.get("/api/settings")).json()["runtime"]
        assert runtime["sandbox_platform"]
        assert isinstance(runtime["sandbox_backends"], list)
        assert runtime["sandbox_backends"], "every platform lists at least one backend"
        for backend in runtime["sandbox_backends"]:
            assert backend["isolation"] in ("isolated", "process")
        if runtime["sandbox_available"]:
            assert "default" in runtime["sandbox_profile_names"]

    async def test_extra_profiles_round_trip_without_a_secret_value(
        self, client: httpx.AsyncClient
    ) -> None:
        before = (await client.get("/api/settings")).json()["runtime"]
        body = {
            **{k: v for k, v in before.items() if k in RUNTIME_IN_FIELDS},
            "sandbox_profiles": [
                {
                    "name": "cluster",
                    "backend": "remote",
                    "base_url": "http://127.0.0.1:1",
                    "credential": "sandbox-token",
                    "hard_limits": {"wall_seconds": 12},
                },
                {"name": "pods", "backend": "container", "image": "python:3.12-slim"},
            ],
        }
        updated = await client.put("/api/settings/runtime", json=body)
        assert updated.status_code == 200, updated.text
        runtime = updated.json()["runtime"]
        names = {p["name"] for p in runtime["sandbox_profiles"]}
        assert names == {"cluster", "pods"}
        cluster = next(p for p in runtime["sandbox_profiles"] if p["name"] == "cluster")
        assert cluster["credential"] == "sandbox-token"  # a name, never a value
        assert cluster["hard_limits"]["wall_seconds"] == 12
        assert "cluster" in runtime["sandbox_profile_names"]
        assert "pods" in runtime["sandbox_profile_names"]

    async def test_checking_a_remote_profile_that_cannot_be_reached_is_not_ready(
        self, client: httpx.AsyncClient
    ) -> None:
        before = (await client.get("/api/settings")).json()["runtime"]
        body = {
            **{k: v for k, v in before.items() if k in RUNTIME_IN_FIELDS},
            "sandbox_profiles": [
                {"name": "cluster", "backend": "remote", "base_url": "http://127.0.0.1:1"}
            ],
        }
        assert (await client.put("/api/settings/runtime", json=body)).status_code == 200
        health = await client.post("/api/settings/sandbox/cluster/check")
        assert health.status_code == 200, health.text
        report = health.json()
        assert report["configured"] is True
        assert report["ready"] is False
        assert report["problems"]
        assert report["backend"] == "remote"

    async def test_checking_an_unknown_profile_says_so(self, client: httpx.AsyncClient) -> None:
        health = await client.post("/api/settings/sandbox/nope/check")
        assert health.status_code == 200
        assert health.json()["configured"] is False
        assert health.json()["ready"] is False

    async def test_checking_the_default_profile_reports_real_guarantees(
        self, client: httpx.AsyncClient
    ) -> None:
        runtime = (await client.get("/api/settings")).json()["runtime"]
        if not runtime["sandbox_available"]:
            pytest.skip(runtime["sandbox_unavailable_reason"])
        health = await client.post("/api/settings/sandbox/default/check")
        assert health.status_code == 200, health.text
        report = health.json()
        assert report["ready"] is True, report["problems"]
        assert report["isolation"] in ("isolated", "process")
        assert report["guarantees"]["process_tree"] == "enforced"
        assert report["mechanisms"]


RUNTIME_IN_FIELDS = {
    "cost_policy",
    "blob_offload_bytes",
    "catalogue_budget_chars",
    "sandbox_enabled",
    "sandbox_limits",
    "sandbox_allow_network",
    "sandbox_profiles",
    "egress_allow",
    "denied_tools",
}


class TestAgentTerms:
    async def test_code_execution_round_trips_and_joins_the_hash(
        self, client: httpx.AsyncClient
    ) -> None:
        runtime = (await client.get("/api/settings")).json()["runtime"]
        if not runtime["sandbox_available"]:
            pytest.skip(runtime["sandbox_unavailable_reason"])
        plain = await _publish(client)
        coder = await _publish(
            client,
            code_execution={
                "isolation": "process",
                "limits": {"wall_seconds": 5},
                "bindings": ["lookup_order"],
                "preview_bytes": 2048,
            },
        )
        assert plain["version_hash"] != coder["version_hash"]
        summary = (await client.get(f"/api/agents/{coder['agent_id']}")).json()
        terms = summary["code_execution"]
        assert terms["enabled"] is True
        assert terms["isolation"] == "process"
        assert terms["limits"]["wall_seconds"] == 5
        assert terms["bindings"] == ["lookup_order"]
        assert terms["preview_bytes"] == 2048
        plain_out = await client.get(f"/api/agents/{plain['agent_id']}")
        assert plain_out.json()["code_execution"] is None

    async def test_a_binding_the_agent_does_not_hold_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/agents", json={**AGENT, "code_execution": {"bindings": ["issue_refund"]}}
        )
        assert response.status_code == 400, response.text

    async def test_a_profile_the_account_does_not_offer_is_refused_at_publish(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/agents", json={**AGENT, "code_execution": {"profile": "gpu"}}
        )
        assert response.status_code == 400, response.text
        assert "gpu" in response.text


class TestReviewData:
    async def test_the_seeder_produces_every_state_and_outputs_read_back(
        self, client: httpx.AsyncClient
    ) -> None:
        runtime = (await client.get("/api/settings")).json()["runtime"]
        if not runtime["sandbox_available"]:
            pytest.skip(runtime["sandbox_unavailable_reason"])
        seeded = await client.post("/api/demo/code-execution")
        assert seeded.status_code == 201, seeded.text
        run_ids = seeded.json()["run_ids"]
        assert len(run_ids) == 4
        summaries = []
        for run_id in run_ids:
            report = (await client.get(f"/api/runs/{run_id}/report")).json()
            call = next(c for c in report["tool_calls"] if c["tool"] == "run_code")
            summaries.append(call)
        success, failure, limited, spill = summaries
        assert success["result"]["ok"] is True
        assert any(a["name"] == "file:orders.csv" for a in success["attachments"])
        assert failure["result"]["ok"] is False
        assert "ZeroDivisionError" in failure["result"]["traceback"]
        assert limited["result"]["limit_hit"] == "wall_seconds"
        assert spill["result"]["stdout_handle"].startswith("out_")
        handle = spill["result"]["stdout_handle"]
        window = await client.get(
            f"/api/runs/{run_ids[3]}/attachments/{handle}", params={"offset": 3998, "limit": 2}
        )
        assert window.status_code == 200, window.text
        assert window.json()["total_lines"] >= 4000
        assert '"id": 3998' in window.json()["content"].replace('"id":3998', '"id": 3998')
        search = await client.get(
            f"/api/runs/{run_ids[3]}/attachments/{handle}", params={"pattern": "SKU-00042"}
        )
        assert search.json()["total_matches"] == 1
        download = await client.get(f"/api/runs/{run_ids[3]}/attachments/{handle}/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("text/plain")
        assert download.content.count(b"\n") >= 4000
        forged = await client.get(f"/api/runs/{run_ids[3]}/attachments/out_forged_stdout")
        assert forged.status_code == 404
        # The agent the seeder published is listed like any other.
        agents = (await client.get("/api/agents")).json()
        assert any(a["name"] == "code-execution-review" for a in agents)

    async def test_another_account_cannot_read_the_output(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        owner, other = two_accounts
        runtime = (await owner.get("/api/settings")).json()["runtime"]
        if not runtime["sandbox_available"]:
            pytest.skip(runtime["sandbox_unavailable_reason"])
        seeded = await owner.post("/api/demo/code-execution")
        assert seeded.status_code == 201, seeded.text
        run_id = seeded.json()["run_ids"][3]
        report = (await owner.get(f"/api/runs/{run_id}/report")).json()
        handle = next(c for c in report["tool_calls"] if c["tool"] == "run_code")["result"][
            "stdout_handle"
        ]
        assert (await other.get(f"/api/runs/{run_id}/attachments/{handle}")).status_code == 404
