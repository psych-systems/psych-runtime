"use client";

import type { StepStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * What a step is doing, in one vocabulary.
 *
 * Deliberately not `ui/status.tsx`'s `Lifecycle`. A step has three states a
 * run does not -- it can be retrying, it can have been skipped because its
 * `when` was false, and it can have been carried over from the run this one
 * was replayed from -- and pushing those through `toLifecycle` would land all
 * three in the `default` case and draw them as "Queued". A step that was
 * skipped and a step that has not started yet are not the same fact, and the
 * whole point of the view is telling them apart.
 *
 * The shape mirrors `ui/status.tsx` on purpose, so the two read as one system.
 */
const META: Record<StepStatus, { label: string; dot: string; text: string; bg: string }> = {
  pending: {
    label: "Not yet",
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
    bg: "bg-muted",
  },
  running: {
    label: "Working",
    dot: "bg-status-running",
    text: "text-status-running",
    bg: "bg-status-running/10",
  },
  waiting: {
    label: "Waiting",
    dot: "bg-status-waiting",
    text: "text-status-waiting",
    bg: "bg-status-waiting/10",
  },
  completed: {
    label: "Done",
    dot: "bg-status-done",
    text: "text-status-done",
    bg: "bg-status-done/10",
  },
  failed: {
    label: "Failed",
    dot: "bg-status-failed",
    text: "text-status-failed",
    bg: "bg-status-failed/10",
  },
  retrying: {
    label: "Retrying",
    dot: "bg-status-waiting",
    text: "text-status-waiting",
    bg: "bg-status-waiting/10",
  },
  skipped: {
    label: "Skipped",
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
    bg: "bg-muted",
  },
  replayed: {
    label: "Carried over",
    dot: "bg-muted-foreground",
    text: "text-muted-foreground",
    bg: "bg-muted",
  },
};

export function stepStatusLabel(status: StepStatus): string {
  return META[status].label;
}

export function StepStatusPill({
  status,
  className,
}: {
  status: StepStatus;
  className?: string;
}) {
  const meta = META[status];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-micro font-medium",
        meta.bg,
        meta.text,
        className
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-block size-1.5 shrink-0 rounded-full",
          meta.dot,
          status === "running" && "animate-pulse"
        )}
      />
      {meta.label}
    </span>
  );
}

/** The graph node tone that goes with a step's status. */
export function stepTone(
  status: StepStatus
): "default" | "running" | "failed" | "waiting" | "done" | "muted" {
  switch (status) {
    case "running":
      return "running";
    case "failed":
      return "failed";
    case "waiting":
    case "retrying":
      return "waiting";
    case "completed":
      return "done";
    case "skipped":
    case "replayed":
      return "muted";
    default:
      return "default";
  }
}

/** How long a step took, or how long it has been going. Null when it has not
 *  started, because "0s" would read as "instant" rather than "not yet". */
export function stepDuration(
  startedAt: string | null,
  completedAt: string | null
): number | null {
  if (startedAt === null) return null;
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return null;
  const end = completedAt === null ? Date.now() : Date.parse(completedAt);
  if (Number.isNaN(end)) return null;
  return Math.max(0, (end - start) / 1000);
}
