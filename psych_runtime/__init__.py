"""Psych: an embeddable agent runtime.

Everything a platform needs except the platform. Psych is a library a company
imports to build their own agentic platform: it gives you the agent loop, durable
execution, the record log, tool resolution, MCP with tenant isolation, token
accounting and the run report. It does not give you an HTTP server, an auth
system, a database, a scheduler or a UI, because you already have those.

Psych is a library, not a framework. A framework inverts control; Psych does not.
You call Psych; Psych calls back only through ports you supplied.

The short way in, which assembles a store, a Worker and a Runtime and stops
them again on the way out:

```python
import psych_runtime

async with psych_runtime.session(model, tools=[lookup_order]) as s:
    view = await s.ask(spec, "where is order A1?")
    print(view.text)
```

The long way, which is what that does and what a real deployment writes,
because it owns the store and the Worker fleet itself:

```python
import psych_runtime
from psych_runtime.store.memory import InMemoryStore

store = InMemoryStore()
version = await psych_runtime.publish(store, spec)
run = await psych_runtime.dispatch(store, version, scope, input={"message": "hello"})

async for record in psych_runtime.stream(store, run.run_id):
    ...
```

The public API is this module and nothing else. Anything reached through a
submodule path is internal and moves without ceremony while the package is
``0.x`` (DESIGN.md §22).
"""

from __future__ import annotations

import logging

from psych_runtime.api import (
    answer,
    dispatch,
    interrupt,
    publish,
    records,
    report,
    resume,
    send,
    state,
    status,
    stream,
    stream_text,
    thread,
)
from psych_runtime.builder.agent import AgentBuilder, agent
from psych_runtime.builder.errors import BuilderError
from psych_runtime.builder.workflow import WorkflowBuilder, workflow
from psych_runtime.core.answer import AnswerView, ToolCallView, WorkTurn
from psych_runtime.core.components import Component
from psych_runtime.core.corruption import CorruptionReason, CorruptLog
from psych_runtime.core.errors import (
    AccessDenied,
    DeadlineExceeded,
    LeaseLost,
    PsychError,
    RunAborted,
    RunAlreadySettled,
    RunEndedWithoutAnswer,
    RunFailed,
    RunNotFound,
    RunNotSuspended,
    SeqConflict,
    SpecValidationError,
    StoreError,
    SuspensionExpired,
    TransientError,
    ValidationIssue,
)
from psych_runtime.core.ids import AttemptId, RunId, StepId, ToolCallId, VersionHash, WorkerId
from psych_runtime.core.messages import ToolDefinition
from psych_runtime.core.questions import AskedQuestion, QuestionOption
from psych_runtime.core.records import (
    ModelTimings,
    QueueKind,
    Record,
    SuspendReason,
    TerminalState,
    ToolFailure,
    ToolOutcome,
)
from psych_runtime.core.reducer import RunStateView
from psych_runtime.core.scope import Scope
from psych_runtime.core.spec import (
    A2APeer,
    AgentSpec,
    AgentStep,
    CodeTool,
    CompactionPolicy,
    HttpTool,
    Limits,
    McpOAuth,
    McpServer,
    ModelRef,
    Skill,
    SpawnEnvelope,
    Spec,
    SubagentRef,
    SuspensionPolicy,
    ToolStep,
    WorkflowSpec,
    WorkflowStepRef,
)
from psych_runtime.core.status import Lifecycle, PendingApproval, PendingQuestion, RunStatus
from psych_runtime.core.tasks import Task, TaskStatus
from psych_runtime.core.thread_view import MessageView, ThreadView
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.core.validation import ValidationContext
from psych_runtime.core.version import Version
from psych_runtime.memory.port import Memory, MemoryKey, MemoryStore
from psych_runtime.model.egress import EgressPolicy, HttpTransport
from psych_runtime.model.openai_compat import OpenAICompatibleClient
from psych_runtime.model.port import ModelClient, ModelRequest
from psych_runtime.model.pricing import (
    DEFAULT_PRICES,
    CostPolicy,
    ModelPrice,
    PriceResolver,
    StaticPriceTable,
)
from psych_runtime.report.model import (
    CompactionReport,
    FailureStreakTrip,
    LatencyReport,
    ModelCallReport,
    RunReport,
    StepReport,
    SubagentReport,
    SubtreeTotals,
    SuspensionReport,
    ToolCallReport,
    TotalsReport,
)
from psych_runtime.runtime.dispatch import Dispatched
from psych_runtime.runtime.execute import Runtime
from psych_runtime.runtime.worker import Worker
from psych_runtime.sandbox.port import (
    HostBinding,
    Sandbox,
    SandboxFailure,
    SandboxLimit,
    SandboxLimits,
    SandboxResult,
)
from psych_runtime.session import Session, session
from psych_runtime.store.blob import BlobKey, BlobStore
from psych_runtime.store.blob_memory import InMemoryBlobStore
from psych_runtime.store.memory import InMemoryStore
from psych_runtime.store.port import RunHeader, RunState, Store
from psych_runtime.telemetry.port import Telemetry
from psych_runtime.tools.a2a import A2APool, A2ATools
from psych_runtime.tools.mcp import McpPool, McpTools
from psych_runtime.tools.policy import AllowAll, Decision, Policy, ToolClass
from psych_runtime.tools.registry import ToolRegistry
from psych_runtime.tools.secrets import (
    CredentialNotFound,
    ResolvedCredential,
    SecretResolver,
)

__version__ = "0.1.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())
"""A library does not decide where its own log lines go.

Without this, a consumer who has configured no logging at all still gets
Psych's warnings printed to their stderr, because Python's ``lastResort``
handler emits anything at WARNING or above when nothing else would. That is
uninvited output from a library into somebody else's process, and the first
sign of it is usually a lease-renewal warning appearing in the middle of an
application's own console.

Attaching a ``NullHandler`` to the root ``psych`` logger makes the default
silence, and leaves every real decision -- level, format, destination -- to the
consumer, who configures ``logging.getLogger("psych_runtime")`` like any other library.
The Python logging HOWTO names this as what a library should do, and Psych had
been the exception rather than the rule.

Psych logs sparingly and only from ``psych_runtime.runtime``: things a person operating
a fleet of Workers needs to know that are not Records. Everything a Run *did*
is in its log and reaches a consumer through ``psych_runtime.report()`` and the
``Telemetry`` port, not through here.
"""

__all__ = [
    "DEFAULT_PRICES",
    "A2APeer",
    "A2APool",
    "A2ATools",
    "AccessDenied",
    "AgentBuilder",
    "AgentSpec",
    "AgentStep",
    "AllowAll",
    "AnswerView",
    "AskedQuestion",
    "AttemptId",
    "BlobKey",
    "BlobStore",
    "BuilderError",
    "CodeTool",
    "CompactionPolicy",
    "CompactionReport",
    "Component",
    "CorruptLog",
    "CorruptionReason",
    "Cost",
    "CostPolicy",
    "CredentialNotFound",
    "DeadlineExceeded",
    "Decision",
    "Dispatched",
    "EgressPolicy",
    "FailureStreakTrip",
    "HostBinding",
    "HttpTool",
    "HttpTransport",
    "InMemoryBlobStore",
    "InMemoryStore",
    "LatencyReport",
    "LeaseLost",
    "Lifecycle",
    "Limits",
    "McpOAuth",
    "McpPool",
    "McpServer",
    "McpTools",
    "Memory",
    "MemoryKey",
    "MemoryStore",
    "MessageView",
    "ModelCallReport",
    "ModelClient",
    "ModelPrice",
    "ModelRef",
    "ModelRequest",
    "ModelTimings",
    "OpenAICompatibleClient",
    "PendingApproval",
    "PendingQuestion",
    "Policy",
    "PriceResolver",
    "PsychError",
    "QuestionOption",
    "QueueKind",
    "Record",
    "ResolvedCredential",
    "RunAborted",
    "RunAlreadySettled",
    "RunEndedWithoutAnswer",
    "RunFailed",
    "RunHeader",
    "RunId",
    "RunNotFound",
    "RunNotSuspended",
    "RunReport",
    "RunState",
    "RunStateView",
    "RunStatus",
    "Runtime",
    "Sandbox",
    "SandboxFailure",
    "SandboxLimit",
    "SandboxLimits",
    "SandboxResult",
    "Scope",
    "SecretResolver",
    "SeqConflict",
    "Session",
    "Skill",
    "SpawnEnvelope",
    "Spec",
    "SpecValidationError",
    "StaticPriceTable",
    "StepId",
    "StepReport",
    "Store",
    "StoreError",
    "SubagentRef",
    "SubagentReport",
    "SubtreeTotals",
    "SuspendReason",
    "SuspensionExpired",
    "SuspensionPolicy",
    "SuspensionReport",
    "Task",
    "TaskStatus",
    "Telemetry",
    "TerminalState",
    "ThreadView",
    "ToolCallId",
    "ToolCallReport",
    "ToolCallView",
    "ToolClass",
    "ToolDefinition",
    "ToolFailure",
    "ToolOutcome",
    "ToolRegistry",
    "ToolStep",
    "TotalsReport",
    "TransientError",
    "Usage",
    "ValidationContext",
    "ValidationIssue",
    "Version",
    "VersionHash",
    "WorkTurn",
    "Worker",
    "WorkerId",
    "WorkflowBuilder",
    "WorkflowSpec",
    "WorkflowStepRef",
    "__version__",
    "agent",
    "answer",
    "dispatch",
    "interrupt",
    "publish",
    "records",
    "report",
    "resume",
    "send",
    "session",
    "state",
    "status",
    "stream",
    "stream_text",
    "thread",
    "workflow",
]
