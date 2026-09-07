"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  ChevronRightIcon,
  MessagesSquareIcon,
  RefreshCwIcon,
  WaypointsIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DetailRow,
  Page,
  PageHeader,
  Section,
  Stat,
  TechnicalDetails,
} from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill } from "@/components/ui/status";
import { AnswerPanel } from "@/components/activity/answer-panel";
import { Copyable, CopyButton } from "@/components/activity/copyable";
import { LatencyPanel } from "@/components/activity/latency-panel";
import { UsagePanel } from "@/components/activity/usage-panel";
import { useRunAnswer } from "@/components/activity/use-run-answer";
import { RunStatePanel } from "@/components/activity/run-state-panel";
import { useRunThread } from "@/components/activity/use-run-thread";
import { useRunReport } from "@/hooks/use-run-report";
import { useThreadReport } from "@/hooks/use-thread-report";
import { ThreadSummary } from "@/components/trace/thread-summary";
import { useRunStatus } from "@/hooks/use-run-status";
import { formatClockTime, formatCost, formatDayLabel, formatDuration, formatTokens, sumUsageTokens } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { RunReport } from "@/lib/types";

/** Lifecycles after which the state cannot change, so one read is enough. */
const SETTLED = new Set(["done", "failed", "stopped"]);

/**
 * One Run, explained before it is enumerated.
 *
 * Activity is the one surface where the technical view is allowed to live, and
 * this is its landing page for a single Run. It still leads in plain words:
 * what the person asked, what the agent answered or why it stopped. The
 * numbers come next, and the identifiers, which are the reason an operator
 * came here at all, sit one disclosure down where they are copyable rather
 * than retyped.
 */
export function RunDetail({ runId }: { runId: string }) {
  const { status, loading: statusLoading, error: statusError, refresh: refreshStatus } = useRunStatus(runId);
  const { report, loading: reportLoading, error: reportError, refresh: refreshReport } = useRunReport(runId);
  const { thread } = useRunThread(runId);
  const { thread: threadReport } = useThreadReport(runId);
  // The answer comes from the library's own split of the log, not from
  // guessing at the last assistant message with text in it: that guess is
  // wrong for every run that ended on a tool result, and it made Activity and
  // Chat disagree about what the same run concluded.
  const { answer, loading: answerLoading, error: answerError } = useRunAnswer(runId);

  const messages = useMemo(
    () => (thread?.messages ?? []).filter((m) => m.run_id === runId),
    [thread, runId]
  );
  const ask = messages.find((m) => m.role === "user") ?? null;

  const refresh = () => {
    refreshStatus();
    refreshReport();
  };

  const loading = (statusLoading && status === null) || (reportLoading && report === null);
  const error = statusError ?? reportError;

  if (loading) {
    return (
      <Page>
        <Skeleton className="h-9 w-64 rounded-md" />
        <Skeleton className="h-28 w-full rounded-xl" />
        <Skeleton className="h-20 w-full rounded-xl" />
        <Skeleton className="h-40 w-full rounded-xl" />
      </Page>
    );
  }

  if (error !== null && status === null && report === null) {
    return (
      <Page>
        <PageHeader title="Run" description="This run could not be loaded." />
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-body text-destructive">
          <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCwIcon className="size-3.5" />
            Try again
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link href="/activity">
              <ArrowLeftIcon className="size-3.5" />
              All activity
            </Link>
          </Button>
        </div>
      </Page>
    );
  }

  const totals = report?.totals ?? null;
  const agentName = report?.spec_name ?? status?.scope.tenant ?? "Run";
  const runIds = thread?.run_ids ?? [];

  return (
    <Page>
      <PageHeader
        title={agentName}
        description={
          report
            ? `${formatDayLabel(report.admitted_at)} at ${formatClockTime(report.admitted_at)}`
            : undefined
        }
        actions={
          <div className="flex items-center gap-2">
            {status && <StatusPill state={status.lifecycle} />}
            <Button asChild variant="outline" size="sm">
              <Link href={`/activity/${runId}/trace`}>
                <WaypointsIcon className="size-3.5" />
                Trace
              </Link>
            </Button>
            <Button asChild variant="ghost" size="sm">
              <Link href={`/chat/${runId}`}>
                <MessagesSquareIcon className="size-3.5" />
                Open in chat
              </Link>
            </Button>
          </div>
        }
      />

      <Link
        href="/activity"
        className="-mt-3 inline-flex w-fit items-center gap-1.5 text-caption text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeftIcon className="size-3.5" aria-hidden />
        All activity
      </Link>

      {error !== null && (
        <p className="flex items-start gap-1.5 text-caption text-status-failed">
          <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>Some of this page could not be loaded: {error}</span>
        </p>
      )}

      {/* The conversation's numbers, first. This page used to lead with what
          one message did and put its figures below the fold, so the headline
          number was a fraction of the exchange and you had to scroll to find
          even that. A conversation is a chain of Runs and the bill
          spans all of them. */}
      {threadReport && (
        <div className="overflow-hidden rounded-xl border border-border">
          <ThreadSummary
            totals={threadReport.totals}
            scope={
              threadReport.run_ids.length === 1
                ? "This conversation"
                : `This conversation · ${threadReport.run_ids.length} messages`
            }
          />
        </div>
      )}

      <Section title="What happened">
        <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface/40 p-4">
          <Exchange label="Asked" text={ask?.content ?? null} fallback="No opening message recorded." />
          {status?.failure_message ? (
            <div className="flex flex-col gap-1.5 border-t border-border pt-3">
              <span className="text-micro font-medium tracking-wide text-status-failed uppercase">
                Stopped
              </span>
              <p className="text-prose text-status-failed">{status.failure_message}</p>
              {status.failure_kind && (
                <span className="w-fit rounded-full bg-muted px-2 py-0.5 font-technical text-micro text-muted-foreground">
                  {status.failure_kind}
                </span>
              )}
              {report?.failure?.traceback && (
                <TechnicalDetails label="Traceback" className="mt-1">
                  <pre className="max-h-72 overflow-y-auto rounded-md bg-muted/60 p-2 font-technical text-micro whitespace-pre-wrap text-status-failed/90">
                    {report.failure.traceback}
                  </pre>
                </TechnicalDetails>
              )}
            </div>
          ) : null}

          <div className="border-t border-border pt-3">
            <AnswerPanel
              answer={answer}
              runId={runId}
              lifecycle={status?.lifecycle ?? null}
              // The failure already has its own block above with the kind and
              // the traceback under it, so this does not restate it.
              failureMessage={null}
              loading={answerLoading}
              error={answerError}
            />
          </div>

          {status?.pending_approval && (
            <div className="flex flex-col gap-1 rounded-lg border border-status-waiting/40 bg-status-waiting/5 p-3">
              <span className="text-micro font-medium tracking-wide text-status-waiting uppercase">
                Waiting for approval
              </span>
              <p className="text-body">
                {status.pending_approval.question ??
                  `Approval needed before running ${status.pending_approval.tool}.`}
              </p>
              <p className="text-caption text-muted-foreground">
                Decide in the conversation. It expires{" "}
                {formatClockTime(status.pending_approval.expires_at)}.
              </p>
            </div>
          )}
        </div>
      </Section>

      <Section
        title="This message alone"
        description="A turn budget applies to one message rather than to the exchange, so these are deliberately not summed above."
      >
        <div className="flex flex-wrap gap-x-8 gap-y-4 rounded-xl border border-border bg-surface/40 p-4">
          <Stat label="Turns" value={status ? status.turn : "-"} />
          <Stat label="Tool calls" value={status ? status.tool_calls : "-"} />
          <Stat label="Model calls" value={status ? status.model_calls : "-"} />
          <Stat
            label="Duration"
            value={totals ? formatDuration(totals.latency.wall_clock_seconds) : "-"}
            hint={report?.settled_at === null ? "still running" : undefined}
          />
          <Stat
            label="Tokens"
            value={totals ? formatTokens(sumUsageTokens(totals.usage)) : "-"}
            hint="every cache state"
          />
          <CostStat report={report} />
        </div>
      </Section>

      {report && (
        <Section
          title="What the agent was told"
          description="The system prompt this run was assembled with. It can differ turn by turn when a tool is withheld or a server stops answering, and the trace shows each turn's own text."
        >
          <RunPrompt prompt={report.system_prompt} runId={runId} />
        </Section>
      )}

      {totals && (
        <Section
          title="Tokens by cache state"
          description="Cache reads and writes are separate from input and priced differently, so they are never added together here."
        >
          <UsagePanel totals={totals} />
        </Section>
      )}

      {totals && (
        <Section
          title="Where the time went"
          description="Model time and tool time are measured. Everything else is real waiting, and is named rather than dropped."
        >
          <LatencyPanel latency={totals.latency} />
        </Section>
      )}

      {report && report.failure_streak_trips.length > 0 && (
        <Section
          title="Repeated tool failures"
          description="The runtime stopped the model repeating a tool that kept failing."
        >
          <ul className="flex flex-col gap-2">
            {report.failure_streak_trips.map((trip) => (
              <li
                key={`${trip.tool}:${trip.at}`}
                className="rounded-lg border border-border bg-surface/40 px-3 py-2 text-body"
              >
                <span className="font-technical">{trip.tool}</span> failed{" "}
                <span className="tabular">{trip.streak}</span> times in a row, at a threshold of{" "}
                <span className="tabular">{trip.threshold}</span>.
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section
        title="What the runtime believes"
        description="The reducer's working object, folded from this run's log. Open tool calls, the three queues, failure streaks, compaction boundaries and every child, exactly as a worker would see them on reclaim."
      >
        <RunStatePanel runId={runId} live={status !== null && !SETTLED.has(status.lifecycle)} />
      </Section>

      {runIds.length > 1 && (
        <Section
          title="Other turns in this conversation"
          description="Each message starts its own run, and each run has its own numbers."
        >
          <div className="flex flex-wrap gap-2">
            {runIds.map((id, index) => (
              <Link
                key={id}
                href={`/activity/${id}`}
                className={cn(
                  "rounded-lg border px-3 py-1.5 text-caption font-medium transition-colors",
                  id === runId
                    ? "border-foreground/30 bg-foreground text-background"
                    : "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
                )}
              >
                Message {index + 1}
              </Link>
            ))}
          </div>
        </Section>
      )}

      <TechnicalDetails className="pt-2">
        <DetailRow label="Run id">
          <Copyable value={runId} />
        </DetailRow>
        {status && (
          <DetailRow label="Version hash">
            <Copyable value={status.version_hash} />
          </DetailRow>
        )}
        {report && <DetailRow label="Spec name">{report.spec_name}</DetailRow>}
        {status && (
          <DetailRow label="Tenant">
            {status.scope.tenant}
            {status.scope.principal ? ` / ${status.scope.principal}` : ""}
          </DetailRow>
        )}
        {status && (
          <DetailRow label="Attempts">
            <span className="tabular">{status.attempt_count}</span>
            {status.current_attempt && (
              <>
                {" "}
                <Copyable value={status.current_attempt} />
              </>
            )}
          </DetailRow>
        )}
        {status?.terminal_state && <DetailRow label="Terminal state">{status.terminal_state}</DetailRow>}
        {status?.failure_kind && <DetailRow label="Failure kind">{status.failure_kind}</DetailRow>}
        {status?.deadline_at && (
          <DetailRow label="Deadline">
            {formatDayLabel(status.deadline_at)} at {formatClockTime(status.deadline_at)}
          </DetailRow>
        )}
        {status?.continues_run_id && (
          <DetailRow label="Continues run">
            <Copyable value={status.continues_run_id} />
          </DetailRow>
        )}
        {status?.parent_run_id && (
          <DetailRow label="Parent run">
            <Copyable value={status.parent_run_id} />
          </DetailRow>
        )}
        {report?.orphaned_attempt_id && (
          <DetailRow label="Orphaned attempt">
            <Copyable value={report.orphaned_attempt_id} />
          </DetailRow>
        )}
        {status && (
          <DetailRow label="Head sequence">
            <span className="tabular">{status.head_seq}</span>
          </DetailRow>
        )}
        {status?.has_dangling_tool_calls && (
          <DetailRow label="Dangling tool calls">
            yes, a call was started and never settled. The trace shows which.
          </DetailRow>
        )}
        {report && <DetailRow label="Admitted at">{report.admitted_at}</DetailRow>}
        {report?.settled_at && <DetailRow label="Settled at">{report.settled_at}</DetailRow>}
      </TechnicalDetails>
    </Page>
  );
}

function Exchange({
  label,
  text,
  fallback,
}: {
  label: string;
  text: string | null;
  fallback: string;
}) {
  const shown = text !== null && text.trim().length > 0;
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      <p className={cn("text-prose whitespace-pre-wrap", !shown && "text-muted-foreground italic")}>
        {shown ? text : fallback}
      </p>
    </div>
  );
}

/**
 * Cost, stated as honestly as the library records it.
 *
 * `psych` writes `cost=None` for a model with no known price and never `0`, so
 * a null total renders "unknown". `cost_is_incomplete` means the amount is
 * real but partial, and says how many calls are missing from it. A silent zero
 * or an unqualified total would make metering look correct and be wrong.
 */
function CostStat({ report }: { report: RunReport | null }) {
  if (report === null) return <Stat label="Cost" value="-" />;
  const totals = report.totals;
  if (totals.cost === null) {
    return (
      <Stat
        label="Cost"
        value="unknown"
        tone="muted"
        hint={
          totals.unpriced_model_calls > 0
            ? `no known price for ${totals.unpriced_model_calls} of ${totals.model_calls} calls`
            : "no priced model call in this run"
        }
      />
    );
  }
  return (
    <Stat
      label="Cost"
      value={formatCost(totals.cost)}
      hint={
        totals.cost_is_incomplete
          ? `partial, ${totals.unpriced_model_calls} call${totals.unpriced_model_calls === 1 ? "" : "s"} had no known price`
          : undefined
      }
    />
  );
}

/**
 * The run level prompt, one disclosure down.
 *
 * Activity leads in plain language even here, and a wall of monospace under
 * "what happened" would bury the answer. It is collapsed rather than absent:
 * an operator needs the exact text, and reconstructing it from the agent's
 * definition is what used to go wrong, because that reconstruction could not
 * include what the runtime added at assembly time.
 *
 * An empty prompt means this run predates the runtime recording it, which is
 * not the same as an agent that was told nothing.
 */
function RunPrompt({ prompt, runId }: { prompt: string; runId: string }) {
  const [open, setOpen] = useState(false);

  if (prompt.trim().length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-border bg-surface/40 px-4 py-3 text-body text-muted-foreground">
        Not recorded. This run was dispatched before the runtime kept the prompt as sent.
      </p>
    );
  }

  const lines = prompt.split("\n").length;

  return (
    <div className="flex flex-col overflow-hidden rounded-xl border border-border bg-surface/40">
      <div className="bar">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-1.5 rounded-sm text-body font-medium transition-colors hover:text-foreground"
        >
          <ChevronRightIcon
            className={cn("size-3.5 transition-transform", open && "rotate-90")}
            aria-hidden
          />
          {open ? "Hide the prompt" : "Show the prompt"}
        </button>
        <span className="tabular text-micro text-muted-foreground">
          {lines.toLocaleString()} line{lines === 1 ? "" : "s"}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <CopyButton value={prompt} />
          <Link
            href={`/activity/${runId}/trace`}
            className="rounded-md px-1.5 py-1 text-caption text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            Per turn
          </Link>
        </div>
      </div>
      {open && (
        <pre
          tabIndex={0}
          className="max-h-96 overflow-y-auto px-4 py-3 font-technical text-caption whitespace-pre-wrap text-foreground/90"
        >
          {prompt}
        </pre>
      )}
    </div>
  );
}
