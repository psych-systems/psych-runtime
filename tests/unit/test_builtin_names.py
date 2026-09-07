"""Every built-in tool name is reserved, and the two lists cannot drift.

`RESERVED_TOOL_NAMES` lives in `psych_runtime.core.spec` and the names themselves live
beside the tools, in `psych_runtime.tools` and `psych_runtime.runtime`. That duplication is
deliberate and forced: `psych_runtime.core` imports nothing from the other packages and
import-linter enforces it, so the set cannot be built from the constants.

What it cannot be, a test can check. A built-in whose name is missing from the
set does not fail loudly; it lets a consumer publish a Spec claiming that name,
and the model is then shown two tools called the same thing and can address
neither. `ask_question`, `update_tasks` and `show_component` were all missing
exactly that way.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from psych_runtime.core.spec import RESERVED_TOOL_NAMES
from psych_runtime.runtime.subagent import CHECK_TOOL, DELEGATE_TOOL, MESSAGE_TOOL, SPAWN_TOOL
from psych_runtime.tools.builtins import ASK_QUESTION, SHOW_COMPONENT, UPDATE_TASKS
from psych_runtime.tools.code import TOOL_NAME as RUN_CODE
from psych_runtime.tools.deferred import CALL_TOOL, GET_TOOL_INFO, LIST_TOOLS
from psych_runtime.tools.large_results import TOOL_NAME as READ_TOOL_OUTPUT

pytestmark = pytest.mark.unit

BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        ASK_QUESTION,
        CALL_TOOL,
        CHECK_TOOL,
        DELEGATE_TOOL,
        GET_TOOL_INFO,
        LIST_TOOLS,
        MESSAGE_TOOL,
        READ_TOOL_OUTPUT,
        RUN_CODE,
        SHOW_COMPONENT,
        SPAWN_TOOL,
        UPDATE_TASKS,
    }
)
"""Every name Psych registers a tool under, read from the constants the tools
themselves use rather than retyped. `load_skill`, `remember` and `forget` are
registered from string literals in `psych_runtime.tools.builtins` and are covered by the
reverse direction below."""


class TestReservedNamesCoverTheBuiltins:
    def test_every_builtin_name_is_reserved(self) -> None:
        missing = sorted(BUILTIN_NAMES - RESERVED_TOOL_NAMES)
        assert not missing, (
            f"{missing} are registered by Psych but not reserved, so a Spec may claim "
            "one and put two tools with that name in front of the model"
        )

    def test_nothing_is_reserved_that_psych_does_not_register(self) -> None:
        """The other direction, which stops the set growing into a wish list.

        The three exceptions are registered from literals rather than
        constants, so they cannot be imported; naming them here is the whole
        check that they still exist.
        """
        from_literals = {"load_skill", "remember", "forget"}
        stray = sorted(RESERVED_TOOL_NAMES - BUILTIN_NAMES - from_literals)
        assert not stray, (
            f"{stray} are reserved but nothing registers them. Either the tool was "
            "removed and the reservation should go, or its name constant moved."
        )

    def test_the_three_literal_names_are_still_registered(self) -> None:
        """Guards the exception above: if one of these is renamed, the set no
        longer describes anything and the previous test would still pass."""
        from psych_runtime.tools import builtins

        source = builtins.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        for name in ("load_skill", "remember", "forget"):
            assert f'"{name}"' in text, f"{name} is reserved but no longer registered"


class TestTheLibraryDoesNotOwnTheConsumersStderr:
    """A library attaches a `NullHandler` and configures nothing else.

    Without one, Python's `lastResort` handler prints anything at WARNING or
    above to stderr when the consumer has configured no logging, so Psych's
    lease-renewal warnings would appear uninvited in the middle of somebody
    else's console output.

    Asserted in a fresh interpreter rather than against this one, and the first
    version of this got that wrong. It read `logging.getLogger("psych_runtime")` here,
    which passed alone and failed in the full suite: the playground's own
    `configure_logging` runs earlier in the session and replaces the handlers
    on that logger, which is a consumer configuring logging and is exactly what
    should happen. The claim being made is about what importing `psych` leaves
    behind, so the only place to check it is somewhere nothing else has
    imported anything.
    """

    def test_importing_psych_configures_a_null_handler_and_nothing_else(self) -> None:
        probe = """
import json, logging
import psych_runtime

logger = logging.getLogger("psych_runtime")
print(json.dumps({
    "null_handlers": sum(isinstance(h, logging.NullHandler) for h in logger.handlers),
    "other_handlers": [type(h).__name__ for h in logger.handlers
                       if not isinstance(h, logging.NullHandler)],
    "level": logger.level,
    "propagate": logger.propagate,
}))
"""
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
        )
        state = json.loads(result.stdout)

        assert state["null_handlers"] == 1
        # Level and destination are the consumer's to choose. A library that
        # set either would override an application's own configuration.
        assert state["other_handlers"] == []
        assert state["level"] == logging.NOTSET
        assert state["propagate"] is True
