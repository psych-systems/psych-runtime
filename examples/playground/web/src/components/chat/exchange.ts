/**
 * One exchange: what was asked, what came back, and the work in between.
 *
 * `lib/conversation.ts` folds Records into a flat list of items, which is the
 * right shape for a transcript and the wrong shape for reading. A person who
 * asked "where is order A1" wants the sentence that answers them, not six
 * blocks of an agent working. So this regroups that flat list into exchanges,
 * and splits each one the way the library splits it.
 *
 * ## The split is the library's, ported, not a second opinion
 *
 * `psych.answer()` calls the answer "the last model call that finished with
 * nothing left to call" and the work "every turn before it". That is not a
 * heuristic: it is the loop's own success condition, so it cannot drift from
 * what the Run did. The same rule is applied here, over the same Records the
 * stream already delivered, so a live conversation and a settled one are the
 * same rendering rather than two.
 *
 * ## Why the current turn is neither
 *
 * While a Run is live the newest turn is not work and is not the answer: it is
 * what is happening now. Counting it as work would make the collapsed line
 * read "3 turns" and then drop back to "2 turns" the moment that turn turned
 * out to be the answer. A summary that counts down is a summary nobody trusts,
 * so the current turn is held out until the log says which of the two it was.
 *
 * Nothing here calls the network or reads a clock.
 */

import type {
  ApprovalItem,
  AssistantTurnItem,
  ComponentItem,
  Conversation,
  EndedItem,
  NoteItem,
  UserMessageItem,
} from "@/lib/conversation";

export interface Exchange {
  key: string;
  /** The Run this exchange is, or the empty string for the rare group of
   *  items that arrived before any message (a note from a reclaimed lease). */
  runId: string;
  message: UserMessageItem | null;
  /** Every turn before the answer, oldest first. Collapsed behind one line. */
  work: AssistantTurnItem[];
  /** The turn the loop finished on, once there is one. */
  answer: AssistantTurnItem | null;
  /** The turn being written right now, while the Run is still live. Null for
   *  a settled Run, whose turns are all either work or the answer. */
  current: AssistantTurnItem | null;
  approvals: ApprovalItem[];
  /** What the agent showed during this exchange, in the order it showed it. */
  components: ComponentItem[];
  notes: NoteItem[];
  ended: EndedItem | null;
}

/**
 * Regroup a conversation into exchanges.
 *
 * `liveRunId` is the Run that can still write Records. Only that Run gets a
 * `current` turn; every other Run's turns are settled history.
 */
export function buildExchanges(conversation: Conversation, liveRunId: string | null): Exchange[] {
  const exchanges: Exchange[] = [];
  let open: Draft | null = null;

  const flush = () => {
    if (open !== null) exchanges.push(finish(open, liveRunId, exchanges.length));
    open = null;
  };

  for (const item of conversation.items) {
    // A message opens an exchange. Every Run has exactly one, written at
    // admission, so this is the Run boundary as a reader experiences it.
    if (item.kind === "user_message") {
      flush();
      open = {
        runId: item.runId,
        message: item,
        turns: [],
        approvals: [],
        components: [],
        notes: [],
        ended: null,
      };
      continue;
    }
    if (open === null) {
      open = {
        runId: "",
        message: null,
        turns: [],
        approvals: [],
        components: [],
        notes: [],
        ended: null,
      };
    }
    switch (item.kind) {
      case "assistant_turn":
        open.turns.push(item);
        if (open.runId === "") open.runId = item.runId;
        break;
      case "approval":
        open.approvals.push(item);
        break;
      case "component":
        open.components.push(item);
        if (open.runId === "") open.runId = item.runId;
        break;
      case "note":
        open.notes.push(item);
        break;
      case "ended":
        open.ended = item;
        if (open.runId === "") open.runId = item.runId;
        break;
    }
  }
  flush();

  return exchanges;
}

interface Draft {
  runId: string;
  message: UserMessageItem | null;
  turns: AssistantTurnItem[];
  approvals: ApprovalItem[];
  components: ComponentItem[];
  notes: NoteItem[];
  ended: EndedItem | null;
}

function finish(draft: Draft, liveRunId: string | null, index: number): Exchange {
  const { turns } = draft;
  const answerIndex = lastAnswerIndex(turns);
  const live = draft.runId !== "" && draft.runId === liveRunId;

  let work: AssistantTurnItem[];
  let answer: AssistantTurnItem | null = null;
  let current: AssistantTurnItem | null = null;

  if (answerIndex !== -1) {
    answer = turns[answerIndex];
    work = turns.slice(0, answerIndex);
  } else if (live && turns.length > 0) {
    current = turns[turns.length - 1];
    work = turns.slice(0, -1);
  } else {
    // Settled without ever finishing a turn: failed, stopped, or abandoned.
    // Every turn is work, and the reason it has no answer is on the status.
    work = turns;
  }

  return {
    // Keyed by Run: an exchange is a Run, and a key that changed between
    // renders would remount the answer and replay its reveal.
    key: draft.runId !== "" ? draft.runId : (draft.message?.key ?? `group:${index}`),
    runId: draft.runId,
    message: draft.message,
    work: work.filter(carriesSomething),
    answer,
    current,
    approvals: draft.approvals,
    components: draft.components,
    notes: draft.notes,
    ended: draft.ended,
  };
}

/**
 * The turn the loop finished on: the last one that came back with text and
 * nothing left to call.
 *
 * `psych.answer()` scans the log backwards for the last `model_call_finished`
 * with no tool calls. A turn that failed is not that, and a turn still open
 * is not that yet.
 */
function lastAnswerIndex(turns: readonly AssistantTurnItem[]): number {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (!turn.pending && turn.callFailure === null && turn.toolCalls.length === 0) return index;
  }
  return -1;
}

/** A turn that started, ran nothing and said nothing is a turn boundary in the
 *  log rather than a step of work. Counting it inflates the collapsed line
 *  with steps a reader would open the section and not find. */
function carriesSomething(turn: AssistantTurnItem): boolean {
  return turn.text.length > 0 || turn.toolCalls.length > 0 || turn.callFailure !== null;
}

/**
 * The one line the work collapses behind.
 *
 * A port of `AnswerView.summary()`, counting rather than characterising for
 * the same reason: "Looked up the order" would be a guess about what the tools
 * did, and the reader opening the section is about to see the rest anyway.
 * Identical output to the server's, which is what makes swapping in the
 * library's own summary when a Run settles invisible.
 */
export function workSummary(work: readonly AssistantTurnItem[]): string {
  if (work.length === 0) return "";
  const turns = work.length;
  const calls = work.reduce((total, turn) => total + turn.toolCalls.length, 0);
  const parts = [`${turns} turn${turns === 1 ? "" : "s"}`];
  if (calls > 0) parts.push(`${calls} tool call${calls === 1 ? "" : "s"}`);
  return parts.join(", ");
}

/** The distinct tools a stretch of work used, in the order they were first
 *  reached. Shown beside the summary because "2 tool calls" says how much
 *  happened and these say what. */
export function toolsUsed(work: readonly AssistantTurnItem[]): string[] {
  const seen: string[] = [];
  for (const turn of work) {
    for (const call of turn.toolCalls) {
      if (!seen.includes(call.tool)) seen.push(call.tool);
    }
  }
  return seen;
}

/**
 * The work to show for an exchange, including the turn in progress when that
 * turn has already called something.
 *
 * A turn with tool calls can never turn out to be the answer -- the loop
 * finishes on a turn that called nothing -- so counting it the moment its
 * first call starts is safe: the collapsed line only ever counts up. That is
 * what lets the section be open during a live Run and show the call that is
 * running right now, with its arguments, without anything moving when the turn
 * settles and becomes ordinary work.
 */
export function workShown(exchange: Exchange): AssistantTurnItem[] {
  const { current } = exchange;
  if (current === null || current.toolCalls.length === 0) return exchange.work;
  return [...exchange.work, current];
}
