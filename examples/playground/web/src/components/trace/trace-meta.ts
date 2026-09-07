import {
  BoxIcon,
  ClockIcon,
  CpuIcon,
  MessageSquareIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";

import type { TraceEntryKind, TraceEntryStatus } from "@/components/trace/trace-model";

export const KIND_META: Record<TraceEntryKind, { label: string; icon: LucideIcon; bar: string; text: string }> = {
  message: { label: "Message", icon: MessageSquareIcon, bar: "bg-primary", text: "text-primary" },
  model: { label: "Model call", icon: CpuIcon, bar: "bg-chart-1", text: "text-chart-1" },
  tool: { label: "Tool call", icon: WrenchIcon, bar: "bg-chart-3", text: "text-chart-3" },
  suspension: { label: "Waiting", icon: ClockIcon, bar: "bg-status-waiting", text: "text-status-waiting" },
  step: { label: "Step", icon: BoxIcon, bar: "bg-muted-foreground", text: "text-muted-foreground" },
};

/**
 * Status overrides a kind's resting color when it isn't a plain success: a
 * failed tool call reads as failed at a glance, not merely as "a tool call
 * happened to be blue".
 *
 * The colors come from the five `--status-*` tokens the app actually defines.
 * This table used to name `status-suspended` and `status-aborted`, which are
 * not tokens, so those bars rendered with no background at all and a dangling
 * call, the one thing on this screen worth spotting, was invisible.
 */
export const STATUS_META: Record<
  TraceEntryStatus,
  { label: string; bar: string; text: string; dashed?: boolean }
> = {
  ok: { label: "ok", bar: "", text: "" },
  error: { label: "error", bar: "bg-status-failed", text: "text-status-failed" },
  aborted: { label: "aborted", bar: "bg-status-stopped", text: "text-status-stopped" },
  unknown: { label: "unknown", bar: "bg-muted-foreground", text: "text-muted-foreground" },
  pending: { label: "in progress", bar: "bg-status-running", text: "text-status-running" },
  dangling: { label: "never finished", bar: "bg-status-failed/50", text: "text-status-failed", dashed: true },
  waiting: { label: "waiting", bar: "bg-status-waiting", text: "text-status-waiting" },
  denied: { label: "denied", bar: "bg-status-stopped", text: "text-status-stopped" },
};
