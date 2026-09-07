import { CircleIcon, Loader2Icon } from "lucide-react";

import { formatClockTime } from "@/lib/format";
import type { ScenarioProgressEvent } from "@/lib/types";

/**
 * The scenario's own `emit(step, detail)` calls, in order. This is what
 * keeps a tens-of-seconds run (`crash-recovery`, `four-stores`) reading as
 * live work rather than a hung spinner -- each step lands the moment the
 * scenario module calls `emit`, not after the whole run settles.
 */
export function ScenarioProgressLog({
  steps,
  running,
}: {
  steps: ScenarioProgressEvent[];
  running: boolean;
}) {
  if (steps.length === 0 && !running) return null;

  return (
    <ol className="flex max-h-56 flex-col gap-0 overflow-y-auto rounded-lg border border-border/60 bg-muted/30 p-3">
      {steps.map((step, i) => {
        const isLast = i === steps.length - 1;
        return (
          <li key={`${step.at}-${i}`} className="flex gap-2.5 py-1 text-xs">
            <div className="flex w-3 shrink-0 flex-col items-center pt-0.5">
              {isLast && running ? (
                <Loader2Icon className="size-3 animate-spin text-status-running" />
              ) : (
                <CircleIcon className="size-1.5 fill-current text-muted-foreground/50" />
              )}
              {!isLast && <span className="mt-1 w-px flex-1 bg-border" aria-hidden />}
            </div>
            <div className="min-w-0 flex-1 pb-1.5">
              <div className="flex items-baseline gap-2">
                <span className="font-technical font-medium text-foreground">{step.step}</span>
                <span className="shrink-0 text-micro text-muted-foreground">
                  {formatClockTime(step.at)}
                </span>
              </div>
              <p className="text-muted-foreground">{step.detail}</p>
            </div>
          </li>
        );
      })}
      {running && steps.length === 0 && (
        <li className="flex items-center gap-2 py-1 text-xs text-muted-foreground">
          <Loader2Icon className="size-3 animate-spin text-status-running" />
          starting…
        </li>
      )}
    </ol>
  );
}
