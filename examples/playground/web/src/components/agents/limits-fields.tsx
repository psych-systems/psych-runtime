"use client";

import { RotateCcwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FieldGrid, NumberField } from "@/components/agents/field-bits";
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
 * Fourteen numbers, three to a row, each with its reasoning behind a "?".
 *
 * Every one of them is filled in with the default the server enforces, so
 * nobody has to read any of this to publish; the words are there for the
 * person deciding how large a bill one runaway conversation may run up.
 */
export function LimitsFields({ value, onChange, fieldErrors }: LimitsFieldsProps) {
  const changed = limitsDifferFromDefaults(value);

  return (
    <div className="flex flex-col gap-5">
      {LIMIT_GROUPS.map((group) => (
        <div key={group.title} className="flex flex-col gap-3">
          <h3 className="text-caption font-medium tracking-wide text-muted-foreground uppercase">
            {group.title}
          </h3>
          <FieldGrid columns={3}>
            {group.fields.map((field) => (
              <NumberField
                key={field.key}
                id={`limit-${field.key}`}
                label={field.label}
                help={
                  <>
                    <p>{field.description}</p>
                    <p>Default {DEFAULT_LIMITS[field.key].toLocaleString()}.</p>
                  </>
                }
                value={value[field.key]}
                min={field.min}
                max={field.max}
                step={field.step}
                error={fieldErrors[`limits.${field.key}`]}
                onChange={(next) => {
                  const parsed = Number(next);
                  onChange({
                    ...value,
                    [field.key]: Number.isFinite(parsed) && next !== "" ? parsed : value[field.key],
                  });
                }}
              />
            ))}
          </FieldGrid>
        </div>
      ))}
      {changed && (
        <div>
          <Button type="button" size="xs" variant="ghost" onClick={() => onChange(DEFAULT_LIMITS)}>
            <RotateCcwIcon /> Reset every limit
          </Button>
        </div>
      )}
    </div>
  );
}
