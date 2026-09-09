"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import { SubagentPanel } from "@/components/chat/subagent-panel";
import { TraceWorkspace } from "@/components/trace/trace-workspace";
import { costText, ThreadSummary } from "@/components/trace/thread-summary";
import { TraceTimeline } from "@/components/trace/trace-timeline";
import type { TimeRange } from "@/components/trace/trace-timeline";
import {
  buildThreadTraceModel,
  buildTraceModel,
} from "@/components/trace/trace-model";
import { useBackendConfig } from "@/components/app-shell/backend-config-provider";
import { useRunStatus } from "@/hooks/use-run-status";
import { useSubagents } from "@/hooks/use-subagents";
import { useThreadReport } from "@/hooks/use-thread-report";
import { Copyable } from "@/components/activity/copyable";
import { formatDuration } from "@/lib/format";
import type { RunReport, ThreadReport, ThreadTotals } from "@/lib/types";

/**
 * The trace panel for one Run, shaped like a browser DevTools network panel:
 * a waterfall across the top, a chronological action list below it, and a
 * detail panel for whichever row is selected. Everything drawn here comes
 * from one `GET /api/runs/{id}/report` read (`useRunReport`), no
 * OpenTelemetry and no second source of truth; the report is already a
 * projection over the durable record log.
 *
 * The one thing the report cannot answer is what the Run is doing *now*. It
 * carries `terminal_state`, which is null for every Run that has not settled,
 * and this header used to render `terminal_state ?? "running"`, so a Run
 * suspended on an approval showed a "Running" pill while it sat waiting for a
 * person. The pill now comes from `psych.status().lifecycle`, the same fold
 * the runtime uses, which has a word for waiting and a word for stopping.
 *
 * ## The conversation is the default, one message is a choice
 *
 * A conversation is a chain of Runs, and `psych.report()` is built
 * over exactly one Run's log. An earlier version of this file used that to
 * argue for showing one Run at a time, on the grounds that combining them
 * would produce "a duration spanning the minutes a person spent typing
 * between turns and a turn count no `Limits` ever applied to".
 *
 * Half of that is still true and is why the per-message view survives. A turn
 * budget governs one Run, so a summed turn count is a number no limit ever
 * applied to, and it stays off the conversation view.
 *
 * The other half was wrong. The typing gaps are not a problem to avoid, they
 * are a fact about the exchange: the summary names them as waiting and puts
 * model and tool time beside the elapsed span rather than letting one stand in
 * for the other. And tokens and cost are quantities somebody is billed for,
 * where the bill spans the conversation, so leading with one message's share
 * of them was the actual error.
 *
 * So: the conversation by default, one message on demand, and each view shows
 * only the numbers that mean something at its own scale.
 */
/** `null` is the whole conversation; a run id is one message within it. */
type Scope = string | null;

export function TraceView({ runId }: { runId: string }) {
  const router = useRouter();
  const { thread, loading, error, refresh } = useThreadReport(runId);
  const [scope, setScope] = useState<Scope>(null);

  // Adjust when the route's own param changes, rather than in an effect.
  const [prevRunId, setPrevRunId] = useState(runId);
  if (runId !== prevRunId) {
    setPrevRunId(runId);
    setScope(null);
  }

  // The newest Run's status, which is the conversation's: an exchange is
  // running if its latest message is.
  const latestRunId = thread?.run_ids[thread.run_ids.length - 1] ?? runId;
  const { status } = useRunStatus(scope ?? latestRunId);
  // The tree under whichever Run is in scope. A subagent's own work is in its
  // own log, so a trace of the parent alone has a gap exactly where the
  // interesting minute is; this is the way into the logs that fill it.
  const subagents = useSubagents(
    scope ?? latestRunId,
    status?.lifecycle === "running",
  );

  const report =
    scope === null
      ? null
      : (thread?.reports.find((r) => r.run_id === scope) ?? null);

  const model = useMemo(() => {
    if (thread === null) return null;
    if (report !== null) return buildTraceModel(report);
    return buildThreadTraceModel(
      thread.reports,
      thread.messages
        .filter((message) => message.role === "user")
        .map((message) => ({ runId: message.run_id, text: message.content })),
      thread.totals.wall_clock_seconds,
    );
  }, [thread, report]);

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [timeRange, setTimeRange] = useState<TimeRange>({ start: 0, end: 0 });
  const [selectionFor, setSelectionFor] = useState<unknown>(null);
  if (model !== selectionFor) {
    // A freshly loaded model, or a change of scope, resets the selection to
    // its first entry. Computed during render rather than in an effect that
    // would commit a second update after mount.
    setSelectionFor(model);
    setSelectedKey(model?.entries[0]?.key ?? null);
    setTimeRange({ start: 0, end: model?.totalSeconds ?? 0 });
  }

  const selectedEntry =
    model?.entries.find((e) => e.key === selectedKey) ?? null;
  const visibleEntries = model?.entries.filter((entry) => {
    if (!entry.timed || entry.offsetSeconds === null || entry.durationSeconds === null) return true;
    const end = entry.offsetSeconds + entry.durationSeconds;
    return end >= timeRange.start && entry.offsetSeconds <= timeRange.end;
  }) ?? [];
  const totals = thread === null ? null : scopedTotals(thread, report);
  // The report's terminal state is the fallback only while the status read is
  // in flight, and `toLifecycle` knows the terminal vocabulary.
  const lifecycle =
    status?.lifecycle ??
    (thread
      ? toLifecycle(
          thread.reports[thread.reports.length - 1]?.terminal_state ?? null,
        )
      : null);
  const specName = thread?.reports[0]?.spec_name ?? "";

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      {/* `.bar`: this header, the action list's and the detail panel's all
          draw the same 3rem rule, so the borders line up down the screen. */}
      <div className="bar px-4">
        {/* Back to the Run being viewed, not the Run the route opened on: the
            switcher below can move `viewing` to an earlier turn, and this
            link used to send you to the newest one regardless. */}
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Go back"
          onClick={() => {
            if (window.history.length > 1) router.back();
            else router.push(`/activity/${scope ?? runId}`);
          }}
        >
          <ArrowLeftIcon className="size-4" />
        </Button>
        <span className="text-body font-medium">Trace</span>
        {lifecycle && <StatusPill state={lifecycle} size="sm" />}
        {specName !== "" && (
          <span className="hidden min-w-0 truncate text-caption text-muted-foreground sm:inline">
            {specName}
          </span>
        )}
        {/* The run id is the reason an operator opened this page, but it is
            also the widest thing in the bar. It keeps its place on a laptop
            and folds away below that rather than pushing the controls off. */}
        <Copyable
          value={scope ?? latestRunId}
          className="hidden text-muted-foreground lg:inline-flex"
        />

        {/* The whole conversation first and selected by default; one message
            is the narrowing, not the starting point. */}
        {thread && thread.run_ids.length > 1 && (
          <div className="flex shrink-0 items-center gap-1">
            <ScopeButton active={scope === null} onClick={() => setScope(null)}>
              Whole conversation
            </ScopeButton>
            {thread.run_ids.map((id, index) => (
              <ScopeButton
                key={id}
                active={id === scope}
                onClick={() => setScope(id)}
              >
                {index + 1}
              </ScopeButton>
            ))}
          </div>
        )}

        {totals && (
          <span className="ml-auto hidden whitespace-nowrap text-micro text-muted-foreground xl:inline">
            {totals.messages.toLocaleString()} {totals.messages === 1 ? "message" : "messages"}
            {" / "}{totals.model_calls.toLocaleString()} model calls
            {" / "}{totals.tool_calls.toLocaleString()} tool calls
            {" / "}{totals.cost_amount === null ? "cost unknown" : costText(totals)}
            {" / "}{formatDuration(totals.wall_clock_seconds)}
          </span>
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          className={totals ? undefined : "ml-auto"}
          aria-label="Refresh"
          onClick={refresh}
          disabled={loading}
        >
          <RefreshCwIcon
            className={loading ? "size-3.5 animate-spin" : "size-3.5"}
          />
        </Button>
      </div>

      {loading && !thread && (
        <div className="flex flex-1 items-center justify-center gap-2 py-16 text-body text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" aria-hidden />
          Loading the report
        </div>
      )}

      {error && !thread && (
        <div className="mx-4 mt-4 flex items-start gap-1.5 rounded-md bg-destructive/10 p-2 text-caption text-destructive">
          <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
      )}

      {thread && model && totals && (
        <>
          <ThreadSummary
            totals={totals}
            scope={
              scope === null
                ? thread.run_ids.length === 1
                  ? "This conversation"
                  : `${thread.run_ids.length} messages`
                : `Message ${thread.run_ids.indexOf(scope) + 1} of ${thread.run_ids.length}`
            }
          />
          <TracingNote />
          {subagents.tree !== null && subagents.tree.children.length > 0 && (
            <div className="px-4 pt-3">
              <SubagentPanel
                tree={subagents.tree}
                onChanged={subagents.refresh}
              />
            </div>
          )}
          <TraceTimeline
            entries={model.entries}
            totalSeconds={model.totalSeconds}
            range={timeRange}
            selectedKey={selectedKey}
            onSelect={setSelectedKey}
            onRangeChange={setTimeRange}
          />

          {model.entries.length === 0 ? (
            <div className="flex flex-1 items-center justify-center px-6 py-16 text-center text-body text-muted-foreground">
              Nothing has been recorded yet: no model call, tool call,
              suspension or step.
            </div>
          ) : (
            <TraceWorkspace
              entries={visibleEntries}
              selectedEntry={selectedEntry}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
            />
          )}
        </>
      )}
    </div>
  );
}

/**
 * Which totals belong to the current scope.
 *
 * The conversation's come from the backend, already summed. One message's are
 * folded from its own report into the same shape, so the summary component
 * renders one type and cannot show a subtly different set of fields depending
 * on what you clicked.
 */
function scopedTotals(
  thread: ThreadReport,
  report: RunReport | null,
): ThreadTotals {
  if (report === null) return thread.totals;
  const { totals } = report;
  const { usage, latency } = totals;
  return {
    messages: 1,
    model_calls: totals.model_calls,
    failed_model_calls: totals.failed_model_calls,
    tool_calls: totals.tool_calls,
    compaction_calls: totals.compaction_calls,
    input_tokens: usage.input,
    output_tokens: usage.output,
    cache_read_tokens: usage.cache_read,
    cache_write_tokens: usage.cache_write,
    reasoning_tokens: usage.reasoning,
    total_tokens:
      usage.input + usage.output + usage.cache_read + usage.cache_write,
    cost_amount: totals.cost === null ? null : totals.cost.amount,
    cost_currency: totals.cost === null ? null : totals.cost.currency,
    cost_source: totals.cost === null ? null : totals.cost.source,
    provider_reported_costs: totals.provider_reported_costs,
    unpriced_model_calls: totals.unpriced_model_calls,
    cost_is_incomplete: totals.cost_is_incomplete,
    wall_clock_seconds: latency.wall_clock_seconds,
    model_seconds: latency.model_seconds,
    tool_seconds: latency.tool_seconds,
  };
}

function ScopeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "rounded-md bg-foreground px-2 py-0.5 text-micro font-medium text-background"
          : "rounded-md px-2 py-0.5 text-micro font-medium text-muted-foreground hover:bg-muted"
      }
    >
      {children}
    </button>
  );
}

/**
 * Where this Run's spans went, said out loud.
 *
 * Everything above is drawn from the record log, so this panel works whether
 * or not anything is collecting traces. That is exactly why it has to say
 * which: "we collect no spans" and "we collect them somewhere you are not
 * looking" are different problems and are indistinguishable from a screen
 * that mentions neither. Somebody who has wired up a collector and then cannot
 * find their trace will otherwise assume the wiring is broken.
 */
function TracingNote() {
  const { config } = useBackendConfig();
  if (config === null) return null;
  const { tracing } = config;
  return (
    <p className="px-4 pt-2 text-micro text-muted-foreground">
      {tracing.enabled ? (
        <>
          Spans for this run were exported to{" "}
          <span className="font-technical">{tracing.endpoint}</span> as{" "}
          <span className="font-technical">{tracing.service_name}</span>.
          Everything on this page comes from the record log rather than from
          there.
        </>
      ) : (
        <>
          Nothing is collecting spans. Psych opens them on every run regardless;
          set{" "}
          <code className="font-technical">PSYCH_PLAYGROUND_OTLP_ENDPOINT</code>{" "}
          to send them somewhere. This page reads the record log, so it works
          either way.
        </>
      )}
    </p>
  );
}
