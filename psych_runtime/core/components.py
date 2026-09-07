"""What an agent can answer with, when a sentence is the wrong shape.

## Psych owns the payload. The consumer owns the drawing.

DESIGN.md §1 refuses "a UI, or any rendering", and this module does not bend
that by a millimetre. Nothing here is a component in the front-end sense: there
is no colour, no font, no spacing, no layout, no element name, no class, no
markup. A ``Chart`` carries series and axis labels and says ``mark="bar"``; it
does not say how wide the bars are, what colour they take, or whether they are
drawn in SVG, on a canvas, in a native list or read aloud. The consumer's
design system answers all of that, because the consumer's design system is the
only thing that knows.

**Somebody will eventually try to add a ``BarChart`` under ``psych/``. This is
the argument against it.** The moment Psych ships a renderer it has an opinion
about React, about CSS, about a colour ramp and about dark mode, and every
consumer whose brand disagrees has to fight it. Shipping the data instead makes
brand control free rather than configurable: there is no styling to override
because none was ever sent. It also keeps the payload renderable by a consumer
who has no browser at all, such as a native app, a terminal or a voice
surface, which a tree of HTML would not be.

The line is the same one §1 draws everywhere else: Psych decides *what is being
said*, the consumer decides *what it looks like*.

## Why a closed vocabulary rather than an open one

The obvious alternative is to let the model emit arbitrary JSON and let the
consumer render whatever it recognises. That fails in both directions. A model
inventing its own shapes gives the consumer an open set to write renderers for,
so a Run silently renders as nothing the first time the model gets creative. And
an open payload is an open injection surface: anything the model can name, it
can name maliciously.

Six kinds, versioned by the enclosing Spec's Version hash like everything else.
A consumer writes six renderers once and is done, and a model that wants a
seventh shape has to say it in prose, which is a good outcome rather than a
missing feature.

## Why the URLs are checked here rather than in the console

``image_url`` and ``href`` are the only two fields that hand the renderer
something to *act* on, and a renderer that puts a model-authored string into
``<img src>`` or ``<a href>`` has handed the model a script-execution primitive
the moment that string starts with ``javascript:``. Validating in the payload
type means every consumer gets the check, including the one that forgot to
write it. ``data:`` is refused for the same reason one step removed: a data URL
is content the consumer never fetched and cannot attribute, and
``data:text/html`` is a page.

Every string is capped. Not for tidiness: an uncapped field is a way to push
megabytes through the log, the store and the stream on the model's say-so.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

__all__ = [
    "MAX_BADGES",
    "MAX_CARDS",
    "MAX_FIELDS",
    "MAX_POINTS",
    "MAX_SERIES",
    "MAX_STEPS",
    "Card",
    "Carousel",
    "Chart",
    "ChartMark",
    "Component",
    "ComponentField",
    "Detail",
    "Metric",
    "Point",
    "Series",
    "Timeline",
    "TimelineStep",
    "TrendDirection",
    "component_summary",
]

MAX_BADGES: Final = 6
MAX_FIELDS: Final = 12
MAX_CARDS: Final = 12
MAX_STEPS: Final = 24
MAX_SERIES: Final = 5
MAX_POINTS: Final = 400
"""Caps on how much of each thing one component may carry.

Sized so that no honest use hits them and no dishonest one runs away: a
carousel of twelve products is a browse, a carousel of four hundred is a
denial-of-service against whoever renders it. ``MAX_SERIES`` is the tightest
and the most deliberate. Past about five series a chart stops being readable
whatever the renderer does, and the honest fix is fewer series rather than more
colours.
"""

_ALLOWED_URL_SCHEMES: Final = ("http://", "https://")


def _require_web_url(value: str) -> str:
    """Refuse any URL the renderer should not be asked to follow.

    An allowlist rather than a blocklist of ``javascript:`` and friends: the
    blocklist version is a bet that nobody will find a scheme we did not think
    of, and that bet has lost repeatedly in every product that has taken it.
    Two schemes are enough for an image and a link, and anything else is either
    a mistake or an attack.
    """
    if not value:
        return value
    candidate = value.strip()
    if not candidate.lower().startswith(_ALLOWED_URL_SCHEMES):
        raise ValueError(
            f"{candidate[:60]!r} is not an http or https URL. A component's links and "
            "images are followed by whoever renders them, so only those two schemes "
            "are accepted; javascript:, data: and the rest are refused."
        )
    return candidate


WebUrl = Annotated[str, Field(max_length=2048), AfterValidator(_require_web_url)]
"""A URL a renderer may safely put in ``src`` or ``href``. Empty means none."""

ChartMark = Literal["line", "bar", "area", "pie"]
"""What kind of comparison the data is making, not how to draw it.

Four, because these are the four shapes of question an agent's answer actually
takes: over time, between categories, over time with magnitude, and share of a
whole. A renderer is free to draw any of them differently, or for ``pie`` draw
something better, because ``mark`` is the model's reading of the data
rather than an instruction.
"""

TrendDirection = Literal["up", "down", "flat"]
"""Which way a metric moved. Deliberately not whether that is good news: up is
good for revenue and bad for latency, and only the consumer knows which."""


class _ComponentModel(BaseModel):
    """Frozen and closed, like every other payload type in the core."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ComponentField(_ComponentModel):
    """One label/value pair. The unit every record-shaped component is built from."""

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(default="", max_length=400)


class Card(_ComponentModel):
    """One entity, worth showing rather than describing.

    A product, a place, a person, a document. The fields are what a person
    reading a card wants: what it is, what distinguishes it, what it costs,
    where to go next.
    """

    kind: Literal["card"] = "card"
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=300)
    image_url: WebUrl = ""
    badges: tuple[Annotated[str, Field(min_length=1, max_length=40)], ...] = Field(
        default=(), max_length=MAX_BADGES
    )
    """Short standing facts: "In stock", "Best seller", "2 left". Not a
    sentence, and not styling: what the badge *says* is the payload, and
    whether it is a pill, a chip or a bracketed word is the consumer's call."""
    fields: tuple[ComponentField, ...] = Field(default=(), max_length=MAX_FIELDS)
    href: WebUrl = ""


class Carousel(_ComponentModel):
    """Several cards to browse between, rather than one to look at.

    A separate kind rather than a list of cards because the intent differs: a
    carousel says "these are alternatives, compare them", and a renderer that
    knows that can scroll them horizontally, grid them, or paginate them. A
    bare list of cards says nothing about their relationship.
    """

    kind: Literal["carousel"] = "carousel"
    title: str = Field(default="", max_length=200)
    cards: tuple[Card, ...] = Field(default=(), max_length=MAX_CARDS)


class Detail(_ComponentModel):
    """A titled record: an order, a booking, a confirmation.

    The difference from a ``Card`` is what the reader is doing. A card is
    scanned among others; a detail is read on its own, top to bottom, and is
    usually the end of a transaction. Hence ``status`` and ``total``, which a
    card has no place for.
    """

    kind: Literal["detail"] = "detail"
    title: str = Field(min_length=1, max_length=200)
    subtitle: str = Field(default="", max_length=300)
    status: str = Field(default="", max_length=60)
    """What state the thing is in, in the words of whatever system owns it:
    "Shipped", "Awaiting payment", "Confirmed". Free text rather than an
    enum on purpose. Psych does not own anybody's order lifecycle, and a
    fixed set would be wrong for the second consumer."""
    fields: tuple[ComponentField, ...] = Field(default=(), max_length=MAX_FIELDS)
    total: ComponentField | None = None
    """The one line that gets read first and remembered. Separate from
    ``fields`` so a renderer can set it apart without guessing which of twelve
    rows was the important one."""
    href: WebUrl = ""


class TimelineStep(_ComponentModel):
    """One point on a timeline."""

    label: str = Field(min_length=1, max_length=200)
    at: str = Field(default="", max_length=80)
    """When, as the model knows it. A string rather than a ``datetime``
    because "Tuesday morning", "Day 3" and "in about an hour" are all real
    answers on an itinerary, and forcing them into a timestamp would either
    lose them or invent a precision nobody has."""
    description: str = Field(default="", max_length=600)
    state: Literal["done", "current", "upcoming"] = "upcoming"


class Timeline(_ComponentModel):
    """An ordered sequence with a position in it: a trip, a delivery, a process."""

    kind: Literal["timeline"] = "timeline"
    title: str = Field(default="", max_length=200)
    steps: tuple[TimelineStep, ...] = Field(default=(), max_length=MAX_STEPS)


class Point(_ComponentModel):
    """One (x, y) reading.

    ``x`` is a number or a label because both are ordinary: a time series has
    numeric x, and "Q1"/"Q2"/"Q3" is a chart every bit as much as one indexed
    by epoch seconds. ``y`` is always a number, because a chart of non-numbers
    is a table and markdown already renders those.
    """

    x: float | Annotated[str, Field(max_length=80)]
    y: float


class Series(_ComponentModel):
    """One named line, bar group, band or set of slices."""

    name: str = Field(default="", max_length=80)
    points: tuple[Point, ...] = Field(default=(), max_length=MAX_POINTS)


class Chart(_ComponentModel):
    """Numbers with a shape, described as data and intent rather than drawing.

    There are no pixels, no colours and no scales here on purpose: those are
    the renderer's, and a payload that carried them would be a picture Psych
    had drawn on the consumer's behalf. ``mark`` is the strongest statement
    this type makes and it is still a reading of the data ("this is a share of
    a whole"), not a command.
    """

    kind: Literal["chart"] = "chart"
    title: str = Field(default="", max_length=200)
    mark: ChartMark
    series: tuple[Series, ...] = Field(default=(), max_length=MAX_SERIES)
    x_label: str = Field(default="", max_length=80)
    y_label: str = Field(default="", max_length=80)


class Metric(_ComponentModel):
    """One number that is the answer, with what moved it.

    The smallest component, and the one most often right: a question with a
    number for an answer is better served by that number set large than by a
    chart of one point.
    """

    kind: Literal["metric"] = "metric"
    label: str = Field(min_length=1, max_length=120)
    value: float | Annotated[str, Field(max_length=40)]
    """A number, or the already-formatted string when formatting is the
    point: "$4.2M", "12.9K", "3h 40m". Psych does not format numbers, and a
    consumer who wants to format it themselves can send a float."""
    unit: str = Field(default="", max_length=24)
    delta: str = Field(default="", max_length=40)
    """How it moved, in the model's words: "+12%", "up 3 since Monday"."""
    direction: TrendDirection | None = None


type Component = Annotated[
    Card | Carousel | Detail | Timeline | Chart | Metric,
    Field(discriminator="kind"),
]
"""Everything an agent can show. A closed set, and closed is the point."""


def component_summary(component: Component) -> str:
    """One line naming what was shown, for a log line or a collapsed row.

    Deliberately says what kind of thing it is and what it is about, never how
    much of it there is: "the agent showed a chart of Weekly revenue" is what a
    reader wants, and "6 series, 42 points" is what a debugger wants and can
    get from the record.
    """
    match component:
        case Card() | Detail():
            return f"{component.kind}: {component.title}"
        case Carousel():
            named = component.title or f"{len(component.cards)} cards"
            return f"carousel: {named}"
        case Timeline():
            return f"timeline: {component.title or f'{len(component.steps)} steps'}"
        case Chart():
            return f"{component.mark} chart: {component.title or component.y_label or 'untitled'}"
        case Metric():
            return f"metric: {component.label}"
