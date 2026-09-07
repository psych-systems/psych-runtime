"""The conversation, as the model sees it.

These live in ``psych_runtime.core`` rather than ``psych_runtime.model`` for the same reason
``Usage`` does: the reducer derives the conversation from the log, the reducer is
core, and core imports nothing from its siblings. ``psych_runtime.model`` turns these
into whatever wire format a provider wants.

Keeping the two apart also means a provider adapter cannot quietly change what a
conversation *is*. It can only change how it is transmitted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from psych_runtime.core.ids import ToolCallId

__all__ = [
    "AssistantMessage",
    "Message",
    "Role",
    "SystemMessage",
    "ToolCall",
    "ToolDefinition",
    "ToolResultMessage",
    "UserMessage",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SystemMessage(_Message):
    """The assembled system prompt.

    Exactly one of these, first. Prompt assembly order is fixed so that a
    mid-conversation change invalidates the shortest possible cache prefix
    (DESIGN.md §19), and that only works if the system message is one block in
    one place.
    """

    role: Literal[Role.SYSTEM] = Role.SYSTEM
    content: str


class UserMessage(_Message):
    role: Literal[Role.USER] = Role.USER
    content: str
    name: str | None = None


class ToolCall(BaseModel):
    """A tool invocation the model asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ToolCallId
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantMessage(_Message):
    """What the model said, and what it asked to call.

    An assistant message with tool calls must be followed by exactly one tool
    result per call. Providers reject a conversation where a call has no answer
    and it cannot be recovered from, which is why the reducer treats a missing
    result as something the Worker settles explicitly rather than something the
    prompt assembler papers over (DESIGN.md §9).
    """

    role: Literal[Role.ASSISTANT] = Role.ASSISTANT
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str | None = None
    """Provider-supplied reasoning, where it is returned and can be replayed.
    Dropped silently by adapters whose provider will not accept it back."""


class ToolResultMessage(_Message):
    """The answer to one tool call.

    ``content`` is what the model sees, which for a large result is a handle plus
    a preview rather than the whole thing. The log always holds the full result
    (DESIGN.md §10.8), so this being elided loses nothing except context window.
    """

    role: Literal[Role.TOOL] = Role.TOOL
    tool_call_id: ToolCallId
    name: str = Field(min_length=1)
    content: str
    is_error: bool = False
    """A failed tool is data the model reads and reacts to, not an exception that
    kills the turn (DESIGN.md §18)."""


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]

MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)


class ToolDefinition(BaseModel):
    """A tool as described to the model.

    Built fresh at every turn by the resolver (DESIGN.md §10.2) from the Spec,
    the registry and whatever the MCP catalogue currently offers, then narrowed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: frozenset[str] = frozenset()
    """MCP annotations: ``read-only``, ``write``, ``destructive``. Selectors match
    on these to decide approvals. An unannotated tool is treated as ``write``
    (DESIGN.md §10.9). Exempting it instead would let a server that forgot to
    annotate a destructive tool buy that tool an exemption."""
