import { formatCost, formatDuration, formatTokens } from "@/lib/format";
import type { RunReport } from "@/lib/types";
import { clampFraction } from "@/components/trace/trace-format";

/**
 * The Run's real latency breakdown, `totals.latency` rendered verbatim
 * rather than re-derived from the entries the timeline draws (DESIGN.md
 * §13.3 -- `unaccounted_seconds` is a first-class number, not a fallback
 * for a broken chart). A negative `unaccounted_seconds` (clock skew between
 * Workers) is shown as its own segment rather than clamped to zero, same as
 * the field itself.
 */
export function TraceSummary({ report }: { report: RunReport }) {
  const { latency } = report.totals;
  const wall = Math.max(latency.wall_clock_seconds, 0.001);
  const modelFrac = clampFraction(latency.model_seconds / wall);
  const toolFrac = clampFraction(latency.tool_seconds / wall);
  const unaccountedFrac = Math.max(0, 1 - modelFrac - toolFrac);

  return (
    <div className="flex flex-col gap-2 border-b border-border px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-caption">
        <Stat
          label="Wall clock"
          value={formatDuration(latency.wall_clock_seconds)}
        />
        <Stat
          label="Model time"
          value={formatDuration(latency.model_seconds)}
          tone="text-chart-1"
        />
        <Stat
          label="Tool time"
          value={formatDuration(latency.tool_seconds)}
          tone="text-chart-3"
        />
        <Stat
          label="Unaccounted"
          value={formatDuration(latency.unaccounted_seconds)}
          tone={
            latency.unaccounted_seconds < 0
              ? "text-status-failed"
              : "text-muted-foreground"
          }
        />
        <span className="ml-auto h-4 w-px bg-border" />
        {/* `cost === null` is "no call in this run had a known price", never
            zero, and `cost_is_incomplete` means the figure is real but
            partial. Both say so here: a bare total that quietly omitted an
            unpriced call would make metering look correct and be wrong. */}
        <Stat
          label="Cost"
          value={formatCost(report.totals.cost)}
          tone={
            report.totals.cost === null ? "text-muted-foreground" : undefined
          }
          hint={
            report.totals.cost === null
              ? report.totals.unpriced_model_calls > 0
                ? // Summaries are counted against the price table but are not
                  // model calls, so a Run that compacted once read "no known
                  // price for 4 of 3". The denominator is every call that
                  // could have had a rate. `thread-summary.tsx` says the same
                  // thing about the same numbers one level up.
                  `no known price for ${report.totals.unpriced_model_calls} of ${
                    report.totals.model_calls + report.totals.compaction_calls
                  }`
                : "no priced model call"
              : report.totals.cost_is_incomplete
                ? `partial, ${report.totals.unpriced_model_calls} without a known price`
                : undefined
          }
        />
        {/* Input and output only, and labelled as such: the cache split is on
            the run detail, and quietly folding cache reads into one "tokens"
            figure here would contradict it. */}
        <Stat
          label="Tokens in and out"
          value={formatTokens(
            report.totals.usage.input + report.totals.usage.output,
          )}
        />
      </div>

      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full bg-chart-1"
          style={{ width: `${modelFrac * 100}%` }}
          title="Model time"
        />
        <div
          className="h-full bg-chart-3"
          style={{ width: `${toolFrac * 100}%` }}
          title="Tool time"
        />
        <div
          className="h-full bg-[repeating-linear-gradient(135deg,var(--color-muted-foreground)_0,var(--color-muted-foreground)_3px,transparent_3px,transparent_6px)] opacity-40"
          style={{ width: `${unaccountedFrac * 100}%` }}
          title="Unaccounted"
        />
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-muted-foreground">{label} </span>
      <span className={`tabular ${tone ?? "text-foreground"}`}>{value}</span>
      {hint && <span className="ml-1 text-muted-foreground">({hint})</span>}
    </span>
  );
}
