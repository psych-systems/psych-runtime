"use client";

import { FieldGrid, NumberField } from "@/components/agents/field-bits";
import type { SuspensionIn } from "@/lib/types";

export const DEFAULT_SUSPENSION: SuspensionIn = {
  approval_expires_seconds: 86_400,
  question_expires_seconds: 86_400,
  external_expires_seconds: 604_800,
  children_expires_seconds: 3_600,
};

export function suspensionIsDefault(value: SuspensionIn): boolean {
  return (Object.keys(DEFAULT_SUSPENSION) as (keyof SuspensionIn)[]).every(
    (key) => value[key] === DEFAULT_SUSPENSION[key],
  );
}

const FIELDS: { key: keyof SuspensionIn; label: string; help: string }[] = [
  {
    key: "approval_expires_seconds",
    label: "For an approval",
    help: "A decision that arrives later settles the conversation as abandoned rather than acting on a stale world.",
  },
  {
    key: "question_expires_seconds",
    label: "For an answer",
    help: "How long a question to the person may go unanswered.",
  },
  {
    key: "external_expires_seconds",
    label: "On something outside",
    help: "A webhook, a clock, a system that will call back.",
  },
  {
    key: "children_expires_seconds",
    label: "On sub-agents",
    help: "How long a parent waits for background sub-agents before giving up on them.",
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
    <FieldGrid columns={3}>
      {FIELDS.map((field) => (
        <NumberField
          key={field.key}
          id={`susp-${field.key}`}
          label={field.label}
          suffix="seconds"
          help={
            <>
              <p>{field.help}</p>
              <p>Default {DEFAULT_SUSPENSION[field.key].toLocaleString()} seconds.</p>
            </>
          }
          min={1}
          value={value[field.key]}
          onChange={(next) => onChange({ ...value, [field.key]: Number(next) || 1 })}
        />
      ))}
    </FieldGrid>
  );
}
