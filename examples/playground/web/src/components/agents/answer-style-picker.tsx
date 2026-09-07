"use client";

import { CheckIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  ANSWER_STYLE_OPTIONS,
  type AnswerStyleChoice,
} from "@/components/agents/answer-style";

/**
 * Two mutually exclusive answers, so radios rather than the checkbox rows the
 * approval picker uses: picking one has to unpick the other, and a pair of
 * toggles that look identical to a multi-select invites someone to try
 * choosing both.
 *
 * A native radio input is kept under each option rather than styled away, so
 * arrow keys move between them and a screen reader reads a group.
 */
export function AnswerStylePicker({
  value,
  onChange,
}: {
  value: AnswerStyleChoice;
  onChange: (next: AnswerStyleChoice) => void;
}) {
  return (
    <div role="radiogroup" aria-label="Answer style" className="flex flex-col gap-2">
      {ANSWER_STYLE_OPTIONS.map((option) => {
        const active = option.value === value;
        const id = `answer-style-${option.value ?? "detailed"}`;
        return (
          <label
            key={id}
            htmlFor={id}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition-colors",
              // The input itself is visually hidden, so the ring the global
              // rule would have drawn around it is drawn around the row a
              // person is actually looking at. Without this, tabbing into the
              // group moved focus nowhere anyone could see.
              "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
              active ? "border-primary/50 bg-primary/5" : "border-border hover:bg-surface/60"
            )}
          >
            <input
              type="radio"
              id={id}
              name="answer-style"
              className="sr-only"
              checked={active}
              onChange={() => onChange(option.value)}
            />
            <span
              className={cn(
                "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border",
                active ? "border-primary bg-primary text-primary-foreground" : "border-input"
              )}
              aria-hidden
            >
              {active && <CheckIcon className="size-3" />}
            </span>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-body font-medium">{option.copy.title}</span>
              <span className="text-caption text-muted-foreground">{option.copy.blurb}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}
