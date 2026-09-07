"use client";

import { AlertTriangleIcon, Loader2Icon, PlayIcon, WifiOffIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ElapsedTimer } from "@/components/capabilities/elapsed-timer";
import { ScenarioProgressLog } from "@/components/capabilities/scenario-progress-log";
import { ScenarioRequirements } from "@/components/capabilities/scenario-requirements";
import { ScenarioVerdict } from "@/components/capabilities/scenario-verdict";
import type { ScenarioRunEntry } from "@/components/capabilities/use-scenario-runner";
import { cn } from "@/lib/utils";
import type { ScenarioSummary } from "@/lib/types";

/**
 * One scenario, end to end: what it proves, what it needs to run, and --
 * once run -- its live progress and its full verdict. `entry.status` alone
 * decides what's shown; nothing here infers a result from a partial state,
 * which is what keeps a still-running scenario from ever reading as passed.
 */
export function ScenarioCard({
  scenario,
  entry,
  disabled,
  onRun,
}: {
  scenario: ScenarioSummary;
  entry: ScenarioRunEntry;
  /** Another scenario is running elsewhere on the page -- this card's own
   * Run button is disabled, but its own status still renders normally. */
  disabled: boolean;
  onRun: () => void;
}) {
  const running = entry.status === "running";
  const hasRunBefore = entry.status !== "idle";

  return (
    <div
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border bg-card p-4 transition-colors",
        !scenario.available && "bg-muted/30"
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-heading text-base font-semibold text-foreground">{scenario.title}</h3>
            <Badge variant="outline" className="font-technical text-micro text-muted-foreground">
              {scenario.design_ref}
            </Badge>
            <ScenarioRequirements requires={scenario.requires} />
          </div>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{scenario.proves}</p>
        </div>

        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <Button
            size="sm"
            variant={hasRunBefore ? "outline" : "default"}
            disabled={disabled || running || !scenario.available}
            onClick={onRun}
            className="gap-1.5"
          >
            {running ? (
              <>
                <Loader2Icon className="size-3.5 animate-spin" /> Running
              </>
            ) : (
              <>
                <PlayIcon className="size-3.5" /> {hasRunBefore ? "Run again" : "Run"}
              </>
            )}
          </Button>
          {running && entry.startedAt !== null && (
            <span className="text-micro text-muted-foreground">
              <ElapsedTimer runningSince={entry.startedAt} until={null} /> elapsed
            </span>
          )}
        </div>
      </div>

      {!scenario.available && scenario.unavailable_reason && (
        <div className="flex items-start gap-2 rounded-lg border border-status-suspended/30 bg-status-suspended/8 px-3 py-2 text-xs text-status-suspended">
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>
            Can&rsquo;t run here right now: {scenario.unavailable_reason}
          </span>
        </div>
      )}

      {(entry.progress.length > 0 || running) && (
        <ScenarioProgressLog steps={entry.progress} running={running} />
      )}

      {entry.status === "connection-error" && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/8 px-3 py-2 text-xs text-destructive">
          <WifiOffIcon className="mt-0.5 size-3.5 shrink-0" />
          <div>
            <p className="font-medium">Lost the connection before the run finished.</p>
            <p className="mt-0.5 text-destructive/90">
              {entry.connectionError} This is the test harness, not a scenario result. Try running it again.
            </p>
          </div>
        </div>
      )}

      {entry.result && <ScenarioVerdict result={entry.result} />}
    </div>
  );
}
