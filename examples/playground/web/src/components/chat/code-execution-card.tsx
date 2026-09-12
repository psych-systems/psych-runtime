"use client";

import { useState } from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  CircleHelpIcon,
  FileIcon,
  OctagonXIcon,
  ShieldCheckIcon,
  ShieldIcon,
  TimerIcon,
  XIcon,
} from "lucide-react";

import { AttachmentReader } from "@/components/chat/attachment-reader";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatBytes, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ToolCallView } from "@/lib/conversation";
import type { ResultAttachment, SandboxGuarantees, ToolCallReport } from "@/lib/types";

/**
 * What the `run_code` tool recorded, as the runtime shaped it for the model:
 * `psych_runtime.tools.code` documents every key. Optional fields are absent
 * rather than null when they do not apply, which is why each is `?`.
 */
interface RunCodePayload {
  ok?: boolean;
  value?: unknown;
  value_preview?: string;
  value_bytes?: number;
  value_handle?: string;
  value_omitted_bytes?: number;
  stdout?: string;
  stdout_bytes?: number;
  stdout_handle?: string;
  stdout_omitted_bytes?: number;
  stdout_truncated?: boolean;
  stderr?: string;
  stderr_bytes?: number;
  stderr_handle?: string;
  stderr_omitted_bytes?: number;
  stderr_truncated?: boolean;
  duration_seconds?: number;
  limit_hit?: string;
  isolation?: string;
  network_denied?: boolean;
  cancelled?: boolean;
  error?: string;
  error_type?: string;
  traceback?: string;
  artifacts?: {
    path: string;
    bytes: number;
    content_type: string;
    handle?: string;
    truncated?: boolean;
    omitted?: boolean;
  }[];
  artifacts_omitted?: number;
}

interface ExecutionReport {
  isolation?: string;
  requested_isolation?: string | null;
  network_denied?: boolean;
  guarantees?: Partial<SandboxGuarantees>;
  limits?: Record<string, number>;
  cancelled?: boolean;
}

const GUARANTEE_LABELS: Record<keyof SandboxGuarantees, string> = {
  filesystem: "Host files hidden",
  network: "Network denied",
  process_tree: "Process tree contained",
  identity: "Separate account",
  cpu: "CPU capped",
  memory: "Memory capped",
  file_size: "File size capped",
  process_count: "Process count capped",
  wall_clock: "Wall clock enforced",
  environment: "Environment scrubbed",
};

const LIMIT_LABELS: Record<string, string> = {
  cpu_seconds: "CPU",
  wall_seconds: "wall clock",
  address_space_bytes: "memory",
  file_size_bytes: "file size",
  process_count: "processes",
};

function payloadOf(result: unknown): RunCodePayload {
  return result !== null && typeof result === "object" ? (result as RunCodePayload) : {};
}

function decodeExecution(attachments: ResultAttachment[]): ExecutionReport | null {
  const report = attachments.find((a) => a.name === "execution");
  if (!report?.data) return null;
  try {
    const binary = atob(report.data.replace(/-/g, "+").replace(/_/g, "/"));
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes)) as ExecutionReport;
  } catch {
    return null;
  }
}

function programOf(args: Record<string, unknown>): string {
  const program = args.program;
  return typeof program === "string" ? program : "";
}

type State = "running" | "ok" | "failed" | "limited" | "refused" | "cancelled" | "unknown";

function stateOf(call: {
  outcome: ToolCallView["outcome"];
  failure: ToolCallView["failure"];
  payload: RunCodePayload;
}): State {
  if (call.outcome === null) return "running";
  if (call.outcome === "aborted" || call.payload.cancelled) return "cancelled";
  if (call.outcome === "error") return "failed";
  if (call.outcome === "unknown") return "unknown";
  if (call.payload.ok) return "ok";
  if (call.payload.limit_hit) return "limited";
  const kind = call.payload.error_type ?? "";
  if (
    kind === "isolation_unavailable" ||
    kind === "network_not_allowed" ||
    kind === "backend_not_ready" ||
    kind === "policy_refused" ||
    kind === "setup"
  ) {
    return "refused";
  }
  return "failed";
}

const STATE_META: Record<
  State,
  { label: string; className: string; icon: typeof CheckIcon | null }
> = {
  running: { label: "Running", className: "text-status-running", icon: null },
  ok: { label: "Ran", className: "text-status-done", icon: CheckIcon },
  failed: { label: "Raised", className: "text-status-failed", icon: XIcon },
  limited: { label: "Hit a limit", className: "text-status-waiting", icon: TimerIcon },
  refused: { label: "Not run", className: "text-status-stopped", icon: ShieldIcon },
  cancelled: { label: "Stopped", className: "text-status-stopped", icon: OctagonXIcon },
  unknown: { label: "Unknown", className: "text-muted-foreground", icon: CircleHelpIcon },
};

/**
 * One program the agent ran, in the conversation.
 *
 * Collapsed to a single line, like every other tool call, because the
 * program is not the answer. Open, it separates what the model wrote from
 * what came back: the program, the returned value, stdout and stderr as the
 * model saw them, the traceback when it raised, the files it wrote, and one
 * quiet line saying how it was contained. A stream too large for the prompt
 * is marked as kept or as cut, and kept ones open a reader that pages
 * through the recorded bytes rather than pouring them into the transcript.
 */
export function CodeExecutionCard({ call }: { call: ToolCallView }) {
  const [open, setOpen] = useState(false);
  const payload = payloadOf(call.result);
  const state = stateOf({ outcome: call.outcome, failure: call.failure, payload });
  const meta = STATE_META[state];
  const Icon = meta.icon;

  return (
    <div className="w-full max-w-xl overflow-hidden rounded-lg border border-border bg-surface/60">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-surface"
      >
        <ChevronRightIcon
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90"
          )}
          aria-hidden
        />
        {state === "running" ? (
          <span className="size-2 shrink-0 animate-pulse rounded-full bg-status-running" />
        ) : (
          Icon && <Icon className={cn("size-3.5 shrink-0", meta.className)} aria-hidden />
        )}
        <span className="truncate text-caption font-medium">Ran a program</span>
        <span className={cn("shrink-0 text-micro", meta.className)}>{meta.label}</span>
        <span className="ml-auto shrink-0 text-micro text-muted-foreground">
          {state === "running"
            ? "Working"
            : formatDuration(payload.duration_seconds ?? call.durationSeconds)}
        </span>
      </button>

      {call.failure && (
        <p className="border-t border-border px-3 py-2 text-caption text-status-failed">
          {call.failure.message}
        </p>
      )}

      {open && (
        <div className="flex flex-col gap-3 border-t border-border px-3 py-2.5">
          <CodeExecutionBody
            runId={call.runId}
            program={programOf(call.arguments)}
            payload={payload}
            attachments={call.attachments}
            state={state}
          />
        </div>
      )}
    </div>
  );
}

/** The same body on the Trace page, drawn from the report instead of the
 *  conversation fold. */
export function CodeExecutionDetail({
  report,
  runId,
}: {
  report: ToolCallReport;
  runId: string | undefined;
}) {
  const payload = payloadOf(report.result);
  const state = stateOf({ outcome: report.outcome, failure: report.failure, payload });
  return (
    <CodeExecutionBody
      runId={runId ?? null}
      program={programOf(report.arguments)}
      payload={payload}
      attachments={report.attachments ?? []}
      state={state}
    />
  );
}

function CodeExecutionBody({
  runId,
  program,
  payload,
  attachments,
  state,
}: {
  runId: string | null;
  program: string;
  payload: RunCodePayload;
  attachments: ResultAttachment[];
  state: State;
}) {
  const execution = decodeExecution(attachments);
  const byHandle = new Map(attachments.map((a) => [a.handle, a]));
  const files = payload.artifacts ?? [];

  return (
    <>
      <Block label="Program">
        <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-technical text-caption">
          {program || "(no program)"}
        </pre>
      </Block>

      {state !== "running" && (
        <ContainmentLine payload={payload} execution={execution} />
      )}

      {payload.error && (
        <Block label={state === "refused" ? "Why it was not run" : "What went wrong"}>
          <p className="whitespace-pre-wrap text-caption text-status-failed">{payload.error}</p>
        </Block>
      )}

      {payload.traceback && (
        <Block label="Traceback, as the model saw it">
          <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-technical text-caption">
            {payload.traceback}
          </pre>
        </Block>
      )}

      {"value" in payload && payload.ok && (
        <Block label="Returned">
          <pre className="max-h-60 max-w-full overflow-auto whitespace-pre-wrap break-words font-technical text-caption">
            {payload.value === null || payload.value === undefined
              ? "nothing"
              : JSON.stringify(payload.value, null, 2)}
          </pre>
        </Block>
      )}
      {payload.value_preview !== undefined && (
        <StreamBlock
          label="Returned"
          runId={runId}
          text={payload.value_preview}
          bytes={payload.value_bytes}
          handle={payload.value_handle}
          omitted={payload.value_omitted_bytes}
          attachment={payload.value_handle ? byHandle.get(payload.value_handle) : undefined}
        />
      )}

      {payload.stdout !== undefined && (
        <StreamBlock
          label="Printed"
          runId={runId}
          text={payload.stdout}
          bytes={payload.stdout_bytes}
          handle={payload.stdout_handle}
          omitted={payload.stdout_omitted_bytes}
          truncated={payload.stdout_truncated}
          attachment={payload.stdout_handle ? byHandle.get(payload.stdout_handle) : undefined}
        />
      )}
      {payload.stderr !== undefined && (
        <StreamBlock
          label="Printed to stderr"
          runId={runId}
          text={payload.stderr}
          bytes={payload.stderr_bytes}
          handle={payload.stderr_handle}
          omitted={payload.stderr_omitted_bytes}
          truncated={payload.stderr_truncated}
          attachment={payload.stderr_handle ? byHandle.get(payload.stderr_handle) : undefined}
        />
      )}

      {(files.length > 0 || (payload.artifacts_omitted ?? 0) > 0) && (
        <Block label="Files it wrote">
          <ul className="flex flex-col gap-1.5">
            {files.map((file) => (
              <ArtifactRow
                key={file.path}
                runId={runId}
                file={file}
                attachment={file.handle ? byHandle.get(file.handle) : undefined}
              />
            ))}
            {(payload.artifacts_omitted ?? 0) > 0 && (
              <li className="text-caption text-muted-foreground">
                {payload.artifacts_omitted} more {payload.artifacts_omitted === 1 ? "file" : "files"}{" "}
                past the collection cap.
              </li>
            )}
          </ul>
        </Block>
      )}
    </>
  );
}

function Block({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      {children}
    </div>
  );
}

/**
 * How the program was contained, in one line, with the full report a hover
 * away. The level and the network are what a reader acts on; the ten
 * per-guarantee states are for someone checking a claim.
 */
function ContainmentLine({
  payload,
  execution,
}: {
  payload: RunCodePayload;
  execution: ExecutionReport | null;
}) {
  const level = execution?.isolation ?? payload.isolation ?? "unverified";
  const Icon = level === "isolated" ? ShieldCheckIcon : ShieldIcon;
  const limits = execution?.limits ?? null;
  const limitText = limits
    ? Object.entries(limits)
        .filter(([key]) => key in LIMIT_LABELS)
        .map(([key, value]) =>
          key === "address_space_bytes"
            ? `${formatBytes(value)} ${LIMIT_LABELS[key]}`
            : key.endsWith("seconds")
              ? `${value}s ${LIMIT_LABELS[key]}`
              : `${value} ${LIMIT_LABELS[key]}`
        )
        .join(" · ")
    : null;
  const guarantees = execution?.guarantees ?? null;
  const networkDenied = execution?.network_denied ?? payload.network_denied !== false;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge
            variant="outline"
            className="gap-1 cursor-default"
            aria-label={`Isolation: ${level}`}
          >
            <Icon className="size-3" aria-hidden />
            {level === "isolated"
              ? "Isolated"
              : level === "process"
                ? "Process only"
                : "Unverified"}
          </Badge>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-xs">
          {guarantees ? (
            <ul className="flex flex-col gap-0.5 text-micro">
              {(Object.keys(GUARANTEE_LABELS) as (keyof SandboxGuarantees)[]).map((key) => (
                <li key={key} className="flex justify-between gap-3">
                  <span>{GUARANTEE_LABELS[key]}</span>
                  <span className="font-technical opacity-80">{guarantees[key] ?? "unknown"}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span>No enforcement report was recorded for this execution.</span>
          )}
        </TooltipContent>
      </Tooltip>
      <Badge variant="outline">{networkDenied ? "No network" : "Network reachable"}</Badge>
      {payload.limit_hit && (
        <Badge variant="outline" className="text-status-waiting">
          Stopped by {LIMIT_LABELS[payload.limit_hit] ?? payload.limit_hit} limit
        </Badge>
      )}
      {limitText && <span className="text-micro text-muted-foreground">{limitText}</span>}
    </div>
  );
}

function StreamBlock({
  label,
  runId,
  text,
  bytes,
  handle,
  omitted,
  truncated,
  attachment,
}: {
  label: string;
  runId: string | null;
  text: string;
  bytes?: number;
  handle?: string;
  omitted?: number;
  truncated?: boolean;
  attachment?: ResultAttachment;
}) {
  const [reading, setReading] = useState(false);
  const partial = handle !== undefined || omitted !== undefined;
  return (
    <Block label={label}>
      <pre className="max-h-60 max-w-full overflow-auto whitespace-pre-wrap break-words font-technical text-caption text-foreground/90">
        {text || "(nothing)"}
      </pre>
      {(partial || truncated) && (
        <div className="flex flex-wrap items-center gap-2 text-micro text-muted-foreground">
          {bytes !== undefined && <span>{formatBytes(bytes)} produced.</span>}
          {truncated && <span>Capture stopped at the cap; the rest was not read.</span>}
          {handle !== undefined && attachment && (
            <>
              <span>Preview shown to the model; the full output is kept.</span>
              {runId && (
                <button
                  type="button"
                  className="text-primary underline-offset-2 hover:underline"
                  onClick={() => setReading((prev) => !prev)}
                  aria-expanded={reading}
                >
                  {reading ? "Hide reader" : "Read it"}
                </button>
              )}
            </>
          )}
          {omitted !== undefined && (
            <span>Preview shown to the model; {formatBytes(omitted)} beyond it was not kept.</span>
          )}
        </div>
      )}
      {reading && runId && attachment && (
        <AttachmentReader runId={runId} attachment={attachment} />
      )}
    </Block>
  );
}

function ArtifactRow({
  runId,
  file,
  attachment,
}: {
  runId: string | null;
  file: NonNullable<RunCodePayload["artifacts"]>[number];
  attachment?: ResultAttachment;
}) {
  const [reading, setReading] = useState(false);
  return (
    <li className="flex flex-col gap-1 rounded-md border border-border px-2.5 py-1.5">
      <div className="flex flex-wrap items-center gap-2 text-caption">
        <FileIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="font-technical">{file.path}</span>
        <span className="text-muted-foreground">
          {formatBytes(file.bytes)} · {file.content_type}
        </span>
        {file.truncated && <Badge variant="outline">cut at the budget</Badge>}
        {file.omitted && <Badge variant="outline">not kept</Badge>}
        {attachment && runId && !file.omitted && (
          <button
            type="button"
            className="ml-auto text-micro text-primary underline-offset-2 hover:underline"
            onClick={() => setReading((prev) => !prev)}
            aria-expanded={reading}
          >
            {reading ? "Hide" : "Open"}
          </button>
        )}
      </div>
      {reading && attachment && runId && (
        <AttachmentReader runId={runId} attachment={attachment} />
      )}
    </li>
  );
}
