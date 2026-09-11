"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import {
  ActivityIcon,
  MessagesSquareIcon,
  SearchIcon,
  TriangleAlertIcon,
  WaypointsIcon,
} from "lucide-react";

import { useRunStatuses } from "@/components/activity/use-run-statuses";
import { relativeTime } from "@/components/chat/relative-time";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import {
  StatusDot,
  statusLabel,
  toLifecycle,
  type Lifecycle,
} from "@/components/ui/status";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRunHistory } from "@/hooks/use-run-history";
import { groupConversations, type Conversation } from "@/lib/branches";
import { formatDuration, truncate } from "@/lib/format";
import type { RunStatus, RunSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATUS_BUDGET = 50;

const FILTERS: { id: string; label: string; matches: (state: Lifecycle) => boolean }[] = [
  { id: "all", label: "All", matches: () => true },
  {
    id: "active",
    label: "Active",
    matches: (state) => ["running", "queued", "stopping"].includes(state),
  },
  { id: "waiting", label: "Needs you", matches: (state) => state === "waiting" },
  { id: "failed", label: "Failed", matches: (state) => state === "failed" },
  { id: "done", label: "Done", matches: (state) => state === "done" },
  { id: "stopped", label: "Stopped", matches: (state) => state === "stopped" },
];

interface ConversationRow {
  conversation: Conversation;
  lifecycle: Lifecycle;
  turns: number | null;
  toolCalls: number | null;
  attention: string | null;
  failureMessage: string | null;
  attempts: number | null;
  review: boolean;
}

function buildRow(
  conversation: Conversation,
  statuses: ReadonlyMap<string, RunStatus>,
): ConversationRow {
  const latestStatus = statuses.get(conversation.latest.run_id);
  let turns: number | null = null;
  let toolCalls: number | null = null;
  for (const run of conversation.runs) {
    const status = statuses.get(run.run_id);
    if (!status) continue;
    turns = (turns ?? 0) + status.turn;
    toolCalls = (toolCalls ?? 0) + status.tool_calls;
  }
  return {
    conversation,
    lifecycle: latestStatus?.lifecycle ?? toLifecycle(conversation.latest.state),
    turns,
    toolCalls,
    attention:
      latestStatus?.pending_approval?.question ??
      latestStatus?.pending_question?.summary ??
      (latestStatus?.has_dangling_tool_calls
        ? "A tool call started but never settled."
        : null),
    failureMessage: latestStatus?.failure_message ?? null,
    attempts: latestStatus?.attempt_count ?? null,
    review: false,
  };
}

function reviewRun(
  id: string,
  conversationId: string,
  name: string,
  message: string,
  state: string,
  startedAt: string,
  settledAt: string | null,
  continuesRunId: string | null,
): RunSummary {
  return {
    run_id: id,
    workflow_id: "",
    kind: "agent",
    agent_id: `review-${name}`,
    conversation_id: conversationId,
    branch_id: `branch-${conversationId}`,
    name,
    tenant: "review",
    state,
    started_at: startedAt,
    version_hash: "review",
    message,
    settled_at: settledAt,
    continues_run_id: continuesRunId,
  };
}

function reviewRow({
  now,
  id,
  name,
  message,
  lifecycle,
  minutesAgo,
  durationSeconds,
  messages,
  turns,
  toolCalls,
  attention = null,
  failureMessage = null,
  attempts = 1,
}: {
  now: number;
  id: string;
  name: string;
  message: string;
  lifecycle: Lifecycle;
  minutesAgo: number;
  durationSeconds: number | null;
  messages: number;
  turns: number;
  toolCalls: number;
  attention?: string | null;
  failureMessage?: string | null;
  attempts?: number;
}): ConversationRow {
  const startedAt = new Date(now - minutesAgo * 60_000).toISOString();
  const settledAt = durationSeconds === null
    ? null
    : new Date(new Date(startedAt).getTime() + durationSeconds * 1_000).toISOString();
  const runs: RunSummary[] = [];
  for (let index = 0; index < messages; index += 1) {
    const previous = runs.at(-1);
    runs.push(
      reviewRun(
        `${id}-${index + 1}`,
        id,
        name,
        index === 0 ? message : `Continue ${message.toLowerCase()}`,
        index === messages - 1 ? lifecycle : "done",
        new Date(new Date(startedAt).getTime() + index * 25_000).toISOString(),
        index === messages - 1
          ? settledAt
          : new Date(new Date(startedAt).getTime() + index * 25_000 + 18_000).toISOString(),
        previous?.run_id ?? null,
      ),
    );
  }
  const conversation: Conversation = {
    conversationId: id,
    key: `review-${id}`,
    head: runs[0],
    latest: runs[runs.length - 1],
    runs,
    ownRuns: runs,
    forked: false,
    branchCount: 1,
    startedAt,
    settledAt,
    elapsedSeconds: durationSeconds,
  };
  return {
    conversation,
    lifecycle,
    turns,
    toolCalls,
    attention,
    failureMessage,
    attempts,
    review: true,
  };
}

function buildReviewRows(reviewNow: string): ConversationRow[] {
  const now = new Date(reviewNow).getTime();
  return [
    reviewRow({ now, id: "access-review", name: "access-reviewer", message: "Review workspace access and flag accounts that need attention", lifecycle: "running", minutesAgo: 2, durationSeconds: null, messages: 3, turns: 8, toolCalls: 6 }),
    reviewRow({ now, id: "release-plan", name: "release-coordinator", message: "Prepare a production rollout plan for the new service", lifecycle: "waiting", minutesAgo: 18, durationSeconds: null, messages: 2, turns: 5, toolCalls: 4, attention: "Approve the infrastructure change before continuing." }),
    reviewRow({ now, id: "incident-brief", name: "incident-analyst", message: "Summarise overnight incidents and identify recurring causes", lifecycle: "failed", minutesAgo: 47, durationSeconds: 94, messages: 1, turns: 3, toolCalls: 5, attempts: 3, failureMessage: "The incident feed stopped responding after three attempts." }),
    reviewRow({ now, id: "support-themes", name: "support-insights", message: "Compare this month’s support themes with the previous month", lifecycle: "done", minutesAgo: 82, durationSeconds: 128, messages: 4, turns: 11, toolCalls: 9, attempts: 2 }),
    reviewRow({ now, id: "operating-brief", name: "operations-writer", message: "Draft the weekly operating brief from team updates", lifecycle: "done", minutesAgo: 165, durationSeconds: 76, messages: 2, turns: 6, toolCalls: 3 }),
    reviewRow({ now, id: "permissions-audit", name: "permissions-auditor", message: "Audit stale service permissions across the workspace", lifecycle: "stopped", minutesAgo: 305, durationSeconds: 41, messages: 1, turns: 2, toolCalls: 2 }),
  ];
}

export function ConversationsView({ reviewNow = null }: { reviewNow?: string | null }) {
  const { runs, loading, error } = useRunHistory();
  const [query, setQuery] = useState("");
  const [filterId, setFilterId] = useState("all");

  const conversations = useMemo(() => groupConversations(runs ?? []), [runs]);
  const detailed = useMemo(() => conversations.slice(0, STATUS_BUDGET), [conversations]);
  const runIds = useMemo(
    () => detailed.flatMap((conversation) => conversation.runs.map((run) => run.run_id)),
    [detailed],
  );
  const runStates = useMemo(
    () => detailed.flatMap((conversation) => conversation.runs.map((run) => `${run.state}:${run.settled_at ?? ""}`)).join(","),
    [detailed],
  );
  const { statuses } = useRunStatuses(runIds, runStates);
  const liveRows = useMemo(
    () => conversations.map((conversation) => buildRow(conversation, statuses)),
    [conversations, statuses],
  );
  const rows = useMemo(
    () => (reviewNow === null ? liveRows : buildReviewRows(reviewNow)),
    [liveRows, reviewNow],
  );
  const filter = FILTERS.find((candidate) => candidate.id === filterId) ?? FILTERS[0];
  const needle = query.trim().toLowerCase();
  const shown = rows.filter((row) => {
    if (!filter.matches(row.lifecycle)) return false;
    if (!needle) return true;
    return row.conversation.runs.some((run) => run.message.toLowerCase().includes(needle));
  });
  const counts = new Map(
    FILTERS.map((candidate) => [candidate.id, rows.filter((row) => candidate.matches(row.lifecycle)).length]),
  );

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <Page>
        <PageHeader
          title="Conversations"
          description="Continue a conversation or inspect exactly how its latest run behaved."
          actions={<Button asChild><Link href="/chat">New conversation</Link></Button>}
        />

        {reviewNow !== null && (
          <div className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent/10 px-3 py-2 text-caption text-foreground">
            <MessagesSquareIcon className="size-4 shrink-0 text-accent" aria-hidden />
            <span>Local review data · open the first row’s Activity and Trace views to inspect the complete flow</span>
          </div>
        )}

        {reviewNow === null && error !== null && (
          <Alert variant="destructive"><TriangleAlertIcon /><AlertDescription>{error}</AlertDescription></Alert>
        )}

        {reviewNow === null && loading && runs === null && (
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((item) => <Skeleton key={item} className="h-20 w-full" />)}
          </div>
        )}

        {reviewNow === null && runs !== null && rows.length === 0 && (
          <EmptyState
            icon={MessagesSquareIcon}
            title="Nothing here yet"
            description="Conversations you have with an agent show up here, ready to continue or inspect."
            action={<Button asChild><Link href="/chat">Start one</Link></Button>}
          />
        )}

        {rows.length > 0 && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex flex-wrap items-center gap-1">
                {FILTERS.map((candidate) => {
                  const count = counts.get(candidate.id) ?? 0;
                  if (count === 0 && candidate.id !== "all" && candidate.id !== filterId) return null;
                  return (
                    <button
                      key={candidate.id}
                      type="button"
                      onClick={() => setFilterId(candidate.id)}
                      className={cn(
                        "rounded-full px-2.5 py-1 text-caption font-medium transition-colors",
                        candidate.id === filterId
                          ? "bg-foreground text-background"
                          : "text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}
                    >
                      {candidate.label}<span className="tabular ml-1.5 opacity-60">{count}</span>
                    </button>
                  );
                })}
              </div>
              <div className="relative ml-auto w-full sm:w-72">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
                <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search conversations" aria-label="Search conversations" className="h-8 pl-8 text-body" />
              </div>
            </div>

            {shown.length === 0 ? (
              <p className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-body text-muted-foreground">No conversation matches that.</p>
            ) : (
              <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
                {shown.map((row) => <ConversationListRow key={row.conversation.key} row={row} />)}
              </ul>
            )}
          </>
        )}
      </Page>
    </div>
  );
}

function ConversationListRow({ row }: { row: ConversationRow }) {
  const { conversation } = row;
  const title = conversation.head.message.trim() || conversation.head.name;
  const detail = row.attention ?? row.failureMessage;
  const actionClass = "opacity-100 transition-opacity md:opacity-0 md:group-hover/row:opacity-100 md:group-focus-within/row:opacity-100";

  return (
    <li className="group/row flex items-center gap-2 px-2 transition-colors hover:bg-surface/60">
      <Link href={row.review ? "/conversations?review=1" : `/chat/${conversation.latest.run_id}`} className="min-w-0 flex-1 px-2 py-3">
        <span className="block truncate text-body font-medium text-foreground">{truncate(title, 140)}</span>
        <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-muted-foreground">
          <span className="inline-flex items-center gap-1.5"><StatusDot state={row.lifecycle} />{statusLabel(row.lifecycle)}</span>
          <span>{conversation.latest.name}</span>
          <span>{relativeTime(conversation.head.started_at)}</span>
          <span>{conversation.runs.length} {conversation.runs.length === 1 ? "message" : "messages"}</span>
          {row.toolCalls !== null && row.toolCalls > 0 && <span>{row.toolCalls} tools</span>}
          {row.turns !== null && row.turns > 0 && <span>{row.turns} turns</span>}
          {conversation.elapsedSeconds !== null && <span>{formatDuration(conversation.elapsedSeconds)}</span>}
          {row.attempts !== null && row.attempts > 1 && <span>{row.attempts} attempts</span>}
        </span>
        {detail && <span className={cn("mt-1 block truncate text-caption", row.failureMessage ? "text-status-failed" : "text-status-waiting")}>{detail}</span>}
      </Link>

      <DiagnosticAction href={`/activity/${conversation.latest.run_id}${row.review ? "?review=1" : ""}`} label="Open activity" icon={ActivityIcon} className={actionClass} />
      <DiagnosticAction href={`/activity/${conversation.latest.run_id}/trace${row.review ? "?review=1" : ""}`} label="Open trace" icon={WaypointsIcon} className={actionClass} />
    </li>
  );
}

function DiagnosticAction({ href, label, icon: Icon, className }: { href: string; label: string; icon: typeof ActivityIcon; className?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button asChild variant="ghost" size="icon-sm" className={className}><Link href={href} aria-label={label}><Icon className="size-4" /></Link></Button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  );
}
