/**
 * One vocabulary for "what is this run doing", everywhere it is shown.
 *
 * The backend now answers with `psych.RunStatus.lifecycle`, which is already
 * the words a person uses: queued, running, waiting, stopping, done, failed,
 * stopped. This maps those to a colour and a label and nothing else, so the
 * sidebar, the conversation header and the activity list cannot drift.
 *
 * The old component took ten values -- the store's coarse states *and* every
 * terminal state -- and mapped both onto four colours, which is how "settled"
 * and "completed" came to be two different pills meaning the same thing. It
 * also had no word at all for a run that had been told to stop and had not
 * finished stopping, so a person who pressed stop watched a "Running" pill and
 * concluded stop was broken.
 */

import { cn } from "@/lib/utils";

export type Lifecycle =
  | "queued"
  | "running"
  | "waiting"
  | "stopping"
  | "done"
  | "failed"
  | "stopped";

const META: Record<Lifecycle, { label: string; dot: string; text: string; bg: string }> = {
  queued: {
    label: "Queued",
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
    label: "Needs you",
    dot: "bg-status-waiting",
    text: "text-status-waiting",
    bg: "bg-status-waiting/10",
  },
  stopping: {
    label: "Stopping",
    dot: "bg-status-stopped",
    text: "text-status-stopped",
    bg: "bg-status-stopped/10",
  },
  done: {
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
  stopped: {
    label: "Stopped",
    dot: "bg-status-stopped",
    text: "text-status-stopped",
    bg: "bg-status-stopped/10",
  },
};

/** A run's state, whatever the backend called it.
 *
 * Accepts the coarse store states an older list endpoint still reports
 * (`runnable`, `settled`) so one component covers every source, rather than
 * each caller inventing a mapping. */
export function toLifecycle(value: string | null | undefined): Lifecycle {
  switch (value) {
    case "queued":
    case "runnable":
      return "queued";
    case "running":
      return "running";
    case "waiting":
    case "suspended":
      return "waiting";
    case "stopping":
      return "stopping";
    case "done":
    case "completed":
      return "done";
    case "failed":
      return "failed";
    case "stopped":
    case "aborted":
    case "abandoned":
    case "force_settled":
      return "stopped";
    case "settled":
      return "done";
    default:
      return "queued";
  }
}

export function StatusDot({ state, className }: { state: Lifecycle; className?: string }) {
  const meta = META[state];
  return (
    <span
      aria-label={meta.label}
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        meta.dot,
        state === "running" && "animate-pulse",
        className
      )}
    />
  );
}

export function StatusPill({
  state,
  className,
  size = "default",
}: {
  state: Lifecycle;
  className?: string;
  size?: "default" | "sm";
}) {
  const meta = META[state];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full font-medium",
        meta.bg,
        meta.text,
        size === "sm" ? "px-2 py-0.5 text-micro" : "px-2.5 py-1 text-caption",
        className
      )}
    >
      <StatusDot state={state} />
      {meta.label}
    </span>
  );
}

export function statusLabel(state: Lifecycle): string {
  return META[state].label;
}
