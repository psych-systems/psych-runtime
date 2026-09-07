"""The example in docs/api.md, executed.

A worked example that does not run is a lie with a long half-life: it is the
first thing a new consumer copies, and it fails for them in a way that looks like
their mistake. DESIGN.md §22's "nothing demoed" rule applies to documentation as
much as to code.

This is the same example, with two substitutions a test has to make: the
in-memory store instead of PostgreSQL, and the scriptable fake instead of a real
provider. Everything else, including the order of the calls and the shape of the
Spec, is what `docs/api.md` shows. If the API changes, this fails and the document
gets fixed.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any

import pytest
from pydantic import ConfigDict, create_model

import psych_runtime
from psych_runtime.core.records import TerminalState, ToolOutcome
from psych_runtime.core.usage import Usage
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.testing.fake_model import FakeModel

# The document reaches for these through `psych_runtime` itself, so this does too:
# every name below being importable from the package root is part of what
# this test pins. `InMemoryStore` and `FakeModel` are the two substitutions the
# module docstring names, and both are deliberately not on the public surface.
Runtime = psych_runtime.Runtime
ToolRegistry = psych_runtime.ToolRegistry
ValidationContext = psych_runtime.ValidationContext
Worker = psych_runtime.Worker

pytestmark = pytest.mark.e2e


def build_registry() -> ToolRegistry:
    """Step 1 of the document: register your tools."""
    registry = ToolRegistry()

    @registry.register(annotations={"read-only"})
    async def lookup_order(order_id: str) -> dict[str, str]:
        """Look up an order by its id."""
        return {"order_id": order_id, "status": "shipped"}

    @registry.register(interruptible=False, annotations={"destructive"})
    async def issue_refund(order_id: str, cents: int) -> str:
        """Refund an order. Not interruptible: a half-issued refund is worse than
        a slow stop."""
        return f"refunded {cents} on {order_id}"

    return registry


def build_spec() -> psych_runtime.AgentSpec:
    """Step 2: describe the agent. This is data, with no callables in it."""
    return psych_runtime.AgentSpec(
        name="support",
        instructions="Help the customer with their order. Be brief.",
        model=psych_runtime.ModelRef(model="fake-standard", temperature=0.2),
        tools=(
            psych_runtime.CodeTool(name="lookup_order"),
            psych_runtime.CodeTool(name="issue_refund", interruptible=False),
        ),
        limits=psych_runtime.Limits(max_turns=12, deadline_seconds=300),
    )


async def call_integration(name: str, **arguments: str) -> dict[str, str]:
    """The one callable behind every dynamically-registered tool in the
    'tools whose shape is known only at runtime' section: what varies per
    tool is the schema and the name it is bound under, not the function."""
    return {"tool": name, **arguments}


def build_dynamic_registry() -> ToolRegistry:
    """The document's dynamic-registration section: a schema built with
    ``create_model`` at runtime, handed to ``register_dynamic`` rather than
    read off a signature."""
    registry = ToolRegistry()
    arguments_model = create_model(
        "lookup_salesforce_account_Arguments",
        __config__=ConfigDict(extra="forbid"),
        account_id=(str, ...),
    )
    registry.register_dynamic(
        "lookup_salesforce_account",
        functools.partial(call_integration, "lookup_salesforce_account"),
        arguments_model,
        description="Look up a Salesforce account by id.",
        annotations={"read-only"},
    )
    return registry


def build_dynamic_spec() -> psych_runtime.AgentSpec:
    return psych_runtime.AgentSpec(
        name="crm-support",
        instructions="Help the customer with their Salesforce account. Be brief.",
        model=psych_runtime.ModelRef(model="fake-standard"),
        tools=(psych_runtime.CodeTool(name="lookup_salesforce_account"),),
        limits=psych_runtime.Limits(max_turns=12, deadline_seconds=300),
    )


class TestTheDocumentedExample:
    async def test_the_whole_example_runs_end_to_end(self) -> None:
        registry = build_registry()
        spec = build_spec()
        store = InMemoryStore()

        # Step 3: publish. Validation happens here, never mid-conversation.
        version = await psych_runtime.publish(
            store, spec, context=ValidationContext(registered_tools=registry.names)
        )

        # Step 4: run a Worker. Your process, Psych's loop.
        model = (
            FakeModel()
            .turn(
                text="Checking.",
                tool_calls=[("lookup_order", {"order_id": "A1"})],
                usage=Usage(input=820, output=14),
            )
            .turn(text="Order A1 has shipped.", usage=Usage(input=910, output=22))
        )
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        worker_task = asyncio.create_task(worker.run())

        # Step 5: admit a Run. Your webhook handler or queue consumer does this.
        scope = psych_runtime.Scope(tenant="acme", principal="user-42")
        run = await psych_runtime.dispatch(
            store,
            version,
            scope,
            input={"message": "where is order A1?"},
            idempotency_key="webhook-evt-8891",
        )

        # Step 6: stream it to your user.
        said: list[str] = []
        async for record in psych_runtime.stream(store, run.run_id):
            if record.type == "model_call_finished" and record.text:
                said.append(record.text)

        # Step 7: say what it cost.
        report = await psych_runtime.report(store, run.run_id)

        worker.stop()
        await asyncio.wait_for(worker_task, timeout=10)

        assert report.terminal_state is TerminalState.COMPLETED
        assert said == ["Checking.", "Order A1 has shipped."]
        assert report.totals.usage.input > 0

    async def test_the_approval_snippet_works(self) -> None:
        """The suspension section of the document."""
        registry = build_registry()
        spec = build_spec()
        store = InMemoryStore()
        version = await psych_runtime.publish(
            store, spec, context=ValidationContext(registered_tools=registry.names)
        )

        model = (
            FakeModel()
            .turn(tool_calls=[("issue_refund", {"order_id": "A1", "cents": 500})])
            .turn(text="Refund issued.")
        )
        runtime = Runtime(
            store=store,
            model=model,
            registry=registry,
            approval_selectors=("@destructive",),
        )
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        task = asyncio.create_task(worker.run())

        run = await psych_runtime.dispatch(
            store, version, psych_runtime.Scope(tenant="acme"), input={"message": "refund A1"}
        )

        state = await _await(store, run.run_id, lambda s: s.suspended)
        assert state.suspend_reason == "approval"
        assert state.pending_approval_call_id is not None

        # The snippet in the document.
        if state.suspended and state.suspend_reason == "approval":
            await psych_runtime.resume(store, run.run_id, approved=True)

        settled = await _await(store, run.run_id, lambda s: s.settled, timeout=10)
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
        assert settled.terminal_state is TerminalState.COMPLETED

    async def test_the_interrupt_snippet_works(self) -> None:
        registry = build_registry()
        store = InMemoryStore()
        version = await psych_runtime.publish(
            store, build_spec(), context=ValidationContext(registered_tools=registry.names)
        )
        run = await psych_runtime.dispatch(
            store, version, psych_runtime.Scope(tenant="acme"), input={"message": "hi"}
        )

        await psych_runtime.interrupt(store, run.run_id, reason="the user pressed stop")

        state = await psych_runtime.state(store, run.run_id)
        assert state.aborted

    async def test_the_dynamic_registration_snippet_runs_a_real_run(self) -> None:
        """The 'tools whose shape is known only at runtime' section: a Run
        whose only tool was registered through ``register_dynamic`` rather
        than the decorator, from dispatch through to the report."""
        registry = build_dynamic_registry()
        spec = build_dynamic_spec()
        store = InMemoryStore()

        version = await psych_runtime.publish(
            store, spec, context=ValidationContext(registered_tools=registry.names)
        )

        model = (
            FakeModel()
            .turn(
                text="Checking.",
                tool_calls=[("lookup_salesforce_account", {"account_id": "acc_1"})],
            )
            .turn(text="Account acc_1 is in good standing.")
        )
        runtime = Runtime(store=store, model=model, registry=registry)
        worker = Worker(store, runtime, poll_interval=0.01, supervisor_interval=0.05)
        worker_task = asyncio.create_task(worker.run())

        run = await psych_runtime.dispatch(
            store,
            version,
            psych_runtime.Scope(tenant="acme"),
            input={"message": "how is account acc_1?"},
            idempotency_key="webhook-evt-dyn-1",
        )

        settled = await _await(store, run.run_id, lambda s: s.settled, timeout=10)
        worker.stop()
        await asyncio.wait_for(worker_task, timeout=10)
        report = await psych_runtime.report(store, run.run_id)

        assert settled.terminal_state is TerminalState.COMPLETED
        assert report.terminal_state is TerminalState.COMPLETED
        assert [call.tool for call in report.tool_calls] == ["lookup_salesforce_account"]
        assert report.tool_calls[0].outcome is ToolOutcome.OK

    async def test_the_testing_snippet_builds_a_usable_fake(self) -> None:
        model = (
            FakeModel()
            .turn(text="Checking.", tool_calls=[("lookup_order", {"order_id": "A1"})])
            .turn(text="A1 has shipped.")
        )
        assert await model.known_models()

    async def test_the_trigger_snippets_admit_one_run_per_key(self) -> None:
        """The three trigger shapes in the document, and the property all three
        rest on: a redelivery keyed the same produces one Run, not two."""
        registry = build_registry()
        store = InMemoryStore()
        version = await psych_runtime.publish(
            store, build_spec(), context=ValidationContext(registered_tools=registry.names)
        )
        scope = psych_runtime.Scope(tenant="acme")

        for key in ("digest:acme:2026-09-01T03", "stripe:evt_881", "sqs:msg-17"):
            first = await psych_runtime.dispatch(
                store, version, scope, input={"message": "go"}, idempotency_key=key
            )
            redelivered = await psych_runtime.dispatch(
                store, version, scope, input={"message": "go"}, idempotency_key=key
            )
            assert redelivered.run_id == first.run_id, f"key {key} admitted twice"
            assert first.created
            assert not redelivered.created

    async def test_every_documented_call_returns_a_type_a_consumer_can_name(self) -> None:
        """docs/api.md's opening rule cuts both ways.

        > Everything a consumer calls is on the `psych_runtime` module. Anything reached
        > through a submodule path is internal and moves without ceremony.

        The calls obeyed that and their return types did not, so annotating the
        result of five of the eight documented calls meant importing from a
        module the documentation calls unstable. Under `mypy --strict`, which
        this project requires of itself, that is not cosmetic.

        Checked by walking the annotations rather than by listing the types,
        so a call added later with an unexported return type fails here instead
        of reaching a consumer.
        """
        import inspect
        import typing

        unexported: list[str] = []
        for name in ("publish", "dispatch", "stream", "state", "report", "records"):
            # eval_str because psych_runtime.api uses `from __future__ import
            # annotations`, so without it every annotation is the string
            # "AsyncIterator[Record]" and the unwrapping below silently
            # finds nothing to check.
            annotation = inspect.signature(
                getattr(psych_runtime, name), eval_str=True
            ).return_annotation
            for referenced in _named_types(annotation):
                if referenced not in psych_runtime.__all__:
                    unexported.append(f"psych.{name}() -> {referenced}")

        assert not unexported, (
            "these documented calls return types that cannot be imported from "
            "`psych_runtime`, so a consumer cannot annotate the result without reaching "
            "into a submodule docs/api.md calls internal:\n  " + "\n  ".join(unexported)
        )
        assert typing is not None  # the import is used by _named_types

    async def test_every_call_the_document_lists_exists(self) -> None:
        """The table at the top of docs/api.md. A documented call that was
        renamed is the same failure as a broken example."""
        for name in (
            "publish",
            "dispatch",
            "stream",
            "state",
            "report",
            "resume",
            "interrupt",
            "records",
        ):
            assert hasattr(psych_runtime, name), (
                f"docs/api.md lists psych.{name}, which does not exist"
            )
            assert name in psych_runtime.__all__, f"psych.{name} is not exported"


def _named_types(annotation: Any) -> list[str]:
    """The concrete type names inside a return annotation.

    Unwraps the containers the public API actually uses, ``AsyncIterator[Record]``
    and ``Sequence[Record]``, so the check is about the type a consumer would
    write down rather than the wrapper around it. ``None`` returns nothing to
    check, which is why ``resume`` and ``interrupt`` are not in the list above.
    """
    import typing

    if annotation in (None, type(None), inspect.Signature.empty):
        return []
    args = typing.get_args(annotation)
    if args:
        return [name for arg in args for name in _named_types(arg)]
    return [getattr(annotation, "__name__", str(annotation))]


async def _await(store: InMemoryStore, run_id: Any, condition: Any, timeout: float = 5.0) -> Any:
    from datetime import UTC, datetime, timedelta

    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        state = await psych_runtime.state(store, run_id)
        if condition(state):
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("the condition was never met")
