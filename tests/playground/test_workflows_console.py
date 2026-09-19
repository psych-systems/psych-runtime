"""The workflow console: composite steps, the step tree a graph renders, and
the three ways a person intervenes in a running workflow.

`test_expanded_features.py` proves a workflow is publishable and dispatchable.
This covers what the console needs beyond that: the twelve step kinds through
the HTTP boundary, `GET /api/runs/{id}/workflow` as the shape a graph is drawn
from, and replay, event delivery and breakpoints -- each driven through the
API exactly as the page drives it, because a route asserted only through the
service beneath it is not proven to be reachable.

The workflows here call registered tools rather than agents wherever they can.
A tool step needs no provider, so these tests exercise the workflow engine
itself rather than the model stub, and a failure here is unambiguously the
workflow code.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.playground.conftest import wait_for

pytestmark = pytest.mark.functional


def path(value: str) -> dict[str, str]:
    """A ``ValuePath`` as the wire carries it."""
    return {"kind": "path", "path": value}


def literal(value: Any) -> dict[str, Any]:
    """A ``LiteralValue`` as the wire carries it."""
    return {"kind": "literal", "value": value}


async def publish(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/workflows", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def dispatch(client: httpx.AsyncClient, workflow_id: str, **body: Any) -> str:
    response = await client.post("/api/runs", json={"workflow_id": workflow_id, **body})
    assert response.status_code == 201, response.text
    return str(response.json()["run_id"])


async def view(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/runs/{run_id}/workflow")
    assert response.status_code == 200, response.text
    return dict(response.json())


def flatten(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every step of a ``WorkflowView`` by name, children included.

    Names are unique across a workflow's whole tree -- the library enforces it,
    because a `ValuePath` addresses a step by name from anywhere -- so one flat
    map is a faithful index of the tree and spares every assertion below a walk
    through branches and iterations.
    """
    found: dict[str, dict[str, Any]] = {}
    for step in steps:
        found[step["name"]] = step
        found.update(flatten(step.get("children", [])))
    return found


async def wait_until_waiting(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    """Poll status until the Run stops for someone, and return it.

    `conftest.wait_for(settled=False)` returns on the first resting state,
    which for a Run that has only just been admitted can be the state it was
    in before the Worker picked it up. This keeps polling until the suspension
    itself is in the log, which is what the next request needs to exist.
    """
    for _ in range(600):
        response = await client.get(f"/api/runs/{run_id}/status")
        assert response.status_code == 200, response.text
        status = dict(response.json())
        if status.get("pending_wait") is not None or status.get("pending_approval") is not None:
            return status
        assert status.get("lifecycle") not in {"done", "failed"}, status
        await asyncio.sleep(0.05)
    raise AssertionError(f"the Run never stopped for anyone: {run_id}")


# The pipeline the tree tests run: one of every composite kind, over tools
# that need no model. `look_a` reads a shipped order, so the branch takes its
# one case and the `otherwise` arm is left skipped -- which is the thing a
# graph has to be able to draw and this file's first assertion.
TREE_STEPS: list[dict[str, Any]] = [
    {"kind": "set_state", "name": "mark", "values": {"stage": literal("checking")}},
    {
        "kind": "parallel",
        "name": "fan",
        "branches": [
            {
                "kind": "tool",
                "name": "look_a",
                "tool": "lookup_order",
                "arguments_from": {"order_id": path("input.order_id")},
            },
            {
                "kind": "tool",
                "name": "look_b",
                "tool": "lookup_order",
                "arguments": {"order_id": "A2"},
            },
        ],
    },
    {
        "kind": "branch",
        "name": "decide",
        "cases": [
            {
                "name": "shipped",
                "when": {
                    "path": "steps.fan.output.look_a.result.status",
                    "op": "eq",
                    "value": "shipped",
                },
                "step": {
                    "kind": "map",
                    "name": "note_shipped",
                    "output": {"note": literal("ship")},
                },
            }
        ],
        "otherwise": {"kind": "map", "name": "note_other", "output": {"note": literal("other")}},
    },
    {
        "kind": "foreach",
        "name": "each_sku",
        "items": path("input.skus"),
        "body": {"kind": "map", "name": "sku_row", "output": {"sku": path("item")}},
    },
    {
        "kind": "map",
        "name": "final",
        "output": {"stage": path("state.stage"), "rows": path("steps.each_sku.output.items")},
    },
]


class TestTheStepTree:
    """Publishing composite steps, running them, and reading the tree back."""

    async def test_runs_a_composite_workflow_and_reports_its_tree(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await publish(client, name="triage", steps=TREE_STEPS)
        workflow_id = created["workflow_id"]

        run_id = await dispatch(client, workflow_id, input={"order_id": "A1", "skus": ["S1", "S2"]})
        status = await wait_for(client, run_id)
        assert status["lifecycle"] == "done", status

        tree = await view(client, run_id)
        assert tree["workflow"] == "triage"
        assert tree["state"] == {"stage": "checking"}
        steps = flatten(tree["steps"])
        assert steps["look_a"]["output"] == {"result": {"order_id": "A1", "status": "shipped"}}
        assert steps["note_shipped"]["status"] == "completed"
        # The arm that was not taken is in the tree and marked, rather than
        # missing: a graph shows where the Run could have gone.
        assert steps["note_other"]["status"] == "skipped"
        assert steps["decide"]["cases"] == ["shipped"]
        # A foreach has as many children as the list had elements, which the
        # Spec cannot count and the log can.
        assert len(steps["each_sku"]["children"]) == 2
        assert steps["final"]["output"] == {
            "stage": "checking",
            "rows": [{"sku": "S1"}, {"sku": "S2"}],
        }

        listed = await client.get(f"/api/workflows/{workflow_id}/runs")
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        assert [row["run_id"] for row in rows] == [run_id]
        assert rows[0]["kind"] == "workflow"
        assert (await client.get(f"/api/workflows/{workflow_id}/runs?state=settled")).json()
        assert (await client.get(f"/api/workflows/{workflow_id}/runs?state=running")).json() == []

    async def test_replays_from_a_later_step_keeping_the_earlier_ones(
        self, client: httpx.AsyncClient
    ) -> None:
        """The point of a replay: the steps before the one being re-run are
        copied in, not executed again, so their side effects happen once."""
        created = await publish(client, name="triage_again", steps=TREE_STEPS)
        run_id = await dispatch(
            client, created["workflow_id"], input={"order_id": "A1", "skus": ["S1"]}
        )
        assert (await wait_for(client, run_id))["lifecycle"] == "done"

        replayed = await client.post(f"/api/runs/{run_id}/replay", json={"from_step": "each_sku"})
        assert replayed.status_code == 201, replayed.text
        new_run_id = replayed.json()["run_id"]
        assert new_run_id != run_id
        assert (await wait_for(client, new_run_id))["lifecycle"] == "done"

        tree = await view(client, new_run_id)
        assert tree["replays_run_id"] == run_id
        assert tree["replay_from_step"] == "each_sku"
        steps = flatten(tree["steps"])
        assert steps["mark"]["status"] == "replayed"
        assert steps["look_a"]["status"] == "replayed"
        assert steps["each_sku"]["status"] == "completed"
        assert steps["final"]["status"] == "completed"
        # The source is untouched: a replay is a new Run, not an edit.
        assert flatten((await view(client, run_id))["steps"])["mark"]["status"] == "completed"

        both = (await client.get(f"/api/workflows/{created['workflow_id']}/runs")).json()
        assert {row["run_id"] for row in both} == {run_id, new_run_id}

    async def test_an_agent_run_has_no_workflow_view(self, client: httpx.AsyncClient) -> None:
        agent = await client.post(
            "/api/agents",
            json={"name": "plain", "instructions": "Answer.", "model": "test-model", "tools": []},
        )
        run = await client.post(
            "/api/runs", json={"agent_id": agent.json()["agent_id"], "message": "hi"}
        )
        response = await client.get(f"/api/runs/{run.json()['run_id']}/workflow")
        assert response.status_code == 409, response.text

    async def test_a_workflow_view_of_somebody_elses_run_is_a_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/runs/run_nope/workflow")
        assert response.status_code == 404, response.text


class TestWaitingOnAnEvent:
    """A `wait` step and the route that answers it."""

    async def test_delivers_an_event_payload_as_the_steps_output(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await publish(
            client,
            name="await_payment",
            steps=[
                {
                    "kind": "tool",
                    "name": "look",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A1"},
                },
                {"kind": "wait", "name": "hold", "event": "payment_confirmed"},
                {
                    "kind": "map",
                    "name": "done",
                    "output": {"reference": path("steps.hold.output.reference")},
                },
            ],
        )
        run_id = await dispatch(client, created["workflow_id"], message="go")

        status = await wait_until_waiting(client, run_id)
        assert status["pending_wait"]["event"] == "payment_confirmed"
        assert flatten((await view(client, run_id))["steps"])["hold"]["status"] == "waiting"

        wrong = await client.post(
            f"/api/runs/{run_id}/events", json={"event": "something_else", "payload": {}}
        )
        assert wrong.status_code == 409, wrong.text
        assert "payment_confirmed" in wrong.json()["detail"]

        delivered = await client.post(
            f"/api/runs/{run_id}/events",
            json={"event": "payment_confirmed", "payload": {"reference": "PAY-7"}, "by": "webhook"},
        )
        assert delivered.status_code == 200, delivered.text

        assert (await wait_for(client, run_id))["lifecycle"] == "done"
        steps = flatten((await view(client, run_id))["steps"])
        assert steps["hold"]["output"] == {"reference": "PAY-7"}
        assert steps["done"]["output"] == {"reference": "PAY-7"}

    async def test_refuses_an_event_for_a_run_that_is_not_waiting(
        self, client: httpx.AsyncClient
    ) -> None:
        created = await publish(
            client,
            name="no_wait",
            steps=[
                {
                    "kind": "tool",
                    "name": "only",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A1"},
                }
            ],
        )
        run_id = await dispatch(client, created["workflow_id"], message="go")
        assert (await wait_for(client, run_id))["lifecycle"] == "done"

        response = await client.post(
            f"/api/runs/{run_id}/events", json={"event": "anything", "payload": {}}
        )
        assert response.status_code == 409, response.text
        assert "not waiting" in response.json()["detail"]


class TestBreakpoints:
    """Pausing a workflow before a named step, and letting it through."""

    async def test_pauses_before_a_named_step_and_resumes(self, client: httpx.AsyncClient) -> None:
        created = await publish(
            client,
            name="stepped",
            steps=[
                {
                    "kind": "tool",
                    "name": "first",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A1"},
                },
                {
                    "kind": "tool",
                    "name": "second",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A3"},
                },
            ],
        )
        run_id = await dispatch(
            client, created["workflow_id"], message="go", breakpoints=["second"]
        )

        await wait_until_waiting(client, run_id)
        tree = await view(client, run_id)
        assert tree["breakpoints"] == ["second"]
        assert tree["waiting"]["name"] == "second"
        assert tree["waiting"]["reason"] == "breakpoint"
        steps = flatten(tree["steps"])
        assert steps["first"]["status"] == "completed"
        assert steps["second"]["status"] == "waiting"

        resumed = await client.post(f"/api/runs/{run_id}/resume", json={"payload": {}})
        assert resumed.status_code == 200, resumed.text

        assert (await wait_for(client, run_id))["lifecycle"] == "done"
        steps = flatten((await view(client, run_id))["steps"])
        assert steps["second"]["output"] == {"result": {"order_id": "A3", "status": "delivered"}}


def subset(smaller: Any, larger: Any, where: str) -> None:
    """Assert every field of ``smaller`` appears, equal, in ``larger``.

    A round trip cannot be an equality check: the response carries every
    default the request left out. What it has to prove is that nothing the
    request *said* was dropped or changed on the way through the store, at any
    depth -- which is exactly containment.
    """
    if isinstance(smaller, dict):
        assert isinstance(larger, dict), where
        for key, value in smaller.items():
            assert key in larger, f"{where}.{key} is missing"
            subset(value, larger[key], f"{where}.{key}")
    elif isinstance(smaller, list):
        assert isinstance(larger, list), where
        assert len(smaller) == len(larger), where
        for index, value in enumerate(smaller):
            subset(value, larger[index], f"{where}[{index}]")
    else:
        assert smaller == larger, where


class TestTheDefinitionRoundTrips:
    """What the console edits: a published workflow read back whole."""

    async def test_every_step_kind_survives_publish_and_fetch(
        self, client: httpx.AsyncClient
    ) -> None:
        agent = await client.post(
            "/api/agents",
            json={"name": "wrap", "instructions": "Summarise.", "model": "test-model", "tools": []},
        )
        assert agent.status_code == 201, agent.text
        inner = await publish(
            client,
            name="inner",
            steps=[
                {
                    "kind": "tool",
                    "name": "inner_look",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A1"},
                }
            ],
        )

        steps: list[dict[str, Any]] = [
            {"kind": "set_state", "name": "s_state", "values": {"total": literal(0)}},
            {
                "kind": "tool",
                "name": "s_tool",
                "tool": "lookup_order",
                "description": "Read the order.",
                "arguments": {"order_id": "A1"},
                "arguments_from": {"order_id": path("input.order_id")},
                "retry": {"max_attempts": 3, "backoff_seconds": 0.5},
                "timeout_seconds": 30.0,
                "on_failure": "continue",
                "output_schema": {"type": "object"},
                "when": {"path": "input.order_id", "op": "exists"},
            },
            {
                "kind": "agent",
                "name": "s_agent",
                "agent_id": agent.json()["agent_id"],
                "input": {"message": literal("summarise it")},
            },
            {
                "kind": "workflow",
                "name": "s_workflow",
                "workflow_id": inner["workflow_id"],
                "input": {"order_id": path("input.order_id")},
            },
            {
                "kind": "parallel",
                "name": "s_parallel",
                "on_branch_failure": "wait_all",
                "branches": [
                    {"kind": "map", "name": "p_one", "output": {"a": literal(1)}},
                    {"kind": "map", "name": "p_two", "output": {"b": path("state.total")}},
                ],
            },
            {
                "kind": "branch",
                "name": "s_branch",
                "mode": "all",
                "cases": [
                    {
                        "name": "always",
                        "when": {
                            "any_of": [
                                {"path": "state.total", "op": "gte", "value": 0},
                                {"path": "state.total", "op": "truthy", "negate": True},
                            ]
                        },
                        "step": {"kind": "map", "name": "b_yes", "output": {"c": literal(True)}},
                    }
                ],
                "otherwise": {"kind": "map", "name": "b_no", "output": {"c": literal(False)}},
            },
            {
                "kind": "foreach",
                "name": "s_foreach",
                "items": path("input.skus"),
                "concurrency": 2,
                "on_item_failure": "wait_all",
                "body": {"kind": "map", "name": "f_body", "output": {"sku": path("item")}},
            },
            {
                "kind": "loop",
                "name": "s_loop",
                "max_iterations": 3,
                "while": {"path": "iteration", "op": "lt", "value": 2},
                "body": {"kind": "map", "name": "l_body", "output": {"n": path("iteration")}},
            },
            {"kind": "sleep", "name": "s_sleep", "seconds": 1.0},
            {
                "kind": "wait",
                "name": "s_wait",
                "event": "something_happened",
                "payload_schema": {"type": "object"},
                "timeout_seconds": 60.0,
            },
            {
                "kind": "human",
                "name": "s_human",
                "prompt": "Is this right?",
                "expires_seconds": 120.0,
                "questions": [
                    {
                        "question": "Refund the customer?",
                        "header": "Refund",
                        "multi_select": False,
                        "options": [
                            {"label": "Yes", "description": "Issue it now."},
                            {"label": "No", "description": "Leave it."},
                        ],
                    }
                ],
            },
            {"kind": "map", "name": "s_map", "output": {"done": literal(True)}},
        ]
        body: dict[str, Any] = {
            "name": "every_kind",
            "description": "One of each.",
            "steps": steps,
            "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
            "initial_state": {"total": 0},
            "output": {"done": path("steps.s_map.output.done")},
            "retry": {"max_attempts": 2},
        }
        created = await publish(client, **body)

        fetched = await client.get(f"/api/workflows/{created['workflow_id']}")
        assert fetched.status_code == 200, fetched.text
        summary = dict(fetched.json())
        assert [step["kind"] for step in summary["steps"]] == [step["kind"] for step in steps]
        for sent, back in zip(steps, summary["steps"], strict=True):
            subset(sent, back, sent["name"])
        assert summary["input_schema"] == body["input_schema"]
        assert summary["initial_state"] == {"total": 0}
        assert summary["output"] == {"done": path("steps.s_map.output.done")}
        assert summary["retry"]["max_attempts"] == 2
        # Every tool in the tree, not only the top-level ones.
        assert summary["tools"] == ["lookup_order"]
        # An embedded child reports the hash this Version pinned, which is what
        # lets the console say "this copy is older than that agent runs today".
        embedded = {step["name"]: step for step in summary["steps"]}
        assert embedded["s_agent"]["version_hash"], summary["steps"]
        assert embedded["s_workflow"]["version_hash"] == inner["version_hash"]

        # The strongest form of the round trip: what came back, published
        # again, is byte-identical as far as the Version hash is concerned.
        again = await publish(client, **{**body, "steps": summary["steps"]})
        assert again["version_hash"] == created["version_hash"]

    async def test_a_step_inside_a_branch_may_still_be_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        """The refusals kept their status codes, and now fire at any depth:
        an unknown tool buried in a loop body is the same 400 it would be at
        the top, rather than a Run that fails at the step nobody checked."""
        response = await client.post(
            "/api/workflows",
            json={
                "name": "deep",
                "steps": [
                    {
                        "kind": "loop",
                        "name": "spin",
                        "until": {"path": "iteration", "op": "gte", "value": 1},
                        "body": {
                            "kind": "tool",
                            "name": "nope",
                            "tool": "does_not_exist",
                            "arguments": {},
                        },
                    }
                ],
            },
        )
        assert response.status_code == 400, response.text
        assert "does_not_exist" in response.json()["detail"]

    async def test_a_nested_workflow_cannot_contain_itself(self, client: httpx.AsyncClient) -> None:
        created = await publish(
            client,
            name="selfish",
            steps=[
                {
                    "kind": "tool",
                    "name": "one",
                    "tool": "lookup_order",
                    "arguments": {"order_id": "A1"},
                }
            ],
        )
        response = await client.post(
            "/api/workflows",
            json={
                "workflow_id": created["workflow_id"],
                "name": "selfish",
                "steps": [
                    {
                        "kind": "parallel",
                        "name": "fan",
                        "branches": [
                            {
                                "kind": "workflow",
                                "name": "me",
                                "workflow_id": created["workflow_id"],
                            }
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 400, response.text
        assert "inside itself" in response.json()["detail"]


class TestOlderIndexFiles:
    """An index written before composite steps existed still reads back.

    The entry's flat `kind`/`tool`/`agent_id` fields are all such a file has,
    and every read path now goes through `definition`. Derived rather than
    migrated, so an installation that upgrades and then rolls back has not had
    its index rewritten underneath it.
    """

    def test_a_legacy_entry_derives_a_definition_the_api_can_return(self) -> None:
        from app.schemas import WorkflowStepIn
        from app.store_index import WorkflowStepEntry
        from pydantic import TypeAdapter

        # An `Annotated` union is not a model, so it is validated through a
        # TypeAdapter -- the same machinery FastAPI uses on the request body.
        adapter: TypeAdapter[Any] = TypeAdapter(WorkflowStepIn)
        legacy = [
            WorkflowStepEntry(
                kind="tool", name="look", tool="lookup_order", arguments={"order_id": "A1"}
            ),
            WorkflowStepEntry(kind="agent", name="ask", agent_id="agt_1", version_hash="sha256:aa"),
            WorkflowStepEntry(
                kind="workflow", name="sub", workflow_id="wf_1", version_hash="sha256:bb"
            ),
        ]
        for entry in legacy:
            assert entry.definition["kind"] == entry.kind
            assert entry.definition["name"] == entry.name
            adapter.validate_python(entry.definition)
        assert legacy[0].definition["arguments"] == {"order_id": "A1"}
        assert legacy[1].definition["version_hash"] == "sha256:aa"

    def test_a_legacy_entry_serialises_through_the_whole_read_path(self) -> None:
        from datetime import UTC, datetime

        from app.schemas import WorkflowSummary
        from app.store_index import WorkflowEntry, WorkflowStepEntry
        from app.workflows import summary_of

        from psych_runtime.core.ids import VersionHash

        now = datetime.now(UTC)
        entry = WorkflowEntry(
            workflow_id="wf_old",
            owner="acct_1",
            version_hash=VersionHash("sha256:cc"),
            history=(VersionHash("sha256:cc"),),
            name="old",
            steps=(
                WorkflowStepEntry(
                    kind="tool", name="look", tool="lookup_order", arguments={"order_id": "A1"}
                ),
            ),
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        summary = WorkflowSummary(**summary_of(entry))
        assert summary.steps[0].kind == "tool"
        assert summary.input_schema is None
        assert summary.initial_state == {}
