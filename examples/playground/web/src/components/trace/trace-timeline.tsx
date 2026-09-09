"use client";

import { useRef, useState, type PointerEvent } from "react";
import { RotateCcwIcon, ZoomInIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { formatDuration } from "@/lib/format";
import { formatOffset } from "@/components/trace/trace-format";
import { KIND_META, STATUS_META } from "@/components/trace/trace-meta";
import type { TraceEntry, TraceEntryKind } from "@/components/trace/trace-model";
import { cn } from "@/lib/utils";

export interface TimeRange { start: number; end: number }

const LANES: Array<{ kind: TraceEntryKind; label: string }> = [
  { kind: "message", label: "Messages" },
  { kind: "model", label: "Model" },
  { kind: "tool", label: "Tools" },
  { kind: "suspension", label: "Waiting" },
  { kind: "step", label: "Steps" },
];

function percent(value: number, range: TimeRange): number {
  return ((value - range.start) / Math.max(range.end - range.start, 0.001)) * 100;
}

function pointerTime(event: PointerEvent<HTMLDivElement>, range: TimeRange): number {
  const rect = event.currentTarget.getBoundingClientRect();
  const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  return range.start + fraction * (range.end - range.start);
}

export function TraceTimeline({
  entries, totalSeconds, range, selectedKey, onSelect, onRangeChange,
}: {
  entries: TraceEntry[];
  totalSeconds: number;
  range: TimeRange;
  selectedKey: string | null;
  onSelect: (key: string) => void;
  onRangeChange: (range: TimeRange) => void;
}) {
  const resizeStart = useRef<{ y: number; height: number } | null>(null);
  const [dragStart, setDragStart] = useState<number | null>(null);
  const [dragEnd, setDragEnd] = useState<number | null>(null);
  const [height, setHeight] = useState(232);
  const fullRange = range.start <= 0 && range.end >= totalSeconds;
  const timed = entries.filter(
    (entry) => entry.timed && entry.offsetSeconds !== null && entry.durationSeconds !== null,
  );

  function beginDrag(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    const value = pointerTime(event, range);
    setDragStart(value);
    setDragEnd(value);
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    if (dragStart !== null) setDragEnd(pointerTime(event, range));
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>) {
    const start = dragStart;
    if (start === null) return;
    const end = pointerTime(event, range);
    setDragStart(null);
    setDragEnd(null);
    const low = Math.max(0, Math.min(start, end));
    const high = Math.min(totalSeconds, Math.max(start, end));
    if (high - low >= Math.max(totalSeconds * 0.002, 0.01)) onRangeChange({ start: low, end: high });
  }

  function resize(event: PointerEvent<HTMLDivElement>) {
    if (!resizeStart.current) return;
    const next = resizeStart.current.height + event.clientY - resizeStart.current.y;
    setHeight(Math.min(460, Math.max(170, next)));
  }

  const selection = dragStart === null || dragEnd === null ? null : {
    left: percent(Math.min(dragStart, dragEnd), range),
    width: Math.abs(percent(dragEnd, range) - percent(dragStart, range)),
  };

  return (
    <section
      className="relative flex shrink-0 flex-col bg-surface/35"
      style={{ height }}
      aria-label="Execution timeline"
    >
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border/70 px-4 py-2">
        <h2 className="text-caption font-semibold">Timeline</h2>
        <span className="text-micro text-muted-foreground">
          {timed.length} timed events / {formatDuration(range.end - range.start)} visible
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5 text-micro text-muted-foreground">
          <ZoomInIcon className="size-3.5" aria-hidden /> Drag to zoom
        </span>
        {!fullRange && (
          <Button type="button" variant="outline" size="sm" className="h-7 gap-1.5 text-micro"
            onClick={() => onRangeChange({ start: 0, end: totalSeconds })}>
            <RotateCcwIcon className="size-3" aria-hidden /> Reset
          </Button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        <div className="grid min-w-[42rem] grid-cols-[5.25rem_1fr]">
          <div className="pt-5">
            {LANES.map((lane) => <div key={lane.kind}
              className="flex h-7 items-center text-micro text-muted-foreground">{lane.label}</div>)}
          </div>
          <div>
            <div className="relative h-5 border-b border-border text-micro text-muted-foreground">
              {[0, .25, .5, .75, 1].map((fraction) => {
                const value = range.start + fraction * (range.end - range.start);
                return <span key={fraction}
                  className="absolute -translate-x-1/2 tabular first:translate-x-0 last:-translate-x-full"
                  style={{ left: `${fraction * 100}%` }}>{formatOffset(value).replace("T+", "")}</span>;
              })}
            </div>
            <div className="relative cursor-crosshair touch-none select-none"
              onPointerDown={beginDrag} onPointerMove={moveDrag} onPointerUp={finishDrag}
              onPointerCancel={() => { setDragStart(null); setDragEnd(null); }}>
              {[0, .25, .5, .75, 1].map((fraction) => <span key={fraction}
                className="pointer-events-none absolute inset-y-0 border-l border-border/50"
                style={{ left: `${fraction * 100}%` }} />)}
              {LANES.map((lane) => <div key={lane.kind}
                className="relative h-7 border-b border-border/60 last:border-0">
                {timed.filter((entry) => entry.kind === lane.kind).map((entry) => {
                  const start = entry.offsetSeconds ?? 0;
                  const end = start + (entry.durationSeconds ?? 0);
                  if (end < range.start || start > range.end) return null;
                  const kind = KIND_META[entry.kind];
                  const status = STATUS_META[entry.status];
                  const left = Math.max(0, percent(start, range));
                  const width = Math.max(.35, Math.min(100, percent(end, range)) - left);
                  return <button key={entry.key} type="button"
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() => onSelect(entry.key)}
                    className={cn("absolute top-1.5 h-4 rounded-sm transition-opacity hover:opacity-80",
                      status.bar || kind.bar, selectedKey === entry.key && "ring-2 ring-foreground")}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${entry.label}: ${entry.summary}`} />;
                })}
              </div>)}
              {selection && <div className="pointer-events-none absolute inset-y-0 border-x border-ring bg-ring/15"
                style={{ left: `${selection.left}%`, width: `${selection.width}%` }} />}
            </div>
          </div>
        </div>
      </div>
      {!fullRange && <p className="border-t border-border/70 px-4 py-1.5 text-micro text-muted-foreground">
        Showing {formatOffset(range.start)} to {formatOffset(range.end)}. The call table follows this range.
      </p>}
      <div
        role="separator"
        aria-label="Resize timeline"
        aria-orientation="horizontal"
        aria-valuemin={170}
        aria-valuemax={460}
        aria-valuenow={height}
        tabIndex={0}
        className="group absolute inset-x-0 bottom-0 z-20 h-2 translate-y-1/2 cursor-row-resize touch-none outline-none"
        onPointerDown={(event) => {
          resizeStart.current = { y: event.clientY, height };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={resize}
        onPointerUp={(event) => {
          resizeStart.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onPointerCancel={() => { resizeStart.current = null; }}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp") setHeight((value) => Math.max(170, value - 12));
          if (event.key === "ArrowDown") setHeight((value) => Math.min(460, value + 12));
        }}
      >
        <span className="absolute inset-x-0 top-1/2 h-px bg-border transition-colors group-hover:bg-primary group-focus-visible:bg-primary" />
      </div>
    </section>
  );
}
