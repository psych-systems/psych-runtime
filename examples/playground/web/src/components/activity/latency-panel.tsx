import { formatDuration } from "@/lib/format";
import { clampFraction } from "@/components/trace/trace-format";
import { cn } from "@/lib/utils";
import type { LatencyReport } from "@/lib/types";

/**
 * Where the Run's wall clock went.
 *
 * `unaccounted_seconds` is a first class number (DESIGN.md §13.3), not the gap
 * a broken chart leaves: it is the time a Run spent queued, leased but not
 * calling anything, or waiting on a person. Labelling it "other" or hiding it
 * would make every Run look like it was working the whole time it existed.
 *
 * It can legitimately be negative when two Workers' clocks disagree, so a
 * negative value is stated rather than clamped. Only the bar is clamped, and
 * then the bar says so.
 */
export function LatencyPanel({ latency }: { latency: LatencyReport }) {
  const wall = Math.max(latency.wall_clock_seconds, 0.001);
  const modelFraction = clampFraction(latency.model_seconds / wall);
  const toolFraction = clampFraction(latency.tool_seconds / wall);
  const unaccountedFraction = Math.max(0, 1 - modelFraction - toolFraction);
  const skewed = latency.unaccounted_seconds < 0;

  const rows: { label: string; seconds: number; swatch: string; note: string }[] = [
    {
      label: "Model time",
      seconds: latency.model_seconds,
      swatch: "bg-chart-1",
      note: "waiting on the model, first token to last",
    },
    {
      label: "Tool time",
      seconds: latency.tool_seconds,
      swatch: "bg-chart-3",
      note: "inside tool calls",
    },
    {
      label: "Unaccounted",
      seconds: latency.unaccounted_seconds,
      swatch: "bg-muted-foreground/40",
      note: skewed
        ? "negative, which means two workers' clocks disagree. Real, and not an error in the run."
        : "queueing, leasing, and waiting on a person. Real time, not a gap.",
    },
  ];

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface/40 p-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
          Wall clock
        </span>
        <span className="tabular text-lg leading-none font-semibold">
          {formatDuration(latency.wall_clock_seconds)}
        </span>
      </div>

      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full bg-chart-1"
          style={{ width: `${modelFraction * 100}%` }}
          title="Model time"
        />
        <div
          className="h-full bg-chart-3"
          style={{ width: `${toolFraction * 100}%` }}
          title="Tool time"
        />
        <div
          className="h-full bg-[repeating-linear-gradient(135deg,var(--color-muted-foreground)_0,var(--color-muted-foreground)_3px,transparent_3px,transparent_6px)] opacity-40"
          style={{ width: `${unaccountedFraction * 100}%` }}
          title="Unaccounted"
        />
      </div>

      <dl className="flex flex-col gap-2">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline gap-2">
            <span className={cn("size-2 shrink-0 translate-y-[-1px] rounded-full", row.swatch)} aria-hidden />
            <dt className="text-body">{row.label}</dt>
            <dd
              className={cn(
                "tabular ml-auto text-body font-medium",
                row.label === "Unaccounted" && skewed && "text-status-failed"
              )}
            >
              {formatDuration(row.seconds)}
            </dd>
            <dd className="w-full pl-4 text-caption text-muted-foreground">{row.note}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
