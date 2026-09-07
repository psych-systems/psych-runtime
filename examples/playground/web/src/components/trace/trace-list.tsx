"use client";

import { ListTreeIcon } from "lucide-react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { TraceRow } from "@/components/trace/trace-row";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

/** The chronological action list -- every model call, tool call, suspension
 * and step, in the order recorded. Paired with `TraceDetail` in a
 * master/detail layout, the way a browser's network panel pairs its request
 * list with the request inspector. */
export function TraceList({
  entries,
  selectedKey,
  onSelect,
  className,
}: {
  entries: TraceEntry[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
  className?: string;
}) {
  return (
    <div className={cn("flex min-h-0 flex-1 flex-col", className)}>
      {/* `.bar`, the same height as the detail panel's header beside it, so
          the two bottom borders read as one line across the split. */}
      <div className="bar">
        <ListTreeIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <h2 className="text-caption font-medium">In order</h2>
        <span className="tabular ml-auto text-micro text-muted-foreground">
          {entries.length} action{entries.length === 1 ? "" : "s"}
        </span>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col">
          {entries.map((entry) => (
            <TraceRow
              key={entry.key}
              entry={entry}
              selected={entry.key === selectedKey}
              onSelect={() => onSelect(entry.key)}
            />
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
