"""The agent loop: call the model, run what it asked for, repeat.

DESIGN.md §5. An Agent is a Step that loops Turns until a stop condition. This is
the loop, and it is deliberately the only one: a workflow step that is an agent
runs through here too, so there is one durability implementation, one record
format, one metering path and one resume path.

## Stop conditions, in the order they are checked

DESIGN.md §5 lists six. The order matters, because more than one can be true at
once and the reason recorded should be the one that actually stopped the Run:

1. The abort record is present. A user pressing stop outranks everything.
2. The deadline fired. The supervisor already decided this Run is over.
3. The failure-streak guard hit its hard stop.
4. A tool suspended the Run.
5. The step or turn budget is spent.
6. The model returned no tool calls and finished.

Only the last is success. The rest are all "stopped for a reason", and the
reason is written into the terminal record so a report can say which.

## Every tool call is recorded before it runs

DESIGN.md §8.4. The start goes in the log, then the call happens, then the result
goes in. That ordering is what makes an orphaned call visibly incomplete rather
than ambiguous, and it is the whole reason force-settlement is safe. Reversing it
to save a write would break crash recovery in a way no test that does not kill a
process would catch.

## A failed tool is data, not an exception

A tool that raises produces a ``ToolCallFinished`` with an error outcome and the
traceback in the result. The model reads it and changes approach. Letting the
exception escape would kill a turn over something the model could have handled,
and DESIGN.md §18 requires the traceback to reach the model as data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import ValidationError

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.errors import AccessDenied, TransientError
from psych_runtime.core.ids import ToolCallId, new_tool_call_id
from psych_runtime.core.messages import Message, SystemMessage, ToolDefinition
from psych_runtime.core.questions import render_questions
from psych_runtime.core.records import (
    ModelTimings,
    QueueKind,
    Record,
    SuspendReason,
    TerminalState,
    ToolFailure,
    ToolOutcome,
)
from psych_runtime.core.reducer import call_digest
from psych_runtime.core.spec import AgentSpec, HttpTool, SpawnEnvelope
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.port import (
    ModelClient,
    ModelRequest,
    ReasoningDelta,
    StreamDone,
    TextDelta,
    ToolCallDelta,
)
from psych_runtime.model.pricing import CostPolicy, PriceResolver, resolve_cost
from psych_runtime.model.prompt import assemble, cache_breakpoints
from psych_runtime.model.transient import is_context_overflow, is_transient, retry_delay_seconds
from psych_runtime.runtime.abort import AbortReason, AbortSignal
from psych_runtime.runtime.compaction import plan_cut, summarisation_request
from psych_runtime.runtime.journal import Journal
from psych_runtime.runtime.subagent import (
    CHECK_TOOL,
    DELEGATE_TOOL,
    MESSAGE_TOOL,
    SPAWN_TOOL,
    ChildOps,
    DelegateFn,
    SpawnRequest,
    check_alive,
    check_definition,
    check_depth,
    check_fanout,
    delegation_definition,
    find_child,
    find_subagent,
    message_definition,
    resolve_child_depth,
    spawn_definition,
)
from psych_runtime.store.blob import BlobStore, blob_key
from psych_runtime.telemetry.port import (
    NOOP_TELEMETRY,
    GuardedTelemetry,
    SpanAttributes,
    Telemetry,
    TelemetrySpan,
)
from psych_runtime.tools import repetition
from psych_runtime.tools.builtins import ASK_QUESTION, parse_questions
from psych_runtime.tools.code import TOOL_NAME as RUN_CODE
from psych_runtime.tools.deferred import DEFERRED_TOOL_NAMES, DeferredDiscovery
from psych_runtime.tools.guidance import (
    describe_argument_errors,
    failure_guidance,
    format_traceback,
)
from psych_runtime.tools.large_results import (
    TOOL_NAME as READ_TOOL_OUTPUT,
)
from psych_runtime.tools.large_results import (
    decide_elision,
    decide_offload,
    force_elision,
    read_tool_output_async,
    read_tool_output_tools,
)
from psych_runtime.tools.policy import AllowAll, Decision, Policy, approval_required
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.resolver import ResolvedTools, ToolResolver

__all__ = ["DEFAULT_OFFLOAD_BYTES", "AgentLoop", "LoopOutcome", "ToolExecutor"]

DEFAULT_OFFLOAD_BYTES: Final = 300_000
"""The offload threshold (``psych_runtime.tools.large_results.decide_offload``) when a
Runtime does not choose its own. Sized against DynamoDB's 400KB item limit
(DESIGN.md §7) with headroom for a ``ToolCallFinished`` record's other fields
(the largest of which, ``preview``, is capped at 2,000 characters): 300KB
leaves roughly 100KB of slack, which is generous against everything else the
record carries. Not a Spec field; see ``psych_runtime.tools.large_results``'s module
docstring for why offload is a Runtime concern rather than a versioned one."""


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    """Why the loop stopped, and with what."""

    state: TerminalState
    reason: str
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None


class _Gate(StrEnum):
    """What the approval gate decided about one call."""

    ALLOWED = "allowed"
    DENIED = "denied"
    SUSPENDED = "suspended"
    """The Run wrote a suspension and stopped. Nothing after this call runs until
    a human decides, so the turn returns rather than starting the next call."""


@dataclass(slots=True)
class _AssembledCall:
    """A tool call reassembled from streamed fragments."""

    index: int
    id: ToolCallId | None = None
    name: str | None = None
    arguments_json: str = ""


@dataclass(frozen=True, slots=True)
class _Compaction:
    """What one attempt at compacting the conversation did.

    Three outcomes rather than two: it compacted, it had nothing to compact, or
    the summarising call failed and the Run stops. A bare bool could not tell
    the last two apart, and the overflow path has to -- nothing left to compact
    there means settling on the provider's own error.
    """

    applied: bool
    failure: ToolFailure | None = None


class ToolExecutor:
    """Runs one tool call, whatever kind it is.

    Kept separate from the loop so the loop is about control flow and this is
    about dispatch. The three kinds (code, http, mcp) converge on one signature,
    which is what lets the loop record them identically.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        http_caller: Any = None,
        mcp_caller: Any = None,
        code_caller: Any = None,
        discovery: DeferredDiscovery | None = None,
        a2a_caller: Any = None,
    ) -> None:
        self._registry = registry
        self._http_caller = http_caller
        self._mcp_caller = mcp_caller
        self._code_caller = code_caller
        self._discovery = discovery
        self._a2a_caller = a2a_caller

    @property
    def registry(self) -> ToolRegistry:
        """The tools this executor dispatches by name.

        Public because the loop needs a tool's registration -- its annotations
        for the approval selectors, its ``safe_to_retry`` for crash recovery --
        and reaching through a private attribute for it made two modules share
        a secret instead of an interface.
        """
        return self._registry

    def with_code_caller(self, code_caller: Any) -> ToolExecutor:
        """A copy of this executor that also runs ``run_code``.

        A copy rather than a mutation because the sandbox's host bindings close
        over the loop that owns this executor, so the caller cannot exist until
        after the loop does; rebuilding keeps this object immutable once handed
        out.
        """
        return ToolExecutor(
            self._registry,
            http_caller=self._http_caller,
            mcp_caller=self._mcp_caller,
            code_caller=code_caller,
            discovery=self._discovery,
            a2a_caller=self._a2a_caller,
        )

    async def call(self, spec: AgentSpec, name: str, arguments: dict[str, Any]) -> Any:
        """Execute ``name``. Raises on failure; the loop records that as data."""
        if name == RUN_CODE and self._code_caller is not None:
            return await self._code_caller(arguments)

        if self._discovery is not None and name in DEFERRED_TOOL_NAMES:
            return await self._discovery.call(spec, name, arguments)

        http = next(
            (t for t in spec.tools if isinstance(t, HttpTool) and t.name == name),
            None,
        )
        if http is not None:
            if self._http_caller is None:
                raise AccessDenied(
                    f"tool {name!r}",
                    "it is an http tool but this Worker has no http caller configured",
                )
            return await self._http_caller(http, arguments)

        if name in self._registry:
            return await self._registry.call(name, arguments)

        # A2A before MCP, because a peer tool's name is prefixed with the
        # Spec's own alias for that peer (`psych_runtime.tools.a2a.tool_name_for`) and
        # therefore cannot collide with a server's tool name by accident. Trying
        # MCP first would send every peer call round every connected server
        # before failing.
        if self._a2a_caller is not None and any(
            name.startswith(f"{peer.name}__") for peer in spec.a2a_peers
        ):
            return await self._a2a_caller(name, arguments)

        if self._mcp_caller is not None:
            return await self._mcp_caller(name, arguments)

        raise AccessDenied(
            f"tool {name!r}",
            "no code, http, mcp or A2A tool of that name is available to this Run",
        )


class AgentLoop:
    """One agent, looping turns against one Run's journal."""

    def __init__(
        self,
        journal: Journal,
        spec: AgentSpec,
        model: ModelClient,
        resolver: ToolResolver,
        executor: ToolExecutor,
        *,
        prices: PriceResolver | None = None,
        cost_policy: CostPolicy = "prefer_provider",
        memories: Sequence[str] = (),
        policy: Policy | None = None,
        approval_selectors: Sequence[str] = (),
        always_approve: Sequence[str] = (),
        never_approve: Sequence[str] = (),
        delegate: DelegateFn | None = None,
        children: ChildOps | None = None,
        telemetry: Telemetry | None = None,
        builtins: Sequence[ToolDefinition] = (),
        blob: BlobStore | None = None,
        blob_offload_bytes: int = DEFAULT_OFFLOAD_BYTES,
        history: Sequence[Message] = (),
        abort: AbortSignal | None = None,
    ) -> None:
        self._journal = journal
        self._spec = spec
        self._model = model
        self._resolver = resolver
        self._executor = executor
        self._prices = prices
        self._cost_policy = cost_policy
        self._memories = tuple(memories)
        # The continued thread's earlier conversation, oldest first:
        # psych_runtime.runtime.thread.load_thread_history's output, resolved once by
        # the caller before the loop starts rather than reloaded every turn,
        # since every ancestor Run it is drawn from is already settled and
        # therefore immutable. Prepended to this Run's own build_conversation
        # on every turn, ahead of anything this Run has said so far, so a
        # crash mid-Run and a fresh claim rebuild the exact same prompt
        # (DESIGN.md §6).
        self._history = tuple(history)
        self._policy = policy if policy is not None else AllowAll()
        self._approval_selectors = tuple(approval_selectors)
        self._always_approve = tuple(always_approve)
        self._never_approve = tuple(never_approve)
        self._delegate = delegate
        # Composing a subagent at run time is a separate capability from
        # routing to one an author wrote, and this is the seam for it. Absent
        # means the three composed-subagent tools are never offered, which is
        # right for a Runtime that has no way to admit a child Run.
        self._children = children
        self._delegations_this_turn = 0
        self._builtins = tuple(builtins)
        self._blob = blob
        self._blob_offload_bytes = blob_offload_bytes
        self._abort = abort
        self._run_code_definition: ToolDefinition | None = None
        # Guarded, always. A consumer's exporter being broken is never the
        # reason a Run failed (DESIGN.md §13.5).
        self._telemetry: Telemetry = GuardedTelemetry(
            telemetry if telemetry is not None else NOOP_TELEMETRY
        )

    def offer_run_code(self, definition: ToolDefinition, caller: Any) -> None:
        """Offer ``run_code`` for this Run, executing through this loop.

        Set after construction rather than in ``__init__`` because the sandbox
        caller's host bindings route back through ``call_as_binding`` on this
        same loop -- the object cannot be built before the loop it calls into
        exists. That circularity is deliberate: routing a program's tool calls
        through the loop is what applies Policy, the approval selectors, the
        guards and the record log to them (DESIGN.md §18).
        """
        self._run_code_definition = definition
        self._executor = self._executor.with_code_caller(caller)

    async def call_as_binding(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute one tool call a sandboxed program made, recorded and gated.

        DESIGN.md §18: "those calls route back through the normal tool path,
        with the same Policy, egress and recording. There is no privileged back
        door for code." Before this, a binding reached ``ToolExecutor`` directly:
        no Policy check, no approval selector, no record in the log, invisible to
        the failure-streak and repetition guards, and invisible to crash recovery.
        A program calling a destructive tool ran it with no approval and left one
        ``run_code`` call in the report.

        An approval cannot suspend mid-program -- there is no turn to resume into
        while a subprocess waits -- so a call the selectors would gate is refused
        here, as data the program can read and explain, naming the tool to call
        directly instead.
        """
        call_id = new_tool_call_id()
        await self._journal.append(
            type="tool_call_started",
            call_id=call_id,
            tool=name,
            arguments=arguments,
            turn=max(self._journal.state.turn, 1),
            interruptible=_interruptible(self._spec, name),
            safe_to_retry=self._safe_to_retry(name),
        )
        gate = await self._gate(call_id, name, arguments, self._binding_definitions(name))
        if gate is _Gate.SUSPENDED:
            # _gate wrote a suspension record. Nothing may resume into a
            # subprocess, so withdraw it and refuse the call instead.
            await self._journal.append(type="resumed", payload={}, approved=False)
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                failure=ToolFailure(
                    kind="approval_required_in_program",
                    message=failure_guidance(
                        "approval_required_in_program",
                        f"Calling {name!r} needs a human approval, which cannot be "
                        "asked for while a program is running.",
                    ),
                ),
            )
            raise AccessDenied(
                f"tool {name!r}",
                "it needs a human approval, which cannot be asked for from inside a "
                "program. Call it directly instead of from run_code.",
            )
        if gate is _Gate.DENIED:
            raise AccessDenied(f"tool {name!r}", "the policy refused this call")
        await self._execute_and_record(call_id, name, arguments)
        settled = self._journal.state.tool_results[-1]
        if settled.outcome is not ToolOutcome.OK:
            # The program reads the failure as an exception it can catch, and
            # the log already holds the whole story.
            raise RuntimeError(settled.failure.message if settled.failure else "the call failed")
        return settled.result

    def _binding_definitions(self, name: str) -> tuple[ToolDefinition, ...]:
        """The definition ``_gate`` needs to classify one binding call.

        The selectors match on annotations, which live on the registered tool,
        so this looks the one tool up rather than re-resolving the whole set:
        a binding call is not a turn boundary and must not change what the
        model was told it could do.
        """
        registered = self._executor.registry.get(name)
        return (registered.definition(),) if registered is not None else ()

    def _safe_to_retry(self, name: str) -> bool:
        """Whether a crash may re-execute this call, from the registration.

        Read from the live registry rather than inferred: a tool registered
        ``safe_to_retry=True`` said so about itself, and defaulting every call
        to False meant an idempotent read was recorded ``unknown`` after a
        crash and its result thrown away.
        """
        registered = self._executor.registry.get(name)
        return registered.safe_to_retry if registered is not None else False

    async def run(self) -> LoopOutcome:
        """Loop until a stop condition, then return why.

        Does not write the terminal record. The caller does, because a workflow
        step that is an agent finishes without the Run finishing, and a loop that
        settled its own Run could not be nested.
        """
        await self._settle_dangling_calls()

        while True:
            stop = self._check_stop_conditions()
            if stop is not None:
                return stop

            outcome = await self._turn()
            if outcome is not None:
                return outcome

    # -- stop conditions ----------------------------------------------------

    def _check_stop_conditions(self) -> LoopOutcome | None:  # noqa: PLR0911 - §5 lists seven
        state = self._journal.state
        limits = self._spec.limits

        stopped = self._check_worker_stop()
        if stopped is not None:
            return stopped

        if state.aborted:
            return LoopOutcome(
                TerminalState.ABORTED,
                f"an abort was requested at seq {state.abort_seq}",
            )

        if state.suspended:
            return LoopOutcome(
                TerminalState.COMPLETED,
                f"the run suspended waiting for {state.suspend_reason}",
            )

        for tool, streak in state.failure_streaks.items():
            if streak >= limits.failure_streak_hard_stop:
                return LoopOutcome(
                    TerminalState.FAILED,
                    f"{tool!r} failed {streak} times in a row",
                    failure=ToolFailure(
                        kind="failure_streak",
                        message=failure_guidance(
                            "failure_streak",
                            f"The tool {tool!r} failed {streak} consecutive times.",
                        ),
                    ),
                )

        if state.turn >= limits.max_turns:
            return LoopOutcome(
                TerminalState.FAILED,
                f"the turn budget of {limits.max_turns} is spent",
                failure=ToolFailure(
                    kind="budget_exhausted",
                    message=failure_guidance(
                        "budget_exhausted",
                        f"This run used all {limits.max_turns} of its turns.",
                    ),
                ),
            )

        if len(state.tool_results) >= limits.max_steps:
            return LoopOutcome(
                TerminalState.FAILED,
                f"the step budget of {limits.max_steps} is spent",
                failure=ToolFailure(
                    kind="budget_exhausted",
                    message=failure_guidance(
                        "budget_exhausted",
                        f"This run used all {limits.max_steps} of its steps.",
                    ),
                ),
            )

        return None

    def _check_worker_stop(self) -> LoopOutcome | None:
        """The Worker's own signal, checked before the log's own stop reasons.

        Split out so ``_check_stop_conditions`` keeps one return per DESIGN.md
        §5 stop condition rather than growing two more.
        """
        if self._abort is None or not self._abort.is_set() or self._journal.state.aborted:
            return None
        if self._abort.reason is AbortReason.DEADLINE:
            return LoopOutcome(
                TerminalState.ABORTED,
                "the run passed its deadline",
                failure=ToolFailure(
                    kind="deadline",
                    message=failure_guidance(
                        "deadline",
                        "The run passed its deadline and stopped at a turn boundary.",
                    ),
                ),
            )
        # Shutdown or a lost lease: stop without settling, so the next Worker
        # continues this Run from where the log ends.
        return LoopOutcome(TerminalState.ABORTED, "this worker stopped holding the run")

    # -- crash recovery -----------------------------------------------------

    async def _settle_dangling_calls(self) -> None:
        """Close tool calls a dead Attempt left open, before doing anything else.

        DESIGN.md §9: a model must never receive an assistant message with tool
        calls whose results are missing. Providers reject it and the conversation
        is unrecoverable, so this runs before the first turn of every Attempt.

        A call recorded ``safe_to_retry`` is re-executed. Everything else is
        recorded as ``unknown``, which is the only truthful outcome: this Worker
        cannot know whether the side effect happened. Guessing "it failed" would
        let the model retry a refund that already went out.
        """
        dangling = list(self._journal.state.open_tool_calls.values())
        decisions = self._journal.state.approval_decisions
        answers = self._journal.state.resume_payloads

        for call in dangling:
            if call.tool == ASK_QUESTION:
                # A question the person answered. Settled here rather than in
                # the turn loop for the same reason an approval is: the Worker
                # that resumes need not be the one that asked, and by the time
                # anyone reclaims this Run the model is not going to be asked
                # to make the call again -- it is already in the log.
                await self._settle_question(call.call_id, answers.get(call.call_id))
                continue

            if call.call_id in decisions:
                # This call suspended for approval and the decision is already in
                # the log. Acting on it here rather than in the turn loop means
                # the Worker that resumes need not be the one that asked.
                if decisions[call.call_id]:
                    await self._execute_and_record(call.call_id, call.tool, call.arguments)
                else:
                    await self._journal.append(
                        type="tool_call_finished",
                        call_id=call.call_id,
                        outcome=ToolOutcome.ERROR,
                        failure=ToolFailure(
                            kind="denied",
                            message=failure_guidance("denied", "A human declined this call."),
                        ),
                    )
                continue

            if call.safe_to_retry:
                await self._execute_and_record(call.call_id, call.tool, call.arguments)
                continue
            await self._journal.append(
                type="tool_call_finished",
                call_id=call.call_id,
                outcome=ToolOutcome.UNKNOWN,
                failure=ToolFailure(
                    kind="orphaned",
                    message=failure_guidance(
                        "orphaned",
                        "The worker running this call stopped before recording a "
                        "result. Whether it took effect is unknown.",
                    ),
                ),
            )

    # -- one turn -----------------------------------------------------------

    async def _turn(self) -> LoopOutcome | None:
        """Run one turn. Returns an outcome only when the loop should stop."""
        # An interrupt, a steer or a resume appended by the consumer while this
        # Attempt held the lease lands here, at the turn boundary, rather than
        # colliding with this loop's next append. Without it, psych_runtime.interrupt()
        # on a Run a Worker was executing failed the Run instead of aborting it.
        await self._refresh()

        # Before the turn is opened, not inside it: a compaction is not a turn,
        # and the summarising call would otherwise land between this turn's
        # `turn_started` and its own model call, where the reducer rightly
        # refuses a second model call in one turn.
        compaction = await self._compact_if_over_threshold()
        if compaction.failure is not None:
            return LoopOutcome(
                TerminalState.FAILED,
                "the conversation could not be compacted",
                failure=compaction.failure,
            )

        state = self._journal.state
        turn_number = state.turn + 1

        self._delegations_this_turn = 0
        extra: list[ToolDefinition] = [*self._builtins, *read_tool_output_tools(state)]
        if self._run_code_definition is not None:
            extra.append(self._run_code_definition)
        if self._delegate is not None:
            granted = [tool.name for tool in self._spec.tools]
            delegation = delegation_definition(self._spec.subagents, granted)
            if delegation is not None:
                extra.append(delegation)
        if self._children is not None:
            granted = [tool.name for tool in self._spec.tools]
            # `message_subagent` is absent when the envelope forbids messaging,
            # and `check_subagent` is offered from the first turn rather than
            # only once a child exists: a tool that appears halfway through a
            # conversation changes the prompt underneath the model's own cache
            # breakpoints, and one that answers "you have no subagents" is a
            # cheaper way to say the same thing.
            extra.extend(
                definition
                for definition in (
                    spawn_definition(self._spec, granted),
                    check_definition(self._spec),
                    message_definition(self._spec),
                )
                if definition is not None
            )

        try:
            resolved = await self._resolver.resolve(
                self._spec,
                self._journal.scope,
                failure_streaks=state.failure_streaks,
                extra=extra,
            )
        except AccessDenied as err:
            # A required MCP server did not answer, or a credential it needs is
            # missing (DESIGN.md §10.7). This settles the Run through the
            # ordinary outcome path rather than escaping to the Worker, so the
            # terminal record names the server and the reason. Letting it
            # escape produced a generic "the attempt raised" settlement whose
            # only diagnostic was a line in the Worker's own log, which a
            # platform reading the record log never sees.
            return LoopOutcome(
                TerminalState.FAILED,
                f"the tool set could not be resolved: {err}",
                failure=ToolFailure(
                    kind="tool_resolution",
                    message=failure_guidance(
                        "tool_resolution",
                        f"The tools for this turn could not be resolved. {err}",
                    ),
                    traceback=format_traceback(err),
                ),
            )

        await self._journal.append(type="turn_started", turn=turn_number)
        turn_attributes: SpanAttributes = {"psych.turn.number": turn_number}

        # A steer that arrived before this turn started is delivered now, inside
        # the turn it was meant to influence. Draining it after the model call
        # would make "steer" mean "follow up", which is a different queue.
        await self._drain(QueueKind.STEER)

        conversation = [*self._history, *build_conversation(await self._read_log())]
        messages = assemble(
            self._spec,
            conversation,
            memories=self._memories,
            advisories=resolved.advisories,
        )

        request = ModelRequest(
            model=self._spec.model.model,
            messages=messages,
            tools=resolved.definitions,
            temperature=self._spec.model.temperature,
            top_p=self._spec.model.top_p,
            max_output_tokens=self._spec.model.max_output_tokens,
            reasoning_effort=self._spec.model.reasoning_effort,
            cache_breakpoints=cache_breakpoints(messages, resolved.definitions),
            idle_timeout_seconds=self._spec.limits.stream_idle_seconds,
        )

        await self._journal.append(
            type="model_call_started",
            turn=turn_number,
            model=request.model,
            tool_names=tuple(d.name for d in resolved.definitions),
            # The assembled prompt, not a description of how to assemble one.
            # `messages[0]` is the system message when there is one; `assemble`
            # returns the conversation alone when the Spec has no instructions,
            # no skills, no memories and no advisories to say.
            system_prompt=(
                messages[0].content if messages and isinstance(messages[0], SystemMessage) else ""
            ),
        )

        async with self._telemetry.start_span("psych.turn", attributes=turn_attributes) as turn:
            async with turn.start_span(
                "psych.model_call",
                attributes={
                    "gen_ai.system": "psych",
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": request.model,
                    "psych.turn.number": turn_number,
                },
            ) as call_span:
                try:
                    text, calls, usage, reported_cost, finish_reason, timings = await self._stream(
                        request
                    )
                except Exception as err:
                    call_span.record_exception(err)
                    return await self._record_model_failure(turn_number, request.model, err)

                call_span.set_attributes(
                    {
                        "gen_ai.response.model": request.model,
                        "gen_ai.response.finish_reasons": (finish_reason,),
                        "gen_ai.usage.input_tokens": usage.input,
                        "gen_ai.usage.output_tokens": usage.output,
                        "psych.usage.cache_read_tokens": usage.cache_read,
                        "psych.usage.cache_write_tokens": usage.cache_write,
                        "psych.model_call.time_to_first_token_seconds": (
                            timings.time_to_first_token_seconds
                        ),
                    }
                )

            return await self._finish_turn(
                turn_number,
                request,
                text=text,
                calls=calls,
                usage=usage,
                reported_cost=reported_cost,
                finish_reason=finish_reason,
                timings=timings,
                resolved=resolved,
                turn=turn,
            )

    async def _finish_turn(
        self,
        turn_number: int,
        request: ModelRequest,
        *,
        text: str,
        calls: list[_AssembledCall],
        usage: Usage,
        reported_cost: Cost | None,
        finish_reason: str,
        timings: ModelTimings,
        resolved: ResolvedTools,
        turn: TelemetrySpan,
    ) -> LoopOutcome | None:
        """Record the model's answer and run whatever it asked for.

        Split out of ``_turn`` only because the telemetry spans made one function
        long enough to be hard to read. The control flow is unchanged.
        """

        # Resolved once, here, and written into the Record. Never recomputed
        # on read: a report is a projection over an immutable log, so changing
        # the policy or the price table later cannot retroactively change what
        # a past Run cost.
        cost = resolve_cost(request.model, usage, self._prices, reported_cost, self._cost_policy)
        call_ids = tuple(call.id for call in calls if call.id is not None)

        await self._journal.append(
            type="model_call_finished",
            turn=turn_number,
            model=request.model,
            usage=usage,
            cost=cost,
            timings=timings,
            finish_reason=finish_reason,
            text=text,
            tool_calls=call_ids,
        )

        if not calls:
            # The one success condition: the model finished with nothing left
            # to do -- unless a follow-up arrived meanwhile, which is what
            # "deliver after the current turn settles" means (DESIGN.md §9).
            # Checked *before* draining: _drain marks every pending entry
            # consumed as a side effect, which empties pending_follow_up on
            # its own, so reading it after draining would always find nothing
            # regardless of what was actually pending a moment ago.
            if self._journal.state.pending_follow_up:
                await self._drain(QueueKind.FOLLOW_UP)
                return None
            if self._children is not None and self._journal.state.live_children:
                # The model has nothing left to do and its background children
                # do. Completing here would settle the Run with the answers it
                # asked for still in flight, and the notifications those children
                # write would land on a settled log; waiting is the whole point
                # of having spawned them.
                waited = await self._wait_for_children(self._children)
                if waited is not None:
                    return waited
                return None
            return LoopOutcome(
                TerminalState.COMPLETED,
                "the model finished with no tool calls",
                output={"text": text},
            )

        await self._run_tool_calls(calls, resolved.definitions, turn)
        return None

    async def _stream(
        self, request: ModelRequest
    ) -> tuple[str, list[_AssembledCall], Usage, Cost | None, str, ModelTimings]:
        """Consume the model stream, reassembling text and tool calls.

        Time to first token is measured here rather than estimated, because §13.3
        wants a latency breakdown that accounts for the Run's wall clock and an
        estimate that looks like a measurement is worse than no number.
        """
        started = time.monotonic()
        first_token_at: float | None = None
        text_parts: list[str] = []
        calls: dict[int, _AssembledCall] = {}
        usage = Usage()
        reported_cost: Cost | None = None
        finish_reason = ""
        done = False

        async for event in self._model.stream(request):
            if first_token_at is None:
                first_token_at = time.monotonic()

            match event:
                case TextDelta():
                    text_parts.append(event.text)
                case ReasoningDelta():
                    # Recorded in usage by the provider; not replayed into the
                    # conversation because most providers refuse it back.
                    pass
                case ToolCallDelta():
                    call = calls.setdefault(event.index, _AssembledCall(index=event.index))
                    if event.id is not None:
                        call.id = event.id
                    if event.name is not None:
                        call.name = event.name
                    call.arguments_json += event.arguments_fragment
                case StreamDone():
                    usage = event.usage
                    reported_cost = event.cost
                    finish_reason = event.finish_reason
                    done = True

        if not done:
            # A stream that ends without StreamDone aborted mid-token. Treating
            # it as a short answer would silently truncate a reply to a customer.
            raise TransientError(
                "the model stream ended without a completion event, which means it "
                "aborted mid-token rather than finishing"
            )

        timings = ModelTimings(
            time_to_first_token_seconds=(
                first_token_at - started if first_token_at is not None else None
            ),
            stream_duration_seconds=time.monotonic() - started,
        )
        ordered = [calls[index] for index in sorted(calls)]
        return (
            "".join(text_parts),
            ordered,
            usage,
            reported_cost,
            finish_reason or "stop",
            timings,
        )

    async def _record_model_failure(
        self, turn: int, model: str, err: Exception
    ) -> LoopOutcome | None:
        """Record a failed model call, and decide whether to try again.

        The transient budget is per Run rather than per call (DESIGN.md §8.6), so
        the decision reads the count out of the folded state rather than a local
        counter that would reset on every reclaim.
        """
        transient = is_transient(err)
        budget = self._spec.limits.transient_retry_budget
        used = self._journal.state.transient_retries_used
        # The prompt was refused for being too long, and there is a cut that
        # would make it shorter. Decided before the record is written so
        # `will_retry` describes what actually happens next: a reader who sees
        # "still trying" is owed another attempt.
        records = await self._read_log()
        overflow_cut = None if transient else self._overflow_cut(err, records)
        will_retry = (transient and used < budget) or overflow_cut is not None

        await self._journal.append(
            type="model_call_failed",
            turn=turn,
            model=model,
            failure=ToolFailure(
                kind=type(err).__name__,
                message=failure_guidance(type(err).__name__, str(err), transient=transient),
                traceback=format_traceback(err),
                transient=transient,
            ),
            will_retry=will_retry,
        )

        if overflow_cut is not None:
            # An overflow retry counts against the transient budget, through the
            # `will_retry` the reducer already folds. It is a second attempt at a
            # call that failed, which is what that budget bounds, and a second
            # budget nobody configured would be a worse answer than sharing the
            # one that exists. It is bounded twice over anyway: every compaction
            # advances the boundary, and once there is nothing left to compact
            # this path stops offering itself and the Run settles on the
            # provider's own error.
            compaction = await self._compact("overflow", records, overflow_cut)
            if compaction.failure is not None:
                return LoopOutcome(
                    TerminalState.FAILED,
                    "the conversation could not be compacted after the model refused it "
                    "for being too long",
                    failure=compaction.failure,
                )
            return None

        if will_retry:
            # Wait before trying again, honouring the provider's own Retry-After
            # when it sent one. Retrying a 429 immediately is how a rate limit
            # becomes a rate-limit storm, and the classifier already parsed the
            # header the provider used to say how long to wait.
            delay = retry_delay_seconds(used + 1, getattr(err, "retry_after_seconds", None))
            await self._sleep_unless_aborted(delay)
            return None

        reason = (
            f"the model call failed and the transient budget of {budget} is spent"
            if transient
            else "the model call failed with an error that will not succeed on retry"
        )
        return LoopOutcome(
            TerminalState.FAILED,
            reason,
            failure=ToolFailure(
                kind=type(err).__name__,
                message=failure_guidance(type(err).__name__, str(err), transient=transient),
                transient=transient,
            ),
        )

    # -- compaction ---------------------------------------------------------

    def _overflow_cut(self, err: Exception, records: Sequence[Record]) -> int | None:
        """The cut to compact to because the provider refused the prompt, if any.

        ``None`` for any error that is not a context overflow, for an agent
        whose Spec did not ask for compaction, and for a conversation with
        nothing left to compact -- the last of which is what stops an agent
        whose recent turns alone exceed the window from summarising forever.
        Those Runs settle on the model's own error, which is the honest answer:
        the request cannot be made to fit.
        """
        policy = self._spec.compaction
        if policy is None or not is_context_overflow(err):
            return None
        return plan_cut(
            records,
            self._journal.state.compaction_boundary_seq,
            policy.keep_recent_turns,
        )

    async def _compact_if_over_threshold(self) -> _Compaction:
        """Compact when the last prompt reached the size the Spec set.

        The measure is ``RunStateView.last_prompt_tokens``: what the provider
        counted for the last call, not what a tokenizer guessed about the next
        one. See ``CompactionPolicy.trigger_tokens`` for why a measured number
        one turn late beats an estimated one on time.
        """
        policy = self._spec.compaction
        if policy is None or self._journal.state.last_prompt_tokens < policy.trigger_tokens:
            return _Compaction(applied=False)
        records = await self._read_log()
        cut = plan_cut(
            records, self._journal.state.compaction_boundary_seq, policy.keep_recent_turns
        )
        if cut is None:
            return _Compaction(applied=False)
        return await self._compact("threshold", records, cut)

    async def _compact(
        self,
        reason: Literal["threshold", "overflow"],
        records: Sequence[Record],
        cut: int,
    ) -> _Compaction:
        """Summarise everything up to ``cut``, then record the boundary.

        The summary comes back before the record goes in, and the record goes in
        before the next model call. ``psych_runtime.runtime.compaction`` sets out what
        each half of that ordering buys a Worker that dies in the middle.

        A summarising call that fails ends the Run rather than carrying on
        quietly. By the time this runs the conversation has either already been
        refused by the provider or reached the size its author said it must not
        exceed, so continuing uncompacted means sending a prompt that is about
        to be rejected, and swallowing the error would leave a Run that silently
        stopped compacting and failed later for a reason with no visible
        connection to the cause.
        """
        policy = self._spec.compaction
        if policy is None:  # pragma: no cover - every caller checks first
            return _Compaction(applied=False)

        request = summarisation_request(self._spec, policy, records, cut, history=self._history)
        async with self._telemetry.start_span(
            "psych.compaction",
            attributes={
                "gen_ai.system": "psych",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": request.model,
                "psych.compaction.reason": reason,
                "psych.compaction.replaced_to_seq": cut,
            },
        ) as span:
            try:
                text, _calls, usage, reported_cost, _finish, timings = await self._stream(request)
            except Exception as err:
                span.record_exception(err)
                return _Compaction(
                    applied=False,
                    failure=ToolFailure(
                        kind="compaction_failed",
                        message=failure_guidance(
                            "compaction_failed",
                            "The conversation had to be summarised to carry on, and the "
                            f"summarising call to {request.model!r} failed: {err}",
                            transient=is_transient(err),
                        ),
                        traceback=format_traceback(err),
                        transient=is_transient(err),
                    ),
                )
            summary = text.strip()
            if not summary:
                return _Compaction(
                    applied=False,
                    failure=ToolFailure(
                        kind="compaction_failed",
                        message=failure_guidance(
                            "compaction_failed",
                            f"The summarising call to {request.model!r} returned no text, "
                            "so there is nothing to stand in for the conversation it was "
                            "asked to replace.",
                        ),
                    ),
                )
            span.set_attributes(
                {
                    "gen_ai.usage.input_tokens": usage.input,
                    "gen_ai.usage.output_tokens": usage.output,
                }
            )

        await self._journal.append(
            type="compaction_applied",
            reason=reason,
            replaced_from_seq=self._journal.state.compaction_boundary_seq + 1,
            replaced_to_seq=cut,
            summary=summary,
            model=request.model,
            usage=usage,
            timings=timings,
            # Priced here, at the call, and written into the Record, for the
            # reason every other model call is: a report is a projection over an
            # immutable log, so a later price table must not change what a past
            # Run cost.
            cost=resolve_cost(request.model, usage, self._prices, reported_cost, self._cost_policy),
        )
        return _Compaction(applied=True)

    # -- tool calls ---------------------------------------------------------

    async def _run_tool_calls(
        self,
        calls: Sequence[_AssembledCall],
        offered: Sequence[ToolDefinition],
        turn: TelemetrySpan | None = None,
    ) -> None:
        offered_names = {definition.name for definition in offered}

        for index, call in enumerate(calls):
            call_id = call.id if call.id is not None else new_tool_call_id()
            name = call.name or ""

            arguments, parse_failure = _parse_arguments(call.arguments_json)

            await self._journal.append(
                type="tool_call_started",
                call_id=call_id,
                tool=name or "<unnamed>",
                arguments=arguments,
                turn=self._journal.state.turn,
                interruptible=_interruptible(self._spec, name),
                safe_to_retry=self._safe_to_retry(name),
            )

            if parse_failure is not None:
                # A malformed tool call is the model's mistake and the model can
                # fix it. Recording it as a failed call hands it back as data;
                # raising would end a turn over a bad JSON fragment.
                await self._journal.append(
                    type="tool_call_finished",
                    call_id=call_id,
                    outcome=ToolOutcome.ERROR,
                    failure=parse_failure,
                )
                continue

            gate = await self._gate(call_id, name, arguments, offered)
            if gate is _Gate.SUSPENDED:
                # Everything the model asked for after this call is answered
                # before returning. A provider rejects an assistant message
                # whose tool calls have no results (DESIGN.md §9), and the
                # remaining calls would otherwise never be started at all: the
                # resume path settles only the one call that suspended.
                await self._answer_unstarted(calls[index + 1 :], "suspended")
                return
            if gate is _Gate.DENIED:
                continue

            if name not in offered_names:
                await self._journal.append(
                    type="tool_call_finished",
                    call_id=call_id,
                    outcome=ToolOutcome.ERROR,
                    failure=ToolFailure(
                        kind="unknown_tool",
                        message=failure_guidance(
                            "unknown_tool",
                            f"There is no tool called {name!r} available to you. "
                            f"Available tools: {', '.join(sorted(offered_names)) or 'none'}.",
                        ),
                    ),
                )
                continue

            # The repetition guard, checked here rather than after
            # the call, because running it again is exactly the cost being
            # avoided. Counted per Run from the log, so a suspend, a resume or
            # a Worker being replaced does not hand the model a fresh budget to
            # loop within.
            call_key = call_digest(name, arguments)
            tally = self._journal.state.repeat_counts.get(call_key)
            limits = self._spec.limits
            verdict = repetition.assess(
                call_key,
                (tally.count if tally is not None else 0) + 1,
                threshold=limits.repeat_call_threshold,
                hard_stop=limits.repeat_call_hard_stop,
            )
            if verdict.refuse:
                await self._journal.append(
                    type="tool_call_finished",
                    call_id=call_id,
                    outcome=ToolOutcome.ERROR,
                    duration_seconds=0.0,
                    failure=ToolFailure(
                        kind="repeated_call",
                        message=failure_guidance(
                            "repeated_call",
                            verdict.advisory or repetition.advisory_message(verdict.repeats),
                        ),
                    ),
                )
                if verdict.fail_turn:
                    await self._answer_unstarted(calls[index + 1 :], "repeated_call")
                    break
                continue

            parent: Telemetry = turn if turn is not None else self._telemetry
            async with parent.start_span(
                "psych.tool_call",
                attributes={
                    "psych.tool.name": name,
                    "psych.tool.call_id": call_id,
                    "psych.tool.interruptible": _interruptible(self._spec, name),
                },
            ):
                await self._execute_and_record(call_id, name, arguments)

            # An interrupt that arrived while that call ran is in the log now,
            # not at the next turn boundary: a person who pressed stop during a
            # three-tool turn expects the second tool not to run.
            await self._refresh()

            if self._journal.state.aborted:
                # An abort landed while tools were running. Stop starting new
                # ones, but answer them: the model's own message already claims
                # them, and a conversation with an unanswered call cannot be
                # replayed to any provider.
                await self._answer_unstarted(calls[index + 1 :], "aborted")
                break

    async def _answer_unstarted(self, remaining: Sequence[_AssembledCall], why: str) -> None:
        """Record a result for every call this turn will not run.

        DESIGN.md §9: a model must never receive an assistant message whose
        tool calls have no results; providers reject it and the conversation is
        unrecoverable. The assistant message already names every id the model
        asked for, so a turn that stops early -- a suspension, an abort, the
        repetition guard -- has to close the rest rather than leave them
        unstarted.
        """
        for call in remaining:
            call_id = call.id if call.id is not None else new_tool_call_id()
            name = call.name or "<unnamed>"
            arguments, _ = _parse_arguments(call.arguments_json)
            await self._journal.append(
                type="tool_call_started",
                call_id=call_id,
                tool=name,
                arguments=arguments,
                turn=self._journal.state.turn,
                interruptible=_interruptible(self._spec, name),
                safe_to_retry=self._safe_to_retry(name),
            )
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                failure=ToolFailure(
                    kind="not_executed",
                    message=failure_guidance(
                        "not_executed",
                        f"This call was not run because the turn stopped first ({why}).",
                    ),
                ),
            )

    async def _settle_question(self, call_id: ToolCallId, answered: dict[str, Any] | None) -> None:
        """Record a person's answer as the `ask_question` call's result.

        One place, called from both the reclaim path and the gate, so the two
        cannot word the same outcome differently.

        An empty or missing answer is said in words rather than returned as an
        empty string. A model handed `""` reads it as "they said nothing",
        which is a different thing from "nobody answered" and is the reading
        that leads it to invent one.

        Two shapes are accepted from `psych_runtime.resume(payload=...)`, because a
        consumer with one question should not have to build a dict to answer
        it: `{"answer": "..."}` for the simple case, and `{"answers": {header
        or question: "..."}}` when several were asked at once. What reaches the
        model is the question paired with its answer either way, so it never
        has to remember which of its own questions came back.
        """
        payload = answered or {}
        answers = payload.get("answers")
        if isinstance(answers, dict) and answers:
            lines = [
                f"{key}: {value}"
                for key, value in answers.items()
                if isinstance(value, str) and value.strip()
            ]
            if lines:
                await self._journal.append(
                    type="tool_call_finished",
                    call_id=call_id,
                    outcome=ToolOutcome.OK,
                    result="\n".join(lines),
                )
                return

        answer = payload.get("answer")
        await self._journal.append(
            type="tool_call_finished",
            call_id=call_id,
            outcome=ToolOutcome.OK,
            result=(
                answer
                if isinstance(answer, str) and answer.strip()
                else "The person did not give an answer. Continue without it, or say what you "
                "need and stop."
            ),
        )

    async def _gate_question(self, call_id: ToolCallId, arguments: dict[str, Any]) -> _Gate:
        """Stop and ask, or deliver the answer that already arrived.

        `ask_question` is the one tool that is never dispatched. Its body would
        have nothing to return: the value is a person's, and it arrives through
        `psych_runtime.resume(payload=...)` on whatever Worker claims the Run next,
        which is usually not the one that asked. So the call suspends here and
        is settled here, and `psych_runtime.tools.builtins._unreachable_ask_question`
        raises if this path is ever missed.

        Same shape as the approval gate above and for the same reason: an
        answer already in the log wins, so a reclaimed Run does not ask twice.
        A Run that asked, was answered, and then crashed before recording the
        result replays to exactly this branch.
        """
        answered = self._journal.state.resume_payloads.get(call_id)
        if answered is not None:
            await self._settle_question(call_id, answered)
            return _Gate.DENIED

        questions = parse_questions(arguments)
        if not questions:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                failure=ToolFailure(
                    kind="invalid_arguments",
                    message=failure_guidance(
                        "invalid_arguments",
                        "ask_question needs at least one `questions` entry with a `question` "
                        "in it. Nothing was asked, so nobody was interrupted.",
                    ),
                ),
            )
            return _Gate.DENIED

        await self._journal.append(
            type="suspended",
            reason=SuspendReason.QUESTION,
            pending_call_id=call_id,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._spec.suspension.question_expires_seconds),
            # Both shapes. `question` has always been a plain string and stays
            # the one-line rendering, so a log reader or a notification that
            # never learns about the structure keeps working.
            question=render_questions(questions),
            questions=questions,
        )
        return _Gate.SUSPENDED

    async def _gate(
        self,
        call_id: ToolCallId,
        name: str,
        arguments: dict[str, Any],
        offered: Sequence[ToolDefinition],
    ) -> _Gate:
        """Ask the consumer's Policy, and suspend when a human must decide.

        A decision already recorded for this call wins: a resumed Run must not
        ask the same question twice, and asking again after a human said yes is
        how an approval flow becomes an approval loop.
        """
        if name == ASK_QUESTION:
            return await self._gate_question(call_id, arguments)

        recorded = self._journal.state.approval_decisions.get(call_id)
        if recorded is True:
            return _Gate.ALLOWED
        if recorded is False:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                failure=ToolFailure(
                    kind="denied",
                    message=failure_guidance("denied", "A human declined this call."),
                ),
            )
            return _Gate.DENIED

        try:
            decision = await self._policy.allow_tool(self._journal.scope, name, arguments)
        except Exception as err:
            # An authorization system being down stops work rather than letting
            # it through, and must not take the Run's log with it.
            decision = Decision.deny(f"the policy check failed: {err}")

        definition = next((d for d in offered if d.name == name), None)
        needs_human = decision.requires_approval or (
            decision.allowed
            and definition is not None
            and self._approval_selectors
            and approval_required(
                definition,
                selectors=self._approval_selectors,
                always=self._always_approve,
                never=self._never_approve,
            )
        )

        if needs_human:
            await self._journal.append(
                type="suspended",
                reason=SuspendReason.APPROVAL,
                pending_call_id=call_id,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=self._spec.suspension.approval_expires_seconds),
                question=(f"Approve calling {name!r} with {json.dumps(arguments, default=str)}?"),
            )
            return _Gate.SUSPENDED

        if not decision.allowed:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                failure=ToolFailure(
                    kind="access_denied",
                    message=failure_guidance(
                        "access_denied",
                        f"You are not allowed to call {name!r}"
                        + (f": {decision.reason}" if decision.reason else "."),
                    ),
                ),
            )
            return _Gate.DENIED

        return _Gate.ALLOWED

    async def _execute_and_record(
        self, call_id: ToolCallId, name: str, arguments: dict[str, Any]
    ) -> None:
        started = time.monotonic()
        try:
            if name == DELEGATE_TOOL:
                result: Any = await self._delegate_to_subagent(arguments)
            elif name in (SPAWN_TOOL, CHECK_TOOL, MESSAGE_TOOL):
                result = await self._composed_subagent(call_id, name, arguments)
            elif name == READ_TOOL_OUTPUT:
                # Reads the stored result out of the folded log (or, for an
                # offloaded one, out of the BlobStore) rather than calling
                # anything: the whole result is already reachable from this
                # Run's own records, and a handle can never resolve outside it.
                result = await read_tool_output_async(self._journal.state, arguments, self._blob)
            else:
                result = await self._executor.call(self._spec, name, arguments)
        except asyncio.CancelledError:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ABORTED,
                duration_seconds=time.monotonic() - started,
            )
            raise
        except ValidationError as err:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                duration_seconds=time.monotonic() - started,
                failure=ToolFailure(
                    kind="invalid_arguments",
                    message=failure_guidance(
                        "invalid_arguments",
                        f"The arguments you sent to {name!r} do not match its schema. "
                        f"{err.error_count()} problem(s): "
                        f"{describe_argument_errors(err.errors())}",
                    ),
                ),
            )
        except Exception as err:
            transient = is_transient(err)
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                duration_seconds=time.monotonic() - started,
                failure=ToolFailure(
                    kind=type(err).__name__,
                    message=failure_guidance(type(err).__name__, str(err), transient=transient),
                    # Recorded for an operator, not replayed to the model: this
                    # is the consumer's own code and its traceback carries their
                    # paths and whatever the exception embedded. `run_code` is
                    # the one path that opts in, because there the traceback is
                    # the model's own program (DESIGN.md §18).
                    traceback=format_traceback(err),
                    traceback_is_for_the_model=(name == RUN_CODE),
                    transient=transient,
                ),
            )
        else:
            await self._record_success(call_id, result, time.monotonic() - started)

    async def _delegate_to_subagent(self, arguments: dict[str, Any]) -> Any:
        """Run a child agent and return what it produced.

        The fan-out check happens here rather than at resolution time because the
        cap is per turn and resolution happens once per turn: a model asking for
        five children in one answer is exactly the cost incident DESIGN.md §17
        names, and it only becomes visible as the calls are executed.
        """
        if self._delegate is None:
            raise AccessDenied("delegation", "this Worker has no delegation configured")

        check_fanout(self._delegations_this_turn, self._spec.limits.max_fanout_per_turn)
        self._delegations_this_turn += 1

        name = str(arguments.get("subagent", ""))
        task = str(arguments.get("task", ""))
        ref = find_subagent(self._spec, name)

        child_depth = resolve_child_depth(self._journal.state.delegation_depth)
        check_depth(child_depth, self._spec.limits.max_delegation_depth)

        return await self._delegate(ref, task, child_depth)

    async def _composed_subagent(
        self, call_id: ToolCallId, name: str, arguments: dict[str, Any]
    ) -> Any:
        """Run one of the three composed-subagent tools (DESIGN.md §17).

        One entry point for the three because they share every check that
        matters -- the envelope exists, this loop was given a way to admit a
        Run -- and splitting them meant writing that pair of guards three times
        and eventually writing it twice.
        """
        if self._children is None or self._spec.spawn is None:
            raise AccessDenied(
                "composing a subagent",
                "this agent may not compose subagents at run time.",
            )
        children, envelope = self._children, self._spec.spawn
        if name == SPAWN_TOOL:
            return await self._spawn_subagent(call_id, arguments, children, envelope)

        child = find_child(self._journal.state, str(arguments.get("name", "")))
        if name == CHECK_TOOL:
            return await children.check(child)
        if not child.alive:
            raise AccessDenied(
                f"messaging subagent {child.name!r}",
                f"it has already finished ({child.terminal_state}), so there is no turn "
                "left for a message to reach. Start another one if there is more to do.",
            )
        return await children.message(call_id, child, str(arguments.get("message", "")))

    async def _spawn_subagent(
        self,
        call_id: ToolCallId,
        arguments: dict[str, Any],
        children: ChildOps,
        envelope: SpawnEnvelope,
    ) -> Any:
        """Compose a child, inside the envelope, and start it in the background.

        The three caps are checked here rather than inside the composer, because
        all three are facts about this Run at this moment -- how deep it already
        is, how many children it already has running -- and the composer is a
        pure function of a Spec and a request.

        Arguments are validated by ``SpawnRequest`` and the ``ValidationError``
        deliberately escapes: ``_execute_and_record`` already turns one into a
        tool result naming the field and the rule it broke, which is exactly
        what a model needs to write a longer brief and try again.
        """
        state = self._journal.state

        request = SpawnRequest.model_validate(arguments)
        check_alive(len(state.live_children), envelope.max_alive)
        child_depth = resolve_child_depth(state.delegation_depth)
        # The envelope's own depth and the Spec's delegation cap both apply, and
        # the tighter one wins: they bound the same tree from two directions,
        # and taking the maximum would let either one quietly widen the other.
        check_depth(child_depth, min(envelope.max_depth, self._spec.limits.max_delegation_depth))
        return await children.spawn(call_id, request, child_depth)

    async def _wait_for_children(self, children: ChildOps) -> LoopOutcome | None:
        """Suspend rather than spin, when the only work left belongs to a child.

        DESIGN.md §11: a Run waiting on something slow persists, releases its
        lease and waits. A parent that instead held its lease and polled would
        pin one Worker per waiting parent, and a fan-out of four would pin four
        Workers doing nothing while their own children queued behind them.

        Returns ``None`` when there is nothing to wait for after all, so the
        caller takes another turn and the model reads what arrived.

        Two guards, because a child can finish at any point in here and the
        second one is the one that is easy to miss:

        1. Reconcile before suspending, so a child that finished without its
           notification landing is recorded now rather than waited out.
        2. Fold the log again *after* writing the suspension. A notification
           that lands between those two writes would otherwise leave a parent
           suspended with nothing left to wake it, since the child's Worker had
           already looked and seen a Run that was not suspended yet. Finding one
           there, this resumes itself and carries on.
        """
        await children.reconcile()
        live = self._journal.state.live_children
        if not live:
            return None

        names = ", ".join(child.name for child in live)
        await self._journal.append(
            type="suspended",
            reason=SuspendReason.CHILDREN,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self._spec.suspension.children_expires_seconds),
            question=f"Waiting on {len(live)} subagent(s): {names}.",
        )

        await self._refresh()
        if not self._journal.state.live_children:
            await self._journal.append(
                type="resumed", payload={"woken_by": "subagent", "while": "suspending"}
            )
            return None
        return LoopOutcome(
            TerminalState.COMPLETED,
            f"waiting on {len(live)} subagent(s): {names}",
        )

    async def _record_success(self, call_id: ToolCallId, result: Any, duration: float) -> None:
        """Record a successful call, eliding the model's view when it is large
        and offloading the payload itself when it is too large to log inline.

        Every byte a tool returned stays durably retrievable either way
        (DESIGN.md §10.8 and §7; see ``psych_runtime.tools.large_results``'s module
        docstring for why both are true even above DynamoDB's item limit).
        Below the offload threshold the whole result sits in the record, the
        same as before this ticket. Above it, the record carries a reference,
        a size and a content type instead, and the payload goes to
        ``self._blob`` keyed by this Run's own scope and run id.

        The threshold comparisons, the preview length and the handle format
        all live in ``psych_runtime.tools.large_results`` rather than here, so this
        method calls into it and writes back exactly what it returns instead
        of restating any of those numbers.
        """
        elision = decide_elision(result, call_id, threshold=self._spec.limits.large_result_bytes)
        offload = decide_offload(result, threshold=self._blob_offload_bytes)

        if not offload.offload:
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.OK,
                result=result,
                duration_seconds=duration,
                result_bytes=elision.result_bytes,
                result_handle=elision.handle,
                preview=elision.preview,
            )
            return

        if self._blob is None:
            # Nothing here can hold a payload this large without either
            # crashing the store write this ticket exists to prevent, or
            # silently discarding what the tool returned. Recording an
            # explicit failure keeps the Run alive and tells the model (and a
            # report) exactly why this call has no result, rather than
            # reproducing the bug with extra steps.
            await self._journal.append(
                type="tool_call_finished",
                call_id=call_id,
                outcome=ToolOutcome.ERROR,
                duration_seconds=duration,
                result_bytes=elision.result_bytes,
                failure=ToolFailure(
                    kind="blob_store_required",
                    message=failure_guidance(
                        "blob_store_required",
                        f"This call's result is {elision.result_bytes} bytes, above "
                        f"the {self._blob_offload_bytes}-byte offload threshold, but "
                        "no BlobStore is configured for this Run. Configure "
                        "Runtime(blob=...) to store results this large, or have "
                        "the tool return less.",
                    ),
                ),
            )
            return

        # Offload always implies elision, regardless of the Spec's own
        # threshold: an offloaded record's `result` is None, and the
        # conversation projection reads an absent handle as "`result` is the
        # model-visible payload" (see the module docstring in
        # psych_runtime.tools.large_results).
        if not elision.elide:
            elision = force_elision(result, call_id)

        key = blob_key(self._journal.scope, self._journal.run_id, call_id)
        await self._blob.put(
            key, offload.payload, content_type=offload.content_type, metadata=offload.metadata
        )
        await self._journal.append(
            type="tool_call_finished",
            call_id=call_id,
            outcome=ToolOutcome.OK,
            result=None,
            duration_seconds=duration,
            result_bytes=elision.result_bytes,
            result_handle=elision.handle,
            preview=elision.preview,
            result_blob_key=str(key),
            result_content_type=offload.content_type,
        )

    # -- queues -------------------------------------------------------------

    async def _drain(self, queue: QueueKind) -> None:
        """Mark every pending entry in ``queue`` as delivered.

        The conversation projection reads consumption rather than enqueueing, so
        a steer appears in the conversation at the point it actually reached the
        model rather than the point it arrived.
        """
        state = self._journal.state
        pending = {
            QueueKind.STEER: state.pending_steer,
            QueueKind.FOLLOW_UP: state.pending_follow_up,
            QueueKind.NEXT_RUN: state.pending_next_run,
        }[queue]
        for entry in list(pending):
            await self._journal.append(type="queue_consumed", entry_id=entry.entry_id)

    async def _read_log(self) -> list[Record]:
        return await self._journal.read_all()

    async def _sleep_unless_aborted(self, seconds: float) -> None:
        """Wait, but wake immediately if this Attempt is told to stop.

        A backoff that ignored the abort signal would hold a Run for up to the
        cap past the moment its deadline fired or its Worker started shutting
        down, which is exactly the wait the grace period is meant to bound.
        """
        if seconds <= 0:
            return
        if self._abort is None:
            await asyncio.sleep(seconds)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._abort.wait(), timeout=seconds)

    async def _refresh(self) -> None:
        """Fold anything the consumer appended, and stop if another Attempt did.

        ``SeqConflict`` here means a second Attempt is writing this log, which
        is the same situation as a lost lease: this one stops immediately
        rather than interleaving records into it.
        """
        await self._journal.refresh()


def _parse_arguments(raw: str) -> tuple[dict[str, Any], ToolFailure | None]:
    """Reassemble streamed argument fragments into a dict.

    A model that produced invalid JSON gets told so as a tool result rather than
    having its turn killed. Empty arguments are legitimate: a tool taking no
    parameters streams nothing.
    """
    if not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        return {}, ToolFailure(
            kind="malformed_arguments",
            message=failure_guidance(
                "malformed_arguments",
                f"The arguments you sent were not valid JSON: {err}. Send the "
                "arguments again as a single valid JSON object.",
            ),
        )
    if not isinstance(parsed, dict):
        return {}, ToolFailure(
            kind="malformed_arguments",
            message=failure_guidance(
                "malformed_arguments",
                f"Tool arguments must be a JSON object, and you sent a "
                f"{type(parsed).__name__}. Send an object with the parameter names as keys.",
            ),
        )
    return parsed, None


def _interruptible(spec: AgentSpec, name: str) -> bool:
    for tool in spec.tools:
        if tool.name == name:
            return tool.interruptible
    # An MCP tool the Spec does not describe individually. Interruptible is the
    # right default: it is what a read is, and a destructive MCP tool should be
    # gated by an approval rather than by being uninterruptible.
    return True
