"use client";

import { useState } from "react";
import {
  AlertTriangleIcon,
  ChevronRightIcon,
  Loader2Icon,
  PlayIcon,
  WifiOffIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help";
import { TableCell, TableRow } from "@/components/ui/table";
import { ElapsedTimer } from "@/components/capabilities/elapsed-timer";
import { ScenarioProgressLog } from "@/components/capabilities/scenario-progress-log";
import { ScenarioRequirements } from "@/components/capabilities/scenario-requirements";
import { ScenarioVerdict } from "@/components/capabilities/scenario-verdict";
import type { ScenarioRunEntry } from "@/components/capabilities/use-scenario-runner";
import { cn } from "@/lib/utils";
import type { ScenarioSummary } from "@/lib/types";

/** The first sentence, for the line; the rest lives behind the "?". */
function firstLine(proves: string): string {
  const stop = proves.indexOf(". ");
  return stop === -1 ? proves : proves.slice(0, stop + 1);
}

/**
 * One scenario as a row: what it proves in a line, what it needs, its last
 * verdict, and a Run button. The live progress and the full verdict open
 * underneath, and open themselves while a run is in flight -- `entry.status`
 * alone decides what is shown, so a still-running scenario never reads as
 * passed.
 */
export function ScenarioRow({
  scenario,
  entry,
  disabled,
  onRun,
}: {
  scenario: ScenarioSummary;
  entry: ScenarioRunEntry;
  /** Another scenario is running elsewhere on the page -- this row's own Run
   * button is disabled, but its own status still renders normally. */
  disabled: boolean;
  onRun: () => void;
}) {
  const running = entry.status === "running";
  const hasRunBefore = entry.status !== "idle";
  // Opens itself when a run starts and stays open once a verdict lands: the
  // payoff of this page is the evidence, and collapsing it the instant it
  // arrived would hide exactly the thing somebody pressed Run for. `null`
  // means nobody has said either way, so the run decides; the chevron takes
  // the decision back.
  const [override, setOverride] = useState<boolean | null>(null);
  const expanded = override ?? (running || entry.result !== null);
  const hasDetail = entry.progress.length > 0 || entry.result !== null || running;

  const line = firstLine(scenario.proves);
  const more = line.length < scenario.proves.length;

  return (
    <>
      <TableRow className={cn("align-top", !scenario.available && "bg-muted/30")}>
        <TableCell>
          <span className="flex min-w-0 items-start gap-2">
            <button
              type="button"
              onClick={() => setOverride(!expanded)}
              aria-expanded={expanded}
              disabled={!hasDetail}
              aria-label={expanded ? `Hide ${scenario.title} detail` : `Show ${scenario.title} detail`}
              className={cn(
                "mt-0.5 rounded text-muted-foreground transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
                hasDetail ? "hover:text-foreground" : "invisible",
              )}
            >
              <ChevronRightIcon
                className={cn("size-4 transition-transform", expanded && "rotate-90")}
                aria-hidden
              />
            </button>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{scenario.title}</span>
                <Badge variant="outline" className="font-technical text-micro text-muted-foreground">
                  {scenario.design_ref}
                </Badge>
              </span>
              <span className="inline-flex items-start gap-1.5 text-caption text-muted-foreground">
                <span className="min-w-0">{line}</span>
                {more && (
                  <HelpTip title={scenario.title} short="What this scenario proves, in full.">
                    <p>{scenario.proves}</p>
                  </HelpTip>
                )}
              </span>
              {!scenario.available && scenario.unavailable_reason && (
                <span className="inline-flex items-start gap-1.5 text-caption text-status-suspended">
                  <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span className="min-w-0">
                    Can&rsquo;t run here right now: {scenario.unavailable_reason}
                  </span>
                </span>
              )}
            </span>
          </span>
        </TableCell>

        <TableCell className="hidden sm:table-cell">
          <span className="flex flex-wrap gap-1">
            {scenario.requires.length === 0 ? (
              <span className="text-caption text-muted-foreground">Nothing</span>
            ) : (
              <ScenarioRequirements requires={scenario.requires} />
            )}
          </span>
        </TableCell>

        <TableCell>
          <span className="flex flex-col gap-0.5 text-caption whitespace-nowrap">
            <span
              className={cn(
                "inline-flex items-center gap-1.5 font-medium",
                entry.status === "passed" && "text-status-completed",
                (entry.status === "failed" || entry.status === "connection-error") &&
                  "text-status-failed",
                (entry.status === "idle" || running) && "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  entry.status === "passed"
                    ? "bg-status-completed"
                    : entry.status === "failed" || entry.status === "connection-error"
                      ? "bg-status-failed"
                      : running
                        ? "animate-pulse bg-status-running"
                        : "bg-muted-foreground/50",
                )}
                aria-hidden
              />
              {entry.status === "passed"
                ? "Passed"
                : entry.status === "failed"
                  ? "Failed"
                  : entry.status === "connection-error"
                    ? "Lost the connection"
                    : running
                      ? "Running"
                      : "Not run"}
            </span>
            {running && entry.startedAt !== null && (
              <span className="text-micro text-muted-foreground">
                <ElapsedTimer runningSince={entry.startedAt} until={null} /> elapsed
              </span>
            )}
          </span>
        </TableCell>

        <TableCell className="text-right">
          <Button
            size="xs"
            variant={hasRunBefore ? "outline" : "default"}
            disabled={disabled || running || !scenario.available}
            // Hands the decision back to the run: somebody who closed the
            // last verdict and pressed Run again wants to watch this one.
            onClick={() => {
              setOverride(null);
              onRun();
            }}
          >
            {running ? <Loader2Icon className="animate-spin" /> : <PlayIcon />}
            {running ? "Running" : hasRunBefore ? "Run again" : "Run"}
          </Button>
        </TableCell>
      </TableRow>

      {expanded && hasDetail && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={4} className="bg-surface/30">
            <div className="flex flex-col gap-3 py-1">
              {(entry.progress.length > 0 || running) && (
                <ScenarioProgressLog steps={entry.progress} running={running} />
              )}

              {entry.status === "connection-error" && (
                <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/8 px-3 py-2 text-caption text-destructive">
                  <WifiOffIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <div>
                    <p className="font-medium">Lost the connection before the run finished.</p>
                    <p className="mt-0.5 text-destructive/90">
                      {entry.connectionError} This is the test harness, not a scenario result. Try
                      running it again.
                    </p>
                  </div>
                </div>
              )}

              {entry.result && <ScenarioVerdict result={entry.result} />}
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
