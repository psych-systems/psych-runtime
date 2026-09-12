"use client";

import { useState } from "react";
import { CheckIcon, ChevronRightIcon, CircleHelpIcon, OctagonXIcon, XIcon } from "lucide-react";

import { CodeExecutionCard } from "@/components/chat/code-execution-card";
import { JsonBlock } from "@/components/chat/json-block";
import { ToolArguments } from "@/components/chat/tool-arguments";
import { formatBytes, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { ToolCallView } from "@/lib/conversation";

const OUTCOME = {
  ok: { icon: CheckIcon, className: "text-status-done", label: "Done" },
  error: { icon: XIcon, className: "text-status-failed", label: "Failed" },
  aborted: { icon: OctagonXIcon, className: "text-status-stopped", label: "Stopped" },
  unknown: { icon: CircleHelpIcon, className: "text-muted-foreground", label: "Unknown" },
} as const;

/**
 * One tool call in the timeline: a single quiet row saying what the agent
 * used, expandable into what it sent and what came back.
 *
 * Collapsed by default because the tool call is not the answer -- a
 * conversation reads as a conversation, and the mechanics are there for the
 * person who wants them. A failure is the exception: its message is written
 * for a reader and stays visible, since it explains an answer that is missing
 * or wrong.
 */
export function ToolCallCard({ call }: { call: ToolCallView }) {
  const [open, setOpen] = useState(false);
  const running = call.outcome === null;
  const meta = running ? null : OUTCOME[call.outcome ?? "unknown"];

  if (call.tool === "run_code") {
    return <CodeExecutionCard call={call} />;
  }

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
        />
        {running ? (
          <span className="size-2 shrink-0 animate-pulse rounded-full bg-status-running" />
        ) : (
          meta && <meta.icon className={cn("size-3.5 shrink-0", meta.className)} />
        )}
        <span className="truncate text-caption font-medium">{toolLabel(call.tool)}</span>
        <span className="ml-auto shrink-0 text-micro text-muted-foreground">
          {running ? "Working" : formatDuration(call.durationSeconds)}
        </span>
      </button>

      {call.failure && (
        <p className="border-t border-border px-3 py-2 text-caption text-status-failed">
          {call.failure.message}
        </p>
      )}

      {open && (
        <div className="flex flex-col gap-3 border-t border-border px-3 py-2.5">
          <div className="flex flex-col gap-1.5">
            <SubLabel>Sent</SubLabel>
            <ToolArguments value={call.arguments} />
          </div>
          {!running && call.outcome === "ok" && (
            <div className="flex flex-col gap-1.5">
              <SubLabel>
                Returned
                {call.resultBytes > 0 && (
                  <span className="ml-1.5 normal-case opacity-70">
                    {formatBytes(call.resultBytes)}
                  </span>
                )}
              </SubLabel>
              <JsonBlock value={call.preview ?? call.result} emptyLabel="Nothing returned" />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SubLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
      {children}
    </span>
  );
}

/** `search_orders` reads as "Search orders". The registered name is the
 * identifier; it is not the name of the thing that happened. */
export function toolLabel(tool: string): string {
  const spaced = tool.replace(/[_-]+/g, " ").replace(/([a-z\d])([A-Z])/g, "$1 $2").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
