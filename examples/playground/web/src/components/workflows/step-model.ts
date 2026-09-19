/**
 * The vocabulary of a workflow step, in one place.
 *
 * The definition is a tree now rather than a list: a `parallel` holds
 * branches, a `branch` holds arms, a `foreach` and a `loop` hold a body. Three
 * surfaces read that tree -- the editor, the definition graph and the run
 * graph -- and each one used to be free to invent its own idea of what a step
 * is called, what an empty one looks like and which children it has. This is
 * that idea, once, so a kind added here shows up in all three.
 */

import type {
  BranchStep,
  Condition,
  ForeachStep,
  LoopStep,
  Mapping,
  MappingValue,
  ParallelStep,
  RetryPolicy,
  Step,
  StepKind,
  ValuePath,
} from "@/lib/types";

export const STEP_KINDS: StepKind[] = [
  "tool",
  "agent",
  "workflow",
  "parallel",
  "branch",
  "foreach",
  "loop",
  "map",
  "set_state",
  "sleep",
  "wait",
  "human",
];

/** The three a linear workflow is made of. They keep their own buttons so
 *  adding one stays a single click, as it was before control flow existed. */
export const SIMPLE_KINDS: StepKind[] = ["tool", "agent", "workflow"];

/** The nine that shape the run rather than do work in it. */
export const CONTROL_KINDS: StepKind[] = STEP_KINDS.filter(
  (kind) => !SIMPLE_KINDS.includes(kind)
);

export const STEP_LABELS: Record<string, string> = {
  tool: "Tool",
  agent: "Agent",
  workflow: "Workflow",
  parallel: "In parallel",
  branch: "Branch",
  foreach: "For each",
  loop: "Loop",
  map: "Map",
  set_state: "Set state",
  sleep: "Sleep",
  wait: "Wait for event",
  human: "Ask a person",
};

export const STEP_BLURBS: Record<string, string> = {
  tool: "Runs one tool with the arguments written here.",
  agent: "Runs one of your agents and keeps what it answered.",
  workflow: "Runs another workflow inside this one.",
  parallel: "Runs every branch at once and waits for them.",
  branch: "Takes the arm whose test passes.",
  foreach: "Runs the body once per item of a list.",
  loop: "Runs the body again until a test says stop.",
  map: "Reshapes values into a new object, running nothing.",
  set_state: "Writes into the shared state the later steps read.",
  sleep: "Waits for a while, or until a time in the run.",
  wait: "Parks until an event with this name arrives.",
  human: "Parks until a person answers.",
};

export function stepLabel(kind: string): string {
  return STEP_LABELS[kind] ?? kind;
}

// ---------------------------------------------------------------------------
// Empty values
// ---------------------------------------------------------------------------

export function emptyCondition(): Condition {
  return { path: "", op: "truthy", value: null };
}

export function emptyPath(): ValuePath {
  return { kind: "path", path: "" };
}

const COMMON = {
  description: "",
  when: null,
  retry: null,
  timeout_seconds: null,
  on_failure: "fail",
  output_schema: null,
} as const;

/** A step of this kind with nothing filled in yet. Every field the backend
 *  expects is present, so a step added and left alone still publishes. */
export function emptyStep(kind: StepKind, name = ""): Step {
  switch (kind) {
    case "tool":
      return { ...COMMON, kind, name, tool: "", arguments: {}, arguments_from: {} };
    case "agent":
      return { ...COMMON, kind, name, agent_id: "", input: {} };
    case "workflow":
      return { ...COMMON, kind, name, workflow_id: "", input: {} };
    case "parallel":
      return { ...COMMON, kind, name, branches: [], on_branch_failure: "fail_fast" };
    case "branch":
      return { ...COMMON, kind, name, cases: [], otherwise: null, mode: "first" };
    case "foreach":
      return {
        ...COMMON,
        kind,
        name,
        items: emptyPath(),
        body: emptyStep("tool", ""),
        concurrency: 1,
        on_item_failure: "fail_fast",
      };
    case "loop":
      return {
        ...COMMON,
        kind,
        name,
        body: emptyStep("tool", ""),
        until: null,
        while: null,
        max_iterations: 10,
      };
    case "map":
      return { ...COMMON, kind, name, output: {} };
    case "set_state":
      return { ...COMMON, kind, name, values: {} };
    case "sleep":
      return { ...COMMON, kind, name, seconds: 1, until: null };
    case "wait":
      return { ...COMMON, kind, name, event: "", payload_schema: {}, timeout_seconds: null };
    case "human":
      return { ...COMMON, kind, name, prompt: "", questions: [], expires_seconds: null };
  }
}

const RETRY_DEFAULTS: RetryPolicy = {
  max_attempts: 1,
  backoff_seconds: 0,
  multiplier: 2,
  max_backoff_seconds: 3600,
  retry_on: [],
};

/**
 * A step as pasted or loaded, with every field the editors read present.
 *
 * JSON typed by hand leaves out what it does not care about (`retry_on`,
 * `description`, a loop's `while`), and a definition written before a field
 * existed leaves it out too. The backend defaults those; the editors must
 * not crash before the backend gets a chance to. Unknown kinds pass through
 * untouched so the backend, not this form, is what refuses them.
 */
export function normalizeStep(step: Step): Step {
  const kind = step.kind as StepKind;
  if (!STEP_KINDS.includes(kind)) return step;
  const base = emptyStep(kind, step.name);
  const merged = { ...base, ...step } as Step;
  const retry = merged.retry ? { ...RETRY_DEFAULTS, ...merged.retry } : null;
  switch (merged.kind) {
    case "parallel":
      return { ...merged, retry, branches: (merged.branches ?? []).map(normalizeStep) };
    case "branch":
      return {
        ...merged,
        retry,
        cases: (merged.cases ?? []).map((one) => ({ ...one, step: normalizeStep(one.step) })),
        otherwise: merged.otherwise ? normalizeStep(merged.otherwise) : null,
      };
    case "foreach":
    case "loop":
      return { ...merged, retry, body: normalizeStep(merged.body ?? emptyStep("tool", "")) };
    default:
      return { ...merged, retry };
  }
}

export function normalizeRetry(retry: RetryPolicy | null | undefined): RetryPolicy | null {
  return retry ? { ...RETRY_DEFAULTS, ...retry } : null;
}

/**
 * The same step under a different kind, keeping what still applies.
 *
 * Only the name and the shared options carry over. Nothing else could: a
 * `tool`'s arguments mean nothing to a `sleep`, and guessing a translation
 * would publish a definition nobody wrote.
 */
export function changeKind(step: Step, kind: StepKind): Step {
  if (step.kind === kind) return step;
  const next = emptyStep(kind, step.name);
  return {
    ...next,
    description: step.description,
    when: step.when,
    retry: step.retry,
    timeout_seconds: step.timeout_seconds,
    on_failure: step.on_failure,
    output_schema: step.output_schema,
  } as Step;
}

// ---------------------------------------------------------------------------
// Walking the tree
// ---------------------------------------------------------------------------

/** The children of a step, with the label each one is drawn under. Every
 *  place that needs to descend uses this, so the graph, the editor and the
 *  name check agree about what is inside a step. */
export function childrenOf(step: Step): { label: string; step: Step }[] {
  switch (step.kind) {
    case "parallel":
      return step.branches.map((branch, i) => ({ label: `branch ${i + 1}`, step: branch }));
    case "branch": {
      const arms = step.cases.map((one) => ({ label: one.name || "case", step: one.step }));
      return step.otherwise ? [...arms, { label: "otherwise", step: step.otherwise }] : arms;
    }
    case "foreach":
      return [{ label: "for each item", step: step.body }];
    case "loop":
      return [{ label: "each time round", step: step.body }];
    default:
      return [];
  }
}

/** Every step in the tree, parents before their children.
 *
 *  Step names key a breakpoint, a replay's `from_step` and a resume, so they
 *  have to be unique across the whole tree and not merely across one level.
 *  One walk feeds the duplicate check, the breakpoint picker and the replay
 *  dialog. */
export function flattenSteps(steps: Step[]): Step[] {
  const out: Step[] = [];
  const walk = (list: Step[]) => {
    for (const step of list) {
      out.push(step);
      walk(childrenOf(step).map((child) => child.step));
    }
  };
  walk(steps);
  return out;
}

/** Names used twice anywhere in the tree, blanks ignored. */
export function duplicateNames(steps: Step[]): string[] {
  const seen = new Set<string>();
  const twice = new Set<string>();
  for (const step of flattenSteps(steps)) {
    const name = step.name.trim();
    if (name === "") continue;
    if (seen.has(name)) twice.add(name);
    seen.add(name);
  }
  return [...twice];
}

// ---------------------------------------------------------------------------
// Mappings
// ---------------------------------------------------------------------------

export function isValuePath(value: MappingValue): value is ValuePath {
  return value.kind === "path";
}

/** A mapping as rows, so an editor can keep the order fields were typed in
 *  rather than re-sorting on every keystroke. */
export function mappingRows(mapping: Mapping): { field: string; value: MappingValue }[] {
  return Object.entries(mapping).map(([field, value]) => ({ field, value }));
}

export function rowsToMapping(rows: { field: string; value: MappingValue }[]): Mapping {
  const out: Mapping = {};
  for (const row of rows) {
    if (row.field.trim() === "") continue;
    out[row.field.trim()] = row.value;
  }
  return out;
}

/** One line saying what a mapping does, for a graph node that has no room
 *  for the editor. */
export function describeMapping(mapping: Mapping): string {
  const keys = Object.keys(mapping);
  if (keys.length === 0) return "";
  return keys
    .slice(0, 3)
    .map((key) => {
      const value = mapping[key];
      return `${key} ← ${isValuePath(value) ? value.path : shortJson(value.value)}`;
    })
    .join(", ")
    .concat(keys.length > 3 ? `, +${keys.length - 3} more` : "");
}

function shortJson(value: unknown): string {
  try {
    const text = JSON.stringify(value) ?? "null";
    return text.length > 24 ? `${text.slice(0, 23)}…` : text;
  } catch {
    return String(value);
  }
}

// ---------------------------------------------------------------------------
// Conditions
// ---------------------------------------------------------------------------

export const CONDITION_OPS: { value: NonNullable<Condition["op"]>; label: string }[] = [
  { value: "eq", label: "is" },
  { value: "ne", label: "is not" },
  { value: "gt", label: "is more than" },
  { value: "gte", label: "is at least" },
  { value: "lt", label: "is less than" },
  { value: "lte", label: "is at most" },
  { value: "in", label: "is one of" },
  { value: "contains", label: "contains" },
  { value: "exists", label: "is set" },
  { value: "truthy", label: "is true" },
  { value: "matches", label: "matches" },
];

/** True for an operator that needs no right-hand value. */
export function isUnaryOp(op: Condition["op"]): boolean {
  return op === "exists" || op === "truthy";
}

/** A condition in words, for a graph node or a step summary. */
export function describeCondition(condition: Condition): string {
  // The backend returns empty groups on a leaf, so presence is not shape.
  if (condition.all_of && condition.all_of.length > 0) {
    return `all of (${condition.all_of.map(describeCondition).join(", ")})`;
  }
  if (condition.any_of && condition.any_of.length > 0) {
    return `any of (${condition.any_of.map(describeCondition).join(", ")})`;
  }
  const op = CONDITION_OPS.find((one) => one.value === condition.op)?.label ?? condition.op ?? "is";
  const negate = condition.negate ? "not " : "";
  const path = condition.path || "?";
  if (isUnaryOp(condition.op)) return `${negate}${path} ${op}`;
  return `${negate}${path} ${op} ${shortJson(condition.value)}`;
}

// ---------------------------------------------------------------------------
// Nodes for the graph
// ---------------------------------------------------------------------------

/** One line under a step's name in a graph node: which tool, which agent,
 *  which event. Everything that identifies the step without opening it. */
export function stepSubtitle(step: Step): string {
  switch (step.kind) {
    case "tool":
      return step.tool || "no tool picked";
    case "agent":
      return step.agent_id || "no agent picked";
    case "workflow":
      return step.workflow_id || "no workflow picked";
    case "parallel":
      return `${step.branches.length} branch${step.branches.length === 1 ? "" : "es"} · ${
        step.on_branch_failure === "fail_fast" ? "stop on the first failure" : "wait for them all"
      }`;
    case "branch":
      return `${step.cases.length} case${step.cases.length === 1 ? "" : "s"}${
        step.otherwise ? " and an otherwise" : ""
      }`;
    case "foreach":
      return `over ${step.items.path || "?"}${
        step.concurrency > 1 ? `, ${step.concurrency} at a time` : ""
      }`;
    case "loop": {
      const test = step.until
        ? `until ${describeCondition(step.until)}`
        : step.while
          ? `while ${describeCondition(step.while)}`
          : "";
      return [test, `at most ${step.max_iterations}`].filter(Boolean).join(", ");
    }
    case "map":
      return describeMapping(step.output) || "nothing mapped yet";
    case "set_state":
      return describeMapping(step.values) || "nothing set yet";
    case "sleep":
      return step.until ? `until ${step.until.path}` : `${step.seconds ?? 0}s`;
    case "wait":
      return step.event || "no event named";
    case "human":
      return step.prompt || "no prompt written";
  }
}

export type ParallelLike = ParallelStep | BranchStep | ForeachStep | LoopStep;

/** True for a step whose children are drawn side by side rather than stacked. */
export function isFanned(step: Step): step is ParallelStep | BranchStep {
  return step.kind === "parallel" || step.kind === "branch";
}
