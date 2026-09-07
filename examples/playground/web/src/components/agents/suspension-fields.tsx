"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { SuspensionIn } from "@/lib/types";

export const DEFAULT_SUSPENSION: SuspensionIn = {
  approval_expires_seconds: 86_400,
  question_expires_seconds: 86_400,
  external_expires_seconds: 604_800,
  children_expires_seconds: 3_600,
};

export function suspensionIsDefault(value: SuspensionIn): boolean {
  return (Object.keys(DEFAULT_SUSPENSION) as (keyof SuspensionIn)[]).every(
    (key) => value[key] === DEFAULT_SUSPENSION[key]
  );
}

const FIELDS: { key: keyof SuspensionIn; label: string; help: string }[] = [
  {
    key: "approval_expires_seconds",
    label: "Waiting for an approval",
    help: "A decision that arrives after this settles the conversation as abandoned rather than acting on a stale world.",
  },
  {
    key: "question_expires_seconds",
    label: "Waiting for an answer",
    help: "How long a question to the person may go unanswered.",
  },
  {
    key: "external_expires_seconds",
    label: "Waiting on something outside",
    help: "A webhook, a clock, a system that will call back.",
  },
  {
    key: "children_expires_seconds",
    label: "Waiting on helpers",
    help: "How long a parent waits for background helpers before giving up on them.",
  },
];

/** How long each kind of wait may last. A waiting conversation holds no
 *  worker and costs nothing, so these are about honesty rather than money:
 *  a conversation nobody will ever answer should say so. */
export function SuspensionFields({
  value,
  onChange,
}: {
  value: SuspensionIn;
  onChange: (next: SuspensionIn) => void;
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {FIELDS.map((field) => (
        <div key={field.key} className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor={`susp-${field.key}`}>{field.label} (seconds)</Label>
            {value[field.key] !== DEFAULT_SUSPENSION[field.key] && (
              <button
                type="button"
                className="text-micro text-muted-foreground underline underline-offset-2"
                onClick={() => onChange({ ...value, [field.key]: DEFAULT_SUSPENSION[field.key] })}
              >
                reset to {DEFAULT_SUSPENSION[field.key].toLocaleString()}
              </button>
            )}
          </div>
          <Input
            id={`susp-${field.key}`}
            type="number"
            className="tabular"
            min={1}
            value={value[field.key]}
            onChange={(e) => onChange({ ...value, [field.key]: Number(e.target.value) || 1 })}
          />
          <p className="text-micro text-muted-foreground">{field.help}</p>
        </div>
      ))}
    </div>
  );
}
