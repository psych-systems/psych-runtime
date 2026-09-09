import { formatDuration, formatTokens } from "@/lib/format";
import type { ThreadTotals } from "@/lib/types";
import { clampFraction } from "@/components/trace/trace-format";

/**
 * A conversation's totals, at the top of the page rather than under it.
 *
 * A conversation is a chain of Runs: each message continues the previous one
 * rather than extending it. Every figure the console used to lead
 * with was therefore one message's share of a larger exchange, and reading
 * "22,511 tokens" at the top of a three-message conversation told you about a
 * third of it while looking like the whole.
 *
 * ## What is summed, and what is measured
 *
 * Tokens, cost and call counts are summed across the chain by the backend,
 * once, because two places adding money is two places to get it wrong
 * differently.
 *
 * `wall_clock_seconds` is **not** a sum. It spans the first message to the
 * last settlement, gaps included, because those gaps are a person reading and
 * typing and pretending they did not happen would report a two minute
 * exchange as eight seconds. Model and tool time sit beside it precisely so
 * the bar's width is never mistaken for work done: the striped remainder is
 * the waiting, and on a conversation it is usually most of it.
 *
 * ## Cost says which kind of number it is
 *
 * `null` means no call in the conversation had a known rate, never zero.
 * `cost_is_incomplete` means the figure is real but partial. Both are said
 * out loud, because a bare total that quietly dropped an unpriced call is
 * exactly the failure DESIGN.md §13.2 exists to prevent.
 *
 * ## Summaries appear only once there are some
 *
 * A compaction is a model call the agent never asked for, so it is not one of
 * the model calls beside it and its tokens are already inside the total. It
 * earns a place here anyway, and only when the count is above zero: without it
 * a conversation whose bill grew while its answers stayed the same length has
 * no explanation anywhere on the page.
 */
export function ThreadSummary({
  totals,
  scope,
}: {
  totals: ThreadTotals;
  /** What the numbers cover, so a reader is never guessing whether they are
   *  looking at one message or the whole exchange. */
  scope: string;
}) {
  const wall = Math.max(totals.wall_clock_seconds, 0.001);
  const modelFrac = clampFraction(totals.model_seconds / wall);
  const toolFrac = clampFraction(totals.tool_seconds / wall);
  const waitingFrac = Math.max(0, 1 - modelFrac - toolFrac);

  return (
    <div className="flex flex-col gap-2 border-b border-border bg-surface/40 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-caption">
        <span className="text-caption font-medium">{scope}</span>
        <span className="h-4 w-px bg-border" />
        <Stat label="Tokens" value={formatTokens(totals.total_tokens)} />
        <Stat label="Cost" value={costText(totals)} tone={costTone(totals)} hint={costHint(totals)} />
        <Stat label="Elapsed" value={formatDuration(totals.wall_clock_seconds)} />
        <Stat label="Model calls" value={totals.model_calls.toLocaleString()} />
        <Stat label="Tool calls" value={totals.tool_calls.toLocaleString()} />
        {totals.compaction_calls > 0 && (
          <Stat
            label="Summaries"
            value={totals.compaction_calls.toLocaleString()}
            hint="older messages replaced so the conversation could carry on"
          />
        )}
        {totals.failed_model_calls > 0 && (
          <Stat
            label="Failed calls"
            value={totals.failed_model_calls.toLocaleString()}
            tone="text-status-failed"
          />
        )}
      </div>

      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full bg-chart-1"
          style={{ width: `${modelFrac * 100}%` }}
          title={`Model time ${formatDuration(totals.model_seconds)}`}
        />
        <div
          className="h-full bg-chart-3"
          style={{ width: `${toolFrac * 100}%` }}
          title={`Tool time ${formatDuration(totals.tool_seconds)}`}
        />
        <div
          className="h-full bg-[repeating-linear-gradient(135deg,var(--color-muted-foreground)_0,var(--color-muted-foreground)_3px,transparent_3px,transparent_6px)] opacity-40"
          style={{ width: `${waitingFrac * 100}%` }}
          title="Waiting, mostly for a person to read and reply"
        />
      </div>

      <p className="text-micro text-muted-foreground">
        <span className="text-chart-1">Model {formatDuration(totals.model_seconds)}</span>
        {" · "}
        <span className="text-chart-3">Tools {formatDuration(totals.tool_seconds)}</span>
        {" · "}
        the rest is waiting, which on a conversation is mostly a person reading and typing.
      </p>
    </div>
  );
}

export function costText(totals: ThreadTotals): string {
  if (totals.cost_amount === null) return "unknown";
  const amount = Number(totals.cost_amount);
  const currency = totals.cost_currency ?? "USD";
  // Four decimals: a short conversation genuinely costs a fraction of a cent,
  // and rounding to two would print "$0.00" for something that was not free.
  const shown = amount < 0.01 && amount > 0 ? amount.toFixed(4) : amount.toFixed(2);
  return `${currency === "USD" ? "$" : `${currency} `}${shown}`;
}

function costTone(totals: ThreadTotals): string | undefined {
  return totals.cost_amount === null ? "text-muted-foreground" : undefined;
}

/**
 * What kind of number this is, in words.
 *
 * A figure the provider reported comes from the party doing the billing and
 * reconciles with an invoice. One Psych computed is as good as whatever price
 * table was in force. A total that blended them without saying so would look
 * like one number and be two, which is the thing worth avoiding on a page
 * somebody reads before paying a bill.
 */
function costHint(totals: ThreadTotals): string | undefined {
  if (totals.cost_amount === null) {
    // Summaries are counted against the price table but are not model calls,
    // so a conversation that compacted twice used to read "no rate for 4 of 3
    // calls". The denominator is every call that could have had a rate.
    const priceable = totals.model_calls + totals.compaction_calls;
    return totals.unpriced_model_calls > 0
      ? `no rate for ${totals.unpriced_model_calls} of ${priceable} calls, add one in Settings`
      : "no priced model call";
  }
  const provenance =
    totals.cost_source === "provider"
      ? "from your provider"
      : totals.cost_source === "mixed"
        ? `${totals.provider_reported_costs} from your provider, the rest estimated`
        : "estimated from rates";
  return totals.cost_is_incomplete
    ? `${provenance}, ${totals.unpriced_model_calls} without a rate`
    : provenance;
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
