"use client";

import { RotateCcwIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { ModelField } from "@/components/agents/model-field";
import {
  COMPACTION_BOUNDS,
  DEFAULT_COMPACTION,
  type CompactionFormState,
} from "@/components/agents/compaction";

interface CompactionFieldsProps {
  value: CompactionFormState;
  onChange: (next: CompactionFormState) => void;
  /** Keyed `compaction.<field>`, merged from this form's own checks and the
   *  server's rejected-publish issues. */
  fieldErrors: Record<string, string>;
  /** What the provider said it serves, for the summariser's own model. Empty
   *  is normal rather than an error, so the control still takes free text. */
  models: string[];
}

/**
 * The four numbers and one model that decide how an agent summarises itself.
 *
 * Shown only once the toggle is on, because they mean nothing when it is off
 * and a form that asks four questions about a feature nobody enabled is four
 * questions of noise on every agent anybody publishes.
 */
export function CompactionFields({ value, onChange, fieldErrors, models }: CompactionFieldsProps) {
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="compaction-trigger">Summarise once the prompt reaches</Label>
            {value.trigger_tokens !== DEFAULT_COMPACTION.trigger_tokens && (
              <ResetLink
                to={DEFAULT_COMPACTION.trigger_tokens.toLocaleString()}
                onClick={() =>
                  onChange({ ...value, trigger_tokens: DEFAULT_COMPACTION.trigger_tokens })
                }
              />
            )}
          </div>
          <Input
            id="compaction-trigger"
            type="number"
            className="tabular"
            min={1}
            step={1000}
            value={value.trigger_tokens}
            aria-invalid={fieldErrors["compaction.trigger_tokens"] !== undefined}
            aria-describedby="compaction-trigger-help"
            onChange={(event) => {
              const parsed = event.target.valueAsNumber;
              onChange({
                ...value,
                trigger_tokens: Number.isFinite(parsed) ? parsed : value.trigger_tokens,
              });
            }}
          />
          <p id="compaction-trigger-help" className="text-caption text-muted-foreground">
            Tokens in the last prompt, counted by the provider rather than
            estimated here. Set it well under the window of the model you named
            above: the count arrives one reply late, so a whole further reply
            has to fit underneath.
          </p>
          <FieldNote message={fieldErrors["compaction.trigger_tokens"]} />
        </div>

        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="compaction-keep">Replies kept word for word</Label>
            {value.keep_recent_turns !== DEFAULT_COMPACTION.keep_recent_turns && (
              <ResetLink
                to={String(DEFAULT_COMPACTION.keep_recent_turns)}
                onClick={() =>
                  onChange({ ...value, keep_recent_turns: DEFAULT_COMPACTION.keep_recent_turns })
                }
              />
            )}
          </div>
          <Input
            id="compaction-keep"
            type="number"
            className="tabular"
            min={COMPACTION_BOUNDS.keep_recent_turns.min}
            max={COMPACTION_BOUNDS.keep_recent_turns.max}
            step={1}
            value={value.keep_recent_turns}
            aria-invalid={fieldErrors["compaction.keep_recent_turns"] !== undefined}
            aria-describedby="compaction-keep-help"
            onChange={(event) => {
              const parsed = event.target.valueAsNumber;
              onChange({
                ...value,
                keep_recent_turns: Number.isFinite(parsed) ? parsed : value.keep_recent_turns,
              });
            }}
          />
          <p id="compaction-keep-help" className="text-caption text-muted-foreground">
            The most recent replies stay below the summary untouched, with the
            tool results they produced. Fewer is cheaper and blunter.
          </p>
          <FieldNote message={fieldErrors["compaction.keep_recent_turns"]} />
        </div>

        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="compaction-cap">Longest the summary may be</Label>
            {value.max_summary_tokens !== DEFAULT_COMPACTION.max_summary_tokens && (
              <ResetLink
                to={DEFAULT_COMPACTION.max_summary_tokens.toLocaleString()}
                onClick={() =>
                  onChange({
                    ...value,
                    max_summary_tokens: DEFAULT_COMPACTION.max_summary_tokens,
                  })
                }
              />
            )}
          </div>
          <Input
            id="compaction-cap"
            type="number"
            className="tabular"
            min={COMPACTION_BOUNDS.max_summary_tokens.min}
            max={COMPACTION_BOUNDS.max_summary_tokens.max}
            step={128}
            value={value.max_summary_tokens}
            aria-invalid={fieldErrors["compaction.max_summary_tokens"] !== undefined}
            aria-describedby="compaction-cap-help"
            onChange={(event) => {
              const parsed = event.target.valueAsNumber;
              onChange({
                ...value,
                max_summary_tokens: Number.isFinite(parsed) ? parsed : value.max_summary_tokens,
              });
            }}
          />
          <p id="compaction-cap-help" className="text-caption text-muted-foreground">
            In tokens. A summary allowed to run as long as the conversation it
            replaced saves nothing.
          </p>
          <FieldNote message={fieldErrors["compaction.max_summary_tokens"]} />
        </div>

        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="compaction-model">Model that writes the summary</Label>
            {value.model !== "" && (
              <ResetLink to="the agent's own" onClick={() => onChange({ ...value, model: "" })} />
            )}
          </div>
          <ModelField
            value={value.model}
            onChange={(next) => onChange({ ...value, model: next })}
            models={models}
            detail={null}
            placeholder="The agent's own model"
            invalid={fieldErrors["compaction.model"] !== undefined}
          />
          <p className="text-caption text-muted-foreground">
            Summarising is a mechanical read of a transcript rather than the
            work itself, so a cheaper model here usually costs nothing in
            quality. Leave it unset to use the agent&apos;s own.
          </p>
          <FieldNote message={fieldErrors["compaction.model"]} />
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <Label htmlFor="compaction-instructions">
          What this agent cannot afford to lose (optional)
        </Label>
        <Textarea
          id="compaction-instructions"
          rows={3}
          value={value.summary_instructions}
          placeholder="Always keep every order number and its status, even for orders already resolved."
          aria-invalid={fieldErrors["compaction.summary_instructions"] !== undefined}
          aria-describedby="compaction-instructions-help"
          onChange={(event) => onChange({ ...value, summary_instructions: event.target.value })}
        />
        <p id="compaction-instructions-help" className="text-caption text-muted-foreground">
          Added to what Psych already keeps, never instead of it. It already
          keeps what was asked and the constraints on it, what tool results
          established, decisions and why, and what is still outstanding, and it
          preserves identifiers and quoted text exactly. Use this to name what
          your work in particular must not lose, because a summariser has no
          idea which of the numbers in a transcript is the one that matters.
        </p>
        <FieldNote message={fieldErrors["compaction.summary_instructions"]} />
      </div>
    </div>
  );
}

function ResetLink({ to, onClick }: { to: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1 text-micro text-muted-foreground transition-colors hover:text-foreground"
    >
      <RotateCcwIcon className="size-3" aria-hidden /> reset to {to}
    </button>
  );
}

function FieldNote({ message }: { message?: string }) {
  if (message === undefined) return null;
  return <p className="text-caption text-status-failed">{message}</p>;
}
