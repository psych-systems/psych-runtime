import Link from "next/link";
import { CheckIcon, ExternalLinkIcon, XIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ScenarioRunResult } from "@/lib/types";

/**
 * The stream's final frame, rendered in full: the verdict, the summary, and
 * every assertion the scenario checked -- claim, whether it held, and the
 * detail behind it. A green tick with no evidence is worth nothing, so this
 * never collapses or truncates an assertion's `detail`.
 *
 * `passed` and `!passed` get the *same* card chrome, differing only in
 * accent color and icon -- a scenario that ran and found its claims did not
 * hold (`degenerate-loop` documents exactly this) is a genuine result, not
 * an error, and should not read as one. A transport failure that never
 * produced a result at all is a different component (`ScenarioConnectionError`
 * in `scenario-card.tsx`) with visibly different chrome, so the two are
 * never confusable.
 */
export function ScenarioVerdict({ result }: { result: ScenarioRunResult }) {
  const passed = result.passed;

  return (
    <div
      className={cn(
        "rounded-lg border px-3 py-2.5",
        passed ? "border-status-completed/30 bg-status-completed/8" : "border-status-failed/30 bg-status-failed/8"
      )}
    >
      <div className="flex items-start gap-2">
        <span
          className={cn(
            "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full",
            passed ? "bg-status-completed text-primary-foreground" : "bg-status-failed text-primary-foreground"
          )}
        >
          {passed ? <CheckIcon className="size-2.5" /> : <XIcon className="size-2.5" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className={cn("text-sm font-medium", passed ? "text-status-completed" : "text-status-failed")}>
            {passed ? "Passed" : "Failed"}
          </p>
          <p className="mt-0.5 text-sm text-foreground/90">{result.summary}</p>
        </div>
      </div>

      {result.assertions.length > 0 && (
        <ul className="mt-2.5 flex flex-col gap-1.5 border-t border-border/60 pt-2.5">
          {result.assertions.map((assertion, i) => (
            <li key={i} className="flex items-start gap-2 text-xs">
              {assertion.held ? (
                <CheckIcon className="mt-0.5 size-3 shrink-0 text-status-completed" />
              ) : (
                <XIcon className="mt-0.5 size-3 shrink-0 text-status-failed" />
              )}
              <div className="min-w-0 flex-1">
                <p className={cn("font-medium", assertion.held ? "text-foreground" : "text-status-failed")}>
                  {assertion.claim}
                </p>
                <p className="mt-0.5 text-muted-foreground">{assertion.detail}</p>
              </div>
            </li>
          ))}
        </ul>
      )}

      {result.run_ids.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t border-border/60 pt-2.5">
          <span className="text-micro text-muted-foreground">record log:</span>
          {result.run_ids.map((runId) => (
            <Badge key={runId} variant="outline" asChild className="font-technical text-micro" title={runId}>
              <Link href={`/chat/${runId}`} target="_blank" rel="noopener noreferrer">
                {runId.slice(0, 12)}
                <ExternalLinkIcon />
              </Link>
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}
