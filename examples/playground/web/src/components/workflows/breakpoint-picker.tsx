"use client";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Which steps a run should stop before.
 *
 * Chips rather than a multi-select, because the list is the workflow's own
 * step names and a person picking them is reading the graph above at the same
 * time. Names are what a breakpoint matches on, which is why the editor
 * insists they are unique across the whole tree.
 */
export function BreakpointPicker({
  stepNames,
  value,
  onChange,
  className,
}: {
  stepNames: string[];
  value: string[];
  onChange: (next: string[]) => void;
  className?: string;
}) {
  // Breakpoints are names, and a run view lists a loop body once per
  // iteration, so the same name arrives several times; offer it once.
  stepNames = Array.from(new Set(stepNames));
  if (stepNames.length === 0) {
    return <p className="text-caption text-muted-foreground">This workflow has no named steps.</p>;
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <ul className="flex flex-wrap gap-1.5">
        {stepNames.map((name) => {
          const on = value.includes(name);
          return (
            <li key={name}>
              <button
                type="button"
                aria-pressed={on}
                onClick={() =>
                  onChange(on ? value.filter((one) => one !== name) : [...value, name])
                }
                className={cn(
                  "rounded-full border px-2.5 py-1 font-technical text-caption transition-colors",
                  "focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                  on
                    ? "border-primary bg-primary/10 text-foreground"
                    : "border-border text-muted-foreground hover:bg-surface/60"
                )}
              >
                {name}
              </button>
            </li>
          );
        })}
      </ul>
      {value.length > 0 && (
        <div>
          <Button type="button" size="sm" variant="ghost" onClick={() => onChange([])}>
            Clear {value.length}
          </Button>
        </div>
      )}
    </div>
  );
}
