"use client";

import Link from "next/link";
import { useState } from "react";
import { AlertCircleIcon, ChevronRightIcon, WaypointsIcon } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AnswerToolCall, AnswerView, Lifecycle } from "@/lib/types";

/**
 * What the run concluded, and the work behind it.
 *
 * `AnswerView` splits the two from the log itself: the answer is the turn the
 * loop finished on, the work is everything before it. `finished: false` means
 * the run never reached an answer, and then `text` is empty because there is
 * no answer, not because the agent said nothing, so this shows the run's own
 * reason instead of an empty paragraph.
 */
export function AnswerPanel({
  answer,
  runId,
  lifecycle,
  failureMessage,
  loading = false,
  error = null,
}: {
  answer: AnswerView | null;
  runId: string;
  lifecycle: Lifecycle | null;
  failureMessage: string | null;
  loading?: boolean;
  error?: string | null;
}) {
  const [openWork, setOpenWork] = useState(false);

  if (answer === null && loading) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-3 w-20 rounded-sm" />
        <Skeleton className="h-4 w-full rounded-sm" />
        <Skeleton className="h-4 w-4/5 rounded-sm" />
      </div>
    );
  }

  if (answer === null && error !== null) {
    return (
      <p className="flex items-start gap-1.5 text-caption text-status-failed">
        <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        <span>The answer could not be loaded: {error}</span>
      </p>
    );
  }

  const text = answer?.text.trim() ?? "";
  const finished = answer?.finished ?? false;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
          Answered
        </span>
        {finished && text.length > 0 ? (
          <p className="text-prose whitespace-pre-wrap">{text}</p>
        ) : (
          <p className="text-prose text-muted-foreground italic">
            {unfinishedReason(lifecycle, failureMessage)}
          </p>
        )}
      </div>

      {answer !== null && answer.work.length > 0 && (
        <div className="flex flex-col gap-2 border-t border-border pt-3">
          <button
            type="button"
            onClick={() => setOpenWork((v) => !v)}
            aria-expanded={openWork}
            className="flex w-fit items-center gap-1.5 rounded-sm text-caption text-muted-foreground transition-colors hover:text-foreground"
          >
            <ChevronRightIcon
              className={cn("size-3.5 transition-transform", openWork && "rotate-90")}
              aria-hidden
            />
            {answer.summary.length > 0
              ? answer.summary
              : `${answer.work.length} turn${answer.work.length === 1 ? "" : "s"} of work`}
          </button>

          {openWork && (
            <ol className="flex flex-col gap-3">
              {answer.work.map((turn) => (
                <li
                  key={`${turn.turn}:${turn.at}`}
                  className="flex flex-col gap-1.5 border-l-2 border-border pl-3"
                >
                  <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
                    Turn {turn.turn}
                  </span>
                  {turn.text.trim().length > 0 && (
                    <p className="text-body whitespace-pre-wrap text-foreground/90">{turn.text}</p>
                  )}
                  {turn.failure_message && (
                    <p className="text-caption text-status-failed">{turn.failure_message}</p>
                  )}
                  {turn.tool_calls.map((call) => (
                    <ToolLine key={call.call_id} call={call} />
                  ))}
                </li>
              ))}
            </ol>
          )}

          {openWork && (
            <Link
              href={`/activity/${runId}/trace`}
              className="inline-flex w-fit items-center gap-1.5 text-caption text-primary hover:underline"
            >
              <WaypointsIcon className="size-3.5" aria-hidden />
              See every prompt, tool call and timing in the trace
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

/** One tool call inside a work turn: what ran, how it ended, how long it
 *  took. The arguments and the result are a click away in the trace, which is
 *  the surface built to show them whole. */
function ToolLine({ call }: { call: AnswerToolCall }) {
  const unfinished = call.outcome === null;
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-caption">
      <span className="font-technical text-foreground/90">{call.tool}</span>
      <span
        className={cn(
          "font-medium",
          call.outcome === "ok" && "text-status-done",
          call.outcome === "error" && "text-status-failed",
          call.outcome === "aborted" && "text-status-stopped",
          unfinished && "text-status-failed"
        )}
      >
        {/* A call with no outcome was started and never settled. It is the
            crash signature, so it says so rather than reading as unknown. */}
        {unfinished ? "never finished" : call.outcome}
      </span>
      <span className="tabular text-muted-foreground">
        {formatDuration(call.duration_seconds)}
      </span>
      {call.failure_message && (
        <span className="min-w-0 text-status-failed">{call.failure_message}</span>
      )}
    </div>
  );
}

function unfinishedReason(lifecycle: Lifecycle | null, failureMessage: string | null): string {
  if (failureMessage) return failureMessage;
  switch (lifecycle) {
    case "waiting":
      return "Waiting on a decision before it can go further.";
    case "running":
    case "queued":
      return "Still working. No answer yet.";
    case "stopping":
      return "Stopping. It will not reach an answer.";
    case "stopped":
      return "Stopped before it reached an answer.";
    case "failed":
      return "Failed before it reached an answer.";
    default:
      return "No answer was recorded for this run.";
  }
}
