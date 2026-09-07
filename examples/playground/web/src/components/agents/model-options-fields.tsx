"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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
    <div className="grid gap-4 sm:grid-cols-2">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="model-top-p">Top p</Label>
        <Input
          id="model-top-p"
          type="number"
          className="tabular"
          min={0.01}
          max={1}
          step={0.05}
          placeholder="Provider default"
          value={value.top_p ?? ""}
          onChange={(e) =>
            onChange({ ...value, top_p: e.target.value === "" ? null : Number(e.target.value) })
          }
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="model-max-out">Longest reply (tokens)</Label>
        <Input
          id="model-max-out"
          type="number"
          className="tabular"
          min={1}
          step={256}
          placeholder="Provider default"
          value={value.max_output_tokens ?? ""}
          onChange={(e) =>
            onChange({
              ...value,
              max_output_tokens: e.target.value === "" ? null : Number(e.target.value),
            })
          }
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="model-effort">Reasoning effort</Label>
        <Select
          value={value.reasoning_effort ?? "__none__"}
          onValueChange={(effort) =>
            onChange({
              ...value,
              reasoning_effort: effort === "__none__" ? null : (effort as ReasoningEffort),
            })
          }
        >
          <SelectTrigger id="model-effort">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">Not set</SelectItem>
            <SelectItem value="low">Low</SelectItem>
            <SelectItem value="medium">Medium</SelectItem>
            <SelectItem value="high">High</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-micro text-muted-foreground">
          Only a reasoning model reads this. Others ignore it.
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="model-fallbacks">Fallbacks, in order</Label>
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
        <p className="text-micro text-muted-foreground">
          Tried when the model above is briefly unreachable. Never for a bad answer.
        </p>
      </div>
    </div>
  );
}
