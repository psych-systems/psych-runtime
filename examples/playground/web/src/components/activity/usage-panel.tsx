import { formatTokens } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { TotalsReport } from "@/lib/types";

/**
 * Tokens, split by cache state, never summed into one number.
 *
 * The split is the whole reason `Usage` has the shape it has: `cache_read` and
 * `cache_write` are disjoint from `input` and priced differently, so a single
 * "tokens" figure hides the one fact that explains the bill. `cache_write_1h`
 * is a subset of `cache_write`, not an addend, so it is shown as a note under
 * it rather than as a fifth column that would double-count.
 */
export function UsagePanel({ totals }: { totals: TotalsReport }) {
  const usage = totals.usage;
  const columns: { label: string; value: number; tone: string; note?: string }[] = [
    { label: "Input", value: usage.input, tone: "bg-chart-1" },
    { label: "Output", value: usage.output, tone: "bg-chart-2" },
    {
      label: "Cache read",
      value: usage.cache_read,
      tone: "bg-chart-3",
      note: "charged at the cached rate",
    },
    {
      label: "Cache write",
      value: usage.cache_write,
      tone: "bg-chart-4",
      note:
        usage.cache_write_1h > 0
          ? `${formatTokens(usage.cache_write_1h)} of these at the 1 hour rate`
          : undefined,
    },
  ];
  const total = columns.reduce((sum, c) => sum + c.value, 0);

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface/40 p-4">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        {columns.map((c) => (
          <div
            key={c.label}
            className={cn("h-full", c.tone)}
            style={{ width: total === 0 ? "0%" : `${(c.value / total) * 100}%` }}
            title={`${c.label}: ${formatTokens(c.value)}`}
          />
        ))}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
        {columns.map((c) => (
          <div key={c.label} className="flex flex-col gap-0.5">
            <dt className="flex items-center gap-1.5 text-micro font-medium tracking-wide text-muted-foreground uppercase">
              <span className={cn("size-2 rounded-full", c.tone)} aria-hidden />
              {c.label}
            </dt>
            <dd className="tabular text-lg leading-tight font-semibold">{formatTokens(c.value)}</dd>
            {c.note && <p className="text-micro text-muted-foreground">{c.note}</p>}
          </div>
        ))}
      </dl>

      {usage.reasoning > 0 && (
        <p className="text-caption text-muted-foreground">
          <span className="tabular text-foreground">{formatTokens(usage.reasoning)}</span> reasoning
          tokens are counted inside output, not beside it.
        </p>
      )}
    </div>
  );
}
