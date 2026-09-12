/**
 * Turns the raw Record logs of a conversation into what the chat view renders.
 *
 * A conversation is a chain of Runs, not one long-lived Run: every message
 * starts a new Run that continues the last. So this folds a *list*
 * of Runs, oldest first, into one timeline. Keys are prefixed with the Run
 * they came from because `seq`, `turn` and even a call id are only unique
 * inside one Run's log, and a thread that keys on `turn` alone collapses turn
 * 1 of every Run onto itself.
 *
 * The fold is over Records rather than the flat `/messages` projection on
 * purpose: `/messages` has no tool arguments, no results and no approvals, so
 * a conversation reopened from history used to lose everything that made it
 * worth reading. `streamRun` replays a settled Run's whole log from seq 0, so
 * there is one source for a live conversation and a finished one.
 *
 * Nothing here calls the network, reads a clock or looks at anything but its
 * argument.
 */

import type {
  ResultAttachment,
  PsychComponent,
  PsychRecord,
  SuspendReason,
  TerminalState,
  ToolFailure,
  ToolOutcome,
} from "@/lib/types";

/** One Run's log, as this module wants it. */
export interface ThreadRun {
  runId: string;
  records: readonly PsychRecord[];
}

export interface ToolCallView {
  callId: string;
  /** The Run this call belongs to, for fetching an output it only names. */
  runId: string;
  tool: string;
  arguments: Record<string, unknown>;
  startedAt: string;
  finishedAt: string | null;
  outcome: ToolOutcome | null;
  result: unknown;
  failure: ToolFailure | null;
  durationSeconds: number | null;
  preview: string | null;
  resultBytes: number;
  /** Outputs kept beside the result (a program's streams and files). */
  attachments: ResultAttachment[];
}

export interface UserMessageItem {
  kind: "user_message";
  key: string;
  runId: string;
  text: string;
  at: string;
}

export interface AssistantTurnItem {
  kind: "assistant_turn";
  key: string;
  runId: string;
  turn: number;
  model: string | null;
  text: string;
  toolCalls: ToolCallView[];
  /** The model call itself failed (a provider error), as opposed to a tool
   * failing. Shown as prose, never as a stack. */
  callFailure: ToolFailure | null;
  willRetry: boolean;
  startedAt: string;
  /** A model call is open: this turn is still being written. */
  pending: boolean;
}

export interface ApprovalItem {
  kind: "approval";
  key: string;
  runId: string;
  reason: SuspendReason;
  question: string | null;
  pendingCallId: string | null;
  suspendedAt: string;
  expiresAt: string;
  /** Null until someone decided. While null this is the live decision the
   * conversation is blocked on. */
  approved: boolean | null;
  decidedAt: string | null;
  decidedBy: string | null;
}

export interface EndedItem {
  kind: "ended";
  key: string;
  runId: string;
  state: TerminalState;
  /** From the Run's own log. The workspace prefers `RunStatus.failure_message`
   * for the live Run, which the library has already written for a reader. */
  failureMessage: string | null;
  at: string;
}

/**
 * Something the agent showed rather than said.
 *
 * Its own item kind rather than a field on the turn that produced it, because
 * a component is addressed to the person and a tool call is addressed to the
 * agent. Folding it into the collapsed work section would hide the one thing
 * in that turn that was meant to be looked at.
 */
export interface ComponentItem {
  kind: "component";
  key: string;
  runId: string;
  component: PsychComponent;
  at: string;
}

export interface NoteItem {
  kind: "note";
  key: string;
  text: string;
  at: string;
}

export type ConversationItem =
  | UserMessageItem
  | AssistantTurnItem
  | ComponentItem
  | ApprovalItem
  | EndedItem
  | NoteItem;

export interface Conversation {
  items: ConversationItem[];
  /** The Version the conversation opened against. The agent is fixed for the
   * life of a thread, so this is what the header names. */
  versionHash: string | null;
  /** True once any Record has arrived, so a caller can tell "nothing yet"
   * from "nothing to show". */
  hasRecords: boolean;
}

const EMPTY: Conversation = {
  items: [],
  versionHash: null,
  hasRecords: false,
};

export function buildConversation(runs: readonly ThreadRun[]): Conversation {
  if (runs.length === 0) return EMPTY;

  const items: ConversationItem[] = [];
  let versionHash: string | null = null;
  let hasRecords = false;

  for (const run of runs) {
    const turns = new Map<number, AssistantTurnItem>();
    const calls = new Map<string, ToolCallView>();
    let openApproval: ApprovalItem | null = null;

    const sorted = [...run.records].sort((a, b) => a.seq - b.seq);
    if (sorted.length > 0) hasRecords = true;

    for (const record of sorted) {
      const key = `${run.runId}:${record.seq}`;

      switch (record.type) {
        case "run_admitted": {
          versionHash ??= record.version_hash;
          const message = record.input["message"];
          items.push({
            kind: "user_message",
            key,
            runId: run.runId,
            text: typeof message === "string" ? message : JSON.stringify(record.input),
            at: record.at,
          });
          break;
        }

        case "attempt_started": {
          // Worth saying out loud: the answer resumed on another machine and
          // may repeat itself slightly. Silence here reads as a glitch.
          if (record.reclaimed_expired_lease) {
            items.push({
              kind: "note",
              key,
              text: "Picked up again after the previous worker stopped responding.",
              at: record.at,
            });
          }
          break;
        }

        case "turn_started": {
          const turn: AssistantTurnItem = {
            kind: "assistant_turn",
            key: `${run.runId}:turn:${record.turn}`,
            runId: run.runId,
            turn: record.turn,
            model: null,
            text: "",
            toolCalls: [],
            callFailure: null,
            willRetry: false,
            startedAt: record.at,
            pending: true,
          };
          turns.set(record.turn, turn);
          items.push(turn);
          break;
        }

        case "model_call_started": {
          const turn = turns.get(record.turn);
          if (turn) {
            turn.model = record.model;
            turn.pending = true;
          }
          break;
        }

        case "model_call_finished": {
          const turn = turns.get(record.turn);
          if (turn) {
            turn.model = record.model;
            turn.text = record.text;
            turn.pending = false;
          }
          break;
        }

        case "model_call_failed": {
          const turn = turns.get(record.turn);
          if (turn) {
            turn.model = record.model;
            turn.callFailure = record.failure;
            turn.willRetry = record.will_retry;
            turn.pending = record.will_retry;
          }
          break;
        }

        case "tool_call_started": {
          const call: ToolCallView = {
            callId: record.call_id,
            runId: run.runId,
            tool: record.tool,
            arguments: record.arguments,
            startedAt: record.at,
            finishedAt: null,
            outcome: null,
            result: null,
            failure: null,
            durationSeconds: null,
            preview: null,
            resultBytes: 0,
            attachments: [],
          };
          calls.set(record.call_id, call);
          const turn = turns.get(record.turn);
          if (turn) turn.toolCalls.push(call);
          break;
        }

        case "tool_call_finished": {
          const call = calls.get(record.call_id);
          if (call) {
            call.outcome = record.outcome;
            call.result = record.result;
            call.failure = record.failure;
            call.durationSeconds = record.duration_seconds;
            call.preview = record.preview;
            call.resultBytes = record.result_bytes;
            call.attachments = record.attachments ?? [];
            call.finishedAt = record.at;
          }
          break;
        }

        case "component_shown": {
          // In log order, which is the order the agent showed them: "here is
          // the flight, then the hotel, then the itinerary" is a sequence, and
          // re-sorting it would be a different answer.
          items.push({
            kind: "component",
            key,
            runId: run.runId,
            component: record.component,
            at: record.at,
          });
          break;
        }

        case "suspended": {
          openApproval = {
            kind: "approval",
            key,
            runId: run.runId,
            reason: record.reason,
            question: record.question,
            pendingCallId: record.pending_call_id,
            suspendedAt: record.at,
            expiresAt: record.expires_at,
            approved: null,
            decidedAt: null,
            decidedBy: null,
          };
          items.push(openApproval);
          break;
        }

        case "resumed": {
          if (openApproval) {
            openApproval.approved = record.approved;
            openApproval.decidedAt = record.at;
            openApproval.decidedBy = record.resumed_by;
            openApproval = null;
          }
          break;
        }

        case "abort_requested": {
          items.push({
            kind: "note",
            key,
            text: record.requested_by
              ? `Stopped by ${record.requested_by}.`
              : "Stop requested.",
            at: record.at,
          });
          break;
        }

        case "compaction_applied": {
          items.push({
            kind: "note",
            key,
            text: "Earlier parts of this conversation were summarised to stay within the model's context.",
            at: record.at,
          });
          break;
        }

        case "run_settled": {
          // "completed" is the expected ending and says nothing a reader does
          // not already see in the answer above it. Only endings that explain
          // a missing or truncated answer earn a line in the chat.
          if (record.state !== "completed") {
            items.push({
              kind: "ended",
              key,
              runId: run.runId,
              state: record.state,
              failureMessage: record.failure?.message ?? null,
              at: record.at,
            });
          }
          break;
        }

        // Steering queues, and the step records a workflow writes, are part
        // of the log rather than part of the conversation. The trace view
        // renders them; a chat surface does not.
        case "step_started":
        case "step_completed":
        case "queue_enqueued":
        case "queue_cancelled":
        case "queue_consumed":
          break;
      }
    }
  }

  return { items, versionHash, hasRecords };
}
