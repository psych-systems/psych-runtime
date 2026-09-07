"use client";

import { RotateCcwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  DEFAULT_LIMITS,
  LIMIT_GROUPS,
  limitsDifferFromDefaults,
  type LimitsFormState,
} from "@/components/agents/limits";

interface LimitsFieldsProps {
  value: LimitsFormState;
  onChange: (next: LimitsFormState) => void;
  /** Keyed `limits.<field>`, merged from this form's own checks and the
   *  server's rejected-publish issues. */
  fieldErrors: Record<string, string>;
}

/**
 * Fourteen numbers is a lot to put in front of someone, so they arrive in
 * three groups that each answer one question, with the defaults already
 * filled in and a reset on anything that has moved away from one.
 */
export function LimitsFields({ value, onChange, fieldErrors }: LimitsFieldsProps) {
  const changed = limitsDifferFromDefaults(value);

  return (
    <div className="flex flex-col gap-6">
      {changed && (
        <div className="flex justify-end">
          <Button type="button" size="xs" variant="ghost" onClick={() => onChange(DEFAULT_LIMITS)}>
            <RotateCcwIcon /> Reset every limit
          </Button>
        </div>
      )}

      {LIMIT_GROUPS.map((group) => (
        <div key={group.title} className="flex flex-col gap-3">
          <div className="flex flex-col gap-0.5">
            <h3 className="text-body font-medium">{group.title}</h3>
            <p className="text-caption text-muted-foreground">{group.description}</p>
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {group.fields.map((field) => {
              const isDefault = value[field.key] === DEFAULT_LIMITS[field.key];
              const errorMessage = fieldErrors[`limits.${field.key}`];
              return (
                <div key={field.key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2">
                    <Label htmlFor={`limit-${field.key}`}>{field.label}</Label>
                    {!isDefault && (
                      <button
                        type="button"
                        onClick={() => onChange({ ...value, [field.key]: DEFAULT_LIMITS[field.key] })}
                        className="flex items-center gap-1 text-micro text-muted-foreground transition-colors hover:text-foreground"
                      >
                        <RotateCcwIcon className="size-3" aria-hidden /> reset to{" "}
                        {DEFAULT_LIMITS[field.key]}
                      </button>
                    )}
                  </div>
                  <Input
                    id={`limit-${field.key}`}
                    type="number"
                    className="tabular"
                    min={field.min}
                    max={field.max}
                    step={field.step}
                    value={value[field.key]}
                    aria-invalid={errorMessage !== undefined}
                    aria-describedby={`limit-${field.key}-help`}
                    onChange={(event) => {
                      const parsed = event.target.valueAsNumber;
                      onChange({
                        ...value,
                        [field.key]: Number.isFinite(parsed) ? parsed : value[field.key],
                      });
                    }}
                  />
                  <p id={`limit-${field.key}-help`} className="text-caption text-muted-foreground">
                    {field.description}
                  </p>
                  {errorMessage && <p className="text-caption text-status-failed">{errorMessage}</p>}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
