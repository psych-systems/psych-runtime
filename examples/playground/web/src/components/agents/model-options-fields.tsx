"use client";

import { Input } from "@/components/ui/input";
import { LabelWithHelp } from "@/components/ui/help";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FieldGrid, NumberField } from "@/components/agents/field-bits";
import type { ModelOptionsIn, ReasoningEffort } from "@/lib/types";

export const DEFAULT_MODEL_OPTIONS: ModelOptionsIn = {
  top_p: null,
  max_output_tokens: null,
  reasoning_effort: null,
  fallbacks: [],
};

export function modelOptionsAreDefault(value: ModelOptionsIn): boolean {
  return (
    value.top_p === null &&
    value.max_output_tokens === null &&
    value.reasoning_effort === null &&
    value.fallbacks.length === 0
  );
}

/**
 * The rest of what a model reference carries: sampling, an output cap, how
 * hard a reasoning model thinks, and the models tried when this one is
 * briefly down. The fallbacks are not a router: a model that answers badly
 * is never swapped for another, only one that fails to answer at all.
 */
export function ModelOptionsFields({
  value,
  onChange,
  models,
}: {
  value: ModelOptionsIn;
  onChange: (next: ModelOptionsIn) => void;
  models: string[];
}) {
  return (
    <FieldGrid columns={3}>
      <NumberField
        id="model-top-p"
        label="Top p"
        help="Narrows the words the model may pick from. Leave empty for the provider's own."
        min={0.01}
        max={1}
        step={0.05}
        placeholder="Provider default"
        value={value.top_p ?? ""}
        onChange={(next) => onChange({ ...value, top_p: next === "" ? null : Number(next) })}
      />
      <NumberField
        id="model-max-out"
        label="Longest reply"
        suffix="tokens"
        help="A hard cap on one reply. Leave empty for the provider's own."
        min={1}
        step={256}
        placeholder="Provider default"
        value={value.max_output_tokens ?? ""}
        onChange={(next) =>
          onChange({ ...value, max_output_tokens: next === "" ? null : Number(next) })
        }
      />
      <div className="flex min-w-0 flex-col gap-1.5">
        <LabelWithHelp
          htmlFor="model-effort"
          label="Reasoning effort"
          help="How long a reasoning model thinks before answering. Other models ignore it."
        />
        <Select
          value={value.reasoning_effort ?? "__none__"}
          onValueChange={(effort) =>
            onChange({
              ...value,
              reasoning_effort: effort === "__none__" ? null : (effort as ReasoningEffort),
            })
          }
        >
          <SelectTrigger id="model-effort" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">Not set</SelectItem>
            <SelectItem value="low">Low</SelectItem>
            <SelectItem value="medium">Medium</SelectItem>
            <SelectItem value="high">High</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="flex min-w-0 flex-col gap-1.5 sm:col-span-2 lg:col-span-3">
        <LabelWithHelp
          htmlFor="model-fallbacks"
          label="Fallbacks, in order"
          help="Models tried when the one above is briefly unreachable. Never for a bad answer. Comma separated."
        />
        <Input
          id="model-fallbacks"
          value={value.fallbacks.join(", ")}
          spellCheck={false}
          placeholder={models[1] ? `${models[1]}, ...` : "Comma separated"}
          onChange={(e) =>
            onChange({
              ...value,
              fallbacks: e.target.value
                .split(",")
                .map((m) => m.trim())
                .filter(Boolean),
            })
          }
        />
      </div>
    </FieldGrid>
  );
}
