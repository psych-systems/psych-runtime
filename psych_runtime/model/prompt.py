"""Prompt assembly, and why the order is fixed.

DESIGN.md §19. Prompt construction is cache-deliberate: the ordering of system
prompt, skills index, memories and tool definitions is fixed so that a
mid-conversation change invalidates the shortest possible prefix.

**Any change to the order below is a performance change and gets reviewed as
one.** Prompt caching is the largest cost lever available, and a provider's cache
matches on a byte prefix. Moving a volatile section above a stable one does not
lose a feature, it silently multiplies the bill.

## The order, most stable first

1. **Agent identity and instructions.** Fixed for the life of a Version. Never
   changes mid-Run, because the Run pinned its Version at admission. The
   answer-style instruction sits here too, for the same reason: it comes from
   the Spec and cannot change while a Run is in flight.
2. **The skills index.** Names and one-line descriptions only, never bodies. Part
   of the Spec, so it changes exactly as often as the instructions do.
3. **Durable memories.** Change between Runs, effectively never within one. Above
   the tool list because a `remember` call is rarer than a tool set changing.
4. **Tool definitions.** The most volatile section: resolved fresh at every turn
   (§10.2), and an MCP server connecting mid-Run changes it.
5. **Runtime advisories.** Withheld-tool notices and anything else the loop needs
   to say. Last because it is the only part that can change within a single turn
   boundary for reasons unrelated to the Spec.

Everything above a change survives in the provider's cache. Everything below it
is re-read. So the sections are ordered by how often they change, and the
conversation follows all of them.

## Why the tool set cannot churn mid-turn

Section 4 would be a problem if the tool set could change while a turn is in
flight: it would rewrite the cached prefix and contradict what the model was told
it could do. It cannot, because resolution happens at turn boundaries and is
pinned for the turn's duration (§10.2). The two decisions hold each other up.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from psych_runtime.core.messages import Message, SystemMessage, ToolDefinition
from psych_runtime.core.spec import AgentSpec, Skill

__all__ = [
    "PromptSections",
    "answer_style_block",
    "assemble",
    "cache_breakpoints",
    "system_prompt",
]

_SECTION_GAP: Final = "\n\n"


class PromptSections:
    """The pieces of a system prompt, kept apart until the last moment.

    Holding them separately rather than as one string is what lets
    ``cache_breakpoints`` say where the stable part ends, and lets a test assert
    the order without parsing prose.
    """

    __slots__ = ("advisories", "answer_style", "instructions", "memories", "skills_index")

    def __init__(
        self,
        instructions: str,
        skills_index: str = "",
        memories: str = "",
        advisories: str = "",
        answer_style: str = "",
    ) -> None:
        self.instructions = instructions
        self.answer_style = answer_style
        self.skills_index = skills_index
        self.memories = memories
        self.advisories = advisories

    def render(self) -> str:
        """Join the sections that have content, in the fixed order."""
        present = [
            section
            for section in (
                self.instructions,
                self.answer_style,
                self.skills_index,
                self.memories,
                self.advisories,
            )
            if section.strip()
        ]
        return _SECTION_GAP.join(present)

    @property
    def stable_prefix(self) -> str:
        """Everything that does not change within a Run.

        Instructions and the skills index come from the pinned Version, so they
        are identical on every turn of a Run by construction. This is the part
        worth telling a provider to cache.
        """
        present = [
            s for s in (self.instructions, self.answer_style, self.skills_index) if s.strip()
        ]
        return _SECTION_GAP.join(present)


def skills_index(skills: Sequence[Skill]) -> str:
    """The skills index: names and descriptions, never bodies.

    DESIGN.md §16: descriptions sit in the system prompt and bodies load only when
    the model calls ``load_skill``. Putting bodies here would defeat the entire
    mechanism, which exists because instruction packs are large and most of them
    are irrelevant to any given turn.
    """
    if not skills:
        return ""
    lines = [
        "## Skills",
        "",
        "Instruction packs you can load when you need them. Call `load_skill` with "
        "a name to read one. Load a skill before doing work it covers rather than "
        "guessing at its content.",
        "",
    ]
    lines.extend(f"- `{skill.name}`: {skill.description}" for skill in skills)
    return "\n".join(lines)


def memories_block(memories: Sequence[str]) -> str:
    """Durable facts recalled for this Scope and end user (DESIGN.md §15)."""
    if not memories:
        return ""
    lines = ["## What you remember about this user", ""]
    lines.extend(f"- {memory}" for memory in memories)
    return "\n".join(lines)


def answer_style_block(style: str | None) -> str:
    """How the agent is asked to shape its final answer, or nothing.

    ``None`` renders nothing at all, so a Spec that does not set it assembles
    byte-identically to one written before this existed. That is what keeps
    every already-published Version behaving exactly as it did.

    The wording asks for structure *when it helps* rather than always. "Always
    use a table" would make a one-line answer absurd, and an agent that
    formats "yes" as a table is worse than one that does not format at all. It
    also does not ask the model to explain its working in the final message:
    the working is right there in the log for a reader who opens it
    (``psych_runtime.core.answer``), and repeating it in the answer is the thing this
    exists to stop.
    """
    if style != "concise":
        return ""
    return (
        "## Answering\n"
        "\n"
        "Lead with the answer. Keep it short: give the conclusion and the facts "
        "that support it, not a description of how you found them.\n"
        "Use bullet points or a small table when you are presenting more than "
        "two or three facts, and plain sentences when you are not.\n"
        "Do not recap the steps you took or the tools you called. Someone "
        "reading this can already see them."
    )


def advisories_block(advisories: Sequence[str]) -> str:
    """Runtime notices, last because they are the most volatile section."""
    if not advisories:
        return ""
    return "\n".join(["## Notices", "", *(f"- {advisory}" for advisory in advisories)])


def system_prompt(
    spec: AgentSpec,
    *,
    memories: Sequence[str] = (),
    advisories: Sequence[str] = (),
) -> PromptSections:
    """Build the system prompt's sections in the fixed order.

    Args:
        spec: the pinned agent Spec. Its instructions and skills are the stable
            part of every turn's prompt.
        memories: durable facts for this Scope and end user.
        advisories: runtime notices, such as a tool the failure-streak guard
            withheld.
    """
    identity = spec.instructions.strip()
    if spec.description.strip() and not identity:
        identity = spec.description.strip()

    return PromptSections(
        instructions=identity,
        answer_style=answer_style_block(spec.answer_style),
        skills_index=skills_index(spec.skills),
        memories=memories_block(memories),
        advisories=advisories_block(advisories),
    )


def assemble(
    spec: AgentSpec,
    conversation: Sequence[Message],
    *,
    memories: Sequence[str] = (),
    advisories: Sequence[str] = (),
) -> tuple[Message, ...]:
    """The full message list for one model call.

    The system message is rebuilt every turn rather than cached in memory. It has
    to be: memories and advisories change, and a stale system prompt is worse
    than a rebuilt one. Rebuilding is cheap and the byte-identical prefix is what
    the provider's cache actually matches on.

    Any ``SystemMessage`` already in ``conversation`` is dropped. There is exactly
    one system message and this function owns it; a second one appearing
    mid-conversation is the kind of thing that quietly costs a cache hit on every
    subsequent turn.
    """
    sections = system_prompt(spec, memories=memories, advisories=advisories)
    body = sections.render()
    rest = tuple(message for message in conversation if not isinstance(message, SystemMessage))
    if not body:
        return rest
    return (SystemMessage(content=body), *rest)


def cache_breakpoints(
    messages: Sequence[Message], tools: Sequence[ToolDefinition]
) -> tuple[int, ...]:
    """Where a provider may usefully place a cache breakpoint.

    One breakpoint, after the system message. That is the boundary between what
    is identical on every turn of a Run and what grows with the conversation, and
    it is the only boundary worth spending a provider's limited breakpoint budget
    on.

    Providers cap how many breakpoints a request may carry (some allow
    four), so scattering them through the conversation wastes the budget on
    positions that will be invalidated by the next message anyway.

    ``tools`` is accepted and deliberately unused for placement: tool definitions
    travel in their own field rather than in the message list, so they cannot
    take a message index. It is in the signature because a caller reasoning about
    cache behaviour needs to pass what it is sending, and a future provider that
    caches tools separately will need it.
    """
    if not messages or not isinstance(messages[0], SystemMessage):
        return ()
    _ = tools
    return (0,)
