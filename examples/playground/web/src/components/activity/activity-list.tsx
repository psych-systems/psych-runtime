"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  ActivityIcon,
  AlertCircleIcon,
  MessagesSquareIcon,
  RefreshCwIcon,
  SearchIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle, type Lifecycle } from "@/components/ui/status";
import { groupConversations, type Conversation } from "@/lib/branches";
import { useRunStatuses } from "@/components/activity/use-run-statuses";
import { useRunHistory } from "@/hooks/use-run-history";
import { formatClockTime, formatDayLabel, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { RunStatus } from "@/lib/types";

/** The filter buttons, in the order a person scans for trouble: everything,
 *  then the two states that want a human, then the two that are over. */
const FILTERS: { id: string; label: string; matches: (l: Lifecycle) => boolean }[] = [
  { id: "all", label: "All", matches: () => true },
  {
    id: "active",
    label: "Active",
    matches: (l) => l === "running" || l === "queued" || l === "stopping",
  },
  { id: "waiting", label: "Needs you", matches: (l) => l === "waiting" },
  { id: "failed", label: "Failed", matches: (l) => l === "failed" },
  { id: "done", label: "Done", matches: (l) => l === "done" },
  { id: "stopped", label: "Stopped", matches: (l) => l === "stopped" },
];

/** How many conversations get a status read on first paint. */
const STATUS_BUDGET = 50;

interface Row {
  conversation: Conversation;
  lifecycle: Lifecycle;
  /** Null until this conversation's statuses land, so counts read as "not
   *  known yet" rather than as zero. A zero here would be a lie about a Run
   *  that has made twelve tool calls. */
  turns: number | null;
  toolCalls: number | null;
  failureMessage: string | null;
}

function buildRow(conversation: Conversation, statuses: Map<string, RunStatus>): Row {
  const latestStatus = statuses.get(conversation.latest.run_id);
  let turns: number | null = null;
  let toolCalls: number | null = null;
  for (const run of conversation.runs) {
    const status = statuses.get(run.run_id);
    if (status === undefined) continue;
    turns = (turns ?? 0) + status.turn;
    toolCalls = (toolCalls ?? 0) + status.tool_calls;
  }
  return {
    conversation,
    lifecycle: latestStatus?.lifecycle ?? toLifecycle(conversation.latest.state),
    turns,
    toolCalls,
    failureMessage: latestStatus?.failure_message ?? null,
  };
}

export function ActivityList() {
  const { runs, loading, error, refresh } = useRunHistory();
  const [filterId, setFilterId] = useState("all");
  const [query, setQuery] = useState("");

  const conversations = useMemo(() => groupConversations(runs ?? []), [runs]);
  // Counts and failure text come from one status read per Run, so the newest
  // conversations are the ones enriched. A history of a thousand Runs must
  // not open by firing a thousand requests; older rows still show their
  // message, their agent and the list's own state.
  const detailed = useMemo(() => conversations.slice(0, STATUS_BUDGET), [conversations]);
  const runIds = useMemo(
    () => detailed.flatMap((c) => c.runs.map((r) => r.run_id)),
    [detailed]
  );
  // The Runs' own states are the revalidation signal: `useRunHistory` polls
  // while anything is active, and without this a Run that settled would keep
  // whatever lifecycle its first status read reported.
  const runStates = useMemo(
    () => detailed.flatMap((c) => c.runs.map((r) => `${r.state}:${r.settled_at ?? ""}`)).join(","),
    [detailed]
  );
  const { statuses } = useRunStatuses(runIds, runStates);

  const rows = useMemo(
    () => conversations.map((c) => buildRow(c, statuses)),
    [conversations, statuses]
  );

  const filter = FILTERS.find((f) => f.id === filterId) ?? FILTERS[0];
  const needle = query.trim().toLowerCase();
  const visible = rows.filter((row) => {
    if (!filter.matches(row.lifecycle)) return false;
    if (needle.length === 0) return true;
    return row.conversation.runs.some((run) => run.message.toLowerCase().includes(needle));
  });

  const counts = new Map(FILTERS.map((f) => [f.id, rows.filter((r) => f.matches(r.lifecycle)).length]));

  return (
    <Page>
      <PageHeader
        title="Activity"
        description="Every conversation this playground has dispatched, what it did, and how it ended. Open one for its numbers, its trace and its identifiers."
        actions={
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCwIcon className={cn("size-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-body text-destructive">
          <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>{error}</span>
        </div>
      )}

      {loading && runs === null && (
        <div className="flex flex-col gap-2">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      )}

      {runs !== null && rows.length === 0 && !error && (
        <EmptyState
          icon={ActivityIcon}
          title="No conversations yet"
          description="Every conversation you start shows up here with its outcome, how long it took, and what it cost."
          action={
            <Button asChild size="sm">
              <Link href="/chat">
                <MessagesSquareIcon className="size-4" />
                Start a conversation
              </Link>
            </Button>
          }
        />
      )}

      {rows.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex flex-wrap items-center gap-1">
              {FILTERS.map((f) => {
                const count = counts.get(f.id) ?? 0;
                if (count === 0 && f.id !== "all" && f.id !== filterId) return null;
                return (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => setFilterId(f.id)}
                    className={cn(
                      "rounded-full px-2.5 py-1 text-caption font-medium transition-colors",
                      f.id === filterId
                        ? "bg-foreground text-background"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    )}
                  >
                    {f.label}
                    <span className="tabular ml-1.5 opacity-60">{count}</span>
                  </button>
                );
              })}
            </div>
            <div className="relative ml-auto w-full sm:w-64">
              <SearchIcon
                className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search messages"
                aria-label="Search messages"
                className="h-8 pl-8 text-body"
              />
            </div>
          </div>

          {visible.length === 0 ? (
            <p className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-body text-muted-foreground">
              No conversation matches that.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {visible.map((row) => (
                <ConversationRow key={row.conversation.key} row={row} />
              ))}
            </ul>
          )}
        </>
      )}
    </Page>
  );
}

function Count({ value, unit }: { value: number | null; unit: string }) {
  if (value === null) return null;
  return (
    <span className="whitespace-nowrap">
      <span className="tabular text-foreground">{value}</span> {unit}
      {value === 1 ? "" : "s"}
    </span>
  );
}

function ConversationRow({ row }: { row: Row }) {
  const { conversation } = row;
  const started = conversation.startedAt;

  return (
    <li>
      <Link
        href={`/activity/${conversation.latest.run_id}`}
        className="flex flex-col gap-2 rounded-xl border border-border bg-surface/40 px-4 py-3 transition-colors hover:border-border hover:bg-surface"
      >
        <div className="flex items-start justify-between gap-3">
          {/* This branch's own opening message, which for a forked branch is
              the question asked instead. Titling every branch with the
              conversation's first message would print the same line twice and
              leave the two rows indistinguishable. */}
          <p className="min-w-0 flex-1 truncate text-body font-medium">
            {conversation.head.message.trim().length > 0
              ? conversation.head.message
              : "(no opening message)"}
          </p>
          <StatusPill state={row.lifecycle} size="sm" />
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted-foreground">
          <span className="font-medium text-foreground/80">{conversation.latest.name}</span>
          <span aria-hidden className="opacity-40">
            ·
          </span>
          <span>
            {formatDayLabel(started)} at {formatClockTime(started)}
          </span>
          {conversation.elapsedSeconds !== null && (
            <>
              <span aria-hidden className="opacity-40">
                ·
              </span>
              <span className="tabular">{formatDuration(conversation.elapsedSeconds)}</span>
            </>
          )}
          {conversation.runs.length > 1 && (
            <>
              <span aria-hidden className="opacity-40">
                ·
              </span>
              <Count value={conversation.runs.length} unit="message" />
            </>
          )}
          {row.turns !== null && (
            <>
              <span aria-hidden className="opacity-40">
                ·
              </span>
              <Count value={row.turns} unit="turn" />
            </>
          )}
          {row.toolCalls !== null && row.toolCalls > 0 && (
            <>
              <span aria-hidden className="opacity-40">
                ·
              </span>
              <Count value={row.toolCalls} unit="tool call" />
            </>
          )}
        </div>

        {row.lifecycle === "failed" && row.failureMessage && (
          <p className="flex items-start gap-1.5 text-caption text-status-failed">
            <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            <span className="min-w-0">{row.failureMessage}</span>
          </p>
        )}
      </Link>
    </li>
  );
}
