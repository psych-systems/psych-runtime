"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { MessagesSquareIcon, SearchIcon, TriangleAlertIcon } from "lucide-react";

import { relativeTime } from "@/components/chat/relative-time";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusDot, statusLabel, toLifecycle } from "@/components/ui/status";
import { groupConversations } from "@/lib/branches";
import { useRunHistory } from "@/hooks/use-run-history";
import { truncate } from "@/lib/format";

/**
 * Everything you have asked, on a page of its own.
 *
 * The same conversations the chat panel lists, and deliberately the same
 * grouping function rather than a second one: two lists that disagreed about
 * where one conversation ends and the next begins would be worse than either
 * alone. This exists because the panel can be collapsed and the nav can be
 * reduced to icons, and a person should still be able to reach what they asked
 * yesterday in one click.
 *
 * Distinct from Activity, which answers an operator's question: how long did it
 * take, what did it cost, what did it call. This one answers a person's: what
 * did I ask, and what came back.
 */
export function ConversationsView() {
  const { runs, loading, error } = useRunHistory();
  const [query, setQuery] = useState("");

  const threads = useMemo(() => groupConversations(runs ?? []), [runs]);
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return threads;
    return threads.filter((thread) => thread.head.message.toLowerCase().includes(needle));
  }, [threads, query]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <Page>
        <PageHeader
          title="Conversations"
          description="Everything you have asked, newest first."
          actions={
            <Button asChild>
              <Link href="/chat">New conversation</Link>
            </Button>
          }
        />

        {error !== null && (
          <Alert variant="destructive">
            <TriangleAlertIcon />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {loading && runs === null && (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        )}

        {runs !== null && threads.length === 0 && (
          <EmptyState
            icon={MessagesSquareIcon}
            title="Nothing here yet"
            description="Conversations you have with an agent show up here, so you can pick one back up later."
            action={
              <Button asChild>
                <Link href="/chat">Start one</Link>
              </Button>
            }
          />
        )}

        {threads.length > 0 && (
          <>
            {/* Only offered once there is enough to need it: a search box over
                three conversations is furniture rather than a feature. */}
            {threads.length > 6 && (
              <div className="relative">
                <SearchIcon
                  className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                  aria-hidden
                />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search what you asked"
                  aria-label="Search conversations"
                  className="pl-9"
                />
              </div>
            )}

            {shown.length === 0 ? (
              <p className="py-8 text-center text-body text-muted-foreground">
                Nothing matches {`"${query.trim()}"`}.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {shown.map((thread) => {
                  const lifecycle = toLifecycle(thread.latest.state);
                  const turns = thread.runs.length;
                  return (
                    <li key={thread.key}>
                      <Link
                        href={`/chat/${thread.latest.run_id}`}
                        className="flex flex-col gap-1.5 rounded-xl border border-border bg-card px-4 py-3 transition-colors hover:border-ring/40 hover:bg-surface/60"
                      >
                        {/* This branch's own opening ask. A forked branch
                            titled with the conversation's first message would
                            be indistinguishable from the branch it was forked
                            out of. */}
                        <span className="text-body font-medium text-foreground">
                          {truncate(thread.head.message, 140)}
                        </span>
                        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-muted-foreground">
                          <span className="inline-flex items-center gap-1.5">
                            <StatusDot state={lifecycle} />
                            {statusLabel(lifecycle)}
                          </span>
                          <span>{thread.latest.name}</span>
                          <span>{relativeTime(thread.head.started_at)}</span>
                          {turns > 1 && (
                            <span>
                              {turns} messages
                            </span>
                          )}
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        )}
      </Page>
    </div>
  );
}
