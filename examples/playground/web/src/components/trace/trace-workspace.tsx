"use client";

import { useRef, useState, type CSSProperties, type PointerEvent } from "react";
import { GripVerticalIcon } from "lucide-react";
import { TraceDetail } from "@/components/trace/trace-detail";
import { TraceList } from "@/components/trace/trace-list";
import type { TraceEntry } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

export function TraceWorkspace({
  entries, selectedEntry, selectedKey, onSelect, className,
}: {
  entries: TraceEntry[];
  selectedEntry: TraceEntry | null;
  selectedKey: string | null;
  onSelect: (key: string) => void;
  className?: string;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [listPercent, setListPercent] = useState(56);
  const dragStart = useRef<{ x: number; percent: number } | null>(null);

  function move(event: PointerEvent<HTMLDivElement>) {
    if (!dragStart.current || !rootRef.current) return;
    const width = rootRef.current.getBoundingClientRect().width;
    const next = dragStart.current.percent + ((event.clientX - dragStart.current.x) / width) * 100;
    setListPercent(Math.min(76, Math.max(30, next)));
  }

  const style = { "--trace-list-width": `${listPercent}%` } as CSSProperties;

  return (
    <div ref={rootRef} style={style} className={cn(
      "grid min-h-0 flex-1 grid-rows-[minmax(14rem,1fr)_minmax(14rem,1fr)] md:grid-cols-[var(--trace-list-width)_6px_minmax(0,1fr)] md:grid-rows-1",
      className,
    )}>
      <TraceList entries={entries} selectedKey={selectedKey} onSelect={onSelect} />
      <div
        role="separator"
        aria-label="Resize calls and detail panels"
        aria-orientation="vertical"
        aria-valuemin={30}
        aria-valuemax={76}
        aria-valuenow={Math.round(listPercent)}
        tabIndex={0}
        className="group relative z-20 hidden cursor-col-resize bg-border outline-none hover:bg-ring focus-visible:bg-ring md:flex md:items-center md:justify-center"
        onPointerDown={(event) => {
          dragStart.current = { x: event.clientX, percent: listPercent };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={move}
        onPointerUp={(event) => {
          dragStart.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onPointerCancel={() => { dragStart.current = null; }}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") setListPercent((value) => Math.max(30, value - 2));
          if (event.key === "ArrowRight") setListPercent((value) => Math.min(76, value + 2));
        }}
      >
        <span className="absolute rounded-full border border-border bg-background p-0.5 opacity-0 shadow-sm group-hover:opacity-100 group-focus-visible:opacity-100">
          <GripVerticalIcon className="size-3 text-muted-foreground" />
        </span>
      </div>
      <div className="min-h-0 overflow-auto border-t border-border md:border-t-0">
        {selectedEntry ? <TraceDetail key={selectedEntry.key} entry={selectedEntry} /> :
          <div className="flex h-full items-center justify-center p-6 text-body text-muted-foreground">Select a call to inspect it.</div>}
      </div>
    </div>
  );
}
