"use client";

import { AlertTriangleIcon, GitCompareArrowsIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { formatDuration } from "@/lib/format";
import { formatOffset } from "@/components/trace/trace-format";
import { KIND_META, STATUS_META } from "@/components/trace/trace-meta";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

/** One row of the action list: kind, name, offset, duration, and a compact
 * result, tokens and cost for a model call, outcome for a tool call. A call
 * that was started and never settled is flagged here as well as on the
 * waterfall: it is the crash signature, and it is worth seeing twice. */
export function TraceRow({
  entry,
  selected,
  onSelect,
}: {
  entry: TraceEntry;
  selected: boolean;
  onSelect: () => void;
}) {
  const kindMeta = KIND_META[entry.kind];
  const statusMeta = STATUS_META[entry.status];
  const flagged = entry.status === "dangling" || entry.willRetry;
  // A turn whose prompt differs from the previous turn's is the one worth
  // opening: something the runtime assembles moved, a withheld tool or a
  // server that stopped answering. It is invisible anywhere else.
  const promptChanged = entry.kind === "model" && entry.context.change === "changed";

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected}
      className={cn(
        "flex w-full flex-col gap-0.5 border-b border-border/60 px-3 py-2 text-left transition-colors hover:bg-muted/50",
        selected && "bg-accent/60 hover:bg-accent/60"
      )}
    >
      <div className="flex items-center gap-2">
        <kindMeta.icon className={cn("size-3.5 shrink-0", kindMeta.text)} />
        <span className="truncate font-technical text-body font-medium">{entry.label}</span>
        {flagged && (
          <Badge
            variant="outline"
            className="h-4.5 gap-1 border-status-failed/40 px-1.5 text-micro text-status-failed"
          >
            <AlertTriangleIcon className="size-2.5" />
            {entry.status === "dangling" ? "never finished" : "will retry"}
          </Badge>
        )}
        {promptChanged && (
          <Badge
            variant="outline"
            className="h-4.5 gap-1 border-status-waiting/40 px-1.5 text-micro text-status-waiting"
          >
            <GitCompareArrowsIcon className="size-2.5" />
            prompt changed
          </Badge>
        )}
        <span className="tabular ml-auto shrink-0 text-caption text-muted-foreground">
          {formatOffset(entry.offsetSeconds)}
        </span>
      </div>
      <div className="flex items-center gap-2 pl-5.5 text-caption text-muted-foreground">
        <span className={cn("font-medium", statusMeta.text || undefined)}>{statusMeta.label}</span>
        <span aria-hidden className="opacity-50">
          ·
        </span>
        <span>{formatDuration(entry.durationSeconds)}</span>
        {entry.ongoing && (
          <span className="text-status-running">· in progress</span>
        )}
        <span className="min-w-0 flex-1 truncate">{entry.summary}</span>
      </div>
    </button>
  );
}
