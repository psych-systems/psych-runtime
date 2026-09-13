"""The closed vocabularies code execution is configured and reported in.

DESIGN.md §18. These enums are the words a Spec uses to *request* how a
model-written program should run and the words a ``Sandbox`` uses to *report*
what an execution actually got. They live in ``psych_runtime.core`` because both
sides need them and ``psych_runtime.core`` imports nothing from the other
subpackages (DESIGN.md §21): the Spec models in ``psych_runtime.core.spec`` name an
isolation level, and ``psych_runtime.sandbox.port`` reports one back, and neither
may import the other for it.

## Two isolation levels, and why there is no third

``IsolationLevel.ISOLATED`` is the level a Spec gets when it says nothing. It
means a kernel-enforced boundary: the program sees a private filesystem and
not the host's, it has no network route unless one was granted deliberately,
every process it starts ends when the execution ends, and its CPU, memory and
process budgets are enforced by the kernel rather than by anything the
program could talk its way past. A container, a Linux namespace sandbox and a
remote provider that promises the same are all ways to get it.

``IsolationLevel.PROCESS`` is for **trusted code only**. It is a fresh
operating-system process with resource limits, a scrubbed environment, a
temporary working directory and whole-tree cleanup, and it hides nothing on
the host filesystem from the program. It exists so a developer can use
``run_code`` on a laptop with nothing installed, and so an operator running
their own vetted programs has a cheap backend. It is never selected
automatically: a Spec that wants it says so, and a backend that can only
provide it refuses an execution that asked for ``ISOLATED`` rather than
quietly running at the weaker level.

There is deliberately no ``"none"`` level. In-process execution is rejected
outright (DESIGN.md §18) and the ``Sandbox`` port has no way to express it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "BINDABLE_BUILTINS",
    "DEFAULT_BINDING_BUDGET",
    "EXCLUDED_BUILTINS",
    "ArtifactCollection",
    "BindingBudget",
    "Enforcement",
    "IsolationLevel",
    "NetworkAccess",
    "OutputPreservation",
    "WorkspacePolicy",
]


class IsolationLevel(StrEnum):
    """How strongly a program is separated from the host it runs on.

    Ordered: ``ISOLATED`` is stronger than ``PROCESS``. ``satisfies`` is the
    one comparison the runtime makes, so the ordering lives here rather than
    being re-derived at each call site.
    """

    ISOLATED = "isolated"
    """A kernel-enforced boundary: private filesystem, network denied unless
    granted, whole process tree contained, kernel-enforced resource limits."""

    PROCESS = "process"
    """A fresh process with resource limits, a scrubbed environment and a
    temporary working directory, sharing the host's filesystem view. For
    trusted code only, and never selected on a Spec's behalf."""

    def satisfies(self, requested: IsolationLevel) -> bool:
        """Whether an execution at this level meets a request for ``requested``."""
        return _RANK[self] >= _RANK[requested]


_RANK: dict[IsolationLevel, int] = {IsolationLevel.PROCESS: 1, IsolationLevel.ISOLATED: 2}


class Enforcement(StrEnum):
    """How much one guarantee is worth, on one execution or one backend.

    A single word per guarantee rather than a boolean, because "we tried"
    and "we know it held" are different facts and a consumer relying on one
    of them needs to know which they have.
    """

    ENFORCED = "enforced"
    """The mechanism was applied and, where a check is possible, verified
    from inside the execution."""

    UNVERIFIED = "unverified"
    """The mechanism was applied, or is promised by the backend, and this
    execution has no way to confirm it from the inside."""

    UNAVAILABLE = "unavailable"
    """Not provided. Either the platform cannot, or it was deliberately
    switched off (network access that was granted, for example)."""


class NetworkAccess(StrEnum):
    """Whether the program may open its own sockets."""

    DENIED = "denied"
    """The default. No route out; anything the program needs fetched goes
    through a host binding that itself uses the egress seam (DESIGN.md §14)."""

    UNRESTRICTED = "unrestricted"
    """Raw sockets allowed. This bypasses the egress seam for that program, so
    a deployment profile must permit it explicitly before a Spec can have it."""


class WorkspacePolicy(StrEnum):
    """What happens to the working directory between executions."""

    EPHEMERAL = "ephemeral"
    """A fresh, empty directory per execution, removed afterwards. The only
    policy today; named so a persistent one can be added as a versioned
    change rather than a silent one."""


class OutputPreservation(StrEnum):
    """What must happen to output too large for the model to see whole."""

    WHEN_AVAILABLE = "when_available"
    """Keep the full bytes in the Runtime's ``BlobStore`` when one is wired,
    otherwise keep only the preview and say so."""

    REQUIRED = "required"
    """The full bytes must be kept. Without a ``BlobStore`` the call fails
    explicitly rather than quietly losing output."""

    NEVER = "never"
    """Keep only the preview, even when a ``BlobStore`` is available."""


@dataclass(frozen=True, slots=True)
class BindingBudget:
    """What one program may spend calling the host's tools.

    A program compresses model turns; it does not get to compress the host's
    work. One ``run_code`` call could otherwise issue a million MCP calls, or
    assemble a gigabyte of arguments, without the model paying a turn for any
    of it, so every binding call is counted and measured against this.

    The **deployment** owns these numbers, not the Spec
    (``psych_runtime.sandbox.profiles.SandboxProfile.binding_budget``), and a
    ``CodeExecutionPolicy`` may narrow them per tenant. An agent's author is
    the wrong party to answer a denial-of-service question about the host they
    are running on, and a Spec field would put the answer inside the Version
    hash where changing it means republishing every agent.

    Here in ``psych_runtime.core`` because both sides need it and neither may
    import the other: ``psych_runtime.sandbox`` carries it on a profile and
    ``psych_runtime.tools.bindings`` enforces it (DESIGN.md §21).

    Attributes:
        max_calls: binding calls in one execution.
        max_calls_per_run: binding calls across every execution of one Run, so
            a model cannot buy a fresh allowance by writing a second program.
        max_argument_bytes: the serialised arguments of one call.
        max_result_bytes: the serialised result of one call, before it is sent
            back into the program. A larger result is refused rather than
            truncated: a program silently handed half a JSON document computes
            a wrong answer confidently.
        max_total_bytes: arguments plus results across one execution. This is
            *transfer*: bytes that actually crossed into the sandbox.
        max_produced_bytes: bytes the host's tools produced for one execution,
            whether or not the program received them. A result replaced by a
            handle was still fetched, parsed and held by the host, and a
            budget that counted only what crossed the boundary would price
            that at the size of the handle.
        max_preserved_bytes_per_run: bytes one Run's programs may leave stored
            in the log or the BlobStore. The ceiling that actually bounds
            somebody's storage bill, and the reason it is separate from
            ``max_produced_bytes``: a result over the transfer ceiling is
            preserved before the program is told about it, so a refused
            transfer still costs storage, while a small inline result costs
            none. Per Run and counted from the log, so a Worker that dies
            does not hand the next Attempt a fresh allowance.
    """

    max_calls: int = 1_000
    max_calls_per_run: int = 10_000
    max_argument_bytes: int = 1 * 1024 * 1024
    max_result_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_produced_bytes: int = 256 * 1024 * 1024
    max_preserved_bytes_per_run: int = 512 * 1024 * 1024

    def narrow(self, other: BindingBudget) -> BindingBudget:
        """The smaller of the two on every dimension. Never wider than either."""
        return BindingBudget(
            max_calls=min(self.max_calls, other.max_calls),
            max_calls_per_run=min(self.max_calls_per_run, other.max_calls_per_run),
            max_argument_bytes=min(self.max_argument_bytes, other.max_argument_bytes),
            max_result_bytes=min(self.max_result_bytes, other.max_result_bytes),
            max_total_bytes=min(self.max_total_bytes, other.max_total_bytes),
            max_produced_bytes=min(self.max_produced_bytes, other.max_produced_bytes),
            max_preserved_bytes_per_run=min(
                self.max_preserved_bytes_per_run, other.max_preserved_bytes_per_run
            ),
        )


DEFAULT_BINDING_BUDGET: Final = BindingBudget()
"""What a profile applies when the deployment sets nothing.

A thousand calls is far more than any sensible program makes and far less than
a loop that got away. The byte ceilings are deliberately well above a real tool
result, because their job is to stop a program assembling something enormous
merely to send it, not to second-guess a tool's own payload.

``max_result_bytes`` is the one limit with no way around it: a single reply
frame carries one value, so a result larger than this cannot be delivered in
one piece by any transport here. That is why exceeding it produces a handle
rather than a failure -- the limit is on the *transfer*, not on the result,
and the result is still whole in the log."""


@dataclass(frozen=True, slots=True)
class ResultHandle:
    """A reference to a tool result too large to send into a sandbox whole.

    A *type*, deliberately, and not a dict with a distinguishing key in it.
    A key can collide: ``{"__psych_result_handle__": "customer-data"}`` is a
    perfectly ordinary thing for somebody's tool to return, and a host that
    decided what a value *was* by looking inside it would hand that program a
    paging object instead of its data -- silently, and only for the results
    whose contents happened to look like ours. Nothing a tool can put in its
    own result can make it an instance of this class, so the ambiguity does
    not exist rather than being made unlikely.

    The sandbox protocol carries the distinction out of band, on the reply
    frame itself, for the same reason.

    Attributes:
        handle: the Run's own handle for the stored result. Opaque, and
            meaningless outside the Run that minted it.
        tool: the tool whose result this names.
        size_bytes: how large the whole result is.
        stored: ``"log"`` or ``"blob"`` -- where the bytes live.
    """

    handle: str
    tool: str
    size_bytes: int
    stored: str

    def as_wire(self) -> dict[str, Any]:
        """The fields as they cross to the child. Carries no marker: the
        reply frame already says what this is."""
        return {
            "handle": self.handle,
            "tool": self.tool,
            "size_bytes": self.size_bytes,
            "stored": self.stored,
        }


RUN_CODE_TOOL_NAME: Final = "run_code"
"""The built-in that runs a program.

Lives here rather than only in ``psych_runtime.tools.code`` because
``psych_runtime.core.reducer`` has to recognise a program's own tool calls
while folding a log, and ``core`` imports nothing from its siblings
(DESIGN.md §21). ``psych_runtime.tools.code.TOOL_NAME`` is this value."""


BINDABLE_BUILTINS: Final[frozenset[str]] = frozenset(
    {"list_tools", "get_tool_info", "read_tool_output"}
)
"""The Psych built-ins a sandboxed program may call.

``list_tools`` and ``get_tool_info`` are read-only catalogue lookups with
bounded output, and a program working against a deferred MCP server needs them
for the same reason the model does: a tool's arguments are not guessable from
its name.

``read_tool_output`` is how a program reads a result too large to hand it in
one piece. A binding whose answer is over the per-call transfer ceiling comes
back as a handle rather than as bytes, and this is the bounded reader for it --
windows and pattern searches, executed on the host, against a result the log
already holds. It was previously excluded on the grounds that a program
"already holds the bytes it produced", which is exactly untrue in the one case
that matters: the case where the bytes were too large to give it. A program
may only read handles its *own* binding calls returned, which the runtime
enforces per execution -- see ``psych_runtime.runtime.agent``.

Everything else Psych registers is excluded, each for its own reason -- see
``EXCLUDED_BUILTINS``.

Here rather than beside the tools themselves because publish-time validation in
``psych_runtime.core.spec`` needs it and may not import
``psych_runtime.tools`` (DESIGN.md §21)."""

EXCLUDED_BUILTINS: Final[Mapping[str, str]] = {
    "ask_question": (
        "it suspends the Run to ask a person, and nothing can resume into a running program"
    ),
    "call_tool": (
        "a program calls a tool by its model-facing name through call_tool(name, "
        "arguments), which routes the same way"
    ),
    "check_subagent": "it reports on a child Run, which a program does not start",
    "delegate": "it starts a child Run that outlives this execution",
    "forget": "it edits durable memory, which belongs to the turn and not to a program",
    "load_skill": "it adds instructions to the model's own context, not to a program's",
    "message_subagent": "it speaks to a child Run, which a program does not start",
    "remember": "it writes durable memory, which belongs to the turn and not to a program",
    "run_code": "a program may not start another program",
    "show_component": (
        "it renders to the person in the conversation, who is not watching a program"
    ),
    "spawn_subagent": "it starts a child Run that outlives this execution",
    "update_tasks": "it edits the model's own task list",
}
"""Every Psych built-in a program may not call, and why, so the refusal, the
agent editor and the documentation all say the same sentence.

``psych_runtime.core.spec.RESERVED_TOOL_NAMES`` enumerates the names Psych
occupies; ``tests/unit/test_bindings.py`` asserts every one of them appears
either here or in ``BINDABLE_BUILTINS``, so a built-in added later cannot
become callable from a program by being forgotten."""


class ArtifactCollection(StrEnum):
    """Whether files the program writes to its workspace are collected."""

    COLLECT = "collect"
    """Regular files under the workspace are returned as artifacts, within
    the count and size caps, and never by their host path."""

    IGNORE = "ignore"
    """Files the program writes are discarded with the workspace."""
