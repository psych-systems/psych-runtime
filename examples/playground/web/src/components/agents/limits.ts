/**
 * The budgets that stop a conversation running away, mirroring
 * `psych.core.spec.Limits` field for field.
 *
 * "Field for field" is now true. The old table claimed to mirror that model
 * and left out `repeat_call_threshold`, `repeat_call_hard_stop` and
 * `max_history_records`, so the three budgets that bound the two most
 * expensive failure modes (a model calling the same tool with the same
 * arguments forever, and a long chat replaying its whole history into every
 * turn) were invisible and unsettable from the only screen that publishes an
 * agent. Every default and bound below is the one the server enforces, so a
 * value this form accepts is a value the publish accepts.
 *
 * Each description says what the number protects against rather than what
 * the field is called again. These are money: a person setting them is
 * deciding how large a bill one runaway conversation may run up.
 */

export interface LimitsFormState {
  max_steps: number;
  max_turns: number;
  max_tool_calls_per_turn: number;
  deadline_seconds: number;
  transient_retry_budget: number;
  max_delegation_depth: number;
  max_fanout_per_turn: number;
  failure_streak_threshold: number;
  failure_streak_hard_stop: number;
  repeat_call_threshold: number;
  repeat_call_hard_stop: number;
  stream_idle_seconds: number;
  large_result_bytes: number;
  max_history_records: number;
}

export const DEFAULT_LIMITS: LimitsFormState = {
  max_steps: 48,
  max_turns: 32,
  max_tool_calls_per_turn: 16,
  deadline_seconds: 900,
  transient_retry_budget: 8,
  max_delegation_depth: 3,
  max_fanout_per_turn: 4,
  failure_streak_threshold: 3,
  failure_streak_hard_stop: 6,
  repeat_call_threshold: 3,
  repeat_call_hard_stop: 6,
  stream_idle_seconds: 300,
  large_result_bytes: 32_768,
  max_history_records: 300,
};

export interface LimitField {
  key: keyof LimitsFormState;
  label: string;
  /** One line, in plain words, naming what runs up a bill if this is unset
   *  or set too high. */
  description: string;
  min: number;
  max: number;
  step: number;
}

export interface LimitGroup {
  title: string;
  description: string;
  fields: LimitField[];
}

export const LIMIT_GROUPS: LimitGroup[] = [
  {
    title: "How long one answer may take",
    description: "The outer bounds. Every one of these ends the answer rather than letting it continue.",
    fields: [
      {
        key: "max_turns",
        label: "Replies from the model",
        description: "Stops a back-and-forth that never reaches an answer.",
        min: 1,
        max: 1000,
        step: 1,
      },
      {
        key: "deadline_seconds",
        label: "Time limit (seconds)",
        description: "Nothing runs forever. Time spent waiting on you does not count against it.",
        min: 1,
        max: 86_400,
        step: 1,
      },
      {
        key: "max_steps",
        label: "Total steps",
        description: "Counts every model reply, tool call and helper together, so a mix of all three still ends.",
        min: 1,
        max: 10_000,
        step: 1,
      },
      {
        key: "max_tool_calls_per_turn",
        label: "Tool calls in one reply",
        description: "Stops a single reply firing off hundreds of calls at once.",
        min: 1,
        max: 256,
        step: 1,
      },
    ],
  },
  {
    title: "When it starts going in circles",
    description:
      "Two different loops, each with a warning and a stop. The warning tells the model to try something else; the stop ends the answer if it carries on regardless.",
    fields: [
      {
        key: "failure_streak_threshold",
        label: "Failures before a nudge",
        description: "The same tool failing this many times in a row tells the model to change approach.",
        min: 1,
        max: 100,
        step: 1,
      },
      {
        key: "failure_streak_hard_stop",
        label: "Failures before stopping",
        description: "It was told and kept going. Must be at least the number above.",
        min: 1,
        max: 200,
        step: 1,
      },
      {
        key: "repeat_call_threshold",
        label: "Repeats before a nudge",
        description:
          "The same call with the same arguments returning the same answer. Success that teaches nothing still costs money.",
        min: 1,
        max: 100,
        step: 1,
      },
      {
        key: "repeat_call_hard_stop",
        label: "Repeats before stopping",
        description: "It already had the answer and asked again anyway. Must be at least the number above.",
        min: 1,
        max: 200,
        step: 1,
      },
      {
        key: "transient_retry_budget",
        label: "Retries after a glitch",
        description:
          "Retries of temporary problems, counted across the whole answer so failures cannot be spread out to retry forever.",
        min: 0,
        max: 100,
        step: 1,
      },
    ],
  },
  {
    title: "Helpers, history and big results",
    description: "What one answer may pull in behind the scenes, which is where the token bill usually grows.",
    fields: [
      {
        key: "max_history_records",
        label: "Earlier messages carried forward",
        description:
          "How much of a long conversation gets re-read on every message. The single largest cost in a chat that runs for weeks. 0 starts each message fresh.",
        min: 0,
        max: 20_000,
        step: 1,
      },
      {
        key: "max_fanout_per_turn",
        label: "Helpers started at once",
        description: "One reply starting a tree of helpers is how a small task becomes a large bill.",
        min: 1,
        max: 64,
        step: 1,
      },
      {
        key: "max_delegation_depth",
        label: "How deep helpers may go",
        description: "A helper that can start its own helpers, and so on, without a floor.",
        min: 0,
        max: 16,
        step: 1,
      },
      {
        key: "stream_idle_seconds",
        label: "Silence tolerated (seconds)",
        description:
          "How long the model may go quiet mid-answer before it counts as stalled. Generous, because thinking is legitimately silent. 0 turns it off.",
        min: 0,
        max: 3600,
        step: 1,
      },
      {
        key: "large_result_bytes",
        label: "Large result cutoff (bytes)",
        description:
          "A result bigger than this is summarised for the model and kept whole in the record, so one huge page does not fill the bill.",
        min: 1,
        max: 10_000_000,
        step: 1,
      },
    ],
  },
];

export const LIMIT_FIELDS: LimitField[] = LIMIT_GROUPS.flatMap((group) => group.fields);

/**
 * The two cross-field rules `Limits`' own model validator enforces, checked
 * here so a person sees them beside the field rather than as a rejected
 * publish. Keys match the `limits.<field>` paths the server's issues use, so
 * client and server messages land in the same place.
 */
export function validateLimits(value: LimitsFormState): Record<string, string> {
  const errors: Record<string, string> = {};
  if (value.failure_streak_hard_stop < value.failure_streak_threshold) {
    errors["limits.failure_streak_hard_stop"] =
      "This is below the nudge above, so the answer would stop before the model was ever told to change approach.";
  }
  if (value.repeat_call_hard_stop < value.repeat_call_threshold) {
    errors["limits.repeat_call_hard_stop"] =
      "This is below the nudge above, so the answer would stop before the model was ever told it already had the answer.";
  }
  for (const field of LIMIT_FIELDS) {
    const n = value[field.key];
    if (!Number.isFinite(n) || n < field.min || n > field.max) {
      errors[`limits.${field.key}`] = `Enter a number between ${field.min} and ${field.max}.`;
    }
  }
  return errors;
}

export function limitsDifferFromDefaults(value: LimitsFormState): boolean {
  return LIMIT_FIELDS.some((field) => value[field.key] !== DEFAULT_LIMITS[field.key]);
}
