"""One call that assembles the pieces, for the first hour with Psych.

Everything here is wiring a consumer would otherwise write themselves, and it
is wiring that has one obvious right answer. A store, a registry, a Runtime, a
Worker running in the background, and the publish-dispatch-wait sequence that
turns a Spec and a sentence into an answer. Nine objects in a fixed order, none
of which the reader has an opinion about yet.

That order is not hard, but it has to be discovered, and until it is nothing
runs at all. A library whose smallest working program is forty lines of setup
gets evaluated on the setup.

## This is a convenience, not a second way to use Psych

``Session`` holds no state the log does not, decides nothing the Runtime would
not, and hides nothing: ``store``, ``registry``, ``runtime`` and ``worker`` are
the real objects, public, and meant to be reached for the moment a consumer
needs something this module does not offer. Reaching past it is the expected
path out, not a failure of it. There is exactly one execution model underneath
either way, which is the property that makes shipping this safe -- see
DESIGN.md §4's warning about a runtime that forks into two.

What it deliberately does not do is grow. A per-tenant model client, an
approval policy that varies per agent, a Worker fleet across processes: those
are real deployments, they are what ``Runtime`` and ``Worker`` are already
shaped for, and answering them here would turn a convenience into the framework
DESIGN.md §1 refuses. ``examples/playground/backend/app/runtime_router.py`` is
what that looks like when written out, and it is not much longer.

## Why the model has no default

``session()`` requires a ``ModelClient``. Defaulting it would mean choosing a
provider, an endpoint and a credential on a consumer's behalf, and the first
run would either bill somebody or fail with a network error that has nothing
to do with the code they just wrote. ``psych_runtime.testing.fake_model.FakeModel`` is
the answer for a first run that must not touch a network, and
``psych_runtime.OpenAICompatibleClient`` is the answer for one that should.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from psych_runtime.api import answer as _answer
from psych_runtime.api import dispatch as _dispatch
from psych_runtime.api import publish as _publish
from psych_runtime.core.answer import AnswerView
from psych_runtime.core.ids import RunId, VersionHash
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import Spec
from psych_runtime.core.validation import ValidationContext
from psych_runtime.core.version import Version
from psych_runtime.model.port import ModelClient
from psych_runtime.runtime.dispatch import Dispatched
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.stream import stream_until_settled
from psych_runtime.runtime.worker import Worker
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import Store
from psych_runtime.tools.registry import ToolRegistry

__all__ = ["Session", "session"]

DEFAULT_WAIT_SECONDS = 120.0
"""How long ``ask()`` waits for a Run to settle before giving up.

A ceiling on the caller's patience, not on the Run: the Run's own
``Limits.deadline_seconds`` is what actually stops it, and this timing out
leaves it running. Two minutes is longer than an interactive agent turn and
shorter than a person will sit looking at a terminal wondering whether it hung.
"""


@runtime_checkable
class _Buildable(Protocol):
    """Anything with a ``.build()``, which is what the builders have.

    Structural rather than an import of ``psych_runtime.builder`` so that accepting a
    builder here costs nothing at import time and creates no cycle: this module
    sits above the whole package and the builder sits at the top of it.
    """

    def build(self) -> Spec: ...


@dataclass
class Session:
    """A running Worker, a store, and the publish-dispatch-wait sequence.

    Built by ``session()``; constructing one directly means owning the Worker's
    lifecycle yourself, which is what ``session()`` exists to avoid.

    The four fields below are the real objects, not wrappers. A consumer who
    needs a second Worker, a per-tenant Runtime or a store transaction reaches
    through them rather than around this class.
    """

    store: Store
    registry: ToolRegistry
    runtime: Runtime
    worker: Worker
    scope: Scope
    _versions: dict[str, Version] = field(default_factory=dict, repr=False)

    async def publish(self, spec: Spec | _Buildable) -> Version:
        """Validate and publish a Spec, with this Session's tools in context.

        Passing the registry's names for you is the point: publish-time
        validation can only reject a Spec naming a tool nobody registered when
        it is told what was registered (DESIGN.md §4), and a first-time caller
        who omits the ``ValidationContext`` gets structural checks only and
        discovers the typo mid-conversation instead.

        Publishing the same Spec twice returns the same Version, so calling
        this on every ``ask()`` is free rather than merely harmless.
        """
        built = spec.build() if isinstance(spec, _Buildable) else spec
        return await _publish(
            self.store,
            built,
            context=ValidationContext(
                registered_tools=self.registry.names,
                sandbox_profiles=self.runtime.sandbox_profiles,
            ),
        )

    async def start(
        self,
        spec: Spec | _Buildable | Version | VersionHash,
        message: str | dict[str, Any] | None = None,
        *,
        scope: Scope | None = None,
        **dispatch_options: Any,
    ) -> Dispatched:
        """Admit a Run and return immediately.

        The non-blocking half of ``ask()``, for a caller who wants to stream
        the Run themselves with ``psych_runtime.stream()`` rather than wait for the
        answer. ``message`` is sugar for ``input={"message": ...}``; pass
        ``input=`` in ``dispatch_options`` for anything else, and pass both and
        this raises rather than guessing which one you meant.
        """
        if message is not None and "input" in dispatch_options:
            raise TypeError(
                "pass message= or input=, not both: they set the same field and "
                "there is no correct way to merge them"
            )
        payload = dispatch_options.pop("input", None)
        if message is not None:
            payload = {"message": message} if isinstance(message, str) else message
        return await _dispatch(
            self.store,
            await self._version_of(spec),
            scope or self.scope,
            input=payload,
            **dispatch_options,
        )

    async def ask(
        self,
        spec: Spec | _Buildable | Version | VersionHash,
        message: str | dict[str, Any] | None = None,
        *,
        scope: Scope | None = None,
        timeout: float | None = DEFAULT_WAIT_SECONDS,
        **dispatch_options: Any,
    ) -> AnswerView:
        """Run a Spec against one message and return what it concluded.

        Publishes if it has not already, dispatches, waits for the Run to
        settle, and projects the log with ``psych_runtime.answer()``. Returns rather
        than raises when a Run fails: ``AnswerView.finished`` is False and
        ``psych_runtime.status()`` and ``psych_runtime.report()`` on ``view.run_id`` say why.
        A failed Run is a thing that happened and is fully recorded, not an
        exception this layer should invent.
        """
        dispatched = await self.start(spec, message, scope=scope, **dispatch_options)
        return await self.wait(dispatched.run_id, timeout=timeout)

    async def wait(
        self, run_id: RunId, *, timeout: float | None = DEFAULT_WAIT_SECONDS
    ) -> AnswerView:
        """Wait for one Run to settle, then project its answer.

        On timeout this returns the answer as it stands rather than raising,
        which for a Run still executing means ``finished`` is False. The Run
        keeps going; the Worker owns it and this call never did.
        """
        await stream_until_settled(self.store, run_id, timeout=timeout)
        return await _answer(self.store, run_id)

    async def _version_of(self, spec: Spec | _Buildable | Version | VersionHash) -> Version:
        """Publish a Spec once per Session, or pass an already-published one through."""
        if isinstance(spec, Version):
            return spec
        if isinstance(spec, str):
            stored = await self.store.get_version(VersionHash(spec))
            if stored is None:
                raise LookupError(f"no Version {spec!r} in this store")
            return stored
        built = spec.build() if isinstance(spec, _Buildable) else spec
        cached = self._versions.get(built.name)
        if cached is not None and cached.spec == built:
            return cached
        version = await self.publish(built)
        self._versions[built.name] = version
        return version


@contextlib.asynccontextmanager
async def session(
    model: ModelClient,
    *,
    store: Store | None = None,
    registry: ToolRegistry | None = None,
    tools: Sequence[Callable[..., Any]] = (),
    tenant: str = "local",
    principal: str | None = None,
    concurrency: int = 4,
    **runtime_options: Any,
) -> AsyncIterator[Session]:
    """Assemble a store, a registry, a Runtime and a running Worker.

    ```python
    async with psych_runtime.session(model, tools=[lookup_order]) as s:
        view = await s.ask(spec, "where is order A1?")
        print(view.text)
    ```

    The Worker runs for the life of the block and is stopped and awaited on the
    way out, including when the block raises. A Run still executing when the
    block ends is not lost: the Worker returns it RUNNABLE and the log is
    intact, so another Worker -- or the next ``session()`` over the same store
    -- picks it up where it stopped (DESIGN.md §8).

    Args:
        model: the ``ModelClient``. Required, deliberately: see the module
            docstring. ``FakeModel`` for a run that must not touch a network.
        store: where Runs live. Defaults to ``InMemoryStore()``, which is a real
            ``Store`` and an entirely in-process one: nothing survives the
            process, so it is right for a first run, a test and a script, and
            wrong for anything whose Runs need to outlive it. Pass a
            ``PostgresStore``, ``MySQLStore`` or ``DynamoDBStore`` for that,
            already migrated.
        registry: an existing ``ToolRegistry``. Defaults to a fresh one.
        tools: plain functions to register into it, as ``@registry.register``
            would. The schema comes from each function's type hints and its
            description from its docstring, so a function with neither is
            refused here rather than reaching the model undescribed.

            Registration here passes no annotations, and an unannotated tool
            classifies as ``write`` (DESIGN.md §10.9). That matters the moment
            approvals are in play: a function registered through this argument
            never matches an ``@destructive`` selector, so a tool you meant to
            gate runs ungated and nothing says so. Anything carrying
            annotations, ``interruptible=False`` or ``safe_to_retry`` wants its
            own ``registry.register(...)`` call and a ``registry=`` here, which
            is what a real integration writes anyway. This argument is for the
            plain case, and the plain case only.
        tenant: the Scope every call in this Session defaults to. ``"local"``
            is a placeholder that suits a single-tenant script; a consumer
            serving end users passes the real one per call to ``ask()``, since
            one Session serves any number of tenants.
        principal: who is acting, recorded on every Record and passed to
            Policy. Psych never interprets it.
        concurrency: how many Runs the Worker executes at once.
        runtime_options: anything else ``Runtime`` takes -- ``policy``,
            ``approval_selectors``, ``telemetry``, ``sandbox``, ``memory``,
            ``mcp``, ``a2a``, ``blob``, ``http``, ``prices``. Passed straight
            through, so this helper never becomes the thing that decides which
            Runtime options exist.
    """
    registry = registry if registry is not None else ToolRegistry()
    for tool in tools:
        registry.register(tool)

    store = store if store is not None else InMemoryStore()
    runtime = Runtime(store=store, model=model, registry=registry, **runtime_options)
    worker = Worker(store, runtime, concurrency=concurrency)

    live = Session(
        store=store,
        registry=registry,
        runtime=runtime,
        worker=worker,
        scope=Scope(tenant=tenant, principal=principal),
    )
    task = asyncio.create_task(worker.run())
    try:
        yield live
    finally:
        worker.stop()
        # Shielded from the caller's own cancellation: a Worker told to stop is
        # mid-shutdown, and abandoning it there leaves claimed Runs holding
        # leases until they expire. Bounded, because a Worker that will not
        # shut down must not also stop the process from exiting.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
