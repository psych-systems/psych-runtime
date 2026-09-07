import Link from "next/link";
import type { ReactNode } from "react";
import { ExternalLinkIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { JsonView } from "@/components/trace/json-view";
import { formatBytes, formatCost, formatDuration } from "@/lib/format";
import { formatOffset } from "@/components/trace/trace-format";
import { STATUS_META } from "@/components/trace/trace-meta";
import { TurnContextPanels } from "@/components/trace/turn-context";
import type { TraceEntry, TurnContext } from "@/components/trace/trace-model";
import type { ModelCallReport, ToolCallReport } from "@/lib/types";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-micro font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </div>
      {children}
    </div>
  );
}

function UsageGrid({ usage }: { usage: ModelCallReport["usage"] }) {
  if (usage === null) {
    return <p className="text-caption text-muted-foreground italic">no usage recorded</p>;
  }
  // Split, never summed: cache reads and writes are disjoint from input and
  // priced differently, so one "tokens" number would hide the only thing
  // that explains this call's cost.
  const rows: [string, number][] = [
    ["Input", usage.input],
    ["Output", usage.output],
    ["Cache read", usage.cache_read],
    ["Cache write", usage.cache_write],
  ];
  if (usage.cache_write_1h > 0) rows.push(["Cache write (1h)", usage.cache_write_1h]);
  if (usage.reasoning > 0) rows.push(["Reasoning", usage.reasoning]);
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 font-technical text-caption sm:grid-cols-3">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-baseline justify-between gap-2">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="tabular">{value.toLocaleString()}</dd>
        </div>
      ))}
    </dl>
  );
}

function ModelDetail({ call, context }: { call: ModelCallReport; context: TurnContext }) {
  const timings = call.timings;
  return (
    <>
      {/* First, above the numbers: what this turn was told and what it was
          offered. Everything below is a measurement of the call this made. */}
      <TurnContextPanels context={context} />

      {(call.dangling || call.will_retry) && (
        <div className="flex flex-wrap gap-1.5">
          {call.dangling && (
            <Badge variant="outline" className="border-status-failed/40 text-status-failed">
              never finished, the attempt died mid-call
            </Badge>
          )}
          {call.will_retry && (
            <Badge variant="outline" className="border-status-waiting/40 text-status-waiting">
              will retry
            </Badge>
          )}
        </div>
      )}

      {call.failure && (
        <Field label={call.failure.kind}>
          <p className="text-caption text-status-failed">{call.failure.message}</p>
          {call.failure.traceback && (
            <pre className="mt-1 max-h-56 overflow-y-auto rounded bg-muted/60 p-2 font-technical text-micro whitespace-pre-wrap text-status-failed/90">
              {call.failure.traceback}
            </pre>
          )}
        </Field>
      )}

      {call.text && (
        <Field label="Text">
          <p className="max-w-full text-body break-words whitespace-pre-wrap">
            {call.text}
          </p>
        </Field>
      )}

      <Field label="Usage">
        <UsageGrid usage={call.usage} />
      </Field>

      <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <Field label="Cost">
          <span className="tabular text-caption">{formatCost(call.cost)}</span>
        </Field>
        <Field label="Finish reason">
          <span className="font-technical text-caption">{call.finish_reason ?? "not recorded"}</span>
        </Field>
        <Field label="Queue wait">
          <span className="tabular text-caption">
            {formatDuration(timings?.queue_wait_seconds ?? null)}
          </span>
        </Field>
        <Field label="Time to first token">
          <span className="tabular text-caption">
            {formatDuration(timings?.time_to_first_token_seconds ?? null)}
          </span>
        </Field>
        <Field label="Stream duration">
          <span className="tabular text-caption">
            {formatDuration(timings?.stream_duration_seconds ?? null)}
          </span>
        </Field>
      </div>

      {call.tool_call_ids.length > 0 && (
        <Field label="Tool calls issued">
          <p className="font-technical text-caption text-muted-foreground">
            {call.tool_call_ids.join(", ")}
          </p>
        </Field>
      )}
    </>
  );
}

/** Whether this call's result bytes physically live in this report or only
 * a reference to them does. `ToolCallReport` has no `result_blob_key`, so a
 * `null` result alongside a set `result_handle` and nonzero `result_bytes`
 * is the only signal an offloaded (rather than merely model-elided) result
 * leaves on this shape. See `psych_runtime/tools/large_results.py`. */
function isOffloadedWithoutBytes(call: ToolCallReport): boolean {
  return call.result === null && call.result_handle !== null && call.result_bytes > 0;
}

function ToolDetail({ call }: { call: ToolCallReport }) {
  const offloaded = isOffloadedWithoutBytes(call);
  const hasResult = call.result !== null || call.result_bytes > 0;

  return (
    <>
      <Field label="Arguments">
        <JsonView value={call.arguments} />
      </Field>

      {call.failure && (
        <Field label={call.failure.kind}>
          <p className="text-caption text-status-failed">{call.failure.message}</p>
          {call.failure.transient && (
            <p className="mt-0.5 text-micro text-muted-foreground">transient, eligible for retry</p>
          )}
          {call.failure.traceback && (
            <pre className="mt-1 max-h-56 overflow-y-auto rounded bg-muted/60 p-2 font-technical text-micro whitespace-pre-wrap text-status-failed/90">
              {call.failure.traceback}
            </pre>
          )}
        </Field>
      )}

      {hasResult && (
        <Field
          label={`Result${call.result_bytes > 0 ? ` (${formatBytes(call.result_bytes)})` : ""}`}
        >
          {offloaded ? (
            <div className="rounded-md border border-dashed border-border bg-muted/40 p-2 text-caption text-muted-foreground">
              This result was above the offload threshold (DESIGN.md §10.8), so its bytes went to
              blob storage. The report carries a handle and a preview, not the value.
              <div className="mt-1.5 font-technical text-micro">
                handle: <span className="text-foreground">{call.result_handle}</span>
              </div>
              {call.preview && (
                <pre className="mt-1.5 max-h-40 overflow-y-auto whitespace-pre-wrap text-foreground/90">
                  {call.preview}
                </pre>
              )}
            </div>
          ) : (
            <>
              {call.result_handle && (
                <p className="mb-1 text-micro text-muted-foreground">
                  {formatBytes(call.result_bytes)} was too much for the model&rsquo;s context, so the
                  runtime elided it there and gave the model handle{" "}
                  <span className="font-technical text-foreground">{call.result_handle}</span>{" "}
                  instead. The full value below is what the log holds.
                </p>
              )}
              <JsonView value={call.result} emptyLabel="(empty result)" />
            </>
          )}
        </Field>
      )}

      {call.outcome === null && (
        <p className="text-caption text-status-failed">
          Started, and the log ends before any result was recorded. A tool call that never settled
          is what a worker dying mid-call leaves behind.
        </p>
      )}

      <div className="flex flex-wrap gap-1.5 text-micro text-muted-foreground">
        <span>{call.interruptible ? "interruptible" : "not interruptible"}</span>
        <span aria-hidden>·</span>
        <span>{call.safe_to_retry ? "safe to retry" : "not safe to retry"}</span>
        <span aria-hidden>·</span>
        <span className="font-technical">{call.call_id}</span>
      </div>
    </>
  );
}

export function TraceDetail({ entry }: { entry: TraceEntry }) {
  const statusMeta = STATUS_META[entry.status];

  return (
    <div className="flex min-w-0 flex-col">
      {/* `.bar` so this header's bottom border continues the action list's
          across the split, rather than sitting a few pixels off it. */}
      <div className="bar sticky top-0 z-10 bg-background/95 backdrop-blur">
        <h2 className="truncate font-technical text-body font-semibold">{entry.label}</h2>
        <Badge variant="outline" className={statusMeta.text || undefined}>
          {statusMeta.label}
        </Badge>
        <p className="tabular ml-auto shrink-0 text-micro text-muted-foreground">
          {formatOffset(entry.offsetSeconds)} · {formatDuration(entry.durationSeconds)}
          {entry.turn !== null && ` · turn ${entry.turn}`}
        </p>
      </div>

      <div className="flex min-w-0 flex-col gap-4 p-4">
        {entry.stepId && (
          <p className="font-technical text-micro text-muted-foreground">step {entry.stepId}</p>
        )}

        {entry.kind === "message" && (
          <section className="flex flex-col gap-2">
            {/* A message has no report of its own: it is the boundary between
                Runs, and everything it caused already has a row underneath it.
                What is worth showing is what was asked. */}
            <h3 className="text-caption font-medium text-muted-foreground">What was asked</h3>
            <p className="text-prose whitespace-pre-wrap">
              {entry.data.text.trim() === "" ? "Nothing was said." : entry.data.text}
            </p>
            <p className="text-micro text-muted-foreground">
              Everything this message did is listed below it. Its own tokens and cost are on the
              summary when you narrow to it.
            </p>
          </section>
        )}
        {entry.kind === "model" && <ModelDetail call={entry.data} context={entry.context} />}
        {entry.kind === "tool" && <ToolDetail call={entry.data} />}

        {entry.kind === "suspension" && (
          <>
            <Field label="Reason">
              <span className="text-caption">{entry.data.reason}</span>
            </Field>
            {entry.data.question && (
              <Field label="Question">
                <p className="text-body">{entry.data.question}</p>
              </Field>
            )}
            {entry.data.payload && (
              <Field label="Payload">
                <JsonView value={entry.data.payload} />
              </Field>
            )}
            {entry.data.reason === "approval" && (
              <Field label="Decision">
                <span className="text-caption">
                  {entry.data.approved === null
                    ? "not decided"
                    : entry.data.approved
                      ? "approved"
                      : "denied"}
                </span>
              </Field>
            )}
            <Field label="Expires">
              <span className="tabular text-caption">
                {new Date(entry.data.expires_at).toLocaleString()}
              </span>
            </Field>
          </>
        )}

        {entry.kind === "step" && (
          <>
            <Field label="Input">
              <JsonView value={entry.data.input} />
            </Field>
            {entry.data.output && (
              <Field label="Output">
                <JsonView value={entry.data.output} />
              </Field>
            )}
            {entry.data.failure && (
              <Field label={entry.data.failure.kind}>
                <p className="text-caption text-status-failed">{entry.data.failure.message}</p>
              </Field>
            )}
            {!entry.timed && (
              <p className="text-caption text-muted-foreground italic">
                Not timed. Nothing recorded under this step correlates a start or an end time, so it
                gets no bar rather than a guessed one.
              </p>
            )}
            {entry.data.child_run_id && (
              <Field label="Delegated to">
                <Link
                  href={`/activity/${entry.data.child_run_id}/trace`}
                  className="inline-flex items-center gap-1 font-technical text-caption text-primary hover:underline"
                >
                  {entry.data.child_run_id}
                  <ExternalLinkIcon className="size-3" aria-hidden />
                </Link>
              </Field>
            )}
          </>
        )}
      </div>
    </div>
  );
}
