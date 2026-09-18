"""A failure report must be buildable from whatever actually failed.

``ToolFailure.message`` and ``SandboxFailure.message`` are capped because the
text is replayed into a prompt. The cap used to be a refusal: text over it
raised while the *report about a first failure* was being built, so the
consumer caught that second error, described it instead, and the real failure
never reached the model. A deployment binding a large MCP catalogue reached
this by ordinary use -- the sandbox's own "no tool named X, available: ..."
diagnostic outgrew the cap, and a mistyped tool name came back as a complaint
about arguments that were never wrong.

Two things stop it, and both are tested here: the caps clamp rather than
refuse, and the sandbox's diagnostic is bounded at the point it is built so
catalogue size cannot decide whether it exists.
"""

from __future__ import annotations

import ast

import pytest

from psych_runtime.core.records import ToolFailure
from psych_runtime.sandbox._bootstrap import BOOTSTRAP_SOURCE
from psych_runtime.sandbox.port import SandboxFailure

pytestmark = pytest.mark.unit

MESSAGE_CAP = 8192
TRACEBACK_CAP = 65_536


def offered_list(names: set[str]) -> str:
    """The bootstrap's own ``_offered_list``, run against ``names``.

    Taken from ``BOOTSTRAP_SOURCE`` rather than copied, so this tests the
    source the child actually runs. The bootstrap is a stdlib-only script that
    connects to its host on import, so it cannot simply be imported; lifting
    the one closure out by name is what makes it reachable.
    """
    tree = ast.parse(BOOTSTRAP_SOURCE)
    found: list[ast.stmt] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_offered_list"
    ]
    assert len(found) == 1, "the bootstrap defines _offered_list exactly once"
    namespace: dict[str, object] = {"offered": names}
    exec(compile(ast.Module(body=found, type_ignores=[]), "<bootstrap>", "exec"), namespace)
    return namespace["_offered_list"]()  # type: ignore[operator,no-any-return]


class TestTheCapsClampRatherThanRefuse:
    def test_a_long_tool_failure_message_is_cut_not_rejected(self) -> None:
        failure = ToolFailure(kind="exception", message="x" * (MESSAGE_CAP * 4))
        assert len(failure.message) == MESSAGE_CAP
        assert failure.message.endswith("[truncated]")

    def test_a_long_traceback_is_cut_to_its_own_larger_cap(self) -> None:
        failure = ToolFailure(kind="exception", message="x", traceback="y" * 200_000)
        assert failure.traceback is not None
        assert len(failure.traceback) == TRACEBACK_CAP

    def test_a_long_sandbox_failure_message_is_cut_not_rejected(self) -> None:
        failure = SandboxFailure(kind="exception", message="x" * (MESSAGE_CAP * 4))
        assert len(failure.message) == MESSAGE_CAP
        assert failure.message.endswith("[truncated]")

    def test_a_message_within_the_cap_is_untouched(self) -> None:
        assert ToolFailure(kind="exception", message="plain").message == "plain"
        assert SandboxFailure(kind="exception", message="plain").message == "plain"


class TestTheNotFoundDiagnosticIsBounded:
    """357 tool names across two servers is what the reported deployment
    carried; the joined list alone was over the cap."""

    def test_a_large_catalogue_still_fits_in_a_failure_message(self) -> None:
        names = {f"orders__get_orders_by_customer_and_status_{index}" for index in range(400)}
        message = (
            f"no tool named 'typo' is available to this program. Available: {offered_list(names)}"
        )
        assert len(message) < MESSAGE_CAP
        assert ToolFailure(kind="binding_not_available", message=message).message == message

    def test_it_says_how_many_names_it_left_out(self) -> None:
        rendered = offered_list({f"tool_{index:04d}" for index in range(400)})
        assert rendered.endswith("more")
        assert "tool_0000" in rendered

    def test_a_small_catalogue_is_listed_in_full(self) -> None:
        assert offered_list({"lookup", "refund"}) == "lookup, refund"

    def test_no_tools_reads_as_none(self) -> None:
        assert offered_list(set()) == "none"

    def test_one_name_longer_than_the_whole_budget_still_says_something(self) -> None:
        rendered = offered_list({"t" * 9000})
        assert rendered != "none"
        assert len(rendered) < MESSAGE_CAP
