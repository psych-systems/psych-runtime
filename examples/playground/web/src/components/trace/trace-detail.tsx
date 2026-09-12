"use client";

import Link from "next/link";
import { ExternalLinkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { JsonView } from "@/components/trace/json-view";
import { formatBytes, formatCost, formatDuration } from "@/lib/format";
import { formatOffset } from "@/components/trace/trace-format";
import { STATUS_META } from "@/components/trace/trace-meta";
import { TurnContextPanels } from "@/components/trace/turn-context";
import { CodeExecutionDetail } from "@/components/chat/code-execution-card";
import type { TraceEntry } from "@/components/trace/trace-model";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><div className="mb-1 text-micro font-medium tracking-wide text-muted-foreground uppercase">{label}</div>{children}</div>;
}
function Overview({ entry }: { entry: TraceEntry }) {
  if (entry.kind === "message") return <><Field label="Message"><p className="text-prose whitespace-pre-wrap">{entry.data.text || "Nothing was said."}</p></Field></>;
  if (entry.kind === "model") return <>
    <TurnContextPanels context={entry.context} />
    {entry.data.failure && <Field label={entry.data.failure.kind}><p className="text-caption text-status-failed">{entry.data.failure.message}</p></Field>}
    <div className="grid grid-cols-2 gap-4">
      <Field label="Model"><span className="font-technical text-caption">{entry.data.model}</span></Field>
      <Field label="Finish reason"><span className="font-technical text-caption">{entry.data.finish_reason ?? "not recorded"}</span></Field>
      <Field label="Cost"><span className="tabular text-caption">{formatCost(entry.data.cost)}</span></Field>
      <Field label="Tool calls issued"><span className="text-caption">{entry.data.tool_call_ids.length}</span></Field>
    </div>
  </>;
  if (entry.kind === "tool" && entry.data.tool === "run_code") return <>
    {entry.data.failure && <Field label={entry.data.failure.kind}><p className="text-caption text-status-failed">{entry.data.failure.message}</p></Field>}
    <CodeExecutionDetail report={entry.data} runId={entry.runId} />
  </>;
  if (entry.kind === "tool") return <>
    {entry.data.failure && <Field label={entry.data.failure.kind}><p className="text-caption text-status-failed">{entry.data.failure.message}</p></Field>}
    <div className="grid grid-cols-2 gap-4">
      <Field label="Call ID"><span className="font-technical text-caption break-all">{entry.data.call_id}</span></Field>
      <Field label="Result size"><span className="text-caption">{formatBytes(entry.data.result_bytes)}</span></Field>
      <Field label="Interrupt"><span className="text-caption">{entry.data.interruptible ? "Allowed" : "Blocked"}</span></Field>
      <Field label="Retry"><span className="text-caption">{entry.data.safe_to_retry ? "Safe" : "Unsafe"}</span></Field>
    </div>
  </>;
  if (entry.kind === "suspension") return <>
    <Field label="Reason"><span className="text-caption">{entry.data.reason}</span></Field>
    {entry.data.question && <Field label="Question"><p className="text-body">{entry.data.question}</p></Field>}
    <Field label="Expires"><span className="tabular text-caption">{new Date(entry.data.expires_at).toLocaleString()}</span></Field>
  </>;
  return <>
    <div className="grid grid-cols-2 gap-4">
      <Field label="Step"><span className="font-technical text-caption">{entry.data.step_id}</span></Field>
      <Field label="Kind"><span className="text-caption">{entry.data.kind}</span></Field>
      <Field label="Attempt"><span className="text-caption">{entry.data.attempt_number}</span></Field>
      <Field label="Complete"><span className="text-caption">{entry.data.completed ? "Yes" : "No"}</span></Field>
    </div>
    {entry.data.failure && <Field label={entry.data.failure.kind}><p className="text-caption text-status-failed">{entry.data.failure.message}</p></Field>}
    {entry.data.child_run_id && <Field label="Delegated run"><Link href={`/activity/${entry.data.child_run_id}/trace`}
      className="inline-flex items-center gap-1 font-technical text-caption text-primary hover:underline">
      {entry.data.child_run_id}<ExternalLinkIcon className="size-3" /></Link></Field>}
  </>;
}

function RequestData({ entry }: { entry: TraceEntry }) {
  if (entry.kind === "message") return <JsonView value={{ text: entry.data.text, run_id: entry.data.runId }} />;
  if (entry.kind === "model") return <><Field label="System instructions"><pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-caption whitespace-pre-wrap">{entry.data.system_prompt}</pre></Field>
    <Field label="Available tools"><JsonView value={entry.data.tool_names} /></Field></>;
  if (entry.kind === "tool") return <JsonView value={entry.data.arguments} />;
  if (entry.kind === "suspension") return <JsonView value={entry.data.payload} emptyLabel="No payload" />;
  return <JsonView value={entry.data.input} />;
}

function ResponseData({ entry }: { entry: TraceEntry }) {
  if (entry.kind === "model") return <><Field label="Text"><p className="text-body whitespace-pre-wrap">{entry.data.text || "(empty response)"}</p></Field>
    <Field label="Tool call IDs"><JsonView value={entry.data.tool_call_ids} /></Field></>;
  if (entry.kind === "tool") return <JsonView value={entry.data.result}
    emptyLabel={entry.data.result_handle ? `Stored as ${entry.data.result_handle}` : "(empty result)"} />;
  if (entry.kind === "step") return <JsonView value={entry.data.output} emptyLabel="No output" />;
  if (entry.kind === "suspension") return <JsonView value={{ approved: entry.data.approved, resumed_at: entry.data.resumed_at }} />;
  return <p className="text-caption text-muted-foreground">This event has no response body.</p>;
}

function Timing({ entry }: { entry: TraceEntry }) {
  return <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
    <Field label="Started"><span className="tabular text-caption">{entry.startedAt ? new Date(entry.startedAt).toLocaleString() : "not recorded"}</span></Field>
    <Field label="Finished"><span className="tabular text-caption">{entry.finishedAt ? new Date(entry.finishedAt).toLocaleString() : "not recorded"}</span></Field>
    <Field label="Offset"><span className="tabular text-caption">{formatOffset(entry.offsetSeconds)}</span></Field>
    <Field label="Duration"><span className="tabular text-caption">{formatDuration(entry.durationSeconds)}</span></Field>
    {entry.kind === "model" && <>
      <Field label="Queue wait"><span className="tabular text-caption">{formatDuration(entry.data.timings?.queue_wait_seconds ?? null)}</span></Field>
      <Field label="First token"><span className="tabular text-caption">{formatDuration(entry.data.timings?.time_to_first_token_seconds ?? null)}</span></Field>
      <Field label="Stream"><span className="tabular text-caption">{formatDuration(entry.data.timings?.stream_duration_seconds ?? null)}</span></Field>
      <Field label="Input tokens"><span className="tabular text-caption">{entry.data.usage?.input.toLocaleString() ?? "unknown"}</span></Field>
      <Field label="Output tokens"><span className="tabular text-caption">{entry.data.usage?.output.toLocaleString() ?? "unknown"}</span></Field>
    </>}
  </div>;
}

export function TraceDetail({ entry }: { entry: TraceEntry }) {
  const status = STATUS_META[entry.status];
  return <Tabs defaultValue="overview" className="h-full min-h-0 gap-0">
    <div className="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex h-11 items-center gap-2 px-4">
        <h2 className="truncate font-technical text-body font-semibold">{entry.label}</h2>
        <Badge variant="outline" className={status.text}>{status.label}</Badge>
        <span className="ml-auto shrink-0 text-micro text-muted-foreground">{entry.turn !== null ? `Turn ${entry.turn}` : ""}</span>
      </div>
      <TabsList variant="line" className="h-9 px-3">
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="request">Request</TabsTrigger>
        <TabsTrigger value="response">Response</TabsTrigger>
        <TabsTrigger value="timing">Timing</TabsTrigger>
        <TabsTrigger value="raw">Raw</TabsTrigger>
      </TabsList>
    </div>
    <TabsContent value="overview" className="m-0 flex flex-col gap-4 overflow-auto p-4"><Overview entry={entry} /></TabsContent>
    <TabsContent value="request" className="m-0 overflow-auto p-4"><RequestData entry={entry} /></TabsContent>
    <TabsContent value="response" className="m-0 overflow-auto p-4"><ResponseData entry={entry} /></TabsContent>
    <TabsContent value="timing" className="m-0 overflow-auto p-4"><Timing entry={entry} /></TabsContent>
    <TabsContent value="raw" className="m-0 overflow-auto p-4"><JsonView value={entry.data} /></TabsContent>
  </Tabs>;
}
