"use client";

import { AlertTriangleIcon } from "lucide-react";

import { formatDuration } from "@/lib/format";
import { KIND_META, STATUS_META } from "@/components/trace/trace-meta";
import type { TraceEntry, TraceEntryKind } from "@/components/trace/trace-model";
import { clampFraction } from "@/components/trace/trace-format";
import { cn } from "@/lib/utils";

// Steps get no lane of their own: every model call or tool call a step
// wraps already has its own bar, in its own lane, at its own (real, not
// derived) offset, so a step bar would only replot that same span a second
// time. The row list still shows every step; this is the waterfall only.
// Messages first: they are the frame the rest sits inside, and reading the
// waterfall top-down should answer "which message was this part of" before it
// answers "what did that message do".
const LANES: TraceEntryKind[] = ["message", "model", "tool", "suspension"];
const TICK_COUNT = 5;

/**
 * A bar narrower than this is a target nobody can hit and a shape nobody can
 * read. Most runs in this playground settle in well under a second, so
 * proportional width alone drew every call as a hairline and the waterfall
 * showed nothing at all. Bars below the floor are drawn at the floor and
 * carry their real duration as a label beside them, so the picture stays
 * legible without the number being fiction.
 */
const MIN_BAR_PX = 14;

/** Above this many bars in one lane the labels would overlap each other, so
 *  only the selected bar keeps its label. */
const LABEL_LIMIT = 8;

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
  const axisSeconds = Math.max(totalSeconds, 0.001);
  const lanes = LANES.map((kind) => ({
    kind,
    items: entries.filter((e) => e.kind === kind && e.timed),
  })).filter((lane) => lane.items.length > 0);
  const ticks = Array.from({ length: TICK_COUNT }, (_, i) => (axisSeconds * i) / (TICK_COUNT - 1));
  const anyDangling = entries.some((e) => e.status === "dangling");

  if (lanes.length === 0) {
    return (
      <div className="border-b border-border bg-surface/40 px-4 py-6 text-center text-body text-muted-foreground">
        Nothing timed to draw yet.
      </div>
    );
  }

  return (
    <div className="border-b border-border bg-surface/40 px-4 pt-3 pb-4">
      <div className="relative mb-2 h-3.5 font-technical text-micro text-muted-foreground">
        {ticks.map((t, i) => (
          <span
            key={t}
            className="absolute -translate-x-1/2 first:translate-x-0 last:-translate-x-full"
            style={{ left: `${(i / (TICK_COUNT - 1)) * 100}%` }}
          >
            {formatDuration(t)}
          </span>
        ))}
      </div>

      <div className="relative flex flex-col gap-2">
        <div className="pointer-events-none absolute inset-0">
          {ticks.map((t, i) => (
            <div
              key={t}
              className="absolute top-0 bottom-0 w-px bg-border/70"
              style={{ left: `${(i / (TICK_COUNT - 1)) * 100}%` }}
            />
          ))}
        </div>

        {lanes.map(({ kind, items }) => {
          const meta = KIND_META[kind];
          const labelAll = items.length <= LABEL_LIMIT;
          return (
            <div key={kind} className="flex items-center gap-2">
              <div className="flex w-24 shrink-0 items-center gap-1.5 text-caption text-muted-foreground">
                <meta.icon className={cn("size-3", meta.text)} aria-hidden />
                {meta.label}
              </div>
              <div className="relative h-7 min-w-0 flex-1 rounded-sm bg-muted/40">
                {items.map((entry) => {
                  const start = clampFraction((entry.offsetSeconds ?? 0) / axisSeconds);
                  const rawDuration =
                    entry.durationSeconds ?? Math.max(axisSeconds - (entry.offsetSeconds ?? 0), 0);
                  const width = clampFraction(rawDuration / axisSeconds);
                  const statusMeta = STATUS_META[entry.status];
                  const color = statusMeta.bar || meta.bar;
                  const dangling = entry.status === "dangling";
                  const showLabel = labelAll || selectedKey === entry.key;
                  // A bar near the right edge would hang its label off the
                  // end of the axis, which on a laptop scrolls the whole
                  // page sideways. Those label to the left instead.
                  const labelLeft = start > 0.66;
                  return (
                    <div
                      key={entry.key}
                      className="absolute top-0.5 bottom-0.5"
                      style={{
                        left: `${start * 100}%`,
                        width: `${width * 100}%`,
                        minWidth: `${MIN_BAR_PX}px`,
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => onSelect(entry.key)}
                        title={`${entry.label}, ${entry.summary}`}
                        aria-label={`${entry.label}, ${statusMeta.label}, ${formatDuration(entry.durationSeconds)}`}
                        className={cn(
                          "h-full w-full rounded-[3px] outline outline-2 outline-offset-1 outline-transparent transition-[filter,outline-color] hover:brightness-110",
                          color,
                          statusMeta.dashed &&
                            "[background-image:repeating-linear-gradient(135deg,transparent,transparent_3px,rgba(0,0,0,0.28)_3px,rgba(0,0,0,0.28)_5px)]",
                          dangling && "outline-dashed outline-status-failed",
                          entry.ongoing && "animate-pulse",
                          selectedKey === entry.key && "outline-ring"
                        )}
                      />
                      {showLabel && (
                        <span
                          className={cn(
                            "pointer-events-none absolute top-1/2 -translate-y-1/2 whitespace-nowrap font-technical text-micro",
                            labelLeft ? "right-full mr-1.5" : "left-full ml-1.5",
                            dangling ? "text-status-failed" : "text-muted-foreground"
                          )}
                        >
                          {dangling ? (
                            <span className="inline-flex items-center gap-0.5">
                              <AlertTriangleIcon className="size-2.5" aria-hidden />
                              never finished
                            </span>
                          ) : (
                            formatDuration(entry.durationSeconds)
                          )}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {anyDangling && (
        <p className="mt-2.5 flex items-start gap-1.5 text-caption text-status-failed">
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>
            A hatched bar was started and never settled. That is the signature of a worker that died
            mid-call, and the reason another worker had to reclaim the lease.
          </span>
        </p>
      )}
    </div>
  );
}
