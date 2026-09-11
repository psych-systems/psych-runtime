"use client";

import Link from "next/link";
import { AlertCircleIcon, ArrowLeftIcon, MessagesSquareIcon, RefreshCwIcon, WaypointsIcon } from "lucide-react";

import { Copyable } from "@/components/activity/copyable";
import { RunStatePanel } from "@/components/activity/run-state-panel";
import { RunViewSwitcher } from "@/components/activity/run-view-switcher";
import { costText } from "@/components/trace/thread-summary";
import { Button } from "@/components/ui/button";
import { DetailRow, Page, PageHeader, Section, Stat, TechnicalDetails } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle, type Lifecycle } from "@/components/ui/status";
import { useRunStatus } from "@/hooks/use-run-status";
import { useThreadReport } from "@/hooks/use-thread-report";
import { formatClockTime, formatCost, formatDayLabel, formatDuration, formatTokens, sumUsageTokens } from "@/lib/format";
import type { RunReport, ThreadMessage, ThreadTotals } from "@/lib/types";

const SETTLED = new Set(["done", "failed", "stopped"]);

/**
 * Operational history for a conversation.
 *
 * The route keeps a Run id because every message is a Run, but the page is a
 * conversation view. One thread-report request supplies the aggregate bill,
 * every Run report and every recorded message, so the summary and message
 * cards cover the same chain and cannot drift apart.
 */
export function RunDetail({ runId }: { runId: string }) {
  const { thread, loading, error, refresh: refreshThread } = useThreadReport(runId);
  const latestRunId = thread?.run_ids[thread.run_ids.length - 1] ?? runId;
  const { status, error: statusError, refresh: refreshStatus } = useRunStatus(latestRunId);

  const refresh = () => {
    refreshThread();
    refreshStatus();
  };

  if (loading && thread === null) {
    return (
      <Page>
        <Skeleton className="h-16 w-full rounded-md" />
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-72 w-full rounded-xl" />
      </Page>
    );
  }

  if (error !== null && thread === null) {
    return (
      <Page>
        <PageHeader title="Conversation activity" description="This conversation could not be loaded." />
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-body text-destructive">
          <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refresh}><RefreshCwIcon className="size-3.5" />Try again</Button>
          <Button asChild variant="ghost" size="sm"><Link href="/conversations"><ArrowLeftIcon className="size-3.5" />All conversations</Link></Button>
        </div>
      </Page>
    );
  }

  if (thread === null) return null;

  const firstReport = thread.reports[0] ?? null;
  const latestReport = thread.reports[thread.reports.length - 1] ?? null;
  const title = firstReport?.spec_name ?? "Conversation";
  const lifecycle = status?.lifecycle ?? toLifecycle(latestReport?.terminal_state);
  const messagesByRun = groupMessages(thread.messages);

  return (
    <Page>
      <PageHeader
        title={title}
        description={firstReport
          ? `${thread.run_ids.length} ${thread.run_ids.length === 1 ? "message" : "messages"} · started ${formatDayLabel(firstReport.admitted_at)} at ${formatClockTime(firstReport.admitted_at)}`
          : `${thread.run_ids.length} messages`}
        actions={<><StatusPill state={lifecycle} /><RunViewSwitcher runId={latestRunId} active="activity" /><Button asChild variant="ghost" size="sm"><Link href={`/chat/${latestRunId}`}><MessagesSquareIcon className="size-3.5" />Open in chat</Link></Button></>}
      />

      <Link href="/conversations" className="-mt-3 inline-flex w-fit items-center gap-1.5 text-caption text-muted-foreground transition-colors hover:text-foreground">
        <ArrowLeftIcon className="size-3.5" aria-hidden />All conversations
      </Link>

      {(error !== null || statusError !== null) && (
        <p className="flex items-start gap-1.5 text-caption text-status-failed"><AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden /><span>Some live details could not be loaded: {error ?? statusError}</span></p>
      )}

      <Section title="Conversation overview" description="Totals across every message in this conversation.">
        <div className="grid gap-4 rounded-xl border border-border bg-card p-4 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Messages" value={thread.totals.messages.toLocaleString()} />
          <Stat label="Model calls" value={thread.totals.model_calls.toLocaleString()} />
          <Stat label="Tool calls" value={thread.totals.tool_calls.toLocaleString()} />
          <Stat label="Elapsed" value={formatDuration(thread.totals.wall_clock_seconds)} />
        </div>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <BreakdownCard
          title="Tokens"
          total={formatTokens(thread.totals.total_tokens)}
          hint={usageHint(thread.totals)}
          values={[
            ["Input", formatTokens(thread.totals.input_tokens)],
            ["Output", formatTokens(thread.totals.output_tokens)],
            ["Cache read", formatTokens(thread.totals.cache_read_tokens)],
            ["Cache write", formatTokens(thread.totals.cache_write_tokens)],
          ]}
        />
        <BreakdownCard
          title="Cost"
          total={costText(thread.totals)}
          hint={costHint(thread.totals)}
          muted={thread.totals.cost_amount === null}
          values={[
            ["Input", formatMoneyPart(thread.totals.cost_input_amount, thread.totals.cost_currency)],
            ["Output", formatMoneyPart(thread.totals.cost_output_amount, thread.totals.cost_currency)],
            ["Cache read", formatMoneyPart(thread.totals.cost_cache_read_amount, thread.totals.cost_currency)],
            ["Cache write", formatMoneyPart(thread.totals.cost_cache_write_amount, thread.totals.cost_currency)],
          ]}
        />
      </div>

      <Section title="Conversation activity" description="Each message starts a Run. Its request, outcome and operational cost stay together.">
        <div className="flex flex-col gap-3">
          {thread.reports.map((report, index) => (
            <MessageActivity key={report.run_id} report={report} messages={messagesByRun.get(report.run_id) ?? []} index={index} lifecycle={report.run_id === latestRunId && status ? status.lifecycle : toLifecycle(report.terminal_state)} />
          ))}
        </div>
      </Section>

      {latestReport && (
        <Section title="Latest runtime state" description="The reducer's current view of the newest message, including open calls, queues and recovery state.">
          <RunStatePanel runId={latestRunId} live={status !== null && !SETTLED.has(status.lifecycle)} />
        </Section>
      )}

      <TechnicalDetails className="pt-2" label="Conversation identifiers">
        {thread.run_ids.map((id, index) => <DetailRow key={id} label={`Message ${index + 1} run id`}><Copyable value={id} /></DetailRow>)}
        {status && <DetailRow label="Latest version hash"><Copyable value={status.version_hash} /></DetailRow>}
      </TechnicalDetails>
    </Page>
  );
}

function MessageActivity({ report, messages, index, lifecycle }: { report: RunReport; messages: ThreadMessage[]; index: number; lifecycle: Lifecycle }) {
  const asked = messages.find((message) => message.role === "user")?.content.trim() ?? "";
  const answer = messages.filter((message) => message.role === "assistant" && message.content.trim().length > 0).map((message) => message.content.trim()).join("\n\n");
  const failure = report.failure?.message ?? null;

  return (
    <article className="overflow-hidden rounded-xl border border-border bg-surface/40">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-4 py-3">
        <span className="font-semibold">Message {index + 1}</span>
        <StatusPill state={lifecycle} size="sm" />
        <span className="text-caption text-muted-foreground">{formatDayLabel(report.admitted_at)} at {formatClockTime(report.admitted_at)}</span>
        <Button asChild variant="ghost" size="sm" className="ml-auto"><Link href={`/activity/${report.run_id}/trace`}><WaypointsIcon className="size-3.5" />Trace this message</Link></Button>
      </header>
      <div className="grid gap-5 px-4 py-4 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="flex min-w-0 flex-col gap-4">
          <Exchange label="Asked" text={asked} fallback="No user message was recorded." />
          <Exchange label={failure ? "Stopped" : "Answered"} text={failure ?? answer} fallback={lifecycle === "running" || lifecycle === "queued" ? "Still working." : "No assistant response was recorded."} failed={failure !== null} />
        </div>
        <dl className="grid shrink-0 grid-cols-2 gap-x-6 gap-y-3 border-t border-border pt-4 text-caption lg:w-64 lg:border-t-0 lg:border-l lg:pt-0 lg:pl-5">
          <Metric label="Model calls" value={report.totals.model_calls.toLocaleString()} />
          <Metric label="Tool calls" value={report.totals.tool_calls.toLocaleString()} />
          <Metric label="Total tokens" value={formatTokens(sumUsageTokens(report.totals.usage))} />
          <Metric label="Total cost" value={formatCost(report.totals.cost)} />
          <Metric label="Elapsed" value={formatDuration(report.totals.latency.wall_clock_seconds)} />
          <Metric label="Recovery" value={report.orphaned_attempt_id ? "Recovered" : "None"} />
        </dl>
      </div>
    </article>
  );
}

function Exchange({ label, text, fallback, failed = false }: { label: string; text: string; fallback: string; failed?: boolean }) {
  const shown = text.length > 0;
  return <div className="flex min-w-0 flex-col gap-1.5"><span className={`text-micro font-medium tracking-wide uppercase ${failed ? "text-status-failed" : "text-muted-foreground"}`}>{label}</span><p className={`text-prose whitespace-pre-wrap ${shown ? "" : "text-muted-foreground italic"}`}>{shown ? text : fallback}</p></div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="flex min-w-0 flex-col gap-0.5"><dt className="text-micro font-medium tracking-wide text-muted-foreground uppercase">{label}</dt><dd className="tabular font-medium">{value}</dd></div>;
}

function BreakdownCard({ title, total, hint, values, muted = false }: { title: string; total: string; hint: string; values: [string, string][]; muted?: boolean }) {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="mt-4 border-b border-border pb-4">
        <Stat label="Total" value={total} hint={hint} tone={muted ? "muted" : undefined} />
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-4">
        {values.map(([label, value]) => <Metric key={label} label={label} value={value} />)}
      </dl>
    </section>
  );
}

function groupMessages(messages: ThreadMessage[]): Map<string, ThreadMessage[]> {
  const grouped = new Map<string, ThreadMessage[]>();
  for (const message of messages) {
    const current = grouped.get(message.run_id) ?? [];
    current.push(message);
    grouped.set(message.run_id, current);
  }
  return grouped;
}

function usageHint(totals: ThreadTotals): string {
  if (totals.usage_source === "unknown") return "Provider did not report usage";
  if (totals.unreported_usage_calls > 0) return `Partial · ${totals.unreported_usage_calls} calls unreported`;
  return "Provider reported";
}

function costHint(totals: ThreadTotals): string {
  if (totals.cost_amount === null) return totals.unpriced_model_calls > 0 ? `${totals.unpriced_model_calls} calls have no rate` : "No priced model call";
  const source = totals.cost_source === "provider" ? "Provider reported" : totals.cost_source === "mixed" ? "Provider and estimated" : "Estimated from rates";
  return totals.cost_is_incomplete ? `${source} · partial` : source;
}

function formatMoneyPart(amount: string | null, currency: string | null): string {
  if (amount === null) return "Not reported";
  const numeric = Number(amount);
  if (!Number.isFinite(numeric)) return "Not reported";
  const shown = numeric > 0 && numeric < 0.01 ? numeric.toFixed(4) : numeric.toFixed(2);
  return `${currency === null || currency === "USD" ? "$" : `${currency} `}${shown}`;
}
