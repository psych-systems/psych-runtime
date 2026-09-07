"use client";

import { AlertTriangleIcon, ArrowRightIcon } from "lucide-react";

import { formatDuration } from "@/lib/format";
import { KIND_META, STATUS_META } from "@/components/trace/trace-meta";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

interface Phase {
  key: string;
  label: string;
  entries: TraceEntry[];
}

function phasesOf(entries: TraceEntry[]): Phase[] {
  const phases: Phase[] = [];
  let current: Phase = { key: "run", label: "Run", entries: [] };
  for (const entry of entries) {
    if (entry.kind === "message") {
      if (current.entries.length > 0) phases.push(current);
      current = { key: entry.key, label: entry.label, entries: [entry] };
    } else if (entry.kind !== "step") {
      current.entries.push(entry);
    }
  }
  if (current.entries.length > 0) phases.push(current);
  return phases;
}

/** A compact execution path. Calls stay in time order and group under the
 * message that started them, so a conversation reads from left to right. */
export function TraceTimeline({
  entries,
  totalSeconds,
  selectedKey,
  onSelect,
}: {
  entries: TraceEntry[];
  totalSeconds: number;
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const phases = phasesOf(entries);
  const anyDangling = entries.some((entry) => entry.status === "dangling");

  if (phases.length === 0) {
    return (
      <div className="border-b border-border bg-surface/30 px-4 py-6 text-center text-body text-muted-foreground">
        Nothing timed to draw yet.
      </div>
    );
  }

  return (
    <section className="border-b border-border bg-surface/30 px-4 py-4" aria-label="Run path">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-caption font-medium">Run path</h2>
        <span className="text-micro text-muted-foreground">
          {entries.filter((entry) => entry.kind !== "step").length} recorded events · {formatDuration(totalSeconds)} elapsed
        </span>
      </div>

      <div className="flex flex-col gap-3">
        {phases.map((phase) => (
          <div key={phase.key} className="grid gap-2 sm:grid-cols-[7rem_1fr] sm:items-start">
            <div className="pt-1 text-micro font-medium tracking-wide text-muted-foreground uppercase">
              {phase.label}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {phase.entries.map((entry, index) => (
                <div key={entry.key} className="flex items-center gap-1.5">
                  {index > 0 && (
                    <ArrowRightIcon className="size-3 shrink-0 text-muted-foreground/45" aria-hidden />
                  )}
                  <EventButton
                    entry={entry}
                    selected={selectedKey === entry.key}
                    onSelect={() => onSelect(entry.key)}
                  />
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      {anyDangling && (
        <p className="mt-3 flex items-start gap-1.5 text-caption text-status-failed">
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          An unfinished event marks work a stopped worker left behind.
        </p>
      )}
    </section>
  );
}

function EventButton({
  entry,
  selected,
  onSelect,
}: {
  entry: TraceEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  const kind = KIND_META[entry.kind];
  const status = STATUS_META[entry.status];
  return (
    <button
      type="button"
      onClick={onSelect}
      title={`${entry.label}: ${entry.summary}`}
      aria-pressed={selected}
      className={cn(
        "group flex max-w-52 items-center gap-2 rounded-full border px-2.5 py-1.5 text-left transition-colors",
        selected
          ? "border-ring bg-background shadow-sm"
          : "border-border/80 bg-background/65 hover:border-foreground/25 hover:bg-background",
        entry.status === "dangling" && "border-status-failed/45"
      )}
    >
      <span className={cn("size-2 shrink-0 rounded-full", status.bar || kind.bar)} aria-hidden />
      <kind.icon className={cn("size-3.5 shrink-0", kind.text)} aria-hidden />
      <span className="min-w-0 truncate text-caption font-medium">{entry.label}</span>
      <span className="tabular shrink-0 text-micro text-muted-foreground">
        {entry.status === "dangling" ? "unfinished" : formatDuration(entry.durationSeconds)}
      </span>
    </button>
  );
}
