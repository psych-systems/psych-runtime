/**
 * The terms on which an agent replaces its older conversation with a summary,
 * mirroring `psych.CompactionPolicy` field for field.
 *
 * Off unless somebody turns it on, which is the library's default too: a
 * summary is lossy, and an agent whose conversations are short would be paying
 * a model call to lose detail it was never going to run out of room for.
 *
 * ## Why there is a starting number for the trigger at all
 *
 * `trigger_tokens` has no default in the library, deliberately. Psych has no
 * context window to take a fraction of: the model port speaks to a proxy that
 * can reach models it was never told about, so a default there would be a
 * guess about somebody else's model, dressed up as a recommendation.
 *
 * A form is a different place from a library. It has to put something in the
 * box, and an empty required number reads as a puzzle rather than as a
 * decision. So the console picks one and leaves it plainly editable, in an
 * ordinary number field with the reasoning beside it, rather than hiding a
 * constant behind the toggle.
 *
 * The number below is a starting point and not a recommendation. It sits under
 * half of a 128k window, which is the smaller end of what this console is
 * usually pointed at, and that headroom is doing real work: the trigger is
 * measured from what the provider reported for the *last* call rather than
 * estimated ahead of one, so a conversation compacts on the turn after the one
 * that crossed the line and one more full turn has to fit underneath. Anybody
 * running a model with a smaller window has to lower it, and that is why it is
 * a field rather than a constant.
 */

export interface CompactionFormState {
  trigger_tokens: number;
  keep_recent_turns: number;
  /** Empty means the agent's own model, which is what `null` means on the
   *  wire. Kept as a string here because the control that edits it is a text
   *  combobox and a form should not carry two spellings of "unset". */
  model: string;
  max_summary_tokens: number;
  /** Empty adds nothing, which is what `null` means on the wire. */
  summary_instructions: string;
}

export const DEFAULT_COMPACTION: CompactionFormState = {
  trigger_tokens: 60_000,
  keep_recent_turns: 3,
  model: "",
  max_summary_tokens: 2_048,
  summary_instructions: "",
};

/** The bounds `psych.CompactionPolicy` itself enforces, so a value this form
 *  accepts is a value the publish accepts. */
export const COMPACTION_BOUNDS = {
  keep_recent_turns: { min: 1, max: 100 },
  max_summary_tokens: { min: 1, max: 32_000 },
  summary_instructions: { max: 4_000 },
} as const;

/**
 * Checked here so a bad number is caught beside the field rather than as a
 * rejected publish. Keys match the `compaction.<field>` paths the server's own
 * issues use, so both messages land in the same place.
 */
export function validateCompaction(value: CompactionFormState): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!Number.isFinite(value.trigger_tokens) || value.trigger_tokens < 1) {
    errors["compaction.trigger_tokens"] = "Enter a number above 0.";
  }
  const keep = COMPACTION_BOUNDS.keep_recent_turns;
  if (
    !Number.isFinite(value.keep_recent_turns) ||
    value.keep_recent_turns < keep.min ||
    value.keep_recent_turns > keep.max
  ) {
    errors["compaction.keep_recent_turns"] = `Enter a number between ${keep.min} and ${keep.max}.`;
  }
  const cap = COMPACTION_BOUNDS.max_summary_tokens;
  if (
    !Number.isFinite(value.max_summary_tokens) ||
    value.max_summary_tokens < cap.min ||
    value.max_summary_tokens > cap.max
  ) {
    errors["compaction.max_summary_tokens"] =
      `Enter a number between ${cap.min} and ${cap.max}.`;
  }
  if (value.summary_instructions.length > COMPACTION_BOUNDS.summary_instructions.max) {
    errors["compaction.summary_instructions"] =
      `${COMPACTION_BOUNDS.summary_instructions.max.toLocaleString()} characters at most.`;
  }
  return errors;
}
