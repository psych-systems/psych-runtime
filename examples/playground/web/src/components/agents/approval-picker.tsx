"use client";

import { CheckIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { ALL_TOOL_CLASSES, TOOL_CLASS_COPY, type ToolClass } from "@/components/agents/policy";

interface ApprovalPickerProps {
  selected: readonly ToolClass[];
  onChange: (next: ToolClass[]) => void;
  disabled?: boolean;
}

/**
 * Which kinds of action pause and wait for a person.
 *
 * The picker this replaces was three badges reading `@read-only`, `@write`
 * and `@destructive`, which is the selector syntax the request carries, not
 * a question anyone can answer. The syntax still goes on the wire; it is not
 * what gets asked.
 */
export function ApprovalPicker({ selected, onChange, disabled }: ApprovalPickerProps) {
  function toggle(toolClass: ToolClass) {
    if (disabled) return;
    onChange(
      selected.includes(toolClass)
        ? selected.filter((s) => s !== toolClass)
        : [...selected, toolClass]
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {ALL_TOOL_CLASSES.map((toolClass) => {
        const active = selected.includes(toolClass);
        const copy = TOOL_CLASS_COPY[toolClass];
        return (
          <button
            key={toolClass}
            type="button"
            disabled={disabled}
            aria-pressed={active}
            onClick={() => toggle(toolClass)}
            className={cn(
              "flex items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors",
              active
                ? "border-primary/50 bg-primary/5"
                : "border-border hover:bg-surface/60",
              disabled && "pointer-events-none opacity-60"
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[4px] border",
                active ? "border-primary bg-primary text-primary-foreground" : "border-input"
              )}
              aria-hidden
            >
              {active && <CheckIcon className="size-3" />}
            </span>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="text-body font-medium">{copy.title}</span>
              <span className="text-caption text-muted-foreground">{copy.blurb}</span>
              {active && (
                <span className="text-caption text-muted-foreground">{copy.consequence}</span>
              )}
            </span>
          </button>
        );
      })}
      {selected.length === 0 && (
        <p className="text-caption text-muted-foreground">
          Nothing pauses. The agent goes ahead with everything it is allowed to use.
        </p>
      )}
    </div>
  );
}
