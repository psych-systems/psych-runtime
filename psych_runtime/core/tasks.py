"""A Run's plan, as the model wrote it down.

## Why this is in Psych at all, and why it is off by default

DESIGN.md §1 refuses a long list of things, and it is defended: no prompt
library, no eval framework, no UI. A task list sits nearer that line than
either other built-in. `load_skill` implements §16 and `remember`/`forget`
implement §15; both are named in the design. This is not, and it is closer to
**product opinion** than to runtime mechanism, because it decides that an
agent's work has a shape worth tracking.

The argument for it is real too. A twelve-turn Run is an opaque stretch of tool
calls to whoever is watching, and a plan the model keeps updated is the one
thing that makes it legible. Modelled as Records it is a projection over the
log like everything else and needs no new store.

The line §1 actually draws is between a capability a consumer chooses and an
opinion Psych imposes. So this is **opt-in per Spec and off by default**: an
agent that does one lookup does not need a plan, and offering the tool anyway
would spend prompt on every turn to no purpose. A consumer who wants it says
so, and it joins the Version hash like everything else about what an agent is.

If that trade ever looks wrong, the honest alternative is that a consumer
registers their own code tool for this in a few lines and Psych stays out of
it entirely.

## What it deliberately is not

No cross-Run tracking, no assignee, no dependencies, no dates, no priorities.
Those make a work tracker, and owning one is exactly what §1 means by turning
Psych into a platform. Three statuses, one list, one Run.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["MAX_TASKS", "Task", "TaskStatus", "active_label", "task_summary"]

MAX_TASKS: Final = 40
"""A cap, because the list rides in the prompt on every turn once written.

Generous enough that no real plan hits it, small enough that a model looping on
"add one more step" cannot quietly grow the prompt without bound.
"""

TaskStatus = Literal["pending", "in_progress", "completed"]
"""Three, and no more.

Anything richer -- blocked, cancelled, deferred -- is a project-management
model, and a model asked to choose between seven statuses spends its attention
on the taxonomy rather than on the work.
"""


class Task(BaseModel):
    """One thing the model plans to do, is doing, or has done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    """What the step is, imperative and short: "Fix the failing login test".
    Read by a person, so it is prose rather than an identifier."""
    active_form: str = Field(default="", max_length=200)
    """The same step in the present continuous: "Fixing the failing login
    test". Shown while the task is `in_progress`.

    This shape keeps a task's present-tense action alongside its description and earns its
    place for a reason that only shows up in the UI: a list of imperatives
    reads as a plan, and the *current* line of a plan should read as an
    activity. "Fix the login test" under a spinner is a instruction to nobody;
    "Fixing the login test" is a status.

    Empty falls back to `title`, so a model that does not bother still produces
    something sensible rather than a blank.
    """
    description: str = Field(default="", max_length=2000)
    """What the step involves, when the title is not enough on its own.
    Optional: most steps are self-evident and a forced description is noise."""
    status: TaskStatus = "pending"


def active_label(task: Task) -> str:
    """What to show for a task that is running now."""
    return task.active_form.strip() or task.title


def task_summary(tasks: tuple[Task, ...]) -> str:
    """One line: how far along the plan is, and what is happening now.

    For a collapsed row in a console, and for anywhere a whole list is more
    than the reader asked for. Leads with the running step's `active_form`
    rather than its title, which is the whole reason that field exists.
    """
    if not tasks:
        return ""
    done = sum(1 for task in tasks if task.status == "completed")
    active = next((task for task in tasks if task.status == "in_progress"), None)
    progress = f"{done} of {len(tasks)} done"
    return f"{active_label(active)} · {progress}" if active is not None else progress
