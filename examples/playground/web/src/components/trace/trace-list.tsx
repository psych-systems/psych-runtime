"use client";

import { ListTreeIcon } from "lucide-react";
import { TraceRow } from "@/components/trace/trace-row";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

export function TraceList({ entries, selectedKey, onSelect, className }: {
  entries: TraceEntry[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
  className?: string;
}) {
  return (
    <div className={cn("flex min-h-0 flex-1 flex-col", className)}>
      <div className="bar">
        <ListTreeIcon className="size-3.5 text-muted-foreground" aria-hidden />
        <h2 className="text-caption font-semibold">Calls</h2>
        <span className="ml-auto text-micro text-muted-foreground">{entries.length} visible</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <div className="min-w-[52rem]">
          <div className="sticky top-0 z-10 grid h-8 grid-cols-[minmax(12rem,2fr)_6rem_5.5rem_6rem_6rem_minmax(12rem,3fr)] items-center border-b border-border bg-surface/95 text-micro font-medium text-muted-foreground backdrop-blur">
            <span className="px-3">Name</span><span className="px-2">Type</span>
            <span className="px-2">Status</span><span className="px-2">Start</span>
            <span className="px-2">Duration</span><span className="px-2">Detail</span>
          </div>
          {entries.map((entry) => <TraceRow key={entry.key} entry={entry}
            selected={entry.key === selectedKey} onSelect={() => onSelect(entry.key)} />)}
          {entries.length === 0 && <p className="p-6 text-center text-caption text-muted-foreground">
            No calls overlap this time range.
          </p>}
        </div>
      </div>
    </div>
  );
}
