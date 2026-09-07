"""What an agent asks a person, and what they answered.

DESIGN.md §11 makes a suspension one mechanism for approvals, questions and
webhook waits. This module is the shape of the question half.

## Why a question is not just a string

The first version of `ask_question` took one line of text and handed back
whatever the person typed. That works and it is the wrong default, for a
reason worth stating: an open text box makes the person do the work of
guessing what an acceptable answer looks like, and makes the model do the work
of parsing prose that may not contain one.

A question with options moves both problems to where they are cheap. The model
already knows the shape of the answer it needs -- it is choosing between
branches -- so it can say so, and the person clicks. What comes back is one of
a known set rather than a sentence somebody has to interpret.

## Options never trap anyone

`AskedQuestion.options` is a suggestion, not an enumeration. A person may
always answer in their own words, whatever the options say, and the answer
that comes back is free text either way. That is deliberate and it is the
property that makes options safe to offer: a model that guessed the branches
wrong has cost the person a click, not the ability to answer.

So an option list is never validated against on the way back in. A consumer
building a UI should render the options as the easy path and keep a way to
type something else, which is what the console does.

## Small on purpose

Four questions, four options, short labels. A prompt that asks eight questions
at once is not gathering requirements, it is a form, and a person faced with
one abandons it. The caps are in the field definitions rather than in advice,
because advice in a tool description is a suggestion a model may take or leave.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "AskedQuestion",
    "QuestionOption",
    "render_questions",
]

MAX_QUESTIONS: Final = 4
"""More than this is a form, and people abandon forms."""

MAX_OPTIONS: Final = 4
"""Beyond four the person is reading rather than choosing, and the model is
usually guessing at branches it has not thought through."""


class QuestionOption(BaseModel):
    """One answer a person can pick without typing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    """A few words. What the person is choosing, not why."""
    description: str = Field(default="", max_length=400)
    """What picking this means, or what happens next. Where the trade-off goes,
    so the label can stay short enough to scan."""


class AskedQuestion(BaseModel):
    """One question, with the answers the model expects to be useful."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    """The whole question, in the model's own words."""
    header: str = Field(default="", max_length=24)
    """A short label for a compact UI: a chip, a column heading. Empty is fine;
    a console falls back to the question itself."""
    options: tuple[QuestionOption, ...] = Field(default=(), max_length=MAX_OPTIONS)
    """Suggested answers. Empty asks for free text.

    Never an enumeration: see the module docstring. A person answers in their
    own words whatever this says, and nothing validates an answer against it.
    """
    multi_select: bool = False
    """Whether several options can be picked at once. False when they are
    mutually exclusive, which is the common case and the safer default."""


def render_questions(questions: tuple[AskedQuestion, ...]) -> str:
    """One line of prose summarising what was asked.

    `Suspended.question` has always been a plain string, and an approval writes
    its own text there. This keeps that field meaning the same thing for a
    question: something a log reader or a notification can show without
    understanding the structure beside it.
    """
    if not questions:
        return ""
    if len(questions) == 1:
        return questions[0].question
    return " ".join(f"({i + 1}) {q.question}" for i, q in enumerate(questions))
