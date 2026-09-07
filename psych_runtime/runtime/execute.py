"""Wiring: what a Worker actually runs when it claims a Run.

One engine, two shapes (DESIGN.md §5). A Worker claims a Run and does not know
whether it is an agent or a workflow; the pinned Version decides, and this module
is where that decision is made once rather than at every call site.

The Run is settled here rather than inside the agent loop, because a workflow
step that is an agent finishes without the Run finishing. A loop that settled its
own Run could not be nested, and nesting is the whole reason there is one engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from psych_runtime.core.answer import split_answer
from psych_runtime.core.errors import AccessDenied, VersionNotFound
from psych_runtime.core.ids import RunId, ToolCallId, WorkerId
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.records import QueueKind, Record, TerminalState, ToolFailure
from psych_runtime.core.reducer import ChildRun, reduce
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import AgentSpec, SubagentRef, WorkflowSpec
from psych_runtime.core.status import status_of
from psych_runtime.core.validation import ValidationContext, validate_spec
from psych_runtime.core.version import Version
from psych_runtime.core.version import compute_hash as _compute_hash
from psych_runtime.core.version import publish as make_version
from psych_runtime.model.port import ModelClient
from psych_runtime.model.pricing import CostPolicy, PriceResolver
from psych_runtime.runtime.abort import AbortReason, AbortSignal
from psych_runtime.runtime.agent import DEFAULT_OFFLOAD_BYTES, AgentLoop, ToolExecutor
from psych_runtime.runtime.dispatch import dispatch, send
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.notify import notify_parent
from psych_runtime.runtime.subagent import SpawnRequest, child_input, compose_child_spec
from psych_runtime.runtime.thread import load_thread_history
from psych_runtime.runtime.workflow import WorkflowEngine
from psych_runtime.sandbox.port import Sandbox
from psych_runtime.store.blob import BlobStore
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.telemetry.port import (
    NOOP_TELEMETRY,
    GuardedTelemetry,
    Telemetry,
    TelemetrySpan,
)
from psych_runtime.tools.a2a import A2ATools
from psych_runtime.tools.builtins import MemoryPort, register_builtins
from psych_runtime.tools.code import make_run_code, run_code_definition
from psych_runtime.tools.deferred import DEFAULT_CATALOGUE_BUDGET_CHARS, DeferredDiscovery
from psych_runtime.tools.guidance import failure_guidance
from psych_runtime.tools.mcp import McpTools
from psych_runtime.tools.policy import AllowAll, Policy
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ToolResolver

__all__ = ["HttpCaller", "Runtime", "StepFailed"]

_LOG: Final = logging.getLogger("psych.runtime.execute")


class StepFailed(Exception):
    """A workflow step's agent ended in a failure, carried whole.

    The engine records ``ToolFailure(kind=type(err).__name__, ...)`` for a step
    that raised, so re-raising a nested agent's failure as a bare
    ``RuntimeError`` renamed every kind to ``RuntimeError`` and dropped the
    traceback the agent had already built. This carries the original so the
    step's record says what actually failed.
    """

    def __init__(self, failure: ToolFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


@runtime_checkable
class HttpCaller(Protocol):
    """What a ``Runtime`` needs to execute an ``HttpTool``.

    Structural, matching ``psych_runtime.tools.http.HttpToolExecutor``: the runtime
    layer may import ``psych_runtime.tools``, but naming the class here would make an
    HTTP transport a hard dependency of every Runtime, including the ones that
    never run an HTTP tool.
    """

    def bind(self, scope: Scope) -> Callable[..., Awaitable[Any]]: ...


@dataclass(frozen=True, slots=True)
class _AttemptContext:
    """The per-Attempt facts a delegation or a workflow step needs.

    Passed as an argument rather than held on ``Runtime``: one ``Runtime`` is
    shared by every concurrent Attempt in a process (its own docstring: "holds
    no per-Run state"), and three fields on it that a later ``await`` read back
    meant a delegation could be dispatched under whichever tenant happened to
    start a Run most recently. That is DESIGN.md §10.4's failure with a
    different mechanism.
    """

    scope: Scope
    run_id: RunId
    span: TelemetrySpan | None


@dataclass
class Runtime:
    """Everything a Worker needs to execute a Run.

    Assembled once at boot by the consumer and handed to every Worker in the
    process. Holds no per-Run state, so one instance serves any number of
    concurrent attempts.
    """

    store: Store
    model: ModelClient
    registry: ToolRegistry
    resolver: ToolResolver | None = None
    executor: ToolExecutor | None = None
    prices: PriceResolver | None = None
    cost_policy: CostPolicy = "prefer_provider"
    """Which cost is written into the Record when a provider reports one.

    ``"prefer_provider"`` by default: a gateway's figure comes from the party
    doing the billing and is computed against the caller's real contract,
    including negotiated rates Psych has no way to know, so recording Psych's
    own arithmetic over the top of it would be recording the worse of two
    numbers. A provider that reports nothing falls back to the table exactly as
    before, so nothing changes for a deployment whose provider is silent.

    ``"computed"`` ignores the provider entirely, and ``"provider_only"``
    records ``None`` rather than falling back. See
    ``psych_runtime.model.pricing.CostPolicy`` for when each is the right answer.

    Whichever applies, the recorded ``Cost`` carries its own ``source``, so a
    report can tell the two apart rather than blending them."""
    memories: Sequence[str] = field(default_factory=tuple)
    policy: Policy | None = None
    approval_selectors: Sequence[str] = field(default_factory=tuple)
    telemetry: Telemetry | None = None
    sandbox: Sandbox | None = None
    """Where a model-written program runs. Absent means no ``run_code``, which
    is right: offering a tool that can only fail is worse than not offering it."""
    memory: MemoryPort | None = None
    end_user_id: str | None = None
    """Whose durable facts a Run may read and write. Required when ``memory`` is
    set: DESIGN.md §15 keys memory by Scope plus an end-user id, and defaulting
    it would quietly point every end user at one bucket of facts."""
    mcp: McpTools | None = None
    """MCP servers, wired for both halves of a Run at once.

    Without this a Spec can declare ``mcp_servers``, publish-time validation
    passes, the Run executes, and the model is never offered a single one of
    those tools. No error and no advisory: they are simply absent, which is
    exactly the silent-incapacity failure DESIGN.md §10.7 exists to prevent.

    One field rather than a catalog on the resolver and a caller on the
    executor, because those two must narrow identically and wiring them
    separately is how they drift. Pass ``McpTools(pool)``; supply a
    ``resolver=`` of your own only if you intend to own that half yourself.
    """

    a2a: A2ATools | None = None
    """Other agents this Worker may call over A2A, wired for both halves of a
    Run at once (``psych_runtime.tools.a2a``).

    The exact shape of ``mcp`` above and for the same reason: a Spec granting
    ``a2a_peers`` with no client wired would publish, run, and never offer the
    model a single peer skill. Absent is the honest state for a deployment that
    does not federate; present means a peer's skills appear in the prompt on
    the next turn boundary, narrowed by the same function as everything else.
    """

    blob: BlobStore | None = None
    """Where an oversized tool result's bytes live once they no longer fit in a
    log record. Absent means large results still elide for the model exactly as
    before, but one large enough to need offload records an explicit failure
    instead of being written inline (``psych_runtime.runtime.agent._record_success``):
    that failure is the honest answer for a consumer who has not wired one up,
    not a reason to force one into every Runtime that never produces results
    this large."""
    catalogue_budget_chars: int = DEFAULT_CATALOGUE_BUDGET_CHARS
    """How many **characters** of tool definitions one MCP server may put in
    the prompt before its catalogue is deferred and the model reaches it
    through discovery instead (``psych_runtime.tools.deferred``).

    Characters, not tokens, and the number is never scaled to resemble a token
    count: a token count needs a tokenizer, tokenizers differ per provider, and
    the same figure would then mean different things depending on which model
    an agent names.

    A Runtime field rather than a Spec one because it describes this
    deployment's prompt budget rather than the agent, so two Workers with
    different budgets can run the same Version. ``McpServer.preload`` still
    overrides it outright for an author who knows better.
    """

    blob_offload_bytes: int = DEFAULT_OFFLOAD_BYTES
    """The offload threshold in bytes. See ``psych_runtime.tools.large_results``'s
    module docstring for why this is a Runtime field and not a Spec one."""

    http: HttpCaller | None = None
    """Where an ``HttpTool`` in a Spec is actually executed. Absent means a Spec
    naming one publishes fine and then fails every call as data, which is the
    honest answer for a Runtime that was never given a way to make the request
    -- and the reason ``psych_runtime.core.validation`` refuses such a Spec at publish
    when the consumer passes a ``ValidationContext`` saying so."""

    _resolver: ToolResolver = field(init=False, repr=False)
    _executor: ToolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # The public fields are optional so a consumer can pass nothing and get
        # sensible defaults. The private ones are not, so nothing downstream has
        # to assert they were filled in.
        self._resolver = self.resolver or ToolResolver(
            self.registry,
            catalog=self.mcp,
            peers=self.a2a,
            catalogue_budget_chars=self.catalogue_budget_chars,
        )
        self._executor = self.executor or ToolExecutor(self.registry)

    async def __call__(self, journal: Journal, header: RunHeader, abort: AbortSignal) -> None:
        """Run one Attempt to a terminal record. The ``AttemptRunner`` signature.

        ``abort`` is the Worker's stop signal, watched alongside the work rather
        than polled inside it so a hung model stream still gets cut off. What it
        means when it fires is carried on the signal itself
        (``psych_runtime.runtime.abort``) and decides what is written afterwards: the
        deadline settles the Run ``ABORTED``, a graceful shutdown writes nothing
        so the next Worker continues from the log, and a lost lease writes
        nothing because another Worker owns that log now.
        """
        version = await self.store.get_version(header.version_hash)
        if version is None:
            raise VersionNotFound(header.version_hash)

        work = asyncio.create_task(self._execute(journal, version.spec, header, abort))
        watch = asyncio.create_task(abort.wait())
        done, _ = await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)

        if work in done:
            watch.cancel()
            await work
            # A background subagent tells its parent how it ended, here, at the
            # one place every ordinary ending passes through (DESIGN.md §17). A
            # parent suspended on `SuspendReason.CHILDREN` is woken by this and
            # by nothing else; the two endings that never reach here -- force
            # settlement and attempts exhausted -- notify from the Worker.
            await notify_parent(self.store, journal.run_id, journal.state)
            return

        # The signal fired. Ask the work to unwind either way; only the deadline
        # then writes a terminal record over it.
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work

        if abort.reason is AbortReason.DEADLINE:
            await self._settle_aborted(journal)
            await notify_parent(self.store, journal.run_id, journal.state)

    async def _execute(
        self,
        journal: Journal,
        spec: AgentSpec | WorkflowSpec,
        header: RunHeader,
        abort: AbortSignal,
    ) -> None:
        """Open the run and attempt spans, then execute inside them.

        The span tree mirrors the record log's own nesting: a run contains
        attempts, an attempt contains turns and steps, a turn contains a model
        call and the tool calls it produced. The schema declares that shape and
        the conformance suite checks emissions against it, so a span opened in
        the wrong place fails a test rather than quietly producing a flat trace
        that is useless for finding where a slow turn went.
        """
        telemetry = GuardedTelemetry(
            self.telemetry if self.telemetry is not None else NOOP_TELEMETRY
        )
        async with telemetry.start_span(
            "psych.run",
            attributes={
                "psych.run.id": journal.run_id,
                "psych.scope.tenant": journal.scope.tenant,
                "psych.version.hash": header.version_hash,
            },
        ) as run_span:
            attempt_number = max(journal.state.attempt_count, 1)
            async with run_span.start_span(
                "psych.attempt",
                attributes={
                    "psych.worker.id": str(journal.attempt_id or "inline"),
                    "psych.attempt.number": attempt_number,
                    # Same rule as the record in `psych_runtime.runtime.worker`, and
                    # deliberately the same expression: the span and the record
                    # describe one attempt, and they have disagreed before.
                    "psych.attempt.reclaimed_expired_lease": (
                        header.attempt_count > 1 and not journal.state.resumed_since_attempt
                    ),
                },
            ) as attempt_span:
                if isinstance(spec, AgentSpec):
                    await self._run_agent(journal, spec, attempt_span, abort)
                else:
                    await self._run_workflow(journal, spec, attempt_span, abort)

    async def _run_agent(
        self,
        journal: Journal,
        spec: AgentSpec,
        parent: TelemetrySpan | None = None,
        abort: AbortSignal | None = None,
    ) -> None:
        loop = await self._loop_for(journal, spec, parent, abort)
        outcome = await loop.run()
        if journal.state.settled or journal.state.suspended:
            return
        if abort is not None and abort.is_set() and abort.reason is not AbortReason.DEADLINE:
            # Shut down or handed over mid-Run. The loop stopped cleanly and the
            # log ends at a turn boundary; the next Worker picks it up there.
            return
        await journal.append(
            type="run_settled",
            state=outcome.state,
            output=outcome.output,
            failure=outcome.failure,
        )

    async def _run_workflow(
        self,
        journal: Journal,
        spec: WorkflowSpec,
        parent: TelemetrySpan | None = None,
        abort: AbortSignal | None = None,
    ) -> None:
        # `parent` is the attempt span. A workflow's own steps open their spans
        # through the engine rather than here, so this signature takes it only
        # to match `_run_agent`'s and to keep the caller's one call site honest
        # about what it is handing each branch.
        _ = parent
        engine = self.engine_for(journal, abort=abort)
        state, outputs = await engine.run(spec)
        if journal.state.settled or journal.state.suspended:
            return
        if abort is not None and abort.is_set() and abort.reason is not AbortReason.DEADLINE:
            return
        await journal.append(type="run_settled", state=state, output=outputs)

    def engine_for(self, journal: Journal, *, abort: AbortSignal | None = None) -> WorkflowEngine:
        """A ``WorkflowEngine`` bound to one Run, for a caller driving a
        workflow directly rather than through a ``Worker``.

        Public because the alternative is what tests and the playground's own
        scenarios were doing: reaching for ``Runtime._nested_agent`` and
        ``_nested_tool`` by name and passing the journal themselves. Those are
        private for a reason (they now need this Attempt's Scope and run id,
        which is what stopped two concurrent Runs sharing one delegation
        context), so the seam belongs here instead.
        """
        context = _AttemptContext(scope=journal.scope, run_id=journal.run_id, span=None)
        return WorkflowEngine(
            journal,
            lambda inner, agent_spec, step_input: self._nested_agent(
                inner, agent_spec, step_input, context, abort
            ),
            lambda inner, tool, arguments: self._nested_tool(inner, tool, arguments, context),
        )

    async def _nested_agent(
        self,
        journal: Journal,
        spec: AgentSpec,
        step_input: dict[str, Any],
        context: _AttemptContext,
        abort: AbortSignal | None,
    ) -> dict[str, Any] | None:
        """An agent inside a workflow step.

        Shares the parent's journal, so the whole workflow is one Run with one
        log and one resume path. The agent's own turns, model calls and tool
        calls appear in that log between the step's start and completion records.
        """
        _ = step_input
        loop = await self._loop_for(journal, spec, context.span, abort)
        outcome = await loop.run()
        if outcome.failure is not None:
            raise StepFailed(outcome.failure)
        return outcome.output

    async def _loop_for(
        self,
        journal: Journal,
        spec: AgentSpec,
        parent: TelemetrySpan | None = None,
        abort: AbortSignal | None = None,
    ) -> AgentLoop:
        """One place that builds an agent loop, so a top-level Run, a workflow
        step and a subagent are all configured identically.

        Built-ins are registered per Spec rather than once at boot, because which
        of them exist depends on the Spec: an agent with no skills gets no
        ``load_skill``, since a tool that can only ever answer "no such skill" is
        worse than no tool. They go into a Run-scoped registry layered over the
        consumer's, so registering them cannot collide with a consumer's own
        names across two concurrent Runs.

        Loads this Run's thread history here rather than inside
        ``AgentLoop`` itself, so the loop's own dependency surface stays the
        resolved things it already takes (a resolver, an executor, prices)
        rather than growing a ``Store`` of its own. Cheap for the overwhelming
        majority of calls -- every nested agent step, every subagent, and every
        top-level Run that is not itself a continuation -- because
        ``load_thread_history`` returns immediately for a ``None``
        ``continues_run_id`` without touching the store at all.
        """
        history = await load_thread_history(
            self.store,
            journal.scope,
            journal.state.continues_run_id,
            max_history_records=spec.limits.max_history_records,
        )
        memories = await self._recall(journal)
        registry, builtins = self._with_builtins(
            spec, journal.scope, self._end_user_id(journal), journal
        )
        # Bound to this Run's Spec and Scope: the executor's caller signature
        # carries only a name and arguments, and both are needed to narrow.
        mcp_caller = self.mcp.caller(spec, journal.scope) if self.mcp is not None else None
        http_caller = self.http.bind(journal.scope) if self.http is not None else None
        # Discovery for a server whose catalogue is too large to put in the
        # prompt (psych_runtime.tools.deferred). Bound to this Run's Scope for the same
        # reason the MCP caller is: the model sends a server name, and which
        # connection that reaches is a tenancy decision.
        discovery = DeferredDiscovery(self.mcp, journal.scope) if self.mcp is not None else None
        # Bound per Run for the same reason the MCP caller is: the model sends
        # a tool name, and which peer that reaches is a tenancy decision.
        a2a_caller = self.a2a.caller(spec, journal.scope) if self.a2a is not None else None
        executor = ToolExecutor(
            registry,
            mcp_caller=mcp_caller,
            http_caller=http_caller,
            discovery=discovery,
            a2a_caller=a2a_caller,
        )
        context = _AttemptContext(scope=journal.scope, run_id=journal.run_id, span=parent)
        loop = AgentLoop(
            journal,
            spec,
            self.model,
            self._resolver,
            executor,
            prices=self.prices,
            cost_policy=self.cost_policy,
            memories=memories,
            policy=self.policy,
            approval_selectors=self.approval_selectors,
            delegate=lambda ref, task, depth: self._delegate(ref, task, depth, context, abort),
            # Only for a Spec that carries an envelope. An agent that may not
            # compose children is never offered the three tools, rather than
            # being offered tools that always refuse.
            children=(
                _ComposedChildren(runtime=self, journal=journal, spec=spec, scope=journal.scope)
                if spec.spawn is not None
                else None
            ),
            # A span is itself a Telemetry, so nesting is just handing the loop
            # the span it should open its turns inside.
            telemetry=parent if parent is not None else self.telemetry,
            builtins=builtins,
            blob=self.blob,
            blob_offload_bytes=self.blob_offload_bytes,
            history=history,
            abort=abort,
        )
        if self.sandbox is not None:
            granted = [tool.name for tool in spec.tools]
            loop.offer_run_code(
                run_code_definition(granted),
                make_run_code(
                    self.sandbox,
                    # A binding routes back through the loop, not the executor:
                    # that is what makes Policy, the approval selectors, the
                    # repetition and failure-streak guards and the record log
                    # apply to a call a program makes exactly as they apply to
                    # one the model makes directly. §18: no back door.
                    host_call=loop.call_as_binding,
                    binding_names=granted,
                ),
            )
        return loop

    def _end_user_id(self, journal: Journal) -> str | None:
        """Whose durable facts this Run may read and write.

        The Run's own input wins (``end_user_id`` on the dispatch payload),
        then whatever the consumer set on the Runtime, then the Scope's
        principal. Reading it per Run rather than per process is what keeps one
        end user's memories out of another's prompt: a ``Runtime`` is built
        once at boot and serves every tenant, so an end-user id fixed there
        pointed every user at one bucket of facts (DESIGN.md §15). The
        principal is last because it is inferred rather than stated: a
        consumer whose principals *are* their end users gets memory for free,
        and one whose principals are service accounts is not silently given a
        bucket per service account -- they set the field, or put it in the
        Run's input.
        """
        from_input = journal.state.run_input.get("end_user_id")
        if isinstance(from_input, str) and from_input:
            return from_input
        if self.end_user_id is not None:
            return self.end_user_id
        return journal.scope.principal or None

    async def _recall(self, journal: Journal) -> tuple[str, ...]:
        """This Run's durable facts, read fresh at the start of every Attempt.

        ``Runtime.memories`` is a process-wide list and stays that way for a
        consumer who wants a fixed preamble, but a fact written by ``remember``
        reaches a later Run's prompt only because this reads it back. Failure
        here is not fatal: a memory store that is down should degrade to an
        agent that has forgotten, not to a Run that cannot start.
        """
        end_user_id = self._end_user_id(journal)
        if self.memory is None or end_user_id is None:
            return tuple(self.memories)
        try:
            facts = await self.memory.recall(journal.scope, end_user_id)
        except Exception:
            _LOG.warning("recalling memories for %s failed", journal.run_id, exc_info=True)
            return tuple(self.memories)
        return (*self.memories, *(fact.content for fact in facts))

    def _with_builtins(
        self,
        spec: AgentSpec,
        scope: Scope,
        end_user_id: str | None,
        journal: Journal | None = None,
    ) -> tuple[ToolRegistry, tuple[ToolDefinition, ...]]:
        """A registry carrying the consumer's tools plus this Spec's built-ins."""
        layered = ToolRegistry()
        for tool in self.registry:
            layered._tools[tool.name] = tool
        builtins = register_builtins(
            layered,
            spec,
            scope,
            memory=self.memory if end_user_id is not None else None,
            end_user_id=end_user_id if self.memory is not None else None,
            # `update_tasks` writes the plan into the log, so it needs the
            # journal rather than a store. Every other built-in is a plain
            # callable and takes none.
            journal=journal,
        )
        return layered, builtins

    async def _delegate(
        self,
        ref: SubagentRef,
        task: str,
        depth: int,
        context: _AttemptContext,
        abort: AbortSignal | None,
    ) -> dict[str, Any]:
        """Run a subagent as a nested Run with its own log and its own budget.

        A separate Run rather than a nested loop on the parent's journal, because
        DESIGN.md §17 says a subagent has its own log and its own budget, and
        because the report needs to be able to walk into it. The parent's
        StepCompleted record carries the child's run id for exactly that.

        The child runs to completion inline. That is the right shape for v1: a
        parent waiting on a child is a parent holding its lease, and the lease
        heartbeat keeps running because the parent's process is alive and working.
        """
        version = await self.store.get_version(_hash_of(ref))
        if version is None:
            version = await self._publish_child(ref)

        dispatched = await dispatch(
            self.store,
            version,
            context.scope,
            input=child_input(task),
            parent_run_id=context.run_id,
            delegation_depth=depth,
            nested=True,
        )

        child_journal = await Journal.open(self.store, dispatched.run_id, context.scope)
        await child_journal.append(
            type="attempt_started",
            worker_id=WorkerId("inline"),
            # Derived, not hardcoded, even though this child was dispatched two
            # lines ago and its log cannot hold a prior attempt today. Every
            # other writer of this record computes the number from the fold,
            # and a literal here is a contradiction waiting for the first
            # change that makes this path reachable twice: the reducer rejects
            # a repeated attempt number and fails the Run rather than repairing
            # it (DESIGN.md §6), so the cost of being wrong is a dead Run and
            # the cost of being right is one attribute access.
            attempt_number=child_journal.state.attempt_count + 1,
        )
        loop = await self._loop_for(child_journal, ref.spec, context.span, abort)
        outcome = await loop.run()
        if not child_journal.state.settled:
            await child_journal.append(
                type="run_settled",
                state=outcome.state,
                output=outcome.output,
                failure=outcome.failure,
            )

        # The child is nested, so nothing else could have claimed it; releasing
        # it settled keeps it out of every Worker's claim query for good.
        with contextlib.suppress(Exception):
            await self.store.release(dispatched.run_id, WorkerId("inline"), RunState.SETTLED)

        if outcome.failure is not None:
            return {
                "run_id": dispatched.run_id,
                "error": outcome.failure.message,
                "state": outcome.state.value,
            }
        return {
            "run_id": dispatched.run_id,
            "state": outcome.state.value,
            **(outcome.output or {}),
        }

    async def _publish_child(self, ref: SubagentRef) -> Version:
        version = make_version(ref.spec)
        await self.store.put_version(version)
        return version

    async def _nested_tool(
        self, journal: Journal, tool: str, arguments: dict[str, Any], context: _AttemptContext
    ) -> Any:
        """A workflow's ``ToolStep``, gated exactly like a model's tool call.

        A step calling the registry directly skipped the consumer's ``Policy``
        entirely, so a Version dispatched under a Scope that may not call a tool
        called it anyway. There is no model here to hand a refusal back to as
        data, so a denial raises and the step records it as a failure.
        """
        _ = journal
        policy = self.policy if self.policy is not None else AllowAll()
        decision = await policy.allow_tool(context.scope, tool, arguments)
        if decision.requires_approval:
            raise AccessDenied(
                f"tool {tool!r}",
                "the policy asks for a human decision, and a workflow tool step has "
                "no model turn to suspend into. Run this tool inside an agent step "
                "if it needs an approval.",
            )
        if not decision.allowed:
            raise AccessDenied(f"tool {tool!r}", decision.reason or "the policy refused it")
        return await self.registry.call(tool, arguments)

    async def _settle_aborted(self, journal: Journal) -> None:
        if journal.state.settled:
            return
        await journal.append(
            type="run_settled",
            state=TerminalState.ABORTED,
            failure=ToolFailure(
                kind="deadline",
                message=failure_guidance(
                    "deadline",
                    "The run passed its deadline and was asked to stop. It unwound "
                    "within the grace period, so this is a clean abort rather than a "
                    "force-settlement.",
                ),
            ),
        )


def _hash_of(ref: SubagentRef) -> Any:
    """A subagent's Version hash, computed rather than looked up.

    A subagent's Spec is embedded in its parent's, so its Version is derivable
    without a store round trip and is the same every time the parent runs. That
    is what stops a delegation from publishing a new Version per call.
    """
    return _compute_hash(ref.spec)


@dataclass(frozen=True, slots=True)
class _ComposedChildren:
    """The four composed-subagent operations, bound to one parent Attempt.

    Implements ``psych_runtime.runtime.subagent.ChildOps``. Bound rather than free, for
    the reason ``_AttemptContext`` records: one ``Runtime`` serves every
    concurrent Attempt in a process, so anything holding a Run's journal, its
    Scope or its id has to be per-Attempt or two Runs eventually share one.

    A child is a **background** Run: admitted ``RUNNABLE`` with its parent named,
    claimed by whatever Worker gets to it, and settled by that Worker. Not
    ``NESTED``, which is what an inline ``delegate`` admits: nested means "this
    Attempt is executing it right now and no Worker may claim it", and the whole
    point here is that the parent goes back to work.
    """

    runtime: Runtime
    journal: Journal
    spec: AgentSpec
    scope: Scope

    async def spawn(self, call_id: ToolCallId, request: SpawnRequest, depth: int) -> dict[str, Any]:
        """Compose a child inside the envelope, publish it, and start it.

        Published as a Version like anything else, and validated first: a
        composed Spec is still a Spec, and DESIGN.md §4's rule that validation
        happens at publish rather than at run is satisfied by publishing here
        rather than by skipping the check. The child Run then pins that hash, so
        a reclaiming Worker re-reads the Spec the model actually wrote instead of
        re-composing one from a prompt it would write differently the second
        time. That is what keeps crash recovery unchanged.
        """
        composed = compose_child_spec(self.spec, request, [tool.name for tool in self.spec.tools])
        validate_spec(
            composed.spec, ValidationContext(registered_tools=self.runtime.registry.names)
        )
        version = make_version(composed.spec)
        await self.runtime.store.put_version(version)

        dispatched = await dispatch(
            self.runtime.store,
            version,
            self.scope,
            input=child_input(request.task),
            parent_run_id=self.journal.run_id,
            delegation_depth=depth,
        )
        await self.journal.append(
            type="subagent_spawned",
            child_run_id=dispatched.run_id,
            name=request.name,
            call_id=call_id,
            child_version_hash=version.hash,
            purpose=request.purpose,
            task=request.task,
            deliverable=request.deliverable,
            tools=composed.tools,
            model=composed.model,
            delegation_depth=depth,
        )
        return {
            "name": request.name,
            "run_id": dispatched.run_id,
            "state": "running",
            "tools": list(composed.tools),
            "model": composed.model,
            "note": (
                "Started in the background. Keep working; its result will arrive here "
                "as a message when it finishes. `check_subagent` looks at it meanwhile."
            ),
        }

    async def check(self, child: ChildRun) -> dict[str, Any]:
        """A snapshot of one child's own log, without waiting for it.

        A projection rather than new state, which is the point: the child has a
        log, so "what has it done so far" is a fold of that log and never a
        second copy kept beside it.
        """
        records = await self._child_records(child)
        if records is None:
            return {
                "name": child.name,
                "run_id": child.run_id,
                "state": "unknown",
                "note": "This subagent's log could not be read. It may have been deleted.",
            }
        status = status_of(reduce(records, run_id=child.run_id))
        answer = split_answer(records)
        snapshot: dict[str, Any] = {
            "name": child.name,
            "run_id": child.run_id,
            "state": status.lifecycle.value,
            "terminal_state": status.terminal_state.value if status.terminal_state else None,
            "turns": status.turn,
            "tool_calls": status.tool_calls,
            # What it has said so far: its answer once it has one, and
            # otherwise the last thing it said on the way there. Read through
            # the same `split_answer` a console renders a Run with, so "what is
            # it saying" means the same thing to a parent agent and to a person
            # watching (DESIGN.md §12).
            "latest": answer.text or (answer.work[-1].text if answer.work else ""),
        }
        if status.output is not None:
            snapshot["output"] = status.output
        if status.failure_message is not None:
            snapshot["error"] = status.failure_message
        return snapshot

    async def message(self, call_id: ToolCallId, child: ChildRun, message: str) -> dict[str, Any]:
        """Put a message into a running child, for its next turn.

        ``QueueKind.STEER``, so it is drained at the start of the child's next
        turn (``psych_runtime.runtime.agent._turn``) rather than injected into the turn
        already running. A message arriving mid-tool-call waits: cancelling a
        call whose side effect has already happened is strictly worse than an
        instruction landing one turn later, because the effect happened either
        way and now the log cannot say whether it did. Stopping a child is a
        different intention with its own record -- ``psych_runtime.interrupt``.
        """
        entry_id = await send(
            self.runtime.store, child.run_id, message=message, queue=QueueKind.STEER
        )
        await self.journal.append(
            type="subagent_messaged",
            child_run_id=child.run_id,
            name=child.name,
            call_id=call_id,
            entry_id=entry_id,
            message=message,
        )
        return {
            "name": child.name,
            "run_id": child.run_id,
            "delivered": True,
            "note": "It will see this at the start of its next turn.",
        }

    async def reconcile(self) -> None:
        """Record any live child that has in fact already finished.

        The parent's own half of the notification. A child's Worker writes
        ``subagent_finished`` when it settles, and that write can be lost --
        the process dies between settling and notifying. Reading the children's
        own logs before suspending turns a lost notification into a moment's
        delay instead of a parent that waits out its entire expiry for a result
        that has been sitting in the store the whole time.
        """
        for child in self.journal.state.live_children:
            records = await self._child_records(child)
            if records is None:
                continue
            state = reduce(records, run_id=child.run_id)
            if not state.settled or state.terminal_state is None:
                continue
            await self.journal.append(
                type="subagent_finished",
                child_run_id=child.run_id,
                name=child.name,
                state=state.terminal_state,
                output=state.output,
                failure=state.failure,
                usage=state.usage,
                cost=state.cost,
                unpriced_model_calls=state.unpriced_model_calls,
            )

    async def _child_records(self, child: ChildRun) -> list[Record] | None:
        """Read a child's log, refusing one that is not this tenant's.

        The check cannot fail today: the child id comes from this Run's own log
        and was admitted under this Scope. It is here because that is one
        refactor away from being untrue, and DESIGN.md §14 asks every read to be
        filtered by Scope rather than trusted because of where the id came from.
        """
        records = await self.runtime.store.read(child.run_id)
        if not records:
            return None
        if records[0].scope.tenant != self.scope.tenant:
            raise AccessDenied(
                f"run {child.run_id}",
                f"belongs to tenant {records[0].scope.tenant!r}, not {self.scope.tenant!r}",
            )
        return records
