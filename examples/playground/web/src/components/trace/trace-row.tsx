"use client";

import { AlertTriangleIcon, GitCompareArrowsIcon } from "lucide-react";
import { formatDuration } from "@/lib/format";
import { formatOffset } from "@/components/trace/trace-format";
import { KIND_META, STATUS_META } from "@/components/trace/trace-meta";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

export function TraceRow({ entry, selected, onSelect }: {
  entry: TraceEntry; selected: boolean; onSelect: () => void;
}) {
  const kind = KIND_META[entry.kind];
  const status = STATUS_META[entry.status];
  const flagged = entry.status === "dangling" || entry.willRetry;
  const promptChanged = entry.kind === "model" && entry.context.change === "changed";
  return (
    <button type="button" onClick={onSelect} aria-current={selected}
      className={cn(
        "grid min-h-9 w-full grid-cols-[minmax(12rem,2fr)_6rem_5.5rem_6rem_6rem_minmax(12rem,3fr)] items-center border-b border-border/60 text-left text-caption transition-colors hover:bg-muted/50",
        selected && "bg-accent/65 hover:bg-accent/65",
      )}>
      <span className="flex min-w-0 items-center gap-2 px-3">
        <kind.icon className={cn("size-3.5 shrink-0", kind.text)} />
        <span className="truncate font-technical font-medium">{entry.label}</span>
        {flagged && <AlertTriangleIcon className="size-3 shrink-0 text-status-failed" aria-label="Needs attention" />}
        {promptChanged && <GitCompareArrowsIcon className="size-3 shrink-0 text-status-waiting" aria-label="Prompt changed" />}
      </span>
      <span className="px-2 text-muted-foreground">{kind.label}</span>
      <span className={cn("px-2 font-medium", status.text)}>{status.label}</span>
      <span className="tabular px-2 text-muted-foreground">{formatOffset(entry.offsetSeconds).replace("T+", "")}</span>
      <span className="tabular px-2 text-muted-foreground">{formatDuration(entry.durationSeconds)}</span>
      <span className="truncate px-2 text-muted-foreground">{entry.summary}</span>
    </button>
  );
}
