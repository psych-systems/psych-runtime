"use client";

import { useState } from "react";
import { CheckIcon, ClockIcon, ShieldQuestionMarkIcon, XIcon } from "lucide-react";

import { ToolArguments } from "@/components/chat/tool-arguments";
import { toolLabel } from "@/components/chat/tool-call-card";
import { Button } from "@/components/ui/button";
import { formatClockTime } from "@/lib/format";
import type { ApprovalItem } from "@/lib/conversation";
import type { PendingApproval } from "@/lib/types";

const WAITING_COPY: Record<ApprovalItem["reason"], string> = {
  approval: "Waiting for a decision.",
  question: "Waiting for an answer.",
  external: "Waiting on something outside this conversation.",
  children: "Waiting on the subagents it started.",
};

/**
 * The one point in a conversation where the agent asks before acting.
 *
 * Written as a decision, not as a log line: it names the action in the words
 * the tool was named in, shows exactly what would be sent, and offers the two
 * answers. The arguments are the whole point -- approving "write_file" tells
 * you nothing, approving "write_file to /etc/hosts" tells you everything.
 *
 * `pending` comes from `RunStatus`, which carries the call's arguments so
 * this never has to parse them back out of the question text. Once decided,
 * the same card collapses to a line saying who decided what.
 */
export function ApprovalCard({
  item,
  pending,
  onDecide,
  decidedByLabel,
}: {
  item: ApprovalItem;
  /** Set only while this Run is genuinely waiting on this decision. */
  pending: PendingApproval | null;
  onDecide: (approved: boolean) => Promise<void>;
  decidedByLabel: string;
}) {
  const [submitting, setSubmitting] = useState<"approve" | "decline" | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (item.approved !== null) {
    return (
      <p className="flex w-full items-center gap-2 text-caption text-muted-foreground">
        {item.approved ? (
          <CheckIcon className="size-3.5 shrink-0 text-status-done" aria-hidden />
        ) : (
          <XIcon className="size-3.5 shrink-0 text-status-stopped" aria-hidden />
        )}
        {item.approved ? "Approved" : "Declined"}
        {item.decidedBy ? ` by ${item.decidedBy}` : ""}
        {item.decidedAt ? ` at ${formatClockTime(item.decidedAt)}` : ""}.
      </p>
    );
  }

  if (pending === null || item.reason !== "approval") {
    return (
      <p className="flex w-full items-center gap-2 text-caption text-muted-foreground">
        <ClockIcon className="size-3.5 shrink-0" aria-hidden />
        {item.question ?? WAITING_COPY[item.reason]}
      </p>
    );
  }

  async function decide(approved: boolean) {
    setSubmitting(approved ? "approve" : "decline");
    setError(null);
    try {
      await onDecide(approved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send that decision.");
    } finally {
      setSubmitting(null);
    }
  }

  return (
    <div className="w-full rounded-xl border border-status-waiting/40 bg-status-waiting/5 p-3.5">
      <div className="flex items-start gap-2.5">
        <ShieldQuestionMarkIcon className="mt-0.5 size-4 shrink-0 text-status-waiting" aria-hidden />
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <div className="flex flex-col gap-1">
            <p className="text-body font-medium">
              Approve {toolLabel(pending.tool)}?
            </p>
            <p className="text-caption text-muted-foreground">
              {item.question ??
                "The agent needs your approval before it runs this. Nothing happens until you decide."}
            </p>
          </div>

          <div className="rounded-lg border border-border bg-background px-3 py-2.5">
            <ToolArguments value={pending.arguments} />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={submitting !== null}
              onClick={() => void decide(true)}
            >
              <CheckIcon />
              {submitting === "approve" ? "Approving" : "Approve"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={submitting !== null}
              onClick={() => void decide(false)}
            >
              <XIcon />
              {submitting === "decline" ? "Declining" : "Decline"}
            </Button>
            <span className="text-micro text-muted-foreground">
              Decided as {decidedByLabel} · expires {formatClockTime(pending.expires_at)}
            </span>
          </div>

          {error && <p className="text-caption text-status-failed">{error}</p>}
        </div>
      </div>
    </div>
  );
}
