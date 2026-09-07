"""The example platform's REST contract, against the real app.

`examples/playground/backend` is the worked answer to "what does a consumer
have to build". It had no tests, while its own README pointed at tests that
would show a stored API key never reaching a response body. This is that
assertion, and the others its contract makes.

Nothing here reaches the network: the app runs in-process over
`httpx.ASGITransport`, its store is the in-memory adapter, and its model base
URL points at a closed port (see `conftest.py`). Two tests connect to an MCP
server, and it is the same `127.0.0.1` stub the library's own MCP tests use.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.ids import RunId
from psych_runtime.testing.mcp_stub import McpStubServer, wire_tool
from tests.playground.conftest import PASSWORD, sign_up

pytestmark = pytest.mark.functional

AGENT = {
    "name": "support",
    "instructions": "Help the customer with their order.",
    "model": "test-model",
    "tools": ["lookup_order"],
}


@pytest_asyncio.fixture
async def mcp_server() -> AsyncIterator[McpStubServer]:
    async with McpStubServer([wire_tool("lookup", read_only=True), wire_tool("wipe")]) as stub:
        yield stub


async def _publish(client: httpx.AsyncClient, **overrides: Any) -> str:
    response = await client.post("/api/agents", json={**AGENT, **overrides})
    assert response.status_code == 201, response.text
    return str(response.json()["version_hash"])


class TestSecretsNeverComeBack:
    """The README's own claim, asserted rather than stated.

    A settings API that stores an API key and a client secret has exactly one
    rule worth testing: neither value appears in any response, ever. The route
    narrows every answer through a response model to make that true, and a
    field added to that model without thinking is how it would stop being
    true.
    """

    async def test_a_provider_key_is_reported_as_a_boolean_and_never_echoed(
        self, client: httpx.AsyncClient
    ) -> None:
        secret = "sk-do-not-echo-me-6f2a"
        await client.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {
                        "label": "openai",
                        "base_url": "http://127.0.0.1:1/v1",
                        "model": "gpt-4o-mini",
                        "api_key": secret,
                    }
                ]
            },
        )

        for path in ("/api/settings", "/api/config"):
            body = (await client.get(path)).text
            assert secret not in body, f"{path} leaked the stored API key"

        provider = (await client.get("/api/settings")).json()["providers"][0]
        assert provider["has_api_key"] is True
        assert "api_key" not in provider

    async def test_a_secret_value_is_reported_by_name_only(self, client: httpx.AsyncClient) -> None:
        value = "s3cr3t-client-secret-91bd"
        stored = await client.put(
            "/api/settings/secrets", json={"secrets": {"crm_client_secret": value}}
        )
        assert stored.status_code == 200
        assert value not in stored.text

        settings = await client.get("/api/settings")
        assert value not in settings.text
        assert settings.json()["secrets"] == ["crm_client_secret"]

    async def test_an_omitted_key_keeps_the_stored_one_rather_than_clearing_it(
        self, client: httpx.AsyncClient
    ) -> None:
        """The merge rule the provider dialog depends on: editing a label must
        not silently unset the key nobody touched."""
        await client.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {
                        "label": "first",
                        "base_url": "http://127.0.0.1:1/v1",
                        "model": "m",
                        "api_key": "keep-me",
                    }
                ]
            },
        )
        provider = (await client.get("/api/settings")).json()["providers"][0]

        await client.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {
                        "id": provider["id"],
                        "label": "renamed",
                        "base_url": provider["base_url"],
                        "model": provider["model"],
                    }
                ]
            },
        )
        after = (await client.get("/api/settings")).json()["providers"][0]
        assert after["label"] == "renamed"
        assert after["has_api_key"] is True


class TestWhatAPublishedAgentActuallyCarries:
    """The bug this class exists for: a connection that passed the settings
    page's own test failed inside every Run.

    The test path built its `McpServer` with the transport the preset named
    and this process's own OAuth callback URL. The publish path built one
    without either, so an `authorization_code` agent published fine, connected
    fine when tested, and could never connect from a Run.
    """

    async def test_the_transport_survives_publication(self, client: httpx.AsyncClient) -> None:
        version_hash = await _publish(
            client,
            tools=[],
            mcp=[{"name": "legacy", "url": "http://127.0.0.1:1/mcp", "transport": "sse"}],
        )
        spec = await _stored_spec(client, version_hash)
        assert spec["mcp_servers"][0]["transport"] == "sse"

    async def test_an_authorization_code_grant_is_published_with_a_redirect_uri(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(
            client,
            tools=[],
            mcp=[
                {
                    "name": "crm",
                    "url": "http://127.0.0.1:1/mcp",
                    "oauth": {"grant": "authorization_code", "preregistered_client_id": "psych"},
                }
            ],
        )
        spec = await _stored_spec(client, version_hash)
        redirect_uris = spec["mcp_servers"][0]["oauth"]["redirect_uris"]
        assert redirect_uris, (
            "an authorization_code grant published with no redirect URI cannot connect "
            "from a Run, however well it tested from the settings page"
        )
        assert redirect_uris[0].endswith("/api/oauth/callback")

    async def test_client_credentials_is_published_without_one(
        self, client: httpx.AsyncClient
    ) -> None:
        """Not decoration: a redirect URI the grant ignores would still change
        the Version hash of every agent that uses this grant."""
        version_hash = await _publish(
            client,
            tools=[],
            mcp=[
                {
                    "name": "crm",
                    "url": "http://127.0.0.1:1/mcp",
                    "oauth": {"grant": "client_credentials"},
                }
            ],
        )
        spec = await _stored_spec(client, version_hash)
        assert spec["mcp_servers"][0]["oauth"]["redirect_uris"] == []

    async def test_preload_is_carried_so_a_large_catalogue_can_be_deferred(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(
            client,
            tools=[],
            mcp=[{"name": "big", "url": "http://127.0.0.1:1/mcp", "preload": False}],
        )
        spec = await _stored_spec(client, version_hash)
        assert spec["mcp_servers"][0]["preload"] is False


class TestPublishing:
    async def test_publishing_twice_creates_two_agents_sharing_one_version(
        self, client: httpx.AsyncClient
    ) -> None:
        """Two publishes with nothing linking them are two agents.

        They share a Version, which is content addressing working: the Spec is
        byte-identical, so there is one of it in the Store. What they do not
        share is identity. `created` used to answer "has this owner published
        this exact hash before", which was the only question askable when the
        console had no notion of an agent apart from a Version, and which said
        `False` for a genuine edit that happened to land back on an earlier
        configuration.
        """
        first = await client.post("/api/agents", json=AGENT)
        second = await client.post("/api/agents", json=AGENT)

        assert first.json()["version_hash"] == second.json()["version_hash"]
        assert first.json()["agent_id"] != second.json()["agent_id"]
        assert first.json()["created"] is True
        assert second.json()["created"] is True
        assert len((await client.get("/api/agents")).json()) == 2

    async def test_editing_an_agent_moves_it_rather_than_making_another(
        self, client: httpx.AsyncClient
    ) -> None:
        """The whole point of the pointer.

        Editing publishes a new Version -- a Version is still immutable and
        still content-hashed -- and moves the agent to it. One row in the list,
        two entries in its history.
        """
        created = (await client.post("/api/agents", json=AGENT)).json()
        edited = (
            await client.post(
                "/api/agents",
                json={**AGENT, "agent_id": created["agent_id"], "instructions": "Be brief."},
            )
        ).json()

        assert edited["agent_id"] == created["agent_id"]
        assert edited["version_hash"] != created["version_hash"]
        assert edited["created"] is False

        listed = (await client.get("/api/agents")).json()
        assert len(listed) == 1
        assert listed[0]["version_hash"] == edited["version_hash"]
        assert listed[0]["version_count"] == 2

        history = (await client.get(f"/api/agents/{created['agent_id']}/versions")).json()
        assert [v["version_hash"] for v in history] == [
            edited["version_hash"],
            created["version_hash"],
        ]
        assert [v["current"] for v in history] == [True, False]

    async def test_editing_back_to_an_earlier_configuration_is_not_a_third_version(
        self, client: httpx.AsyncClient
    ) -> None:
        """A hash this agent has already had moves to the end of the history
        rather than appearing twice. Listing it twice would claim an edit that
        produced nothing."""
        created = (await client.post("/api/agents", json=AGENT)).json()
        agent_id = created["agent_id"]
        await client.post(
            "/api/agents", json={**AGENT, "agent_id": agent_id, "instructions": "Be brief."}
        )
        back = (await client.post("/api/agents", json={**AGENT, "agent_id": agent_id})).json()

        assert back["version_hash"] == created["version_hash"]
        history = (await client.get(f"/api/agents/{agent_id}/versions")).json()
        assert len(history) == 2
        assert history[0]["version_hash"] == created["version_hash"]
        assert history[0]["current"] is True

    async def test_editing_an_agent_that_is_not_yours_publishes_nothing(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """Checked before the publish, so a refused edit does not leave a
        Version behind that nothing points at."""
        first, second = two_accounts
        mine = (await first.post("/api/agents", json=AGENT)).json()

        refused = await second.post(
            "/api/agents",
            json={**AGENT, "agent_id": mine["agent_id"], "instructions": "Different."},
        )
        assert refused.status_code == 404
        assert (await second.get("/api/agents")).json() == []

    async def test_a_run_pins_the_version_the_agent_pointed_at_when_it_started(
        self, client: httpx.AsyncClient
    ) -> None:
        """Why editing freely is safe.

        `psych_runtime.runtime.execute` reloads the pinned Version at the top of every
        Attempt, a reclaiming Worker's included. A Run whose Spec could change
        underneath would resume a conversation the model never had. So the
        pointer is read once, at admission, and the hash it gave is what the
        Run carries for the rest of its life.
        """
        created = (await client.post("/api/agents", json=AGENT)).json()
        run_id = (
            await client.post(
                "/api/runs", json={"agent_id": created["agent_id"], "message": "hello"}
            )
        ).json()["run_id"]

        edited = (
            await client.post(
                "/api/agents",
                json={**AGENT, "agent_id": created["agent_id"], "instructions": "Be brief."},
            )
        ).json()
        assert edited["version_hash"] != created["version_hash"]

        listed = {run["run_id"]: run for run in (await client.get("/api/runs")).json()}
        assert listed[run_id]["version_hash"] == created["version_hash"]

    async def test_a_dispatch_naming_both_an_agent_and_a_version_is_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """They can disagree, and there is no defensible way to pick. Refusing
        beats silently preferring one and running something nobody asked for."""
        created = (await client.post("/api/agents", json=AGENT)).json()
        response = await client.post(
            "/api/runs",
            json={
                "agent_id": created["agent_id"],
                "version_hash": created["version_hash"],
                "message": "hello",
            },
        )
        assert response.status_code == 400
        assert (await client.post("/api/runs", json={"message": "hello"})).status_code == 400

    async def test_an_unregistered_tool_is_refused_at_publish_with_its_path(
        self, client: httpx.AsyncClient
    ) -> None:
        """DESIGN.md §4: validation happens at publish, never at run. The
        response carries the path so a form can put the message on the field
        that caused it."""
        response = await client.post("/api/agents", json={**AGENT, "tools": ["no_such_tool"]})
        assert response.status_code == 400
        body = response.json()
        assert body["issues"], "a refused publish with no issues gives a form nothing to show"
        assert any("no_such_tool" in issue["message"] for issue in body["issues"])

    async def test_deleting_an_agent_stops_offering_it_and_leaves_its_runs_readable(
        self, client: httpx.AsyncClient
    ) -> None:
        created = (await client.post("/api/agents", json=AGENT)).json()
        agent_id = created["agent_id"]
        run_id = (
            await client.post("/api/runs", json={"agent_id": agent_id, "message": "hello"})
        ).json()["run_id"]

        assert (await client.delete(f"/api/agents/{agent_id}")).status_code == 200
        assert (await client.get("/api/agents")).json() == []

        # The Version is immutable and the Run pinned it, so the Run still reads.
        assert (await client.get(f"/api/runs/{run_id}/status")).status_code == 200
        assert (await client.delete(f"/api/agents/{agent_id}")).status_code == 404


class TestAttributingARunToItsAgent:
    """Two ways a Run can be attributed to the wrong agent, both from treating
    a Version hash as an identity.

    It is not one. A Version *is* its content, so two agents built from the
    same Spec -- which "Duplicate, then publish unchanged" produces in two
    clicks -- share a hash, and an agent edited since a Run started no longer
    runs the hash that Run pinned. The Run records its agent at admission
    instead, which is the only moment either question has an answer.
    """

    async def test_a_run_belongs_to_the_agent_it_was_dispatched_against(
        self, client: httpx.AsyncClient
    ) -> None:
        """The one a hash cannot answer.

        Both agents are byte-identical, so one Version covers both -- correct,
        and the point of content addressing. Attributing the Run by that hash
        would show it under both.
        """
        mine = (await client.post("/api/agents", json=AGENT)).json()
        twin = (await client.post("/api/agents", json=AGENT)).json()
        assert mine["version_hash"] == twin["version_hash"], "the premise"
        assert mine["agent_id"] != twin["agent_id"]

        run_id = (
            await client.post("/api/runs", json={"agent_id": mine["agent_id"], "message": "hi"})
        ).json()["run_id"]

        listed = {run["run_id"]: run for run in (await client.get("/api/runs")).json()}
        assert listed[run_id]["agent_id"] == mine["agent_id"]
        assert listed[run_id]["agent_id"] != twin["agent_id"]

    async def test_editing_the_agent_does_not_orphan_its_earlier_conversations(
        self, client: httpx.AsyncClient
    ) -> None:
        """The other direction. The Run keeps the Version it pinned, and keeps
        the agent that produced it, and the two facts are independent."""
        created = (await client.post("/api/agents", json=AGENT)).json()
        run_id = (
            await client.post("/api/runs", json={"agent_id": created["agent_id"], "message": "hi"})
        ).json()["run_id"]

        await client.post(
            "/api/agents",
            json={**AGENT, "agent_id": created["agent_id"], "instructions": "Be brief."},
        )

        listed = {run["run_id"]: run for run in (await client.get("/api/runs")).json()}
        assert listed[run_id]["agent_id"] == created["agent_id"]
        assert listed[run_id]["version_hash"] == created["version_hash"]

    async def test_a_second_message_stays_with_the_agent_the_thread_opened_on(
        self, client: httpx.AsyncClient
    ) -> None:
        """Continuing settles both fields from the Run it continues.

        A conversation is one agent's for its whole life, and it runs the
        Version it opened on. Re-resolving the pointer on every message would
        change the agent's instructions halfway through a thread, which is
        precisely what pinning exists to prevent.
        """
        created = (await client.post("/api/agents", json=AGENT)).json()
        first = (
            await client.post("/api/runs", json={"agent_id": created["agent_id"], "message": "hi"})
        ).json()["run_id"]

        await client.post(
            "/api/agents",
            json={**AGENT, "agent_id": created["agent_id"], "instructions": "Be brief."},
        )
        second = (
            await client.post(
                "/api/runs",
                json={
                    "agent_id": created["agent_id"],
                    "message": "and again",
                    "continues_run_id": first,
                },
            )
        ).json()["run_id"]

        listed = {run["run_id"]: run for run in (await client.get("/api/runs")).json()}
        assert listed[second]["agent_id"] == created["agent_id"]
        assert listed[second]["version_hash"] == created["version_hash"], (
            "a second message must run what the conversation opened on, not what "
            "the agent points at now"
        )

    async def test_a_thread_cannot_be_continued_into_someone_else_conversation(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """`continues_run_id` settles the Version, so it has to be checked
        against the caller before it is trusted for that."""
        first, second = two_accounts
        mine = (await first.post("/api/agents", json=AGENT)).json()
        run_id = (
            await first.post("/api/runs", json={"agent_id": mine["agent_id"], "message": "hi"})
        ).json()["run_id"]

        theirs = (await second.post("/api/agents", json=AGENT)).json()
        response = await second.post(
            "/api/runs",
            json={
                "agent_id": theirs["agent_id"],
                "message": "sneak in",
                "continues_run_id": run_id,
            },
        )
        # 403 rather than 404, and from Psych rather than from here: the
        # tenant check in `_resolve_dispatch_target` only declines to *inherit*
        # from a Run that is not the caller's, and `psych_runtime.dispatch(continues=)`
        # then refuses to chain across Scopes itself. Two independent checks,
        # and the inner one is the one that cannot be forgotten.
        assert response.status_code == 403


class _TreeCases:
    """Helpers shared by the branching and forking cases below."""

    @staticmethod
    async def _thread(client: httpx.AsyncClient, run_id: str) -> list[str]:
        """Everything said on ``run_id``'s branch, oldest first."""
        thread = (await client.get(f"/api/runs/{run_id}/thread")).json()
        return [message["content"] for message in thread["messages"]]

    @staticmethod
    async def _listed(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
        return {run["run_id"]: run for run in (await client.get("/api/runs")).json()}

    @staticmethod
    async def _conversation(client: httpx.AsyncClient) -> tuple[str, str, str, str]:
        """An agent and a three-message thread on it: the fixture every case
        below starts from."""
        agent = (await client.post("/api/agents", json=AGENT)).json()["agent_id"]
        first = (await client.post("/api/runs", json={"agent_id": agent, "message": "one"})).json()[
            "run_id"
        ]
        second = (
            await client.post(
                "/api/runs",
                json={"agent_id": agent, "message": "two", "continues_run_id": first},
            )
        ).json()["run_id"]
        third = (
            await client.post(
                "/api/runs",
                json={"agent_id": agent, "message": "three", "continues_run_id": second},
            )
        ).json()["run_id"]
        return agent, first, second, third


class TestBranchingAConversation(_TreeCases):
    """A message asked again, differently, without leaving the conversation.

    This is what editing a message does: the old
    answer stays, the new one sits beside it, and the history list still shows
    one chat that a person pages between with the small arrows. Branching is an
    ordinary dispatch -- the new Run continues the branched message's
    predecessor, so two Runs name one predecessor and the chain becomes a tree.
    Nothing in the runtime forbids that, and nothing in it helps either:
    ``Store`` has no query from a Run to the Runs continuing it, deliberately
    (DESIGN.md §7). What the console needs on top is a branch identity, and
    these are the cases that stop existing without one.
    """

    async def test_a_branch_shares_the_prefix_and_carries_only_its_own_tail(
        self, client: httpx.AsyncClient
    ) -> None:
        """The shape of the feature, asserted through the library's own
        projection rather than through this backend's bookkeeping.

        Branching at "two" means continuing the Run *before* it, so "one" is
        shared and "two" is where the two futures part.
        """
        _, _first, second, third = await self._conversation(client)

        branched = (
            await client.post(f"/api/runs/{second}/branch", json={"message": "two, differently"})
        ).json()["run_id"]

        assert await self._thread(client, branched) == ["one", "two, differently"]
        assert await self._thread(client, third) == ["one", "two", "three"]

    async def test_every_branch_belongs_to_one_conversation(
        self, client: httpx.AsyncClient
    ) -> None:
        """The distinction this whole pair of routes exists to draw.

        A branch takes a new ``branch_id`` and keeps the ``conversation_id``,
        so the history list shows one chat however many times a message is
        re-asked. It used to take a new conversation too, which put every
        re-wording in the sidebar as if it were a separate chat -- neither what
        branching means anywhere else nor what anybody wanted.
        """
        _, first, second, third = await self._conversation(client)

        branched = (
            await client.post(f"/api/runs/{second}/branch", json={"message": "two, differently"})
        ).json()["run_id"]

        listed = await self._listed(client)
        conversations = {listed[run]["conversation_id"] for run in (first, second, third, branched)}
        assert len(conversations) == 1, "one chat, however many branches"
        assert conversations != {""}
        assert listed[branched]["branch_id"] not in {"", listed[first]["branch_id"]}
        assert (
            listed[first]["branch_id"] == listed[second]["branch_id"] == listed[third]["branch_id"]
        )

    async def test_deleting_takes_every_branch_of_the_conversation(
        self, client: httpx.AsyncClient
    ) -> None:
        """Deleting a chat deletes the chat. Branches are the versions of a
        message a person pages between, so leaving some of them behind would
        leave fragments of a conversation that has been thrown away."""
        _, _first, second, _third = await self._conversation(client)
        await client.post(f"/api/runs/{second}/branch", json={"message": "two, differently"})

        assert (await client.delete(f"/api/runs/{second}?thread=true")).status_code == 200

        assert (await client.get("/api/runs")).json() == []

    async def test_each_branch_resolves_to_its_own_newest_run(
        self, client: httpx.AsyncClient
    ) -> None:
        """The bug the feature creates if branches have no identity.

        ``GET /api/threads/{run_id}/report`` walks *forward* from the Run it is
        given to find the end of the conversation, because ``psych_runtime.thread``
        can only walk backwards. The Run before the branch point now has two
        children, so a walk that follows "a child" follows an arbitrary one --
        and entering the report from the first message would report whichever
        branch it happened to meet. Entered from either branch's own newest
        Run, or from anywhere on it, the answer has to be that branch.
        """
        agent, first, second, third = await self._conversation(client)
        branched = (
            await client.post(f"/api/runs/{second}/branch", json={"message": "two, differently"})
        ).json()["run_id"]
        follow_on = (
            await client.post(
                "/api/runs",
                json={"agent_id": agent, "message": "four", "continues_run_id": branched},
            )
        ).json()["run_id"]

        original = (await client.get(f"/api/threads/{third}/report")).json()
        branch = (await client.get(f"/api/threads/{follow_on}/report")).json()

        assert original["run_ids"] == [first, second, third]
        assert branch["run_ids"] == [first, branched, follow_on]
        # Entering from a message in the middle of a branch lands on the same
        # branch, which is what a link from an older message has to do.
        assert (await client.get(f"/api/threads/{branched}/report")).json()["run_ids"] == branch[
            "run_ids"
        ]
        # And the totals are one branch's, never a mix: the two share exactly
        # the one Run before the branch point.
        assert original["totals"]["messages"] == 3
        assert branch["totals"]["messages"] == 3

    async def test_a_branch_cannot_change_the_agent(self, client: httpx.AsyncClient) -> None:
        """Re-asking a question of one conversation is a different wording, not
        a different correspondent. Allowing it would produce a thread whose two
        halves were answered by different Specs with nothing in the chat saying
        so. ``/fork`` is where a second agent belongs, and the field simply
        does not exist here rather than being accepted and ignored."""
        _, _first, second, _third = await self._conversation(client)
        other = (await client.post("/api/agents", json={**AGENT, "name": "second_opinion"})).json()

        response = await client.post(
            f"/api/runs/{second}/branch",
            json={"message": "two, differently", "agent_id": other["agent_id"]},
        )

        assert response.status_code == 422

    async def test_branching_the_opening_message_stays_in_the_conversation(
        self, client: httpx.AsyncClient
    ) -> None:
        """There is no predecessor to continue, so there is no history to
        carry -- but it is still the same chat, with two openings to page
        between. Refusing instead would make the affordance appear and
        disappear down the timeline for no reason a person could see."""
        _, first, _, _ = await self._conversation(client)

        branched = (
            await client.post(f"/api/runs/{first}/branch", json={"message": "one, differently"})
        ).json()["run_id"]

        assert await self._thread(client, branched) == ["one, differently"]
        listed = await self._listed(client)
        assert listed[branched]["conversation_id"] == listed[first]["conversation_id"]

    async def test_the_original_conversation_is_unchanged_by_the_branch(
        self, client: httpx.AsyncClient
    ) -> None:
        """An append-only log has no other honest answer, and this is the
        assertion that says so: every Record already written to every Run of
        the original is still there, unchanged and in the same order."""
        _, first, second, third = await self._conversation(client)

        async def logs() -> dict[str, list[str]]:
            """Every Record of the original, as the stream serialises them.

            Read as a list rather than compared as one blob because these Runs
            are still being executed by the Worker while the test runs: a
            Record appended between the two reads is that Run making progress,
            not the branch reaching into it. What has to hold is that nothing
            already written changed, which is prefix equality, and that is the
            whole claim an append-only log makes.
            """
            return {
                run_id: [
                    line
                    for line in (await client.get(f"/api/runs/{run_id}/stream")).text.splitlines()
                    if line.startswith("data: ") and line != "data: {}"
                ]
                for run_id in (first, second, third)
            }

        before = await logs()
        assert all(before.values()), "the premise: there is something to be unchanged"
        await client.post(f"/api/runs/{second}/branch", json={"message": "two, differently"})

        after = await logs()
        assert set(after) == set(before)
        for run_id, records in before.items():
            assert after[run_id][: len(records)] == records

    async def test_one_account_cannot_branch_another_account_conversation(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """A run id is not a secret. The route answers "not yours" with the
        same 404 as "does not exist"."""
        first, second = two_accounts
        mine = (await first.post("/api/agents", json=AGENT)).json()
        run_id = (
            await first.post("/api/runs", json={"agent_id": mine["agent_id"], "message": "hi"})
        ).json()["run_id"]

        await second.post("/api/agents", json=AGENT)
        response = await second.post(
            f"/api/runs/{run_id}/branch", json={"message": "let me see that"}
        )

        assert response.status_code == 404
        assert len((await second.get("/api/runs")).json()) == 0


class TestForkingAConversation(_TreeCases):
    """The same divergence point as a branch, taken into a new chat.

    A fork diverges exactly where a branch does and carries exactly the same
    history. The one difference is that it takes a fresh ``conversation_id``,
    so it is its own row in the history list, deleted on its own, and left
    standing when the conversation it came from is deleted.

    Nothing is copied to achieve that: the fork continues the very Runs it
    forked from. Those Runs therefore belong to one conversation and are read
    by another, which is Git's situation, and the deletion cases below are
    Git's answer to it.
    """

    async def test_a_fork_is_a_conversation_of_its_own(self, client: httpx.AsyncClient) -> None:
        _, first, second, _third = await self._conversation(client)

        forked = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, differently"})
        ).json()["run_id"]

        listed = await self._listed(client)
        assert listed[forked]["conversation_id"] not in {"", listed[first]["conversation_id"]}
        assert listed[forked]["branch_id"] not in {"", listed[first]["branch_id"]}
        # And it reads the history it forked from, which is what makes it a
        # fork rather than a new chat that happens to exist.
        assert await self._thread(client, forked) == ["one", "two, differently"]

    async def test_deleting_a_fork_leaves_the_conversation_it_came_from(
        self, client: httpx.AsyncClient
    ) -> None:
        """`git branch -d` deletes the ref and leaves the commits any other ref
        still reaches, and there is no reason for this to be different."""
        _, first, second, third = await self._conversation(client)
        forked = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, differently"})
        ).json()["run_id"]

        assert (await client.delete(f"/api/runs/{forked}?thread=true")).status_code == 200

        listed = await self._listed(client)
        assert forked not in listed, "the chat that was named is gone"
        assert {first, second, third} <= set(listed), "the one it was forked from is not"
        # Asked at the conversation's newest Run: `psych_runtime.thread` walks
        # parent-ward, so asking at `second` would correctly stop before
        # "three".
        assert await self._thread(client, third) == ["one", "two", "three"]

    async def test_deleting_the_original_keeps_the_fork_and_its_shared_history(
        self, client: httpx.AsyncClient
    ) -> None:
        """The harder direction, and the one reachability is for.

        The Runs before the fork point belong to the original conversation and
        are also the fork's own history. Removing them because the original was
        deleted would break the fork; keeping the original listed would delete
        nothing. Git keeps the commit and drops the ref, so the shared prefix
        survives and stops being a chat of its own.
        """
        _, first, second, third = await self._conversation(client)
        forked = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, differently"})
        ).json()["run_id"]

        assert (await client.delete(f"/api/runs/{third}?thread=true")).status_code == 200

        listed = await self._listed(client)
        assert forked in listed, "the fork survives its original"
        assert second not in listed, "the original conversation is gone"
        assert third not in listed
        assert first not in listed, "the shared prefix is no longer a chat of its own"
        # And the fork still reads it, which is the whole point of keeping it.
        assert await self._thread(client, forked) == ["one", "two, differently"]

    async def test_deleting_the_last_reader_collects_what_it_was_holding(
        self, client: httpx.AsyncClient
    ) -> None:
        """The collection pass. A Run kept only because some fork reached it is
        nobody's history once that fork goes, and leaving it would accumulate
        fragments nobody can open."""
        _, _first, second, third = await self._conversation(client)
        forked = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, differently"})
        ).json()["run_id"]

        await client.delete(f"/api/runs/{third}?thread=true")
        await client.delete(f"/api/runs/{forked}?thread=true")

        assert (await client.get("/api/runs")).json() == []

    async def test_a_sibling_fork_is_untouched(self, client: httpx.AsyncClient) -> None:
        """Two forks off one conversation, one deleted, the other and the
        original left."""
        _, first, second, _third = await self._conversation(client)
        one = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, a second way"})
        ).json()["run_id"]
        two = (
            await client.post(f"/api/runs/{second}/fork", json={"message": "two, a third way"})
        ).json()["run_id"]

        assert (await client.delete(f"/api/runs/{one}?thread=true")).status_code == 200

        listed = await self._listed(client)
        assert one not in listed
        assert {two, first, second} <= set(listed)
        assert await self._thread(client, two) == ["one", "two, a third way"]

    async def test_a_fork_can_run_a_different_agent(self, client: httpx.AsyncClient) -> None:
        """The most useful version of the feature: the same question, two
        agents, two chats a person can hold open side by side.

        A conversation is with one agent for its whole life, so this is the
        only way to ask a second one -- and it works because a fork is an
        ordinary dispatch rather than a special case in the runtime.
        """
        first_agent, _, second, _ = await self._conversation(client)
        other = (await client.post("/api/agents", json={**AGENT, "name": "second_opinion"})).json()

        forked = (
            await client.post(
                f"/api/runs/{second}/fork",
                json={"message": "two, differently", "agent_id": other["agent_id"]},
            )
        ).json()["run_id"]

        listed = await self._listed(client)
        assert listed[forked]["agent_id"] == other["agent_id"] != first_agent
        assert listed[forked]["version_hash"] == other["version_hash"]
        # The history still reaches it, so the second agent answers the same
        # question with the same context the first one had.
        assert await self._thread(client, forked) == ["one", "two, differently"]

    async def test_forking_the_opening_message_starts_a_fresh_conversation(
        self, client: httpx.AsyncClient
    ) -> None:
        """There is no predecessor to continue, so there is no history to
        carry. Refusing instead would make the affordance appear and disappear
        down the timeline for no reason a person could see."""
        _, first, _, _ = await self._conversation(client)

        forked = (
            await client.post(f"/api/runs/{first}/fork", json={"message": "one, differently"})
        ).json()["run_id"]

        assert await self._thread(client, forked) == ["one, differently"]

    async def test_one_account_cannot_fork_another_account_conversation(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """A run id is not a secret -- it is in a URL, in a log line -- and
        forking one would copy its history into the forker's own thread. The
        route answers "not yours" with the same 404 as "does not exist", since
        telling an unauthorized caller that a run id is real is itself a fact
        they did not have."""
        first, second = two_accounts
        mine = (await first.post("/api/agents", json=AGENT)).json()
        run_id = (
            await first.post("/api/runs", json={"agent_id": mine["agent_id"], "message": "hi"})
        ).json()["run_id"]

        theirs = (await second.post("/api/agents", json=AGENT)).json()
        response = await second.post(
            f"/api/runs/{run_id}/fork",
            json={"message": "let me see that", "agent_id": theirs["agent_id"]},
        )
        assert response.status_code == 404
        assert len((await second.get("/api/runs")).json()) == 0


class TestTracingIsReported:
    """`GET /api/config` says whether spans are being collected.

    Reported at all because "nothing is collecting" and "something is, but not
    where you are looking" are different problems that look identical from a
    console saying neither. Psych opens spans on every Run through the
    `Telemetry` port either way; this only says whether anything downstream is
    listening.
    """

    async def test_off_by_default(self, client: httpx.AsyncClient) -> None:
        """No collector configured means the no-op, which is what keeps
        `docker run` a single command: nobody should have to stand up a
        collector to look at a chat window."""
        tracing = (await client.get("/api/config")).json()["tracing"]
        assert tracing["enabled"] is False
        assert tracing["endpoint"] is None
        # Named even when off, because it is what somebody will search for once
        # they do point it at a collector.
        assert tracing["service_name"] != ""


class TestRunStatus:
    async def test_status_answers_in_the_vocabulary_a_screen_uses(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(client)
        run_id = (
            await client.post(
                "/api/runs",
                json={"version_hash": version_hash, "message": "hello"},
            )
        ).json()["run_id"]

        status = (await client.get(f"/api/runs/{run_id}/status")).json()
        assert status["lifecycle"] in {"queued", "running", "waiting", "stopping"}
        assert status["run_id"] == run_id
        # Never the store's own coarse words, which a person does not use.
        assert status["lifecycle"] not in {"runnable", "settled"}

    async def test_an_unknown_run_is_a_404_rather_than_a_hang(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/runs/run_does_not_exist/status")
        assert response.status_code == 404
        assert response.json()["detail"]

    async def test_a_thread_reports_the_conversation_across_its_chain(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(client)
        first = (
            await client.post(
                "/api/runs",
                json={"version_hash": version_hash, "message": "first"},
            )
        ).json()["run_id"]
        second = (
            await client.post(
                "/api/runs",
                json={
                    "version_hash": version_hash,
                    "message": "second",
                    "continues_run_id": first,
                },
            )
        ).json()["run_id"]

        thread = (await client.get(f"/api/runs/{second}/thread")).json()
        assert thread["run_ids"] == [first, second]
        said = [m["content"] for m in thread["messages"] if m["role"] == "user"]
        assert said == ["first", "second"]
        # Every message names the Run that produced it: `seq` is unique only
        # within one Run's log, so a flattened thread needs it to key them.
        assert {m["run_id"] for m in thread["messages"]} == {first, second}

        from_first = (await client.get(f"/api/runs/{first}/thread")).json()
        assert from_first["run_ids"] == [first, second]
        assert [m["content"] for m in from_first["messages"] if m["role"] == "user"] == [
            "first",
            "second",
        ]


class TestConnections:
    """What the console reads back after a refresh.

    The connect result used to live only in the browser's component state, so
    a server that had just connected offered a "Connect" button again on the
    next page load, with no memory that it had ever worked.
    """

    async def test_a_successful_connection_is_persisted_with_its_catalogue(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        await client.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url}]},
        )

        probe = (await client.post("/api/settings/mcp/orders/test")).json()
        assert probe["ok"] is True, probe["detail"]
        assert sorted(probe["tools"]) == ["lookup", "wipe"]

        stored = (await client.get("/api/settings")).json()["mcp_servers"][0]
        assert stored["last_connection"]["ok"] is True
        assert sorted(stored["last_connection"]["tools"]) == ["lookup", "wipe"]
        assert stored["last_connection"]["checked_at"]

    async def test_a_failed_connection_names_the_error_class_not_just_a_boolean(
        self, client: httpx.AsyncClient
    ) -> None:
        """ "It didn't work" sends someone to check the wrong thing. A missing
        credential and an unreachable host need different fixes."""
        await client.put(
            "/api/settings/mcp",
            json={
                "mcp_servers": [
                    {"name": "gone", "url": "http://127.0.0.1:1/mcp", "credential": "nope"}
                ]
            },
        )
        probe = (await client.post("/api/settings/mcp/gone/test")).json()
        assert probe["ok"] is False
        assert probe["error_type"] == "CredentialNotFound"

        stored = (await client.get("/api/settings")).json()["mcp_servers"][0]
        assert stored["last_connection"]["ok"] is False
        assert stored["last_connection"]["error_type"] == "CredentialNotFound"

    async def test_editing_a_preset_keeps_the_connection_it_already_proved(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        await client.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url}]},
        )
        await client.post("/api/settings/mcp/orders/test")

        # An allow-list edit changes nothing about reaching the server, so the
        # card must not fall back to "never connected" for it.
        await client.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url, "allow": ["lookup"]}]},
        )
        stored = (await client.get("/api/settings")).json()["mcp_servers"][0]
        assert stored["allow"] == ["lookup"]
        assert stored["last_connection"] is not None
        assert stored["last_connection"]["ok"] is True

    async def test_testing_an_unknown_preset_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/settings/mcp/nope/test")).status_code == 404


class TestStreaming:
    """`event: done` means the Run is over, and nothing else means it.

    The bug: the endpoint wrapped the record iterator in `wait_for`, so the
    first idle timeout cancelled the generator, the next read raised
    `StopAsyncIteration`, and `done` went out on a Run that had merely been
    quiet. A client treats `done` as terminal, so a suspended approval, a slow
    model or an OAuth wait all froze the page until a reload. The keep-alive
    runs beside the stream now rather than around it.
    """

    async def test_done_is_only_sent_once_the_run_has_actually_settled(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(client)
        run_id = (
            await client.post(
                "/api/runs",
                json={"version_hash": version_hash, "message": "hello"},
            )
        ).json()["run_id"]

        frames: list[str] = []
        async with client.stream("GET", f"/api/runs/{run_id}/stream", timeout=30) as response:
            assert response.status_code == 200
            # Without these a proxy buffers the whole response and the
            # "stream" arrives in one piece when the Run ends.
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            async for line in response.aiter_lines():
                frames.append(line)
                if line.startswith("event: done"):
                    break

        assert any(line.startswith("data:") for line in frames), "no records were delivered"
        status = (await client.get(f"/api/runs/{run_id}/status")).json()
        assert status["lifecycle"] in {"done", "failed", "stopped"}, (
            "the stream said done while the Run was still open, which is the bug "
            "that froze the page: a client treats done as terminal"
        )

    async def test_a_keep_alive_is_a_comment_rather_than_an_event(
        self, client: httpx.AsyncClient
    ) -> None:
        """A client parsing SSE must not mistake the idle heartbeat for a
        Record or for the end. Asserted on the frame this endpoint writes,
        because the shape is the whole point: a `:`-prefixed line is a comment
        per the SSE grammar and carries no field."""
        from app.main import _SSE_KEEPALIVE_SECONDS

        assert _SSE_KEEPALIVE_SECONDS > 0
        keep_alive = b": keep-alive\n\n"
        assert keep_alive.startswith(b":")
        assert b"data:" not in keep_alive
        assert b"event:" not in keep_alive

    async def test_reconnecting_with_after_delivers_exactly_the_remainder(
        self, client: httpx.AsyncClient
    ) -> None:
        """DESIGN.md §23 item 4, through the HTTP boundary: the log is the
        stream, so a client that reconnects with the last sequence it saw
        misses nothing and repeats nothing."""
        version_hash = await _publish(client)
        run_id = (
            await client.post(
                "/api/runs",
                json={"version_hash": version_hash, "message": "hello"},
            )
        ).json()["run_id"]

        whole = await _drain(client, run_id, after=0)
        assert [record["seq"] for record in whole] == list(range(1, len(whole) + 1))

        for cut in range(len(whole)):
            resumed = await _drain(client, run_id, after=cut)
            assert [r["seq"] for r in resumed] == [r["seq"] for r in whole[cut:]]


async def _drain(client: httpx.AsyncClient, run_id: str, *, after: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    async with client.stream(
        "GET", f"/api/runs/{run_id}/stream", params={"after": after}, timeout=30
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:") and line != "data: {}":
                records.append(json.loads(line[len("data:") :]))
            elif line.startswith("event: done"):
                break
    return records


async def _stored_spec(client: httpx.AsyncClient, version_hash: str) -> dict[str, Any]:
    """The Spec as published, read back through the process's own Store.

    Not served by any route: the console has no reason to read a Spec back,
    and inventing an endpoint so a test can look would be testing something
    nobody runs.
    """
    from app.main import app

    store = app.state.playground.store
    version = await store.get_version(version_hash)
    assert version is not None
    dumped: dict[str, Any] = version.spec.model_dump(mode="json")
    return dumped


class TestConnectionDescriptions:
    """What a connected system is for reaches the model, and never the hash.

    An agent wired to three servers sees three names. `eq-admin`, `deepwiki`
    and `crm` are not answers to "which of these should I use".
    """

    async def test_a_description_is_stored_and_returned(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        await client.put(
            "/api/settings/mcp",
            json={
                "mcp_servers": [
                    {
                        "name": "orders",
                        "url": mcp_server.url,
                        "description": "The order system: lookups and refunds.",
                    }
                ]
            },
        )
        stored = (await client.get("/api/settings")).json()["mcp_servers"][0]
        assert stored["description"] == "The order system: lookups and refunds."

    async def test_describing_a_connection_does_not_republish_agents(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        """The reason the description is not a Spec field.

        Publish an agent against a connection, describe the connection, then
        republish the same agent. Same Version. If the description lived in the
        Spec this would mint a second one, and every agent naming the server
        would need republishing whenever somebody improved a sentence.
        """
        await client.put(
            "/api/settings/mcp", json={"mcp_servers": [{"name": "orders", "url": mcp_server.url}]}
        )
        body = {**AGENT, "tools": [], "mcp": [{"name": "orders", "url": mcp_server.url}]}
        first = await client.post("/api/agents", json=body)
        assert first.json()["created"] is True
        body = {**body, "agent_id": first.json()["agent_id"]}

        await client.put(
            "/api/settings/mcp",
            json={
                "mcp_servers": [
                    {"name": "orders", "url": mcp_server.url, "description": "Orders and refunds."}
                ]
            },
        )

        second = await client.post("/api/agents", json=body)
        assert second.json()["version_hash"] == first.json()["version_hash"]
        assert second.json()["created"] is False
        # And the agent still has exactly one configuration in its history.
        history = (await client.get(f"/api/agents/{first.json()['agent_id']}/versions")).json()
        assert len(history) == 1

    async def test_an_edited_description_takes_effect_without_a_restart(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        """The runtime reads a live map rather than the file it was booted
        with, so a correction in the console reaches the next turn."""
        from app.main import app

        # Keyed by `(tenant, url)`: a description belongs to one account's
        # connection, and two accounts pointing at one URL must not share one.
        account = (await client.get("/api/config")).json()["account_id"]
        key = (account, mcp_server.url)

        await client.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url, "description": "old"}]},
        )
        assert app.state.playground.descriptions[key] == "old"

        await client.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url, "description": "new"}]},
        )
        assert app.state.playground.descriptions[key] == "new"

        # And removing it stops the text reaching the prompt, rather than
        # leaving the old sentence in place until someone restarts.
        await client.put(
            "/api/settings/mcp", json={"mcp_servers": [{"name": "orders", "url": mcp_server.url}]}
        )
        assert key not in app.state.playground.descriptions


class TestTheAnswerEndpoint:
    """The console reads a run two ways and they cannot disagree, because both
    project the same log."""

    async def test_an_answer_carries_its_work_and_its_summary(
        self, client: httpx.AsyncClient
    ) -> None:
        version_hash = await _publish(client)
        run_id = (
            await client.post(
                "/api/runs",
                json={"version_hash": version_hash, "message": "hello"},
            )
        ).json()["run_id"]

        answer = (await client.get(f"/api/runs/{run_id}/answer")).json()
        assert answer["run_id"] == run_id
        # No model is reachable in this suite, so the run fails rather than
        # answering. The shape is what matters here: an unfinished run says so
        # instead of presenting an empty string as an answer.
        assert answer["finished"] is False
        assert answer["text"] == ""
        assert isinstance(answer["work"], list)

    async def test_an_unknown_run_is_a_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/runs/run_nope/answer")).status_code == 404

    async def test_the_answer_style_is_published_and_reported(
        self, client: httpx.AsyncClient
    ) -> None:
        """It changes the prompt, so it is part of the agent and part of its
        hash. Two agents differing only in style are two agents."""
        plain = await client.post("/api/agents", json={**AGENT, "name": "plain"})
        concise = await client.post(
            "/api/agents", json={**AGENT, "name": "plain", "answer_style": "concise"}
        )
        assert plain.json()["version_hash"] != concise.json()["version_hash"]

        listed = {a["name"]: a for a in (await client.get("/api/agents")).json()}
        assert listed["plain"]["answer_style"] in {None, "concise"}

        spec = await _stored_spec(client, concise.json()["version_hash"])
        assert spec["answer_style"] == "concise"

    async def test_structured_components_are_published_and_read_back(
        self, client: httpx.AsyncClient
    ) -> None:
        """The flag has to survive the round trip, or an edit turns it off.

        The console fills its edit form from the agent list, so a flag the list
        cannot report is a flag the next edit silently drops: somebody changes
        the instructions and the agent quietly loses the ability to answer with
        a chart.
        """
        plain = await client.post("/api/agents", json={**AGENT, "name": "shopper"})
        rich = await client.post(
            "/api/agents", json={**AGENT, "name": "shopper", "components_enabled": True}
        )
        # A different tool set on every turn is a different agent.
        assert plain.json()["version_hash"] != rich.json()["version_hash"]

        spec = await _stored_spec(client, rich.json()["version_hash"])
        assert spec["components_enabled"] is True

        # Keyed by id rather than name: these are two agents that happen to
        # share one, which is exactly the pair worth telling apart here.
        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        assert listed[rich.json()["agent_id"]]["components_enabled"] is True
        assert listed[plain.json()["agent_id"]]["components_enabled"] is False

    async def test_editing_an_agent_keeps_the_flag_it_was_published_with(
        self, client: httpx.AsyncClient
    ) -> None:
        """Read from the Spec rather than remembered, so what comes back is
        what was published."""
        created = (
            await client.post(
                "/api/agents", json={**AGENT, "name": "shopper", "components_enabled": True}
            )
        ).json()
        edited = (
            await client.post(
                "/api/agents",
                json={
                    **AGENT,
                    "name": "shopper",
                    "agent_id": created["agent_id"],
                    "instructions": "Be brief.",
                    "components_enabled": True,
                },
            )
        ).json()
        assert edited["agent_id"] == created["agent_id"]

        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        assert listed[created["agent_id"]]["components_enabled"] is True

        # And turning it off is an ordinary edit rather than a stuck flag.
        off = (
            await client.post(
                "/api/agents",
                json={**AGENT, "name": "shopper", "agent_id": created["agent_id"]},
            )
        ).json()
        assert off["version_hash"] != edited["version_hash"]
        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        assert listed[created["agent_id"]]["components_enabled"] is False

    async def test_an_oversized_description_is_refused_rather_than_truncated(
        self, client: httpx.AsyncClient, mcp_server: McpStubServer
    ) -> None:
        """The stored value and the sentence the agent reads are the same one.

        Psych truncates anything longer before it reaches a prompt, so an
        oversized description was never a risk to the model. It was a risk to
        the person: they type six hundred characters, the form says four
        hundred, the API takes all six, and the prompt quietly carries the
        first four hundred.
        """
        response = await client.put(
            "/api/settings/mcp",
            json={
                "mcp_servers": [{"name": "orders", "url": mcp_server.url, "description": "x" * 401}]
            },
        )
        assert response.status_code == 422

        ok = await client.put(
            "/api/settings/mcp",
            json={
                "mcp_servers": [{"name": "orders", "url": mcp_server.url, "description": "x" * 400}]
            },
        )
        assert ok.status_code == 200


class TestTheFrontDoor:
    """Sign up, sign in, sign out."""

    async def test_every_route_is_either_guarded_or_deliberately_public(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """The test that catches the route somebody adds next year.

        Reads the routes off the live app and requires each one to either
        demand an account or appear on a list with a written reason. A new
        endpoint that forgets the check fails here rather than in production,
        and the failure names the path.

        The public list is deliberately short and each entry earns its place:
        `/api/health` answers nothing about anybody, the three auth routes are
        how a session comes to exist at all, and the OAuth callback receives a
        browser redirected by an authorization server that has no reason to
        carry this console's cookie -- it is guarded instead by the unguessable
        `state` value `OAuthClient` minted (see the route's own docstring).
        """
        import inspect
        import re

        import app.main as backend
        from app.auth import PUBLIC_PATHS

        public = set(PUBLIC_PATHS)

        source = inspect.getsource(backend)
        blocks = re.split(r"\n@app\.(?:get|post|put|delete)\(", source)
        guarded: set[str] = set()
        unguarded: set[str] = set()
        for block in blocks[1:]:
            # The path may sit on the decorator's own line or on the next one:
            # a route with enough arguments wraps, and the formatter decides
            # which. This scan is about whether a route checks for an account,
            # so it must not also be a check on how the decorator was laid out.
            match = re.match(r'\s*"([^"]+)"', block)
            assert match is not None, f"could not read a path from: {block[:120]!r}"
            path = match.group(1)
            body = block.split("\n@app.")[0]
            (guarded if "_account(request)" in body else unguarded).add(path)

        assert unguarded == public, (
            f"routes missing an account check: {sorted(unguarded - public)}; "
            f"routes on the public list that now check: {sorted(public - unguarded)}"
        )
        assert len(guarded) >= 25, "the route scan found suspiciously little"

    async def test_a_401_still_carries_its_cors_headers(self, anonymous: httpx.AsyncClient) -> None:
        """The ordering bug that presents as "cannot reach the backend".

        Starlette's `add_middleware` inserts at position 0, so the last
        registration is the outermost layer. Registered the wrong way round,
        the session check answered 401 before CORS attached a header, and the
        browser refused to read a perfectly correct response -- while the
        server log showed the request arriving and being answered, which sends
        you looking in entirely the wrong place.

        Asserted on a 401 specifically, because a 200 goes through the same
        code either way and would pass with the ordering broken.
        """
        origin = "http://127.0.0.1:3010"
        response = await anonymous.get("/api/config", headers={"Origin": origin})

        assert response.status_code == 401
        assert response.headers.get("access-control-allow-origin") == origin
        assert response.headers.get("access-control-allow-credentials") == "true"

    async def test_a_preflight_is_answered_without_a_session(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """A CORS preflight carries no cookie by design, so refusing it for
        want of a session would block the very request that would have had
        one."""
        response = await anonymous.request(
            "OPTIONS",
            "/api/runs",
            headers={
                "Origin": "http://127.0.0.1:3010",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3010"

    async def test_an_unauthenticated_request_is_refused_everywhere_it_matters(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        for method, path in [
            ("GET", "/api/config"),
            ("GET", "/api/agents"),
            ("POST", "/api/agents"),
            ("GET", "/api/runs"),
            ("POST", "/api/runs"),
            ("GET", "/api/settings"),
            ("PUT", "/api/settings/secrets"),
            ("GET", "/api/tools"),
            ("GET", "/api/auth/me"),
        ]:
            response = await anonymous.request(method, path, json={})
            assert response.status_code == 401, f"{method} {path} answered {response.status_code}"

    async def test_health_answers_before_anyone_has_signed_up(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """So the console's first screen can offer to create an account rather
        than showing a sign-in form for accounts that do not exist."""
        first = (await anonymous.get("/api/health")).json()
        assert first["ok"] is True
        assert first["needs_first_account"] is True

        await sign_up(anonymous, "someone@example.com")

        after = (await anonymous.get("/api/health")).json()
        assert after["needs_first_account"] is False

    async def test_signing_in_wrong_says_the_same_thing_either_way(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """A login form that distinguishes "no such account" from "wrong
        password" is an account-enumeration oracle."""
        await sign_up(anonymous, "real@example.com")

        unknown = await anonymous.post(
            "/api/auth/signin", json={"email": "ghost@example.com", "password": PASSWORD}
        )
        wrong = await anonymous.post(
            "/api/auth/signin", json={"email": "real@example.com", "password": "not the password"}
        )

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_a_short_password_is_refused_with_advice(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        response = await anonymous.post(
            "/api/auth/signup", json={"email": "a@example.com", "password": "short"}
        )
        assert response.status_code == 400
        assert "12 characters" in response.json()["detail"]

    async def test_the_same_address_cannot_be_registered_twice(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        await sign_up(anonymous, "taken@example.com")
        again = await anonymous.post(
            "/api/auth/signup", json={"email": "TAKEN@Example.com ", "password": PASSWORD}
        )
        assert again.status_code == 409, "an address differing only in case is the same address"

    async def test_signing_out_ends_the_session_and_is_idempotent(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        await sign_up(anonymous, "bye@example.com")
        assert (await anonymous.get("/api/auth/me")).status_code == 200

        assert (await anonymous.post("/api/auth/signout")).status_code == 200
        assert (await anonymous.get("/api/auth/me")).status_code == 401
        # A sign-out that can fail is one people stop trusting.
        assert (await anonymous.post("/api/auth/signout")).status_code == 200

    async def test_a_password_never_appears_in_any_response(
        self, anonymous: httpx.AsyncClient
    ) -> None:
        """The same assertion the README already makes about API keys, applied
        to the one credential that is worse to leak."""
        await sign_up(anonymous, "quiet@example.com")
        for path in ("/api/auth/me", "/api/config", "/api/settings"):
            body = (await anonymous.get(path)).text
            assert PASSWORD not in body
            assert "password" not in body.lower()
            assert "scrypt" not in body


class TestIsolationBetweenAccounts:
    """Two people, one backend, one state file.

    The property under test is not "the list is filtered". It is that naming
    somebody else's resource directly gets you nothing, because a run id is not
    a secret: it sits in a URL, in a log line, in a link someone pasted.
    """

    async def test_neither_account_sees_the_other_agents(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        first, second = two_accounts
        await first.post("/api/agents", json=AGENT)

        assert len((await first.get("/api/agents")).json()) == 1
        assert (await second.get("/api/agents")).json() == []

    async def test_two_accounts_publishing_one_spec_each_own_their_own_agent(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """The case a hash-keyed index gets wrong.

        A Version is its content hash, so byte-identical Specs published by two
        people are one Version -- correct, and the point of content addressing.
        The *index* entry is per owner, so neither person's list, timestamps or
        deletions touch the other's.
        """
        first, second = two_accounts
        mine = await first.post("/api/agents", json=AGENT)
        theirs = await second.post("/api/agents", json=AGENT)

        assert mine.json()["version_hash"] == theirs.json()["version_hash"], (
            "identical Specs must still be one Version"
        )
        # And each person created something, from where they are standing.
        assert mine.json()["created"] is True
        assert theirs.json()["created"] is True

        assert mine.json()["agent_id"] != theirs.json()["agent_id"]

        # Deleting from one list leaves the other intact.
        assert (await first.delete(f"/api/agents/{mine.json()['agent_id']}")).status_code == 200
        assert (await first.get("/api/agents")).json() == []
        assert len((await second.get("/api/agents")).json()) == 1

    async def test_an_agent_cannot_be_deleted_by_someone_who_does_not_own_it(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        first, second = two_accounts
        agent_id = (await first.post("/api/agents", json=AGENT)).json()["agent_id"]

        assert (await second.delete(f"/api/agents/{agent_id}")).status_code == 404
        assert len((await first.get("/api/agents")).json()) == 1

    async def test_a_run_cannot_be_dispatched_against_someone_else_agent(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """A Version hash is content, so anyone holding the same Spec can
        compute it. Knowing the hash must not be enough to run it."""
        first, second = two_accounts
        version_hash = (await first.post("/api/agents", json=AGENT)).json()["version_hash"]

        response = await second.post(
            "/api/runs", json={"version_hash": version_hash, "message": "hello"}
        )
        assert response.status_code == 404

    async def test_naming_another_account_run_id_directly_gets_nothing(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """Every read path, not just the list. This is the test that would have
        caught filtering `GET /api/runs` and forgetting the rest."""
        first, second = two_accounts
        version_hash = (await first.post("/api/agents", json=AGENT)).json()["version_hash"]
        run_id = (
            await first.post("/api/runs", json={"version_hash": version_hash, "message": "hello"})
        ).json()["run_id"]

        # The owner can reach it.
        assert (await first.get(f"/api/runs/{run_id}/status")).status_code == 200

        for path in (
            f"/api/runs/{run_id}/status",
            f"/api/runs/{run_id}/report",
            f"/api/runs/{run_id}/messages",
            f"/api/runs/{run_id}/thread",
            f"/api/runs/{run_id}/answer",
            f"/api/runs/{run_id}/stream",
        ):
            response = await second.get(path)
            assert response.status_code == 404, f"{path} answered {response.status_code}"

        for method, path, body in (
            ("DELETE", f"/api/runs/{run_id}", None),
            ("POST", f"/api/runs/{run_id}/interrupt", {}),
            ("POST", f"/api/runs/{run_id}/resume", {"approved": True}),
        ):
            response = await second.request(method, path, json=body)
            assert response.status_code == 404, f"{method} {path} answered {response.status_code}"

        # And it is still there afterwards, so none of that half-worked.
        assert (await first.get(f"/api/runs/{run_id}/status")).status_code == 200

    async def test_not_yours_and_does_not_exist_answer_the_same(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """A 403 would confirm the run id is real, which is a fact the caller
        did not have."""
        first, second = two_accounts
        version_hash = (await first.post("/api/agents", json=AGENT)).json()["version_hash"]
        run_id = (
            await first.post("/api/runs", json={"version_hash": version_hash, "message": "hi"})
        ).json()["run_id"]

        real = await second.get(f"/api/runs/{run_id}/status")
        invented = await second.get("/api/runs/run_0000000000000000000000000000/status")
        assert real.status_code == invented.status_code == 404

    async def test_secrets_and_providers_do_not_cross(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """The credential leak this whole change exists to close: one state
        file that used to hand whoever loaded the page next the API key the
        last person pasted in."""
        first, second = two_accounts
        await first.put("/api/settings/secrets", json={"secrets": {"eq-secret": "first's value"}})
        await first.put(
            "/api/settings/providers",
            json={
                "providers": [
                    {
                        "label": "mine",
                        "base_url": "http://127.0.0.1:1/v1",
                        "model": "m",
                        "api_key": "sk-first",
                    }
                ]
            },
        )

        theirs = (await second.get("/api/settings")).json()
        assert theirs["secrets"] == []
        assert theirs["providers"] == []
        assert "sk-first" not in (await second.get("/api/settings")).text
        assert "first's value" not in (await second.get("/api/settings")).text

        # The same secret *name* resolves to each account's own value, which is
        # what stops one account's MCP connection borrowing another's token.
        await second.put("/api/settings/secrets", json={"secrets": {"eq-secret": "second's value"}})
        assert (await first.get("/api/settings")).json()["secrets"] == ["eq-secret"]
        assert (await second.get("/api/settings")).json()["secrets"] == ["eq-secret"]

        from app.main import app

        resolver = app.state.playground.secrets
        import psych_runtime

        first_id = (await first.get("/api/config")).json()["account_id"]
        second_id = (await second.get("/api/config")).json()["account_id"]
        mine = await resolver.resolve(psych_runtime.Scope(tenant=first_id), "eq-secret")
        yours = await resolver.resolve(psych_runtime.Scope(tenant=second_id), "eq-secret")
        assert mine is not None
        assert yours is not None
        assert mine.secret.get_secret_value() == "first's value"
        assert yours.secret.get_secret_value() == "second's value"
        assert mine.identity != yours.identity, (
            "the pool keys connections by credential identity, so two values must differ there too"
        )

    async def test_connections_do_not_cross(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], mcp_server: McpStubServer
    ) -> None:
        first, second = two_accounts
        await first.put(
            "/api/settings/mcp",
            json={"mcp_servers": [{"name": "orders", "url": mcp_server.url}]},
        )
        assert (await first.post("/api/settings/mcp/orders/test")).json()["ok"] is True

        # Not listed, and not testable by name either.
        assert (await second.get("/api/settings")).json()["mcp_servers"] == []
        assert (await second.post("/api/settings/mcp/orders/test")).status_code == 404
        assert (await second.get("/api/config")).json()["mcp_servers"] == []

    async def test_a_live_connection_is_not_shown_to_another_account(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], mcp_server: McpStubServer
    ) -> None:
        """The pool is process-wide and holds every account's connections.
        Keyed by server name alone, one person's live connection showed up
        under another person's preset of the same name."""
        first, second = two_accounts
        preset = {"mcp_servers": [{"name": "orders", "url": mcp_server.url}]}
        await first.put("/api/settings/mcp", json=preset)
        await second.put("/api/settings/mcp", json=preset)
        await first.post("/api/settings/mcp/orders/test")

        theirs = (await second.get("/api/settings")).json()["mcp_servers"][0]
        assert theirs["live"] is None, "a connection nobody on this account made"

    async def test_a_run_carries_its_owner_scope_into_the_log(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """The tenant reaches Psych itself, not just this backend's index.

        `RunHeader.scope` is what Psych filters every store query by and what
        the ownership check reads, so it has to be the account rather than a
        process-wide constant.
        """
        first, _ = two_accounts
        account_id = (await first.get("/api/config")).json()["account_id"]
        version_hash = (await first.post("/api/agents", json=AGENT)).json()["version_hash"]
        run_id = (
            await first.post("/api/runs", json={"version_hash": version_hash, "message": "hi"})
        ).json()["run_id"]

        from app.main import app

        from psych_runtime.core.ids import RunId

        header = await app.state.playground.store.get_run(RunId(run_id))
        assert header is not None
        assert header.scope.tenant == account_id
        assert account_id.startswith("acct_")


REFUND_BODY = (
    "1. Confirm the order is under 30 days old.\n"
    "2. Refund to the original payment method, never store credit.\n"
    "3. Anything over $500 needs a manager. See [[skill:escalation]]."
)
ESCALATION = {
    "name": "escalation",
    "description": "When to involve a manager",
    "body": "Page the on-call manager in #escalations.",
}
REFUND = {"name": "refund-policy", "description": "How refunds work", "body": REFUND_BODY}


PEER: dict[str, Any] = {
    "name": "research",
    "url": "https://agents.example.com",
    "description": "Deep research",
    "credential": "research_token",
    "scheme": "Bearer",
    "tenant": None,
    "allow": [],
    "optional": False,
    "extensions": [],
}


ATTACHED = {key: value for key, value in PEER.items() if key != "description"}
"""The same peer as `POST /api/agents` takes it.

`description` is a console-only field: it says what the peer is for in a
person's words, and the model learns what it does from the peer's own agent
card instead. So it lives on the preset and never reaches the Spec, which is
why attaching one has to drop it rather than pass the preset through whole.
"""


class TestA2APeers:
    """A2A's console half: pointing an agent at another agent.

    The library can serve A2A and can call it, but nothing in this backend
    could *configure* an outbound peer, so A2A was inbound-only in practice.
    These cover the preset round trip and the one property that matters:
    attaching copies, so editing a preset never changes a published agent.
    """

    async def test_a_peer_round_trips_through_settings(self, client: httpx.AsyncClient) -> None:
        response = await client.put("/api/settings/a2a", json={"a2a_peers": [PEER]})
        assert response.status_code == 200
        listed = (await client.get("/api/settings")).json()["a2a_peers"]
        assert [peer["name"] for peer in listed] == ["research"]
        assert listed[0]["credential"] == "research_token", "the credential NAME, not a secret"

    async def test_a_published_agent_carries_its_peers(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/agents", json={**AGENT, "a2a": [ATTACHED]})
        assert response.status_code == 201, response.text
        published = response.json()
        spec = await _stored_spec(client, published["version_hash"])
        assert [peer["name"] for peer in spec["a2a_peers"]] == ["research"]
        assert spec["a2a_peers"][0]["url"] == "https://agents.example.com"
        # By name in the catalogue, like `mcp_servers`: the address and the
        # credential name live in the Spec and are not reported back.
        agent = (await client.get(f"/api/agents/{published['agent_id']}")).json()
        assert agent["a2a_peers"] == ["research"]

    async def test_editing_a_preset_leaves_a_published_agent_alone(
        self, client: httpx.AsyncClient
    ) -> None:
        """The whole reason a peer is copied rather than referenced."""
        await client.put("/api/settings/a2a", json={"a2a_peers": [PEER]})
        published = (await client.post("/api/agents", json={**AGENT, "a2a": [ATTACHED]})).json()

        await client.put(
            "/api/settings/a2a",
            json={"a2a_peers": [{**PEER, "url": "https://somewhere-else.example.com"}]},
        )

        spec = await _stored_spec(client, published["version_hash"])
        assert spec["a2a_peers"][0]["url"] == "https://agents.example.com"

    async def test_attaching_a_peer_moves_the_version_hash(self, client: httpx.AsyncClient) -> None:
        """Which agents an agent may call is part of what it is."""
        without = await client.post("/api/agents", json=AGENT)
        with_peer = await client.post("/api/agents", json={**AGENT, "a2a": [ATTACHED]})
        assert without.json()["version_hash"] != with_peer.json()["version_hash"]

    async def test_two_peers_with_one_name_are_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.put(
            "/api/settings/a2a",
            json={"a2a_peers": [PEER, {**PEER, "url": "https://other.example.com"}]},
        )
        assert response.status_code == 400
        assert "research" in response.text

    async def test_a_peer_needs_a_name_and_an_address(self, client: httpx.AsyncClient) -> None:
        blank = await client.put("/api/settings/a2a", json={"a2a_peers": [{**PEER, "name": " "}]})
        assert blank.status_code == 400
        no_url = await client.put("/api/settings/a2a", json={"a2a_peers": [{**PEER, "url": " "}]})
        assert no_url.status_code == 400

    async def test_one_account_cannot_see_another_peers(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        first, second = two_accounts
        await first.put("/api/settings/a2a", json={"a2a_peers": [PEER]})
        assert (await second.get("/api/settings")).json()["a2a_peers"] == []


class TestSkillLibrary:
    """Skills written once, attachable to any agent this account builds.

    The design decision worth testing is what "global" is allowed to mean. It
    means "available to every agent you build", never "reaching into every
    agent you have built". Attaching a library skill **copies** it into the
    published Spec, so it joins the Version hash, and editing the library
    afterwards changes nothing about an agent already published.

    That is deliberately unlike an MCP server description, which Psych does
    read at turn time. A description is a fact about somebody else's
    system. A skill body is instructions the model follows, and instructions
    that can change under a published Version make two Runs of one Version
    behave differently -- exactly what DESIGN.md §23.1 asks not to happen.
    """

    async def test_the_library_is_stored_and_read_back_whole(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.put("/api/settings/skills", json={"skills": [REFUND, ESCALATION]})
        assert response.status_code == 200

        listed = (await client.get("/api/settings")).json()["skills"]
        assert {skill["name"] for skill in listed} == {"refund-policy", "escalation"}
        # Bodies and all: a library that returned descriptions only could not
        # fill in the form it exists to fill in.
        assert {skill["body"] for skill in listed} == {REFUND["body"], ESCALATION["body"]}

    async def test_editing_the_library_leaves_a_published_agent_alone(
        self, client: httpx.AsyncClient
    ) -> None:
        """The whole design argument, as an assertion."""
        await client.put("/api/settings/skills", json={"skills": [ESCALATION]})
        # Attaching is a copy, which is what the form does: the library entry's
        # three fields go into the request.
        published = (
            await client.post("/api/agents", json={**AGENT, "skills": [ESCALATION]})
        ).json()

        await client.put(
            "/api/settings/skills",
            json={"skills": [{**ESCALATION, "body": "Rewritten. Nobody may escalate."}]},
        )

        spec = await _stored_spec(client, published["version_hash"])
        assert spec["skills"][0]["body"] == ESCALATION["body"]
        # And the agent still points where it did: a library edit is not a
        # publish, so nothing about the agent moved either.
        agent = (await client.get(f"/api/agents/{published['agent_id']}")).json()
        assert agent["version_hash"] == published["version_hash"]
        assert agent["skills"][0]["body"] == ESCALATION["body"]

    async def test_republishing_picks_the_library_up_under_a_new_version(
        self, client: httpx.AsyncClient
    ) -> None:
        """The other half, and the honest meaning of a library edit: it changes
        what the *next* publish gets."""
        await client.put("/api/settings/skills", json={"skills": [ESCALATION]})
        first = (await client.post("/api/agents", json={**AGENT, "skills": [ESCALATION]})).json()

        rewritten = {**ESCALATION, "body": "Escalate anything over $100 to a manager."}
        await client.put("/api/settings/skills", json={"skills": [rewritten]})
        second = (
            await client.post(
                "/api/agents",
                json={**AGENT, "agent_id": first["agent_id"], "skills": [rewritten]},
            )
        ).json()

        assert second["version_hash"] != first["version_hash"]
        assert second["agent_id"] == first["agent_id"]

    async def test_two_agents_attaching_the_same_library_skills_are_one_version(
        self, client: httpx.AsyncClient
    ) -> None:
        """`AgentSpec.skills` is a sorted set, so attaching the same library
        entries in either order has to hash the same. Otherwise a library would
        multiply Versions for no reason a person could see."""
        await client.put("/api/settings/skills", json={"skills": [REFUND, ESCALATION]})
        one = await client.post("/api/agents", json={**AGENT, "skills": [REFUND, ESCALATION]})
        two = await client.post("/api/agents", json={**AGENT, "skills": [ESCALATION, REFUND]})

        assert one.json()["version_hash"] == two.json()["version_hash"]

    async def test_two_library_skills_with_one_name_are_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """A Spec's skills are keyed by name, so attaching both would publish
        one and silently drop the other."""
        response = await client.put(
            "/api/settings/skills",
            json={"skills": [REFUND, {**ESCALATION, "name": "refund-policy"}]},
        )
        assert response.status_code == 400
        assert "refund-policy" in response.text

    async def test_a_library_skill_needs_a_name(self, client: httpx.AsyncClient) -> None:
        response = await client.put(
            "/api/settings/skills", json={"skills": [{**REFUND, "name": "  "}]}
        )
        assert response.status_code == 400

    async def test_one_account_cannot_see_another_library(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        first, second = two_accounts
        await first.put("/api/settings/skills", json={"skills": [REFUND]})

        assert (await second.get("/api/settings")).json()["skills"] == []
        assert len((await first.get("/api/settings")).json()["skills"]) == 1


class TestSkills:
    """DESIGN.md §16 through the console's own contract.

    The library's own behaviour is covered by `tests/e2e/test_skills_memory.py`.
    What these assert is the half that was missing: that an agent published
    through this API can carry a skill at all, and that the properties a person
    depends on survive the round trip through the request shape and the index.
    """

    async def test_a_published_agent_carries_its_skills(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/agents", json={**AGENT, "skills": [REFUND, ESCALATION]})
        assert response.status_code == 201

        listed = (await client.get("/api/agents")).json()[0]
        names = [skill["name"] for skill in listed["skills"]]
        assert names == ["escalation", "refund-policy"], "sorted, as the Spec stores them"
        # Bodies included: the duplicate form is filled from this list, and a
        # skill returned without its instructions would publish an empty one.
        bodies = {skill["name"]: skill["body"] for skill in listed["skills"]}
        assert bodies["refund-policy"] == REFUND_BODY

    async def test_the_order_they_were_typed_in_does_not_change_the_version(
        self, client: httpx.AsyncClient
    ) -> None:
        """`AgentSpec.skills` is a sorted set, so two people describing the same
        agent must get one Version rather than two that differ only in typing
        order (DESIGN.md §4)."""
        one = await client.post("/api/agents", json={**AGENT, "skills": [REFUND, ESCALATION]})
        two = await client.post("/api/agents", json={**AGENT, "skills": [ESCALATION, REFUND]})

        assert one.json()["version_hash"] == two.json()["version_hash"]

    async def test_adding_a_skill_moves_the_version(self, client: httpx.AsyncClient) -> None:
        """The other half: a skill is part of what the agent *is*, so changing
        the set has to produce a different Version."""
        without = await client.post("/api/agents", json=AGENT)
        with_skill = await client.post("/api/agents", json={**AGENT, "skills": [ESCALATION]})

        assert without.json()["version_hash"] != with_skill.json()["version_hash"]

    async def test_a_dangling_link_is_refused_at_publish_and_names_the_field(
        self, client: httpx.AsyncClient
    ) -> None:
        """Validation runs at publish, never at run (DESIGN.md §4), and the
        console's agent form is exactly where somebody hand-writes a link and
        gets it wrong. The refusal has to reach the field that wrote it, or the
        form can only show a generic failure at the top."""
        response = await client.post("/api/agents", json={**AGENT, "skills": [REFUND]})

        assert response.status_code == 400
        problem = response.json()
        paths = [issue["path"] for issue in problem["issues"]]
        assert "support.skills.refund-policy.body" in paths
        message = next(issue["message"] for issue in problem["issues"] if issue["path"] == paths[0])
        assert "escalation" in message, "the message names the link that dangles"
        # And nothing was published.
        assert (await client.get("/api/agents")).json() == []

    async def test_skills_do_not_cross_between_accounts(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient]
    ) -> None:
        """A skill body is text somebody wrote, and it is the kind of text a
        company would not want in another tenant's console."""
        first, second = two_accounts
        await first.post("/api/agents", json={**AGENT, "skills": [ESCALATION]})

        assert (await second.get("/api/agents")).json() == []
        assert "#escalations" not in (await second.get("/api/agents")).text


class TestMemory:
    """DESIGN.md §15 through the console's own contract.

    The library's own behaviour is covered by `tests/functional/test_memory.py`
    and `tests/e2e/test_skills_memory.py`. What these assert is the half that
    was missing: that a Run in this console can remember anything at all, that
    what it remembered survives, and that one account's facts are unreachable
    from another's.
    """

    async def test_an_account_reads_only_its_own_memories(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], app: Any
    ) -> None:
        """Written through the store rather than through a Run, so the check is
        about the isolation key and not about whether a scripted model happened
        to call `remember`.

        The second account asks under its own scope and gets nothing. That is
        the assertion that matters: a memory is keyed by tenant *and* end user,
        and reading someone else's requires holding both, which a signed-in
        person cannot do for anybody but themselves.
        """
        first, second = two_accounts
        memory = app.state.playground.memory
        mine = (await first.get("/api/auth/me")).json()
        theirs = (await second.get("/api/auth/me")).json()

        await memory.remember(
            psych_runtime.Scope(tenant=mine["id"], principal=mine["id"]),
            mine["id"],
            "Prefers refunds to the original card.",
        )

        assert len((await first.get("/api/memories")).json()["memories"]) == 1
        assert (await second.get("/api/memories")).json()["memories"] == []
        # And not by naming the other account's key directly either.
        assert (
            await memory.recall(
                psych_runtime.Scope(tenant=theirs["id"], principal=theirs["id"]), theirs["id"]
            )
            == ()
        )

    async def test_forgetting_a_fact_that_is_not_yours_finds_nothing(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], app: Any
    ) -> None:
        """A memory id is not a secret. `forget` looks the id up under the
        caller's own key rather than scanning, so holding one buys nothing."""
        first, second = two_accounts
        memory = app.state.playground.memory
        mine = (await first.get("/api/auth/me")).json()
        fact = await memory.remember(
            psych_runtime.Scope(tenant=mine["id"], principal=mine["id"]),
            mine["id"],
            "A private fact.",
        )

        assert (await second.delete(f"/api/memories/{fact.id}")).status_code == 404
        assert len((await first.get("/api/memories")).json()["memories"]) == 1

    async def test_erase_is_complete_and_leaves_the_other_account_alone(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], app: Any
    ) -> None:
        """§15's actual requirement: a consumer's own customers will ask them to
        delete their data, and this is what answering that looks like."""
        first, second = two_accounts
        memory = app.state.playground.memory
        mine = (await first.get("/api/auth/me")).json()
        theirs = (await second.get("/api/auth/me")).json()
        for owner, client_id in ((mine, mine["id"]), (theirs, theirs["id"])):
            for text in ("one", "two"):
                await memory.remember(
                    psych_runtime.Scope(tenant=owner["id"], principal=owner["id"]), client_id, text
                )

        assert (await first.delete("/api/memories")).status_code == 200

        assert (await first.get("/api/memories")).json()["memories"] == []
        assert len((await second.get("/api/memories")).json()["memories"]) == 2

    async def test_a_run_names_the_end_user_it_reads_memories_for(
        self, client: httpx.AsyncClient
    ) -> None:
        """`end_user_id` is passed at dispatch rather than left to fall through
        to `Scope.principal`, so a trace says whose memories a Run was reading
        instead of leaving it to be re-derived. See `app.memory_store`."""
        me = (await client.get("/api/auth/me")).json()
        published = await client.post("/api/agents", json=AGENT)
        run = await client.post(
            "/api/runs",
            json={"version_hash": published.json()["version_hash"], "message": "hello"},
        )
        run_id = run.json()["run_id"]

        # Read from the store's log rather than from a route: `end_user_id` is
        # dispatch input, and no endpoint serves raw records. What matters is
        # that it reached `RunAdmitted`, where a trace can find it.
        from app.main import app as fastapi_app

        log = await fastapi_app.state.playground.store.read(RunId(run_id))
        admitted = next(record for record in log if type(record).__name__ == "RunAdmitted")
        assert admitted.input["end_user_id"] == me["id"]


class TestCompaction:
    """A compaction policy has to survive the round trip whole.

    The console fills its edit form from `GET /api/agents`, so a field the list
    cannot report is a field the next edit silently changes. A bare "compaction
    is on" would be worse than nothing here: the form would put its own
    starting numbers back, and somebody who fixed a typo in the instructions
    would republish an agent that summarises on terms they never chose.
    """

    POLICY: ClassVar[dict[str, Any]] = {
        "trigger_tokens": 40_000,
        "keep_recent_turns": 5,
        "model": "cheap-summariser",
        "max_summary_tokens": 1_024,
        "summary_instructions": "Always keep every order number and its status.",
    }

    async def test_a_policy_is_published_and_read_back_field_for_field(
        self, client: httpx.AsyncClient
    ) -> None:
        plain = await client.post("/api/agents", json={**AGENT, "name": "longhaul"})
        compacting = await client.post(
            "/api/agents", json={**AGENT, "name": "longhaul", "compaction": self.POLICY}
        )
        assert compacting.status_code == 201, compacting.text

        # An agent shown a summary in place of its own older turns is not being
        # shown the same conversation, so it is not the same agent.
        assert plain.json()["version_hash"] != compacting.json()["version_hash"]

        spec = await _stored_spec(client, compacting.json()["version_hash"])
        assert spec["compaction"] == self.POLICY
        assert (await _stored_spec(client, plain.json()["version_hash"]))["compaction"] is None

        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        assert listed[compacting.json()["agent_id"]]["compaction"] == self.POLICY
        assert listed[plain.json()["agent_id"]]["compaction"] is None

    async def test_republishing_what_came_back_changes_nothing(
        self, client: httpx.AsyncClient
    ) -> None:
        """The edit form's whole round trip, asserted through the hash.

        Sending back exactly what the list reported must land on the same
        Version. Any field the list dropped would show up here as a second
        hash, which is the failure this test is for and the only one a reader
        of the JSON would not notice.
        """
        created = (
            await client.post(
                "/api/agents", json={**AGENT, "name": "longhaul", "compaction": self.POLICY}
            )
        ).json()
        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        read_back = listed[created["agent_id"]]["compaction"]

        edited = (
            await client.post(
                "/api/agents",
                json={
                    **AGENT,
                    "name": "longhaul",
                    "agent_id": created["agent_id"],
                    "compaction": read_back,
                },
            )
        ).json()
        assert edited["version_hash"] == created["version_hash"]

        # And turning it off is an ordinary edit rather than a stuck policy.
        off = (
            await client.post(
                "/api/agents",
                json={**AGENT, "name": "longhaul", "agent_id": created["agent_id"]},
            )
        ).json()
        assert off["version_hash"] != created["version_hash"]
        listed = {agent["agent_id"]: agent for agent in (await client.get("/api/agents")).json()}
        assert listed[created["agent_id"]]["compaction"] is None

    async def test_the_defaults_are_the_library_own(self, client: httpx.AsyncClient) -> None:
        """Only `trigger_tokens` is required, and nothing here invents the rest.

        `trigger_tokens` has no default anywhere below this API on purpose:
        Psych has no context window to take a fraction of. The console's form
        picks a starting number and says so; this API does not, and the other
        three come from `psych_runtime.CompactionPolicy` rather than from a second
        table that would drift from it.
        """
        version_hash = await _publish(
            client, name="longhaul", compaction={"trigger_tokens": 20_000}
        )
        spec = await _stored_spec(client, version_hash)
        assert spec["compaction"] == {
            "trigger_tokens": 20_000,
            "keep_recent_turns": 3,
            "model": None,
            "max_summary_tokens": 2_048,
            "summary_instructions": None,
        }

    async def test_a_trigger_of_zero_is_refused_at_publish(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/agents",
            json={**AGENT, "name": "longhaul", "compaction": {"trigger_tokens": 0}},
        )
        assert response.status_code == 422, response.text


class TestSubagents:
    """The console's half of DESIGN.md §17: watch a tree, and intervene in it.

    The tree is built here rather than by running one, because these tests run
    against a model base URL pointing at a closed port -- no Run in this suite
    reaches a model. What the routes actually read is the log, so a log written
    directly is the same input a real Run would produce, and building it by hand
    is what makes "still running" and "already finished" facts rather than
    races.
    """

    async def _account_id(self, client: httpx.AsyncClient) -> str:
        return str((await client.get("/api/auth/me")).json()["id"])

    async def _tree(
        self, client: httpx.AsyncClient, app: Any, *, finish_child: bool = True
    ) -> tuple[str, str]:
        """A parent Run with one composed child, written straight into the log."""
        from psych_runtime.core.scope import Scope
        from psych_runtime.runtime.dispatch import dispatch as dispatch_run
        from psych_runtime.runtime.journal import Journal

        state = app.state.playground
        scope = Scope(tenant=await self._account_id(client), principal="tester")

        version_hash = await _publish(client, subagents_enabled=True)
        parent = (
            await client.post(
                "/api/runs", json={"version_hash": version_hash, "message": "split this"}
            )
        ).json()["run_id"]

        child_spec = psych_runtime.AgentSpec(
            name="alpha",
            instructions="You are a subagent. Your job: price part 88-B.",
            model=psych_runtime.ModelRef(model="test-model"),
        )
        child_version = await psych_runtime.publish(state.store, child_spec)
        child = await dispatch_run(
            state.store,
            child_version,
            scope,
            input={"message": "Find the list price of part 88-B."},
            parent_run_id=RunId(parent),
            delegation_depth=1,
        )

        journal = await Journal.open(state.store, RunId(parent), scope)
        await journal.append(type="attempt_started", worker_id="wrk_1", attempt_number=1)
        await journal.append(type="turn_started", turn=1)
        await journal.append(type="model_call_started", turn=1, model="test-model")
        await journal.append(
            type="model_call_finished",
            turn=1,
            model="test-model",
            usage={"input": 30, "output": 5},
            cost=None,
            timings={},
            finish_reason="tool_calls",
            tool_calls=("call-1",),
        )
        await journal.append(
            type="tool_call_started",
            call_id="call-1",
            tool="spawn_subagent",
            arguments={"name": "alpha"},
            turn=1,
        )
        await journal.append(
            type="subagent_spawned",
            child_run_id=child.run_id,
            name="alpha",
            call_id="call-1",
            child_version_hash=child_version.hash,
            purpose="Price part 88-B across our suppliers.",
            task="Find the list price of part 88-B from every supplier we use.",
            deliverable="A list of supplier and price.",
            tools=("lookup_order",),
            model="test-model",
            delegation_depth=1,
        )
        await journal.append(type="tool_call_finished", call_id="call-1", outcome="ok", result={})

        if finish_child:
            child_journal = await Journal.open(state.store, child.run_id, scope)
            await child_journal.append(type="attempt_started", worker_id="wrk_2", attempt_number=1)
            await child_journal.append(type="turn_started", turn=1)
            await child_journal.append(type="model_call_started", turn=1, model="test-model")
            await child_journal.append(
                type="model_call_finished",
                turn=1,
                model="test-model",
                usage={"input": 100, "output": 20},
                cost=None,
                timings={},
                finish_reason="stop",
                text="Acme at 12.40.",
            )
            await child_journal.append(
                type="run_settled", state="completed", output={"text": "Acme at 12.40."}
            )
            await journal.append(
                type="subagent_finished",
                child_run_id=child.run_id,
                name="alpha",
                state="completed",
                output={"text": "Acme at 12.40."},
                usage={"input": 100, "output": 20},
            )
        return parent, child.run_id

    async def test_the_flag_publishes_an_envelope_rather_than_a_roster(
        self, client: httpx.AsyncClient
    ) -> None:
        """On, the agent may write its own children. The Spec says what they may
        be given, never which ones exist."""
        version_hash = await _publish(client, subagents_enabled=True)
        spec = await _stored_spec(client, version_hash)
        assert spec["spawn"] is not None
        assert spec["subagents"] == []

    async def test_the_flag_is_off_by_default_and_reported_back(
        self, client: httpx.AsyncClient
    ) -> None:
        """It joins the Version hash, so an edit form that could not read it
        back would publish a different agent from the one being edited."""
        plain = await _publish(client)
        assert (await _stored_spec(client, plain))["spawn"] is None

        composing = await _publish(client, subagents_enabled=True)
        agents = {
            a["version_hash"]: a["subagents_enabled"]
            for a in (await client.get("/api/agents")).json()
        }
        assert agents[composing] is True
        assert agents[plain] is False

    async def test_the_tree_reports_each_child_with_its_branch_rolled_up(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        parent, child_id = await self._tree(client, app)

        tree = (await client.get(f"/api/runs/{parent}/subagents")).json()
        assert tree["run_id"] == parent
        assert len(tree["children"]) == 1

        node = tree["children"][0]
        assert node["name"] == "alpha"
        assert node["run_id"] == child_id
        assert node["parent_run_id"] == parent
        assert node["state"] == "done"
        assert node["terminal_state"] == "completed"
        assert node["tools"] == ["lookup_order"]
        assert node["purpose"].startswith("Price part 88-B")
        assert node["latest"] == "Acme at 12.40."
        assert node["input_tokens"] == 100

        # The parent's own tokens plus the child's, and honest about the price:
        # no model here has one, so the cost is null rather than "0.00".
        assert tree["total_input_tokens"] == 130
        assert tree["total_cost"] is None
        assert tree["complete"] is True

    async def test_a_running_child_is_reported_as_running_and_incomplete(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """A branch still working cannot be rolled up, and the tree says so
        rather than reporting a total that will change."""
        parent, _ = await self._tree(client, app, finish_child=False)
        tree = (await client.get(f"/api/runs/{parent}/subagents")).json()
        assert tree["children"][0]["state"] in {"queued", "running"}
        assert tree["children"][0]["terminal_state"] is None
        assert tree["complete"] is False

    async def test_a_console_message_reaches_the_childs_own_log(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """The same steering queue the parent agent's own tool uses, so a person
        and an agent steering one child do it one way."""
        parent, child_id = await self._tree(client, app, finish_child=False)
        response = await client.post(
            f"/api/runs/{parent}/subagents/{child_id}/message",
            json={"message": "Only suppliers we have a contract with."},
        )
        assert response.status_code == 200

        # In the child's own log, as a queue entry: a steer is delivered at the
        # start of its next turn, so it is recorded now and consumed later.
        from psych_runtime.core.records import QueueEnqueued

        records = await psych_runtime.records(app.state.playground.store, RunId(child_id))
        enqueued = [r for r in records if isinstance(r, QueueEnqueued)]
        assert [r.payload["message"] for r in enqueued] == [
            "Only suppliers we have a contract with."
        ]

    async def test_messaging_a_finished_child_is_a_conflict_rather_than_a_lie(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        parent, child_id = await self._tree(client, app)
        response = await client.post(
            f"/api/runs/{parent}/subagents/{child_id}/message", json={"message": "one more thing"}
        )
        assert response.status_code == 409

    async def test_one_child_can_be_stopped_without_stopping_its_parent(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        parent, child_id = await self._tree(client, app, finish_child=False)
        response = await client.post(
            f"/api/runs/{parent}/subagents/{child_id}/interrupt",
            json={"reason": "wrong supplier list"},
        )
        assert response.status_code == 200

        child_status = (await client.get(f"/api/runs/{child_id}/status")).json()
        assert child_status["lifecycle"] in {"stopping", "stopped"}
        parent_status = (await client.get(f"/api/runs/{parent}/status")).json()
        assert parent_status["lifecycle"] not in {"stopping", "stopped"}

    async def test_retrying_a_child_starts_a_new_run_of_the_same_version(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """Not a second attempt at the old one: a Run is admitted once and its
        log is append-only, so the failed Run stays in the tree next to it."""
        parent, child_id = await self._tree(client, app)
        response = await client.post(f"/api/runs/{parent}/subagents/{child_id}/retry")
        assert response.status_code == 201

        body = response.json()
        assert body["retried_from"] == child_id
        assert body["run_id"] != child_id

        original = (await client.get(f"/api/runs/{child_id}/status")).json()
        retried = (await client.get(f"/api/runs/{body['run_id']}/status")).json()
        assert retried["version_hash"] == original["version_hash"]
        assert retried["run_id"] == body["run_id"]

    async def test_a_control_aimed_at_an_unrelated_run_is_a_404(
        self, client: httpx.AsyncClient, app: Any
    ) -> None:
        """Parentage is checked as well as ownership: a control aimed at a tree
        must not act on another Run of the same account's that happens to be
        open in another tab."""
        parent, _ = await self._tree(client, app)
        version_hash = await _publish(client)
        stranger = (
            await client.post(
                "/api/runs", json={"version_hash": version_hash, "message": "unrelated"}
            )
        ).json()["run_id"]

        response = await client.post(
            f"/api/runs/{parent}/subagents/{stranger}/interrupt", json={"reason": "no"}
        )
        assert response.status_code == 404

    async def test_another_accounts_tree_is_not_readable(
        self, two_accounts: tuple[httpx.AsyncClient, httpx.AsyncClient], app: Any
    ) -> None:
        first, second = two_accounts
        parent, child_id = await self._tree(first, app)

        assert (await second.get(f"/api/runs/{parent}/subagents")).status_code == 404
        assert (
            await second.post(
                f"/api/runs/{parent}/subagents/{child_id}/message", json={"message": "hello"}
            )
        ).status_code == 404
