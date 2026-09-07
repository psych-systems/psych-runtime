"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { PanelLeftIcon, SquarePenIcon } from "lucide-react";

import { HistoryPanel } from "@/components/chat/history-panel";
import { TraceLink } from "@/components/trace/trace-link";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { StatusPill, type Lifecycle } from "@/components/ui/status";

/**
 * The one line above the conversation: what you are talking to, and whether it
 * is doing anything. The run's identifiers are not here; they are behind the
 * settings control, which is the only technical door on this screen.
 */
export function ConversationHeader({
  runId,
  title,
  lifecycle,
  settings,
}: {
  runId: string | null;
  title: string;
  /** Null before the first status read, so the pill does not flash "Queued"
   * at a conversation that finished yesterday. */
  lifecycle: Lifecycle | null;
  settings: ReactNode;
}) {
  return (
    <header className="bar">
      <Sheet>
        <SheetTrigger asChild>
          <Button variant="ghost" size="icon-sm" className="md:hidden" aria-label="Conversations">
            <PanelLeftIcon />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="sr-only">
            <SheetTitle>Conversations</SheetTitle>
          </SheetHeader>
          <HistoryPanel activeRunId={runId} collapsible={false} className="w-full border-r-0" />
        </SheetContent>
      </Sheet>

      <h1 className="min-w-0 truncate text-body font-medium">{title}</h1>
      {lifecycle !== null && lifecycle !== "done" && <StatusPill state={lifecycle} size="sm" />}

      <div className="ml-auto flex shrink-0 items-center gap-1">
        {runId !== null && <TraceLink runId={runId} />}
        {settings}
        <Button asChild variant="ghost" size="icon-sm" aria-label="New chat">
          <Link href="/chat">
            <SquarePenIcon />
          </Link>
        </Button>
      </div>
    </header>
  );
}
