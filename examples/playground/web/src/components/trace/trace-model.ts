/**
 * Turns one `RunReport` into a flat, time-ordered set of trace entries.
 *
 * This is the pure fold the trace view is built from -- no fetching, no
 * `Date.now()`, nothing but arithmetic over the report already in hand, so
 * the waterfall and the row list can both be derived from the same list
 * without re-deriving it twice. Given the same report this always produces
 * the same entries.
 *
 * A `RunReport` carries no wall-clock timing for `steps[]` (`StepReport` has
 * no `started_at`/`finished_at` -- see `psych_runtime/report/model.py`). Rather than
 * leave a step off the timeline entirely, its span is *derived* here from
 * the model calls and tool calls recorded under the same `step_id`, and from
 * a nested child Run's own `admitted_at`/`settled_at` when the step wraps
 * one. When neither exists (a workflow step wrapping nothing that was ever
 * timed) the entry is marked `timed: false` and carries no offset or
 * duration -- shown as "not timed" rather than guessed at zero.
 */

import type { PromptChange } from "@/components/trace/prompt-diff";
import type {
  ModelCallReport,
  RunReport,
  StepReport,
  SuspensionReport,
  ToolCallReport,
} from "@/lib/types";

export type TraceEntryKind = "message" | "model" | "tool" | "suspension" | "step";

/**
 * A coarse status used only to pick a color/badge in the UI. Distinct from
 * `ToolOutcome`/`TerminalState` on the wire because it also has to cover
 * "still going", "the log ends before this settled" and "waiting", which
 * those types don't need to.
 */
export type TraceEntryStatus =
  | "ok"
  | "error"
  | "aborted"
  | "unknown"
  | "pending"
  | "dangling"
  | "waiting"
  | "denied";

interface TraceEntryBase {
  key: string;
  /** The Run this entry belongs to. Stamped by `entriesOf`, so a detail
   *  panel can fetch something the report only names, such as a program's
   *  recorded output by handle. */
  runId?: string;
  turn: number | null;
  stepId: string | null;
  label: string;
  summary: string;
  status: TraceEntryStatus;
  /** Distinct from `status === "dangling"`: this call failed but the
   * transient-failure budget allowed another attempt (DESIGN.md §10.6). */
  willRetry: boolean;
  startedAt: string | null;
  finishedAt: string | null;
  /** False only for a step whose span could not be derived (see module
   * docstring). Every model call, tool call and suspension is always timed. */
  timed: boolean;
  offsetSeconds: number | null;
  durationSeconds: number | null;
  /** True when this entry has no `finishedAt` and the Run has not settled --
   * still in flight as of this report, as opposed to `dangling` (the Attempt
   * that was doing it died and nothing will finish it). */
  ongoing: boolean;
}

/**
 * What one turn was told and what it was offered, relative to the turn
 * before it.
 *
 * Both halves are recorded per turn now (`system_prompt` and `tool_names` on
 * `ModelCallReport`), and the report used to carry one run-wide prompt
 * rebuilt from the agent's definition. That rebuilt text could not contain
 * what the runtime added at assembly time, which is exactly the part that
 * moves between turns, so a person debugging "why did it stop calling the
 * refund tool" had nothing to look at. This carries the comparison so the
 * row list and the detail panel mark the same turns as changed.
 */
export interface TurnContext {
  prompt: string;
  change: PromptChange;
  /** The prompt this one is compared against, or null when there is none. */
  previousPrompt: string | null;
  previousTurn: number | null;
  toolNames: string[];
  /** Empty unless both turns recorded a tool list: an old run records none,
   *  and calling every tool "added" on its first turn would be a lie about a
   *  run that never had the field. */
  toolsAdded: string[];
  toolsRemoved: string[];
}

/** Where one message begins, so a conversation-wide timeline shows the shape
 *  of the exchange rather than an undifferentiated run of calls. Carries no
 *  report of its own: a message is the boundary between Runs, and everything
 *  that happened inside it already has its own entry. */
export interface MessageMark {
  runId: string;
  /** 1-based, in the order they were sent. */
  index: number;
  text: string;
}

export type TraceEntry =
  | (TraceEntryBase & { kind: "message"; data: MessageMark })
  | (TraceEntryBase & { kind: "model"; data: ModelCallReport; context: TurnContext })
  | (TraceEntryBase & { kind: "tool"; data: ToolCallReport })
  | (TraceEntryBase & { kind: "suspension"; data: SuspensionReport })
  | (TraceEntryBase & { kind: "step"; data: StepReport });

export interface TraceModel {
  entries: TraceEntry[];
  /** How many Runs this model spans. 1 for a single message, more for a
   *  conversation. Lets a view label its own scope without counting entries. */
  messageCount: number;
  /** The Run's `admitted_at`, as epoch milliseconds -- offset zero. */
  t0: number;
  /** `totals.latency.wall_clock_seconds`, the authoritative axis width. Not
   * re-derived from entry offsets: a report already computed this once from
   * the full log, including spans (a suspension's wait, say) that don't
   * necessarily correspond to a bar on the timeline. */
  totalSeconds: number;
}

function toEpochMs(iso: string): number {
  return new Date(iso).getTime();
}

function secondsBetween(startIso: string, endIso: string): number {
  return (toEpochMs(endIso) - toEpochMs(startIso)) / 1000;
}

function tokenFlow(usage: ModelCallReport["usage"]): string {
  if (usage === null) return "no usage recorded";
  const parts = [`${usage.input.toLocaleString()} in`, `${usage.output.toLocaleString()} out`];
  if (usage.cache_read > 0) parts.push(`${usage.cache_read.toLocaleString()} cache read`);
  return parts.join(" · ");
}

function modelStatus(call: ModelCallReport): TraceEntryStatus {
  if (call.dangling) return "dangling";
  if (call.failure) return "error";
  if (call.finished_at === null) return "pending";
  return "ok";
}

function toolStatus(call: ToolCallReport): TraceEntryStatus {
  if (call.outcome === null) return "dangling";
  return call.outcome;
}

function stepStatus(step: StepReport): TraceEntryStatus {
  if (!step.completed) return "dangling";
  if (step.failure) return "error";
  return "ok";
}

function suspensionStatus(susp: SuspensionReport): TraceEntryStatus {
  if (susp.resumed_at === null) return "waiting";
  if (susp.reason === "approval" && susp.approved === false) return "denied";
  return "ok";
}

/** The turn-over-turn comparison for one model call. `previous` is the model
 *  call recorded immediately before it, which is the only turn a change is
 *  meaningful against. */
function buildTurnContext(call: ModelCallReport, previous: ModelCallReport | null): TurnContext {
  const prompt = call.system_prompt;
  const previousPrompt =
    previous !== null && previous.system_prompt.length > 0 ? previous.system_prompt : null;
  const change: PromptChange =
    prompt.length === 0
      ? "unrecorded"
      : previousPrompt === null
        ? "first"
        : previousPrompt === prompt
          ? "same"
          : "changed";

  const comparable = previous !== null && previous.tool_names.length > 0 && call.tool_names.length > 0;
  const before = new Set(previous?.tool_names ?? []);
  const after = new Set(call.tool_names);
  return {
    prompt,
    change,
    previousPrompt,
    previousTurn: previousPrompt === null ? null : (previous?.turn ?? null),
    toolNames: call.tool_names,
    toolsAdded: comparable ? call.tool_names.filter((t) => !before.has(t)) : [],
    toolsRemoved: comparable ? (previous?.tool_names ?? []).filter((t) => !after.has(t)) : [],
  };
}

function buildModelEntry(
  call: ModelCallReport,
  previous: ModelCallReport | null,
  index: number,
  t0: number
): TraceEntry {
  const offsetSeconds = (toEpochMs(call.started_at) - t0) / 1000;
  const durationSeconds = call.finished_at ? secondsBetween(call.started_at, call.finished_at) : null;
  const costLabel =
    call.cost === null ? (call.usage === null ? "" : " · cost unknown") : ` · ${call.cost.currency} ${call.cost.amount}`;
  return {
    key: `model:${call.turn}:${index}`,
    kind: "model",
    data: call,
    context: buildTurnContext(call, previous),
    turn: call.turn,
    stepId: call.step_id,
    label: call.model,
    summary: call.failure ? call.failure.message : `${tokenFlow(call.usage)}${costLabel}`,
    status: modelStatus(call),
    willRetry: call.will_retry,
    startedAt: call.started_at,
    finishedAt: call.finished_at,
    timed: true,
    offsetSeconds,
    durationSeconds,
    ongoing: call.finished_at === null && !call.dangling,
  };
}

function buildToolEntry(call: ToolCallReport, t0: number): TraceEntry {
  const offsetSeconds = (toEpochMs(call.started_at) - t0) / 1000;
  const summary =
    call.outcome === null
      ? "started, and no result was ever recorded"
      : call.outcome === "error" && call.failure
        ? call.failure.message
        : call.outcome === "ok"
          ? call.result_bytes > 0
            ? `ok · ${call.result_bytes.toLocaleString()} bytes`
            : "ok"
          : call.outcome;
  return {
    key: `tool:${call.call_id}`,
    kind: "tool",
    data: call,
    turn: call.turn,
    stepId: call.step_id,
    label: call.tool,
    summary,
    status: toolStatus(call),
    willRetry: false,
    startedAt: call.started_at,
    finishedAt: call.finished_at,
    timed: true,
    offsetSeconds,
    durationSeconds: call.duration_seconds,
    ongoing: call.outcome === null,
  };
}

function buildSuspensionEntry(susp: SuspensionReport, index: number, t0: number): TraceEntry {
  const offsetSeconds = (toEpochMs(susp.suspended_at) - t0) / 1000;
  const endIso = susp.resumed_at;
  const durationSeconds = endIso ? secondsBetween(susp.suspended_at, endIso) : null;
  const summary = susp.question
    ? susp.question
    : susp.reason === "approval"
      ? susp.approved === null
        ? "awaiting approval"
        : susp.approved
          ? "approved"
          : "denied"
      : `waiting: ${susp.reason}`;
  return {
    key: `suspension:${index}:${susp.suspended_at}`,
    kind: "suspension",
    data: susp,
    turn: null,
    stepId: null,
    label: `Suspended (${susp.reason})`,
    summary,
    status: suspensionStatus(susp),
    willRetry: false,
    startedAt: susp.suspended_at,
    finishedAt: susp.resumed_at,
    timed: true,
    offsetSeconds,
    durationSeconds,
    ongoing: susp.resumed_at === null,
  };
}

/** Derives a step's span from the calls it wraps and its child Run's own
 * bounds, per the module docstring. Returns `null` when nothing timed could
 * be found for it. */
function deriveStepSpan(
  step: StepReport,
  report: RunReport
): { startedAt: string; finishedAt: string | null } | null {
  const candidates: { startedAt: string; finishedAt: string | null }[] = [];

  for (const call of report.model_calls) {
    if (call.step_id === step.step_id) {
      candidates.push({ startedAt: call.started_at, finishedAt: call.finished_at });
    }
  }
  for (const call of report.tool_calls) {
    if (call.step_id === step.step_id) {
      candidates.push({ startedAt: call.started_at, finishedAt: call.finished_at });
    }
  }
  if (step.child) {
    candidates.push({ startedAt: step.child.admitted_at, finishedAt: step.child.settled_at });
  }

  if (candidates.length === 0) return null;

  let startedAt = candidates[0].startedAt;
  let finishedAt: string | null = candidates[0].finishedAt;
  let anyOpen = finishedAt === null;
  for (const c of candidates.slice(1)) {
    if (toEpochMs(c.startedAt) < toEpochMs(startedAt)) startedAt = c.startedAt;
    if (c.finishedAt === null) {
      anyOpen = true;
    } else if (finishedAt === null || toEpochMs(c.finishedAt) > toEpochMs(finishedAt)) {
      finishedAt = c.finishedAt;
    }
  }
  return { startedAt, finishedAt: anyOpen ? null : finishedAt };
}

function buildStepEntry(step: StepReport, index: number, report: RunReport, t0: number): TraceEntry {
  const span = deriveStepSpan(step, report);
  const summary = [
    step.kind,
    step.completed ? `attempt ${step.attempt_number}` : "incomplete",
    step.child_run_id ? "delegated" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return {
    key: `step:${step.step_id}:${step.attempt_number}:${index}`,
    kind: "step",
    data: step,
    turn: null,
    stepId: step.step_id,
    label: step.name,
    summary: step.failure ? step.failure.message : summary,
    status: stepStatus(step),
    willRetry: false,
    startedAt: span?.startedAt ?? null,
    finishedAt: span?.finishedAt ?? null,
    timed: span !== null,
    offsetSeconds: span ? (toEpochMs(span.startedAt) - t0) / 1000 : null,
    durationSeconds: span?.finishedAt ? secondsBetween(span.startedAt, span.finishedAt) : null,
    ongoing: span !== null && span.finishedAt === null,
  };
}

/** Every entry one report contributes, offset against a caller-supplied `t0`.
 *
 * `t0` is a parameter rather than the report's own `admitted_at` so a
 * conversation can lay several reports on one axis: each Run's offsets are
 * then measured from the conversation's start rather than from its own, which
 * is what puts the second message's calls after the first message's on the
 * same picture.
 */
function entriesOf(report: RunReport, t0: number, keyPrefix: string): TraceEntry[] {
  const prefixed = (entry: TraceEntry): TraceEntry => ({
    ...entry,
    key: keyPrefix + entry.key,
    runId: report.run_id,
  });
  return [
    ...report.model_calls.map((call, i) =>
      prefixed(buildModelEntry(call, i === 0 ? null : report.model_calls[i - 1], i, t0))
    ),
    ...report.tool_calls.map((call) => prefixed(buildToolEntry(call, t0))),
    ...report.suspensions.map((susp, i) => prefixed(buildSuspensionEntry(susp, i, t0))),
    ...report.steps.map((step, i) => prefixed(buildStepEntry(step, i, report, t0))),
  ];
}

/** Time order, with untimed steps trailing (there is no offset to sort them
 *  by) but stable relative to each other. */
function inTimeOrder(entries: TraceEntry[]): TraceEntry[] {
  return [...entries].sort((a, b) => {
    if (a.offsetSeconds === null && b.offsetSeconds === null) return 0;
    if (a.offsetSeconds === null) return 1;
    if (b.offsetSeconds === null) return -1;
    return a.offsetSeconds - b.offsetSeconds;
  });
}

export function buildTraceModel(report: RunReport): TraceModel {
  const t0 = toEpochMs(report.admitted_at);
  return {
    entries: inTimeOrder(entriesOf(report, t0, "")),
    t0,
    messageCount: 1,
    totalSeconds: report.totals.latency.wall_clock_seconds,
  };
}

/**
 * One conversation on one axis.
 *
 * A conversation is a chain of Runs: each message continues the previous one
 * rather than extending it. Laying them on a shared axis is what
 * lets somebody see the shape of an exchange rather than one message's share
 * of it.
 *
 * ## What is safe to combine here, and what is not
 *
 * `psych.report()` is built over exactly one Run, and some of its numbers are
 * per-Run quantities that mean nothing added up: a turn budget applies to one
 * Run, so a summed turn count is a number no `Limits` ever governed. Those
 * stay on the per-message view, which is why that view is kept rather than
 * replaced.
 *
 * Tokens and cost are not like that. They are quantities somebody is billed
 * for, and the bill spans the conversation, so summing them is exactly right.
 * The backend sums those (`GET /api/threads/{id}/report`), once, rather than
 * this file adding money in a second place.
 *
 * ## The axis includes the gaps
 *
 * `totalSeconds` here spans the whole conversation, so the quiet stretches
 * where a person was reading and typing appear as quiet stretches. That is a
 * true picture of an exchange and a false picture of machine time, so the
 * summary beside it names model and tool seconds separately rather than
 * letting the axis width stand in for work done.
 */
export function buildThreadTraceModel(
  reports: RunReport[],
  messages: { runId: string; text: string }[],
  totalSeconds: number
): TraceModel {
  if (reports.length === 0) {
    return { entries: [], t0: 0, messageCount: 0, totalSeconds: 0 };
  }
  const t0 = Math.min(...reports.map((report) => toEpochMs(report.admitted_at)));
  const textByRun = new Map(messages.map((message) => [message.runId, message.text]));

  const entries: TraceEntry[] = [];
  reports.forEach((report, index) => {
    const runId = report.run_id;
    // The mark sits at the Run's admission, which is when the message was
    // accepted. Zero duration on purpose: a message is an instant in the
    // conversation, and giving it a width would claim it took time that
    // belongs to the calls underneath it.
    const startedAt = report.admitted_at;
    entries.push({
      kind: "message",
      key: `message:${runId}`,
      runId,
      turn: null,
      stepId: null,
      label: `Message ${index + 1}`,
      summary: textByRun.get(runId) ?? "",
      status: statusOfRun(report),
      willRetry: false,
      startedAt,
      finishedAt: report.settled_at,
      timed: true,
      offsetSeconds: (toEpochMs(startedAt) - t0) / 1000,
      durationSeconds:
        report.settled_at === null ? null : secondsBetween(startedAt, report.settled_at),
      ongoing: report.settled_at === null,
      data: { runId, index: index + 1, text: textByRun.get(runId) ?? "" },
    });
    // Keyed by Run, because two Runs each have a "turn 1" model call and an
    // unprefixed key would make React treat them as the same row.
    entries.push(...entriesOf(report, t0, `${runId}:`));
  });

  return { entries: inTimeOrder(entries), t0, messageCount: reports.length, totalSeconds };
}

function statusOfRun(report: RunReport): TraceEntryStatus {
  if (report.terminal_state === null) return "pending";
  if (report.terminal_state === "completed") return "ok";
  if (report.terminal_state === "aborted") return "aborted";
  return "error";
}
