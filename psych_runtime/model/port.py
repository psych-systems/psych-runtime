"""The ModelClient port.

DESIGN.md §19. Psych speaks a wire protocol through this port, not a provider
SDK. The consumer supplies a provider endpoint or a compatible gateway.

The port is deliberately richer than the OpenAI-compatible shape, because that
shape loses prompt-cache control and reasoning blocks, and prompt caching is the
largest cost lever available. A native provider adapter should not require
changing this.

## Streaming, not request-response

Everything streams. A non-streaming provider is adapted by yielding one event,
never the reverse: time to first token is a number the report needs (§13.3), and
you cannot recover it from a call that already returned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from psych_runtime.core.ids import ToolCallId
from psych_runtime.core.messages import Message, ToolDefinition
from psych_runtime.core.usage import Cost, Usage
from psych_runtime.model.tool_normalize import normalize_tools

__all__ = [
    "ModelClient",
    "ModelRequest",
    "ReasoningDelta",
    "StreamDone",
    "StreamEvent",
    "TextDelta",
    "ToolCallDelta",
]


class ModelRequest(BaseModel):
    """One call to a model.

    Attributes:
        model: the model id. A Spec names its model; there is no router
            (DESIGN.md §19).
        messages: the conversation, system message first.
        tools: what the model may call this turn. Resolved per turn and fixed for
            its duration, so the provider's cached prompt prefix stays valid
            (§10.2).
        cache_breakpoints: indices into ``messages`` after which the provider may
            place a cache breakpoint. Providers that do not support explicit
            breakpoints ignore this. Expressed as indices rather than as flags on
            the messages so that the same conversation can be sent to two
            providers with different caching models.
        idle_timeout_seconds: fail the read when no chunk arrives for this long.
            ``0`` disables it. The timer runs only while a source read is
            outstanding, so consumer backpressure never trips it (§8.5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...] = ()
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    stop: tuple[str, ...] = ()
    cache_breakpoints: tuple[int, ...] = ()
    idle_timeout_seconds: float = Field(default=300.0, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)
    """Provider-specific passthrough. Deliberately untyped and deliberately last:
    anything here is outside the contract and an adapter may ignore it."""

    @field_validator("tools", mode="after")
    @classmethod
    def _normalize_tools(cls, tools: tuple[ToolDefinition, ...]) -> tuple[ToolDefinition, ...]:
        """Sort tools by name and every schema's keys, once, here.

        This runs on every construction of a ``ModelRequest``, regardless of
        which caller assembled ``tools`` or which adapter reads them back, so
        it is the one point every provider adapter's request bytes go
        through. See ``psych_runtime.model.tool_normalize`` for why this is that
        point rather than the resolver that most often builds this field.
        """
        return normalize_tools(tools)


class TextDelta(BaseModel):
    """A chunk of assistant text."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["text"] = "text"
    text: str


class ReasoningDelta(BaseModel):
    """A chunk of reasoning, where the provider exposes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolCallDelta(BaseModel):
    """Part of a tool call.

    Providers stream tool arguments as JSON fragments, so ``arguments_fragment``
    is a partial string and the adapter accumulates it. ``id`` and ``name``
    typically arrive once, on the first fragment, and are ``None`` after that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["tool_call"] = "tool_call"
    index: int = Field(ge=0)
    id: ToolCallId | None = None
    name: str | None = None
    arguments_fragment: str = ""


class StreamDone(BaseModel):
    """The stream ended cleanly, carrying what only the end knows.

    Usage arrives here rather than being estimated, because an estimate that
    looks like a measurement is worse than no number at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    type: Literal["done"] = "done"
    finish_reason: str = Field(min_length=1)
    usage: Usage = Field(default_factory=Usage)
    usage_reported: bool = True
    """Whether ``usage`` came from the provider response.

    ``False`` means the provider omitted usage entirely. The counters remain
    zero for backwards compatibility, but callers must not price those zeros
    or present them as a measured value.
    """
    cost: Cost | None = None
    """What the provider says this call cost, when it says anything.

    ``None`` is the ordinary case and means "the provider did not tell me",
    never "free". A client that cannot get a number from its provider leaves
    this alone and Psych falls back to computing one from a ``PriceResolver``.

    Worth carrying because a gateway's figure is better than Psych's. Several
    gateways return a cost computed against the
    caller's real contract, including negotiated rates Psych has no way to
    know; a consumer routing through one gets a number that reconciles with
    their invoice, where a locally computed one plausibly does not and nobody
    notices until month end.

    Which of the two is recorded is the consumer's choice, not this field's:
    see ``psych_runtime.model.pricing.CostPolicy``. What is recorded carries
    ``Cost.source`` so a reader can tell them apart.
    """


StreamEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallDelta | StreamDone,
    Field(discriminator="type"),
]


@runtime_checkable
class ModelClient(Protocol):
    """What Psych needs from a model provider.

    One method. Everything else Psych does with models, it does itself.
    """

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Call the model and yield events until the stream ends.

        The final event is a ``StreamDone``. A stream that ends without one is a
        stream that aborted mid-token, and the caller treats it as a retryable
        failure rather than as a short answer.

        Raises:
            TransientError: something worth retrying inside the Run's transient
                budget: 5xx, 429, 408, a connection error, or an idle timeout.
            PsychError: anything not worth retrying: 400, 401, 403, a schema
                error. Retrying these burns budget and never succeeds.
        """
        ...

    async def known_models(self) -> Sequence[str]:
        """Model ids this client will accept, for publish-time validation.

        An empty sequence means "I cannot tell you", which is the honest answer
        from a proxy that can reach models it has never been told about. The
        validator treats empty as "do not check" rather than as "nothing is
        valid" (see ``psych_runtime.core.validation``).
        """
        ...
