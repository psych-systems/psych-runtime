"""Writing a compaction: deciding where to cut, and asking for the summary.

``psych_runtime.core`` could already represent and replay a compacted
conversation -- the ``CompactionApplied`` Record, the reducer's boundary, and
``build_conversation`` standing the summary in for the range it replaced --
and nothing could produce one. This is the missing half, and it lives in
``psych_runtime.runtime`` because it is a write: it reads the log, calls a model, and
appends. The read half stays where it is; see both packages' READMEs.

## Where the cut goes, and why it is a turn boundary

The Record replaces a range of sequences, so a cut is a sequence. This module
only ever picks the sequence immediately before a ``turn_started``, keeping the
policy's most recent turns verbatim below the summary.

That is not tidiness. A turn's assistant message and the tool results answering
it are written between one ``turn_started`` and the next, and a provider rejects
a conversation carrying an assistant message whose tool calls have no results
(DESIGN.md §9). Cutting anywhere else could put the assistant message above the
line and its results below it, which is the one shape the conversation must
never take. Cutting at a turn boundary makes that unrepresentable rather than
merely unlikely.

## Why the cut has to advance, and what that buys

``plan_cut`` returns nothing unless the cut lands strictly past the boundary
already in the log. That single rule is what makes compaction crash-safe in
both directions, which is the case the ordering below is chosen for:

- A Worker that died after the summary call and before the append left no
  record. The next Worker recomputes the same cut, finds it still ahead of the
  boundary, and summarises again. One model call is paid for twice; nothing is
  compacted twice.
- A Worker that died after the append left the boundary at the cut. The next
  Worker recomputes the same cut -- no turn has started since -- finds it no
  longer ahead of the boundary, and does not summarise at all.

So the record is appended *after* the summary comes back and *before* the next
model call, and a Run that is replayed converges on the same conversation
either way. The alternative ordering, appending first and filling the summary
in later, would leave a boundary in the log with nothing standing in for the
range it replaced, and a replay would send the model a conversation with a hole
in it.

## The summariser is told what it is doing, not who the agent is

The request built here carries its own system message rather than the agent's.
A summariser is doing a bounded, mechanical read of a transcript; handing it the
agent's persona spends tokens on instructions it must not follow and invites it
to answer the conversation instead of summarising it. It is also why
``CompactionPolicy.model`` exists: this call can go to a cheaper model than the
one doing the work.
"""

from __future__ import annotations

from collections.abc import Sequence

from psych_runtime.core.conversation import build_conversation
from psych_runtime.core.messages import Message, SystemMessage, UserMessage
from psych_runtime.core.records import Record, TurnStarted
from psych_runtime.core.spec import AgentSpec, CompactionPolicy
from psych_runtime.model.port import ModelRequest

__all__ = ["plan_cut", "summarisation_request"]


def plan_cut(records: Sequence[Record], boundary: int, keep_recent_turns: int) -> int | None:
    """The sequence to compact up to, or ``None`` when there is nothing to do.

    Args:
        records: this Run's log so far, ascending by seq.
        boundary: ``RunStateView.compaction_boundary_seq``, what an earlier
            compaction already replaced.
        keep_recent_turns: how many turns stay verbatim below the summary.

    Returns:
        The last sequence the summary should replace, strictly greater than
        ``boundary``, always the record immediately before a ``turn_started``.
        ``None`` when the Run has not yet run more turns than the policy keeps,
        or when the cut would not advance the boundary -- which is also how a
        replay after a crash declines to compact a second time.
    """
    turn_seqs = [
        record.seq
        for record in records
        if isinstance(record, TurnStarted) and record.seq > boundary
    ]
    if len(turn_seqs) <= keep_recent_turns:
        return None
    cut = turn_seqs[-keep_recent_turns] - 1
    return cut if cut > boundary else None


_SUMMARY_SYSTEM = (
    "You are compacting a transcript so that another assistant can carry on "
    "the conversation without it. Write a summary of everything below that "
    "the assistant will still need."
)

_SUMMARY_INSTRUCTION = (
    "Summarise the conversation above so it can be replaced by your summary "
    "and the conversation can continue.\n\n"
    "Keep: what the user asked for and any constraints they gave, facts "
    "established by tool results, decisions taken and why, what has already "
    "been done, and anything still outstanding. Preserve identifiers, names, "
    "numbers and quoted text exactly; they cannot be recovered once the "
    "records they came from are out of context.\n\n"
    "Drop: pleasantries, superseded attempts, and detail that changed nothing.\n\n"
    "Write it as notes to the assistant that will read it. Do not address the "
    "user, do not answer the conversation, and do not offer to do anything "
    "next."
)

_EXTRA_INSTRUCTION = (
    "\n\nThis agent has further requirements about what its summary must keep. "
    "They add to the rules above and do not replace them: where they name "
    "something to keep, keep it as well.\n\n"
)
"""Lead-in for ``CompactionPolicy.summary_instructions``.

Placed last, after Psych's own rules, for two reasons that point the same way.
A model weighs the end of a prompt most heavily, so the agent author's
domain-specific requirements are the thing it reads on the way out; and saying
in the same breath that these *add* to the rules above stops a terse instruction
like "just keep the order numbers" being read as permission to drop everything
else. The floor is not negotiable, and the prompt says so rather than hoping.
"""


def summarisation_request(
    spec: AgentSpec,
    policy: CompactionPolicy,
    records: Sequence[Record],
    cut: int,
    *,
    history: Sequence[Message] = (),
) -> ModelRequest:
    """The model call that writes the summary for everything up to ``cut``.

    Built from the same ``build_conversation`` projection the loop sends the
    agent, over the records being replaced, so the summariser reads what the
    model actually saw rather than a second rendering of the log that could
    drift from it. An earlier compaction's own summary is inside that
    projection, which is what keeps a second compaction from losing the first.

    ``policy.summary_instructions``, when set, is appended after Psych's own
    instruction rather than replacing it. See ``_EXTRA_INSTRUCTION``.

    ``history`` is a continued thread's earlier conversation
    (``psych_runtime.runtime.thread.load_thread_history``). It is included so the
    summary is of the conversation as the agent sees it, and it is not itself
    compacted: it belongs to Runs that are already settled, and their logs are
    immutable.

    No tools are offered. A summariser that could call something would be
    taking actions on a conversation nobody is watching.
    """
    replaced = [record for record in records if record.seq <= cut]
    conversation = [*history, *build_conversation(replaced)]
    instruction = _SUMMARY_INSTRUCTION
    extra = (policy.summary_instructions or "").strip()
    if extra:
        instruction = f"{instruction}{_EXTRA_INSTRUCTION}{extra}"
    messages: list[Message] = [
        SystemMessage(content=_SUMMARY_SYSTEM),
        *conversation,
        UserMessage(content=instruction),
    ]
    return ModelRequest(
        model=policy.model or spec.model.model,
        messages=tuple(messages),
        max_output_tokens=policy.max_summary_tokens,
        idle_timeout_seconds=spec.limits.stream_idle_seconds,
    )
