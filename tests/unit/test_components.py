"""The component vocabulary, and the two properties that make it safe to ship.

`psych_runtime/core/components.py` argues the design; these assert the two
things that argument rests on:

- **Nothing in the vocabulary is styling.** That is what makes brand control
  free rather than configurable: a consumer receives no colour, font or layout
  to override because none was ever sent. A field named `color` added in a
  hurry would break that quietly, so the property is asserted over the whole
  union rather than trusted to review.
- **A URL a renderer will act on is http or https or it is refused.** `href`
  and `image_url` are the only two fields a renderer *follows*, which makes
  them the only two that can turn a model's output into execution.

The rest is caps and lenient parsing, both of which are cheaper to check here
than through a Run.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from psych_runtime.core.components import (
    MAX_BADGES,
    MAX_CARDS,
    MAX_FIELDS,
    MAX_SERIES,
    Card,
    Carousel,
    Chart,
    Component,
    ComponentField,
    Detail,
    Metric,
    Point,
    Series,
    Timeline,
    TimelineStep,
    component_summary,
)
from psych_runtime.tools.builtins import parse_component

_MODELS: tuple[type[BaseModel], ...] = (
    Card,
    Carousel,
    Chart,
    ComponentField,
    Detail,
    Metric,
    Point,
    Series,
    Timeline,
    TimelineStep,
)

_STYLING_WORDS = frozenset(
    {
        "align",
        "background",
        "border",
        "class",
        "color",
        "colour",
        "css",
        "font",
        "height",
        "html",
        "layout",
        "margin",
        "padding",
        "palette",
        "position",
        "radius",
        "shadow",
        "size",
        "spacing",
        "style",
        "theme",
        "variant",
        "weight",
        "width",
    }
)


class TestNothingHereIsStyling:
    def test_no_field_name_in_the_vocabulary_names_an_appearance(self) -> None:
        """Psych owns the payload, the consumer owns the drawing.

        This is the mechanical half of that promise. The moment one of these
        models grows a `color` or a `width`, Psych has an opinion about how the
        consumer's product looks and the consumer has something to override.
        """
        offenders = [
            f"{model.__name__}.{name}"
            for model in _MODELS
            for name in model.model_fields
            for word in name.replace("_", " ").split()
            if word in _STYLING_WORDS
        ]
        assert offenders == []

    def test_no_field_carries_free_text_long_enough_to_be_markup(self) -> None:
        """Every string is capped, and capped small.

        Not tidiness: an uncapped field is a way to push a page of HTML, or a
        megabyte of anything, through the log and the stream on the model's
        say-so. The one generous cap is a URL, which is a URL.
        """
        uncapped: list[str] = []
        for model in _MODELS:
            for name, field in model.model_fields.items():
                if not _is_string_field(field.annotation):
                    continue
                limits = [
                    getattr(item, "max_length", None)
                    for item in field.metadata
                    if getattr(item, "max_length", None) is not None
                ]
                if not limits:
                    uncapped.append(f"{model.__name__}.{name}")
        assert uncapped == []


def _is_string_field(annotation: object) -> bool:
    """True for `str`, and for the unions and annotated aliases that wrap one."""
    if annotation is str:
        return True
    if get_origin(annotation) is None:
        return False
    return any(argument is str for argument in get_args(annotation))


class TestUrlsARendererWouldFollow:
    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "/relative/path",
            "example.com/no-scheme",
        ],
    )
    def test_only_http_and_https_are_accepted(self, hostile: str) -> None:
        with pytest.raises(ValidationError):
            Card(title="Chair", href=hostile)
        with pytest.raises(ValidationError):
            Card(title="Chair", image_url=hostile)

    @pytest.mark.parametrize("safe", ["http://example.com", "https://example.com/a?b=c#d", ""])
    def test_the_two_schemes_a_renderer_may_follow_pass(self, safe: str) -> None:
        assert Card(title="Chair", href=safe).href == safe

    def test_an_allowlist_rather_than_a_blocklist(self) -> None:
        """A scheme nobody thought of is refused by default, which is the whole
        reason the check is written the way it is."""
        with pytest.raises(ValidationError):
            Card(title="Chair", href="chrome-extension://abc/page.html")


class TestCaps:
    def test_a_carousel_cannot_carry_an_unbounded_browse(self) -> None:
        cards = [Card(title=f"Chair {index}") for index in range(MAX_CARDS + 1)]
        with pytest.raises(ValidationError):
            Carousel(cards=tuple(cards))

    def test_a_chart_cannot_carry_more_series_than_anyone_can_read(self) -> None:
        series = [Series(name=str(index)) for index in range(MAX_SERIES + 1)]
        with pytest.raises(ValidationError):
            Chart(mark="line", series=tuple(series))

    def test_badges_and_fields_are_bounded_too(self) -> None:
        with pytest.raises(ValidationError):
            Card(title="Chair", badges=tuple(str(i) for i in range(MAX_BADGES + 1)))
        with pytest.raises(ValidationError):
            Card(
                title="Chair",
                fields=tuple(ComponentField(label=str(i)) for i in range(MAX_FIELDS + 1)),
            )


class TestLenientParsing:
    def test_a_mapping_of_fields_is_read_in_the_order_it_was_written(self) -> None:
        """`{"Price": "£40"}` is what a model writes when it is thinking about
        the data rather than about this schema, and it is unambiguous."""
        parsed = parse_component(
            {"kind": "card", "title": "Chair", "fields": {"Price": "£40", "Colour": "Black"}}
        )
        assert isinstance(parsed, Card)
        assert [(field.label, field.value) for field in parsed.fields] == [
            ("Price", "£40"),
            ("Colour", "Black"),
        ]

    def test_a_bare_list_of_points_becomes_one_unnamed_series(self) -> None:
        parsed = parse_component(
            {"kind": "chart", "mark": "area", "series": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]}
        )
        assert isinstance(parsed, Chart)
        assert len(parsed.series) == 1
        assert [point.x for point in parsed.series[0].points] == [1.0, 2.0]

    def test_an_unknown_step_state_becomes_upcoming_rather_than_failing(self) -> None:
        parsed = parse_component(
            {"kind": "timeline", "steps": [{"label": "Packed", "state": "blocked"}]}
        )
        assert isinstance(parsed, Timeline)
        assert parsed.steps[0].state == "upcoming"

    def test_a_total_sent_as_a_bare_value_is_still_a_total(self) -> None:
        parsed = parse_component({"kind": "detail", "title": "Order A1", "total": "£40"})
        assert isinstance(parsed, Detail)
        assert parsed.total == ComponentField(label="Total", value="£40")

    def test_over_long_lists_are_truncated_rather_than_refused(self) -> None:
        """A model that sent thirty cards still picked twelve good ones first."""
        parsed = parse_component(
            {
                "kind": "carousel",
                "cards": [{"kind": "card", "title": f"Chair {i}"} for i in range(30)],
            }
        )
        assert isinstance(parsed, Carousel)
        assert len(parsed.cards) == MAX_CARDS

    @pytest.mark.parametrize(
        "junk", [None, "a card please", [], {"kind": "sculpture"}, {"kind": "card"}]
    )
    def test_what_cannot_be_read_raises_rather_than_guessing(self, junk: Any) -> None:
        """Including a card with no title: there is no sensible guess, and a
        blank card in front of a person is worse than a sentence."""
        with pytest.raises(ValidationError):
            parse_component(junk)


class TestSummary:
    def test_it_names_the_thing_rather_than_counting_it(self) -> None:
        assert component_summary(Card(title="Aeron")) == "card: Aeron"
        assert (
            component_summary(Chart(mark="bar", title="Weekly revenue"))
            == "bar chart: Weekly revenue"
        )
        assert component_summary(Metric(label="Spend", value=12)) == "metric: Spend"

    def test_an_untitled_group_says_what_it_holds(self) -> None:
        carousel = Carousel(cards=(Card(title="a"), Card(title="b")))
        assert component_summary(carousel) == "carousel: 2 cards"


def test_the_union_is_closed() -> None:
    """Six kinds, and closed is the point: a consumer writes six renderers once
    and is never surprised by a seventh."""
    # `Component` is a PEP 695 alias, so its union hides behind `__value__`
    # and then behind the `Annotated` carrying the discriminator.
    members = get_args(get_args(Component.__value__)[0])
    assert {member.model_fields["kind"].default for member in members} == {
        "card",
        "carousel",
        "detail",
        "timeline",
        "chart",
        "metric",
    }
