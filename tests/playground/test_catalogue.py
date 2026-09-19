"""The out-of-the-box catalogue: what a person has the moment they sign up.

The product claim these tests exist to hold is narrow and checkable: run the
container, sign up, paste one provider key, and there are ready providers,
connectors to connect, a specialist agent per connector, an orchestrator over
all of them, and workflows that use them. Every assertion below is one clause
of that sentence.

They use the `seeded_client` fixture rather than `client`, because seeding is
off for the rest of this suite -- see `conftest`. That is also why the
idempotence tests here are the only place the seed route runs twice: it is the
property most likely to rot, since "already there" is answered differently for
settings (by id) than for the index (by `catalogue_id`).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app import catalogue
from app.catalogue import PSYCH_AGENT_ID

from tests.playground.conftest import (
    StubProvider,
    Turn,
    dispatch,
    publish_agent,
    sign_up,
    wait_for,
)


async def _catalogue(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/catalogue")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestTheCatalogueItself:
    """Properties of the data, checkable without a backend at all."""

    def test_every_connector_has_a_specialist_and_the_orchestrator_names_them_all(self) -> None:
        specialists = {a.catalogue_id for a in catalogue.AGENTS if a.connector is not None}
        assert specialists == {c.name for c in catalogue.CONNECTORS}

        psych = next(a for a in catalogue.AGENTS if a.catalogue_id == PSYCH_AGENT_ID)
        assert set(psych.subagents) == specialists, "a specialist the orchestrator cannot reach"
        assert psych.connector is None

    def test_no_connector_ships_a_credential(self) -> None:
        """Nothing fake, and nothing secret. A connector may *name* the secret
        it wants; it may never carry one."""
        for connector in catalogue.CONNECTORS:
            assert connector.url.startswith("https://"), connector.name
            assert connector.credential_name.isidentifier(), connector.name
            # The hint says where a token comes from; it never is one.
            hint = connector.auth.credential_hint.lower()
            for prefix in ("sk-", "ghp_", "github_pat_", "xoxb-", "xoxp-", "glpat-"):
                assert prefix not in hint, (connector.name, prefix)

    def test_every_catalogue_workflow_names_only_catalogue_agents(self) -> None:
        known = {a.catalogue_id for a in catalogue.AGENTS}
        for flow in catalogue.WORKFLOWS:
            assert set(flow.agents) <= known, flow.catalogue_id
            assert set(flow.connectors) <= {c.name for c in catalogue.CONNECTORS}

    def test_a_connectors_workflows_all_exist(self) -> None:
        known = {flow.catalogue_id for flow in catalogue.WORKFLOWS}
        for connector in catalogue.CONNECTORS:
            assert set(connector.workflows) <= known, connector.name

    def test_cloudflare_is_the_only_provider_needing_substitution(self) -> None:
        needing = [p.id for p in catalogue.PROVIDERS if p.requires]
        assert needing == ["cloudflare"]
        cloudflare = catalogue.provider_by_id("cloudflare")
        assert cloudflare is not None
        assert "{account_id}" in cloudflare.base_url
        # With the environment set, the placeholder is gone before a person
        # ever sees it.
        resolved = catalogue.resolve_base_url(cloudflare, {catalogue.CF_ACCOUNT_ID_ENV: "acct-123"})
        assert "{account_id}" not in resolved
        assert "acct-123" in resolved

    def test_google_workspace_is_absent(self) -> None:
        """Google runs no first-party Workspace MCP server, so the catalogue
        says nothing about Google rather than shipping somebody else's."""
        names = {c.name for c in catalogue.CONNECTORS}
        assert not {"google", "gmail", "gdrive", "google-workspace"} & names


class TestSigningUpSeedsIt:
    async def test_one_signup_leaves_a_workspace_with_something_to_press(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_client)

        assert [p["id"] for p in body["providers"]] == [p.id for p in catalogue.PROVIDERS]
        assert all(p["seeded"] for p in body["providers"])
        # Seeded, and honestly unconfigured: nothing here writes a key.
        assert not any(p["configured"] for p in body["providers"] if not p["local"])
        # Nothing is marked active here, and that is the seeder keeping its
        # hands off: this account inherited an env-configured provider on
        # signup (`adopt_legacy`) and it stays the active one. Seeding only
        # activates when the account had nothing active at all.
        assert not any(p["active"] for p in body["providers"])
        assert (await seeded_client.get("/api/config")).json()["provider_label"]

        assert all(c["seeded"] for c in body["connectors"])
        assert all(c["state"] == "disconnected" for c in body["connectors"])
        assert all(a["seeded"] and a["agent_id"] for a in body["agents"])
        assert all(w["seeded"] and w["workflow_id"] for w in body["workflows"])

    async def test_the_settings_page_sees_the_same_connectors_by_name(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        """The console joins a catalogue connector to its preset on `name`, so
        the two spellings have to be one spelling."""
        settings = (await seeded_client.get("/api/settings")).json()
        presets = {server["name"]: server for server in settings["mcp_servers"]}
        assert {c.name for c in catalogue.CONNECTORS} <= set(presets)

        github = presets["github"]
        assert github["url"] == "https://api.githubcopilot.com/mcp/"
        # Optional, so the specialist publishes and runs before anybody has
        # finished an OAuth flow; and a browser redirect, not the preset's own
        # `client_credentials` default, which would fail at connect time.
        assert github["optional"] is True
        assert github["oauth"]["grant"] == "authorization_code"
        assert github["oauth"]["allow_dynamic_registration"] is False

    async def test_no_secret_is_created_for_an_api_key_connector(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        settings = (await seeded_client.get("/api/settings")).json()
        presets = {server["name"]: server for server in settings["mcp_servers"]}
        assert presets["zapier"]["credential"] == "zapier_token"
        assert settings["secrets"] == []

        body = await _catalogue(seeded_client)
        zapier = next(c for c in body["connectors"] if c["name"] == "zapier")
        assert zapier["credential"] == "zapier_token"
        assert zapier["credential_set"] is False

    async def test_the_psych_agent_rosters_every_specialist(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_client)
        by_catalogue_id = {a["catalogue_id"]: a for a in body["agents"]}
        psych_id = by_catalogue_id[PSYCH_AGENT_ID]["agent_id"]

        summary = (await seeded_client.get(f"/api/agents/{psych_id}")).json()
        roster = {ref["name"] for ref in summary["subagents"]}
        assert roster == {c.name for c in catalogue.CONNECTORS}
        # Each roster entry points at the agent the catalogue published, not at
        # some other agent with the same name.
        ids = {ref["name"]: ref["agent_id"] for ref in summary["subagents"]}
        for connector in catalogue.CONNECTORS:
            assert ids[connector.name] == by_catalogue_id[connector.name]["agent_id"]
        # And it can compose children of its own as well as delegate to these.
        assert summary["subagents_enabled"] is True

    async def test_a_specialist_publishes_although_nothing_is_connected(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        """The whole reason connectors seed `optional`. A required server would
        mean twenty-five agents that cannot run until twenty-five OAuth flows
        are finished."""
        body = await _catalogue(seeded_client)
        github = next(a for a in body["agents"] if a["catalogue_id"] == "github")
        summary = (await seeded_client.get(f"/api/agents/{github['agent_id']}")).json()
        assert summary["mcp_servers"] == ["github"]

        run = await seeded_client.post(
            "/api/runs", json={"agent_id": github["agent_id"], "message": "hello"}
        )
        assert run.status_code == 201, run.text

    async def test_the_workflows_reference_the_seeded_agents(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_client)
        agents = {a["catalogue_id"]: a["agent_id"] for a in body["agents"]}
        report = next(w for w in body["workflows"] if w["catalogue_id"] == "github-activity-report")

        summary = (await seeded_client.get(f"/api/workflows/{report['workflow_id']}")).json()
        gather = next(step for step in summary["steps"] if step["name"] == "gather")
        assert gather["kind"] == "agent"
        assert gather["agent_id"] == agents["github"]
        # A human step before anything leaves the building.
        assert any(step["kind"] == "human" for step in summary["steps"])

    async def test_seeding_is_per_account(
        self, seeded_client: httpx.AsyncClient, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A second account gets its own copy, not the first one's agents."""
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as second:
            await sign_up(second, "other@example.com")
            mine = await _catalogue(seeded_client)
            theirs = await _catalogue(second)

        assert all(a["seeded"] for a in theirs["agents"])
        mine_ids = {a["agent_id"] for a in mine["agents"]}
        their_ids = {a["agent_id"] for a in theirs["agents"]}
        assert not mine_ids & their_ids


class TestSeedingAgain:
    async def test_the_route_adds_nothing_the_second_time(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        first = await seeded_client.post("/api/catalogue/seed", json={})
        assert first.status_code == 200, first.text
        # Signup already seeded, so even the *first* explicit call adds nothing.
        assert first.json()["added"] == {
            "providers": [],
            "connectors": [],
            "agents": [],
            "workflows": [],
        }

        before = await _catalogue(seeded_client)
        second = await seeded_client.post("/api/catalogue/seed", json={})
        assert second.json()["added"] == first.json()["added"]
        assert second.json()["catalogue"] == before

    async def test_it_seeds_an_account_that_never_had_it(self, client: httpx.AsyncClient) -> None:
        """`client` has seeding off at signup, which is exactly the account an
        upgrade produces: made before the catalogue existed."""
        body = await _catalogue(client)
        assert not any(a["seeded"] for a in body["agents"])

        response = await client.post("/api/catalogue/seed", json={})
        assert response.status_code == 200, response.text
        added = response.json()["added"]
        assert added["providers"] == [p.id for p in catalogue.PROVIDERS]
        assert added["connectors"] == [c.name for c in catalogue.CONNECTORS]
        assert set(added["agents"]) == {a.catalogue_id for a in catalogue.AGENTS}
        assert set(added["workflows"]) == {w.catalogue_id for w in catalogue.WORKFLOWS}

        assert response.json()["catalogue"] == await _catalogue(client)
        assert (await client.post("/api/catalogue/seed", json={})).json()["added"] == {
            "providers": [],
            "connectors": [],
            "agents": [],
            "workflows": [],
        }

    async def test_one_kind_at_a_time(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/catalogue/seed", json={"kinds": ["providers"]})
        assert response.status_code == 200, response.text
        added = response.json()["added"]
        assert added["providers"]
        assert not added["connectors"]
        assert not added["agents"]
        assert not added["workflows"]

        body = response.json()["catalogue"]
        assert all(p["seeded"] for p in body["providers"])
        assert not any(c["seeded"] for c in body["connectors"])

    async def test_a_deleted_agent_comes_back(self, seeded_client: httpx.AsyncClient) -> None:
        body = await _catalogue(seeded_client)
        linear = next(a for a in body["agents"] if a["catalogue_id"] == "linear")
        deleted = await seeded_client.delete(f"/api/agents/{linear['agent_id']}")
        assert deleted.status_code == 200, deleted.text

        added = (await seeded_client.post("/api/catalogue/seed", json={})).json()["added"]
        assert added["agents"] == ["linear"]

        after = await _catalogue(seeded_client)
        restored = next(a for a in after["agents"] if a["catalogue_id"] == "linear")
        assert restored["agent_id"]
        assert restored["agent_id"] != linear["agent_id"]
        # And the orchestrator was republished onto the agent that exists now,
        # rather than left pointing at the one that was deleted.
        psych = next(a for a in after["agents"] if a["catalogue_id"] == PSYCH_AGENT_ID)
        summary = (await seeded_client.get(f"/api/agents/{psych['agent_id']}")).json()
        ids = {ref["name"]: ref["agent_id"] for ref in summary["subagents"]}
        assert ids["linear"] == restored["agent_id"]


class TestTheStatusOnEachEntry:
    async def test_a_provider_reads_configured_once_it_has_a_key(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        settings = (await seeded_client.get("/api/settings")).json()
        providers = [
            {
                "id": p["id"],
                "label": p["label"],
                "base_url": p["base_url"],
                "model": p["model"],
                **({"api_key": "sk-test"} if p["id"] == "openai" else {}),
            }
            for p in settings["providers"]
        ]
        saved = await seeded_client.put("/api/settings/providers", json={"providers": providers})
        assert saved.status_code == 200, saved.text

        body = await _catalogue(seeded_client)
        openai = next(p for p in body["providers"] if p["id"] == "openai")
        assert openai["configured"] is True
        assert all(
            not p["configured"] for p in body["providers"] if p["id"] != "openai" and not p["local"]
        )

    async def test_ready_means_the_provider_in_use_can_answer(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        # This account inherited the env provider, which has no key: not ready.
        assert (await _catalogue(seeded_client))["provider_ready"] is False

        settings = (await seeded_client.get("/api/settings")).json()
        providers = [
            {
                "id": p["id"],
                "label": p["label"],
                "base_url": p["base_url"],
                "model": p["model"],
                **({"api_key": "sk-test"} if p["id"] == "anthropic" else {}),
            }
            for p in settings["providers"]
        ]
        saved = await seeded_client.put("/api/settings/providers", json={"providers": providers})
        assert saved.status_code == 200, saved.text
        # A key on a provider that is standing by changes nothing about the
        # one in use, so the console still has nothing that can answer.
        assert (await _catalogue(seeded_client))["provider_ready"] is False

        switched = await seeded_client.post("/api/settings/providers/anthropic/activate")
        assert switched.status_code == 200, switched.text
        assert (await _catalogue(seeded_client))["provider_ready"] is True

    async def test_a_local_provider_in_use_is_ready_without_a_key(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        switched = await seeded_client.post("/api/settings/providers/ollama/activate")
        assert switched.status_code == 200, switched.text
        assert (await _catalogue(seeded_client))["provider_ready"] is True

    async def test_it_needs_an_account(self, anonymous: httpx.AsyncClient) -> None:
        assert (await anonymous.get("/api/catalogue")).status_code == 401
        assert (await anonymous.post("/api/catalogue/seed", json={})).status_code == 401


class TestTheConsoleSurfacesAsTools:
    """`app.tools` offers the Agents and Workflows pages to an agent. These are
    the two that publish or dispatch, so these are the two where "the same path
    the route uses" has to be true rather than merely intended."""

    async def test_create_agent_publishes_for_the_calling_account(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        stub_provider.says(
            Turn(
                tool_calls=(
                    (
                        "create_agent",
                        {
                            "name": "sentry_watch",
                            "instructions": "Watch the error tracker and report what is new.",
                            "description": "Keeps an eye on Sentry.",
                            "connectors": ["sentry"],
                            "tools": ["current_time"],
                        },
                    ),
                ),
            ),
            Turn(text="Published sentry_watch."),
        )
        builder = await publish_agent(
            talking, name="builder", tools=["list_agents", "create_agent"]
        )
        run_id = await dispatch(talking, builder, "make me a Sentry agent")

        assert (await wait_for(talking, run_id))["terminal_state"] == "completed"

        listed = (await talking.get("/api/agents")).json()
        made = next((a for a in listed if a["name"] == "sentry_watch"), None)
        assert made is not None, listed
        # A catalogue connector by name, attached although this account has no
        # preset for it, and attached optionally so it publishes regardless.
        assert made["mcp_servers"] == ["sentry"]
        assert made["tools"] == ["current_time"]

        # And it is this account's, not a global: a stranger cannot see it.
        from app.main import app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as stranger:
            await sign_up(stranger, "stranger@example.com")
            assert (await stranger.get("/api/agents")).json() == []

    async def test_run_workflow_dispatches_one(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        created = await talking.post(
            "/api/workflows",
            json={
                "name": "sums",
                "steps": [
                    {
                        "kind": "tool",
                        "name": "add",
                        "tool": "calculate",
                        "arguments": {"expression": "2+2"},
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["workflow_id"]

        stub_provider.says(
            Turn(tool_calls=(("run_workflow", {"workflow_id": workflow_id, "input": {}}),)),
            Turn(text="Started it."),
        )
        runner = await publish_agent(
            talking, name="runner", tools=["list_workflows", "run_workflow"]
        )
        run_id = await dispatch(talking, runner, "run the sums workflow")
        assert (await wait_for(talking, run_id))["terminal_state"] == "completed"

        # The Run the tool started is indexed as a workflow Run, exactly as one
        # started from the console would be.
        rows = (await talking.get(f"/api/workflows/{workflow_id}/runs")).json()
        assert len(rows) == 1, rows
        assert rows[0]["kind"] == "workflow"
        started = rows[0]["run_id"]
        assert (await wait_for(talking, started))["lifecycle"] == "done"

    async def test_create_workflow_takes_an_agent_step(
        self, talking: httpx.AsyncClient, stub_provider: StubProvider
    ) -> None:
        """It used to accept tool steps only. With a specialist agent per
        connector there is something worth delegating to, so the tool validates
        the same twelve-kind union the route does."""
        helper = await publish_agent(talking, name="helper", tools=["current_time"])
        stub_provider.says(
            Turn(
                tool_calls=(
                    (
                        "create_workflow",
                        {
                            "name": "briefing",
                            "description": "Asks the helper, then waits for a person.",
                            "steps": [
                                {
                                    "kind": "agent",
                                    "name": "ask",
                                    "agent_id": helper,
                                    "input": {
                                        "message": {"kind": "literal", "value": "what day is it?"}
                                    },
                                },
                                {"kind": "human", "name": "sign_off", "prompt": "Looks right?"},
                            ],
                        },
                    ),
                ),
            ),
            Turn(text="Published briefing."),
        )
        author = await publish_agent(talking, name="author", tools=["create_workflow"])
        run_id = await dispatch(talking, author, "set up the briefing")
        assert (await wait_for(talking, run_id))["terminal_state"] == "completed"

        listed = (await talking.get("/api/workflows")).json()
        briefing = next(w for w in listed if w["name"] == "briefing")
        kinds = [step["kind"] for step in briefing["steps"]]
        assert kinds == ["agent", "human"]


class TestTheDockerComposeFirstRun:
    """No provider in the environment, seeding on: the install the README and
    the compose file describe, and the one the product claim is about."""

    async def test_it_ends_up_with_one_provider_selected(
        self, seeded_first_boot: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_first_boot)
        active = [p for p in body["providers"] if p["active"]]
        assert len(active) == 1, active
        # And the console reports it, rather than "no provider configured"
        # with eleven of them sitting in the list.
        assert (await seeded_first_boot.get("/api/config")).json()["provider_label"]
        # Honestly, though: seeding writes no key, so nothing reads configured.
        assert not any(p["configured"] for p in body["providers"] if not p["local"])
        # And a keyless provider in use is not one that can answer. The
        # console's "add a key" step stays open until one is added.
        assert body["provider_ready"] is False

    async def test_the_orchestrator_is_there_to_talk_to(
        self, seeded_first_boot: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_first_boot)
        psych = next(a for a in body["agents"] if a["catalogue_id"] == PSYCH_AGENT_ID)
        assert psych["agent_id"]
        summary = (await seeded_first_boot.get(f"/api/agents/{psych['agent_id']}")).json()
        assert len(summary["subagents"]) == len(catalogue.CONNECTORS)


class TestProvenanceSurvivesEditing:
    """The half a delete test does not reach: an agent is *meant* to be
    edited, and an edit that dropped `catalogue_id` would make the next seed
    publish a second copy of something somebody had merely tuned."""

    async def test_editing_a_seeded_agent_does_not_duplicate_it(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_client)
        github = next(a for a in body["agents"] if a["catalogue_id"] == "github")
        edited = await seeded_client.post(
            "/api/agents",
            json={
                "agent_id": github["agent_id"],
                "name": "github",
                "description": "Reads GitHub, and only reads it.",
                "instructions": "Look things up in GitHub. Never write anything.",
                "model": "test-model",
            },
        )
        assert edited.status_code == 201, edited.text

        added = (await seeded_client.post("/api/catalogue/seed", json={})).json()["added"]
        assert added["agents"] == []

        after = await _catalogue(seeded_client)
        still = next(a for a in after["agents"] if a["catalogue_id"] == "github")
        assert still["agent_id"] == github["agent_id"]
        assert still["seeded"] is True
        listed = (await seeded_client.get("/api/agents")).json()
        assert len(listed) == len(catalogue.AGENTS)

    async def test_editing_a_seeded_workflow_does_not_duplicate_it(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        body = await _catalogue(seeded_client)
        notes = next(w for w in body["workflows"] if w["catalogue_id"] == "release-notes")
        current = (await seeded_client.get(f"/api/workflows/{notes['workflow_id']}")).json()
        edited = await seeded_client.post(
            "/api/workflows",
            json={
                "workflow_id": notes["workflow_id"],
                "name": current["name"],
                "description": "Draft release notes, with one fewer step.",
                "steps": current["steps"][:1],
            },
        )
        assert edited.status_code == 201, edited.text

        added = (await seeded_client.post("/api/catalogue/seed", json={})).json()["added"]
        assert added["workflows"] == []

    async def test_update_agent_refuses_to_flatten_the_orchestrator(
        self, seeded_client: httpx.AsyncClient
    ) -> None:
        """`update_agent` can only describe instructions, description and
        connectors, so on an agent built out of more than that it stops. The
        alternative is republishing `psych` without its roster."""
        body = await _catalogue(seeded_client)
        psych = next(a for a in body["agents"] if a["catalogue_id"] == PSYCH_AGENT_ID)

        from app import tools as tool_module

        agents = tool_module._agents
        assert agents is not None
        account_id = (await seeded_client.get("/api/auth/me")).json()["id"]
        with pytest.raises(ValueError, match="delegation roster"):
            await agents.update_for(
                account_id,
                agent_id=psych["agent_id"],
                instructions="Do less.",
                description=None,
                connectors=None,
            )

        summary = (await seeded_client.get(f"/api/agents/{psych['agent_id']}")).json()
        assert len(summary["subagents"]) == len(catalogue.CONNECTORS)
