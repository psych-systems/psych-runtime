/**
 * One Run, as its log, with the branch a person decides.
 *
 * A support agent looks up an order, loads the refund policy, and is stopped
 * before the destructive `issue_refund` call. What happens next depends on
 * whether the approval is granted, so the fixture holds both tails.
 *
 * ## Where the records come from
 *
 * Nothing here is typed from memory. `scripts/generate_site_fixtures.py` in the
 * repository executes the Run against `FakeModel` twice, once approving and once
 * denying, across two Workers over one store, and writes the logs it read back
 * with `psych_runtime.records()` to `content/site/approval-run.json` beside the
 * library's own `report()` totals. The gate regenerates that file and fails on
 * a diff, so a renamed field or a reordered record shows up as a red build, not
 * as a demo quietly describing last month's protocol.
 *
 * This module turns those records into rows, and folds them into the state the
 * panel shows. The fold reads the records' own fields (usage, cost, outcomes,
 * the pending call) rather than a script, and at load time it is checked
 * against the report the library produced for the same log: if the two ever
 * disagree, the build fails here.
 *
 * The token counts are the one scripted thing in the fixture, since the fake
 * model reads no prompt. The cost is not scripted: the runtime priced each call
 * from `DEFAULT_PRICES` for gpt-4o-mini as it recorded it.
 *
 * One thing that is easy to draw wrongly, and the log settles: `tool_call_started`
 * is written **before** `suspended`. The call is opened, the approval gate stops
 * it, and the same `call_id` is settled by `tool_call_finished` in the *next*
 * Attempt. A denial settles it as an error whose `failure.kind` is `denied`, and
 * the Run carries on and answers.
 */
import fixture from "@/content/site/approval-run.json";

export type Branch = "approved" | "denied";
export type Tone = "run" | "model" | "tool" | "wait" | "end";

/** A record as the store held it, minus the wall-clock fields the generator normalised. */
export type RawRecord = { seq: number; type: string } & Record<string, unknown>;

export type ScenarioRecord = {
  seq: number;
  type: string;
  tone: Tone;
  /** The fields the row shows, in the record's own names. */
  detail: string;
  /**
   * Which tool this row is about, when the record itself does not say.
   *
   * A `tool_call_finished` carries a `call_id` and no tool name: the pair is
   * resolved by reading back to the matching `tool_call_started`, which is
   * exactly what `report()` does to fill in `ToolCallReport.tool`. Kept out of
   * `detail` so the raw view stays raw.
   */
  tool?: string;
  /** What just happened, in a sentence, for the panel beside the log. */
  note: string;
  raw: RawRecord;
};

export const SCENARIO_INPUT: string = fixture.input;
export const SCENARIO_MODEL: string = fixture.model;
export const SCENARIO_APPROVAL_SELECTORS: readonly string[] = fixture.approval_selectors;
export const SCENARIO_RUNTIME_VERSION: string = fixture.psych_runtime;

type Usage = { input: number; output: number; cache_read: number };
type Cost = { amount: string; currency: string } | null;

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}
function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}
function obj(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}
function usageOf(v: unknown): Usage {
  const u = obj(v);
  return { input: num(u.input), output: num(u.output), cache_read: num(u.cache_read) };
}
function costOf(v: unknown): Cost {
  const c = obj(v);
  return typeof c.amount === "string" && typeof c.currency === "string"
    ? { amount: c.amount, currency: c.currency }
    : null;
}
function short(hash: string): string {
  const hex = hash.replace(/^sha256:/, "");
  return `${hex.slice(0, 4)}…${hex.slice(-4)}`;
}
function json(v: unknown): string {
  return JSON.stringify(v);
}

/** Money as the price table records it: eight decimals, never rounded away. */
export function usd(amount: string): string {
  return `$${amount}`;
}

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

function toneOf(type: string): Tone {
  if (type.startsWith("model_")) return "model";
  if (type.startsWith("tool_")) return "tool";
  if (type === "suspended" || type === "resumed") return "wait";
  if (type === "run_settled") return "end";
  return "run";
}

/**
 * The fields worth reading at a glance, in the record's own names and order.
 * Every value comes from the record; the raw view shows the rest.
 */
function detailOf(r: RawRecord, toolFor: (callId: string) => string | undefined): string {
  switch (r.type) {
    case "run_admitted":
      return `version_hash=${short(str(r.version_hash))} input=${json(r.input)} deadline_at=${str(r.deadline_at)}`;
    case "attempt_started":
      return `worker_id=${str(r.worker_id)} attempt_number=${num(r.attempt_number)} reclaimed_expired_lease=${r.reclaimed_expired_lease === true}`;
    case "turn_started":
      return `turn=${num(r.turn)}`;
    case "model_call_started":
      return `model=${str(r.model)} tool_names=[${(Array.isArray(r.tool_names) ? r.tool_names : []).join(", ")}]`;
    case "model_call_finished": {
      const u = usageOf(r.usage);
      const c = costOf(r.cost);
      const text = str(r.text);
      return [
        `finish_reason=${str(r.finish_reason)}`,
        text ? `text=${json(text)}` : null,
        `usage={input: ${u.input}, output: ${u.output}, cache_read: ${u.cache_read}}`,
        `cost=${c ? usd(c.amount) : "None"}`,
      ]
        .filter(Boolean)
        .join(" ");
    }
    case "tool_call_started":
      return `tool=${str(r.tool)} arguments=${json(r.arguments)} interruptible=${r.interruptible !== false}`;
    case "tool_call_finished": {
      const failure = obj(r.failure);
      return [
        `call_id=${str(r.call_id)}`,
        `outcome=${str(r.outcome)}`,
        r.result !== null && r.result !== undefined ? `result=${json(r.result)}` : null,
        failure.kind ? `failure.kind=${str(failure.kind)}` : null,
      ]
        .filter(Boolean)
        .join(" ");
    }
    case "suspended":
      return `reason=${str(r.reason)} pending_call_id=${str(r.pending_call_id)} expires_at=${str(r.expires_at)}`;
    case "resumed":
      return `approved=${r.approved === true} resumed_by=${json(r.resumed_by)}`;
    case "run_settled":
      return `state=${str(r.state)}`;
    default:
      void toolFor;
      return "";
  }
}

/**
 * What each record means, one sentence per row, keyed by the record it
 * annotates. `expect` is asserted against the fixture below: a note about a
 * record that moved would otherwise sit under the wrong row and mislead.
 */
type Note = { expect: string; note: string };

const SHARED_NOTES: Record<number, Note> = {
  1: {
    expect: "run_admitted",
    note: "Record 1 of every Run. It pins the Version for the Run's whole life, so editing the agent later never changes a Run already in flight. The Run is queued: it has a deadline but no Worker yet.",
  },
  2: {
    expect: "attempt_started",
    note: "A Worker claimed the lease in one conditional write. From here only this Attempt may append; a second writer would conflict and abort rather than interleave.",
  },
  3: {
    expect: "turn_started",
    note: "Tools are resolved at the start of every turn, never at boot. What the tenant may reach right now is what the model is told it can call.",
  },
  4: {
    expect: "model_call_started",
    note: "Written before the call goes out, so a crash mid-call leaves a visibly incomplete pair rather than no evidence. The prompt carries the instructions, the skill index and the five tools resolved for this turn: two from the Spec, three built in.",
  },
  5: {
    expect: "model_call_finished",
    note: "Usage is recorded per call and split by cache state. The cost is computed from the price table in force at the time and written into the record, so a later price change never rewrites history.",
  },
  6: {
    expect: "tool_call_started",
    note: "A code tool: a Python function the consumer registered. The Spec names it and never held the function itself.",
  },
  7: {
    expect: "tool_call_finished",
    note: "The result is in the log. Had the Worker died between records 6 and 7, the next Attempt would find an open call and settle it as unknown rather than assume it succeeded.",
  },
  8: { expect: "turn_started", note: "A turn that made tool calls keeps the loop going. Limits.max_turns caps how many." },
  9: { expect: "model_call_started", note: "" },
  10: {
    expect: "model_call_finished",
    note: "Most of the prompt came from the provider's cache this time, billed at a different rate. That is why the split exists rather than one input number.",
  },
  11: {
    expect: "tool_call_started",
    note: "A skill's description sits in every prompt; its body loads only when the model asks for it. A long policy is not billed on every turn.",
  },
  12: { expect: "tool_call_finished", note: "The policy text is in the conversation for the turns that follow." },
  13: { expect: "turn_started", note: "" },
  14: { expect: "model_call_started", note: "" },
  15: {
    expect: "model_call_finished",
    note: "The model decides to refund. issue_refund is annotated destructive, and this Runtime's approval selectors include @destructive.",
  },
  16: {
    expect: "tool_call_started",
    note: "The call is opened before the gate runs, which is why this record comes first. Not interruptible: an interrupt arriving during the call waits rather than leaving a half-issued refund.",
  },
  17: {
    expect: "suspended",
    note: "The gate stopped the call. The Run releases its lease and persists; no thread blocks. The process that approves need not be the one that asked, and a UI reads status().pending_approval to show what is waiting.",
  },
};

const APPROVED_NOTES: Record<number, Note> = {
  18: {
    expect: "resumed",
    note: 'psych_runtime.resume(store, run_id, approved=True, by="manager-7"). The decision and who made it are both in the log, because an approval whose record cannot say who approved it is not an audit trail.',
  },
  19: {
    expect: "attempt_started",
    note: "A different Worker picks the Run up. It replays records 1 to 18 through the pure reducer and continues from exactly where the first Attempt stopped.",
  },
  20: {
    expect: "tool_call_finished",
    note: "The same call_id opened at record 16, settled by the second Attempt. One call, one pair of records, across two processes and a wait of any length.",
  },
  21: { expect: "turn_started", note: "" },
  22: { expect: "model_call_started", note: "" },
  23: { expect: "model_call_finished", note: "" },
  24: {
    expect: "tool_call_started",
    note: "The remember built-in writes to the MemoryStore under (tenant, end_user_id). The fact outlives this Run and is recalled into the next one for the same person.",
  },
  25: { expect: "tool_call_finished", note: "" },
  26: { expect: "turn_started", note: "" },
  27: { expect: "model_call_started", note: "" },
  28: {
    expect: "model_call_finished",
    note: "A turn with no tool calls ends the loop. answer() picks this turn as the conclusion and everything before it as the work.",
  },
  29: {
    expect: "run_settled",
    note: "The terminal record. Nothing follows it and the reducer refuses anything that tries. report() is a fold over these 29 records, recomputed on every read and never stored twice.",
  },
};

const DENIED_NOTES: Record<number, Note> = {
  18: {
    expect: "resumed",
    note: 'psych_runtime.resume(store, run_id, approved=False, by="manager-7"). A refusal is a record like any other, and it names who refused.',
  },
  19: {
    expect: "attempt_started",
    note: "A second Attempt starts either way. Replaying the log tells it the call was refused before it decides what to do next.",
  },
  20: {
    expect: "tool_call_finished",
    note: "The call opened at record 16 is settled as an error the model can read, and the message says not to try again. No refund was issued and nothing was rolled back, because nothing ran.",
  },
  21: { expect: "turn_started", note: "" },
  22: { expect: "model_call_started", note: "" },
  23: {
    expect: "model_call_finished",
    note: "The model was told, and answered anyway. A denied call is not a failed Run: the failure-streak guard does not count it, and the customer gets a reply rather than an error page.",
  },
  24: {
    expect: "run_settled",
    note: "Completed, not failed. The Run did everything it was allowed to do, and said so.",
  },
};

function rows(raw: RawRecord[], notes: Record<number, Note>): ScenarioRecord[] {
  const toolByCall = new Map<string, string>();
  for (const r of raw) {
    if (r.type === "tool_call_started") toolByCall.set(str(r.call_id), str(r.tool));
  }
  const toolFor = (id: string) => toolByCall.get(id);
  return raw.map((r) => {
    const note = notes[r.seq];
    if (!note) throw new Error(`scenario: record ${r.seq} (${r.type}) has no note`);
    if (note.expect !== r.type) {
      throw new Error(
        `scenario: record ${r.seq} is ${r.type} in the fixture but the note expects ${note.expect}. ` +
          "Regenerate the fixture and move the notes to the records they describe.",
      );
    }
    return {
      seq: r.seq,
      type: r.type,
      tone: toneOf(r.type),
      detail: detailOf(r, toolFor),
      tool: r.type === "tool_call_finished" ? toolFor(str(r.call_id)) : undefined,
      note: note.note,
      raw: r,
    };
  });
}

const RAW = {
  approved: fixture.branches.approved.records as RawRecord[],
  denied: fixture.branches.denied.records as RawRecord[],
};

/** The record that suspends the Run. Stepping stops here until somebody decides. */
export const SUSPEND_SEQ: number = (() => {
  const seq = RAW.approved.find((r) => r.type === "suspended")?.seq;
  if (!seq) throw new Error("scenario: the fixture has no suspended record");
  for (let i = 0; i < seq; i++) {
    if (RAW.approved[i].type !== RAW.denied[i].type) {
      throw new Error(`scenario: the two branches diverge before the suspension, at record ${i + 1}`);
    }
  }
  return seq;
})();

const BRANCHES: Record<Branch, ScenarioRecord[]> = {
  approved: rows(RAW.approved, { ...SHARED_NOTES, ...APPROVED_NOTES }),
  denied: rows(RAW.denied, { ...SHARED_NOTES, ...DENIED_NOTES }),
};

export function scenario(branch: Branch): ScenarioRecord[] {
  return BRANCHES[branch];
}

/** Before a decision, the only log there is is the shared prologue. */
export const PROLOGUE: ScenarioRecord[] = BRANCHES.approved.slice(0, SUSPEND_SEQ);

/** The record that opened the call the approval is about. */
export const OPEN_SEQ: number = (() => {
  const suspended = RAW.approved[SUSPEND_SEQ - 1];
  const opened = RAW.approved.find(
    (r) => r.type === "tool_call_started" && r.call_id === suspended.pending_call_id,
  );
  if (!opened) throw new Error("scenario: the suspension names a call the log never opened");
  return opened.seq;
})();

/** The record that settles that same call, in either branch. */
export const SETTLE_SEQ: number = (() => {
  const suspended = RAW.approved[SUSPEND_SEQ - 1];
  const settled = RAW.approved.find(
    (r) => r.type === "tool_call_finished" && r.call_id === suspended.pending_call_id,
  );
  if (!settled) throw new Error("scenario: the pending call is never settled");
  return settled.seq;
})();

/** Somewhere worth jumping to, by sequence number. */
export const MILESTONES = [
  { seq: RAW.approved.find((r) => r.type === "tool_call_started")?.seq ?? 1, label: "Tool call" },
  { seq: SUSPEND_SEQ, label: "Approval" },
  { seq: SUSPEND_SEQ + 1, label: "Decision" },
  { seq: SETTLE_SEQ, label: "Outcome" },
] as const;

// ---------------------------------------------------------------------------
// The fold
// ---------------------------------------------------------------------------

/** The derived view: what status() and report() would say after `upTo` records. */
export type Derived = {
  lifecycle: "queued" | "running" | "waiting" | "done";
  attempt: number;
  worker: string | null;
  turn: number;
  usage: Usage;
  /** Summed from each call's recorded cost, in the record's currency. Null until a priced call exists. */
  cost: { amount: string; currency: string } | null;
  /** True once any model call was recorded with no price: the total is then a floor, not a figure. */
  unpricedCalls: number;
  toolCalls: { callId: string; tool: string; outcome: string; failureKind: string | null }[];
  /** What lookup_order returned, once it has: the facts the decision is about. */
  order: Record<string, unknown> | null;
  pendingApproval: { callId: string; tool: string; arguments: Record<string, unknown> } | null;
  decision: Branch | null;
  resumedBy: string | null;
  answer: string | null;
  terminal: string | null;
};

/** Decimal strings summed exactly, in units of 1e-8, so eight-decimal prices never pick up float noise. */
function addAmounts(a: string, b: string): string {
  const scale = (s: string) => {
    const [whole, frac = ""] = s.split(".");
    return BigInt(whole + frac.padEnd(8, "0").slice(0, 8));
  };
  const sum = scale(a) + scale(b);
  const digits = sum.toString().padStart(9, "0");
  return `${digits.slice(0, -8)}.${digits.slice(-8)}`;
}

export function fold(records: ScenarioRecord[], upTo: number): Derived {
  const state: Derived = {
    lifecycle: "queued",
    attempt: 0,
    worker: null,
    turn: 0,
    usage: { input: 0, output: 0, cache_read: 0 },
    cost: null,
    unpricedCalls: 0,
    toolCalls: [],
    order: null,
    pendingApproval: null,
    decision: null,
    resumedBy: null,
    answer: null,
    terminal: null,
  };
  const open = new Map<string, { tool: string; arguments: Record<string, unknown> }>();

  for (const { raw: r } of records.slice(0, upTo)) {
    switch (r.type) {
      case "run_admitted":
        state.lifecycle = "queued";
        break;
      case "attempt_started":
        state.lifecycle = "running";
        state.attempt = num(r.attempt_number);
        state.worker = str(r.worker_id);
        break;
      case "turn_started":
        state.turn = num(r.turn);
        break;
      case "model_call_finished": {
        const u = usageOf(r.usage);
        state.usage.input += u.input;
        state.usage.output += u.output;
        state.usage.cache_read += u.cache_read;
        const c = costOf(r.cost);
        if (c === null) state.unpricedCalls += 1;
        else if (state.cost === null) state.cost = { ...c };
        else if (state.cost.currency === c.currency) {
          state.cost = { amount: addAmounts(state.cost.amount, c.amount), currency: c.currency };
        } else {
          throw new Error("scenario: model calls priced in two currencies cannot be summed");
        }
        if (str(r.finish_reason) === "stop" && str(r.text)) state.answer = str(r.text);
        break;
      }
      case "tool_call_started":
        open.set(str(r.call_id), { tool: str(r.tool), arguments: obj(r.arguments) });
        break;
      case "tool_call_finished": {
        const id = str(r.call_id);
        const call = open.get(id);
        const failure = obj(r.failure);
        state.toolCalls.push({
          callId: id,
          tool: call?.tool ?? id,
          outcome: str(r.outcome),
          failureKind: failure.kind ? str(failure.kind) : null,
        });
        if (call?.tool === "lookup_order" && str(r.outcome) === "ok") state.order = obj(r.result);
        break;
      }
      case "suspended": {
        state.lifecycle = "waiting";
        const id = str(r.pending_call_id);
        const call = open.get(id);
        state.pendingApproval = call ? { callId: id, ...call } : null;
        break;
      }
      case "resumed":
        state.pendingApproval = null;
        state.decision = r.approved === true ? "approved" : "denied";
        state.resumedBy = str(r.resumed_by) || null;
        break;
      case "run_settled":
        state.lifecycle = "done";
        state.terminal = str(r.state);
        break;
    }
  }
  return state;
}

// ---------------------------------------------------------------------------
// The fold must agree with the library's report for the same log.
// ---------------------------------------------------------------------------

for (const branch of ["approved", "denied"] as const) {
  const derived = fold(BRANCHES[branch], BRANCHES[branch].length);
  const report = fixture.branches[branch].report;
  const problems: string[] = [];
  if (derived.terminal !== report.terminal_state) problems.push("terminal_state");
  if (
    derived.usage.input !== report.usage.input ||
    derived.usage.output !== report.usage.output ||
    derived.usage.cache_read !== report.usage.cache_read
  )
    problems.push("usage");
  if ((derived.cost?.amount ?? null) !== (report.cost?.amount ?? null)) problems.push("cost");
  if (derived.answer !== report.answer) problems.push("answer");
  const calls = report.tool_calls.map((c) => `${c.tool}:${c.outcome}`).join(",");
  if (derived.toolCalls.map((c) => `${c.tool}:${c.outcome}`).join(",") !== calls) problems.push("tool_calls");
  if (problems.length) {
    throw new Error(
      `scenario: the page's fold disagrees with psych_runtime.report() on the ${branch} branch: ${problems.join(", ")}`,
    );
  }
}
