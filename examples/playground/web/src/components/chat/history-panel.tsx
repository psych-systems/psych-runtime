"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import {
  GitBranchIcon,
  GitForkIcon,
  MessagesSquareIcon,
  PanelRightCloseIcon,
  PanelRightOpenIcon,
  SquarePenIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { toast } from "sonner";

import { relativeTime } from "@/components/chat/relative-time";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusDot, statusLabel, toLifecycle } from "@/components/ui/status";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useLocalStorage } from "@/hooks/use-local-storage";
import { useRunHistory } from "@/hooks/use-run-history";
import { deleteRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { groupConversations, type Conversation } from "@/lib/branches";
import { formatDayLabel, truncate } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Whether the panel is open, remembered. Someone who works with it shut
 *  should not have to shut it again on every visit, and someone who never
 *  touches it never learns this key exists. */
const PANEL_KEY = "psych.playground.conversations-open";

function groupByDay(threads: Conversation[]): [string, Conversation[]][] {
  const groups = new Map<string, Conversation[]>();
  // Dated and ordered by the thread's most recent turn, not by when it was
  // opened: a conversation replied to today belongs under today.
  const byRecency = [...threads].sort((a, b) =>
    b.latest.started_at.localeCompare(a.latest.started_at),
  );
  for (const thread of byRecency) {
    const label = formatDayLabel(thread.latest.started_at);
    const bucket = groups.get(label);
    if (bucket) bucket.push(thread);
    else groups.set(label, [thread]);
  }
  return [...groups.entries()];
}

/**
 * Every conversation, newest first. One row per conversation rather than one
 * per turn: a person had one conversation, whatever the runtime had.
 *
 * It collapses. The main navigation already does, and a chat screen that keeps
 * a fixed 16rem of history beside it on a 13 inch laptop is a chat screen with
 * no room left for the answer, which is the thing the person came to read.
 * Collapsed it keeps a rail: the way back, and the one action worth reaching
 * without opening anything.
 */
export function HistoryPanel({
  activeRunId,
  className,
  /** False inside the mobile sheet, which is already a panel someone opened
   *  on purpose. Collapsing it there would leave a rail floating over the
   *  conversation with nothing to return to. */
  collapsible = true,
}: {
  activeRunId: string | null;
  className?: string;
  collapsible?: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useLocalStorage<boolean>(PANEL_KEY, true);
  const collapsed = collapsible && !open;
  const { runs, loading, error, refresh } = useRunHistory(activeRunId);
  const groups = useMemo(
    () => groupByDay(groupConversations(runs ?? [])),
    [runs],
  );
  const [pendingDelete, setPendingDelete] = useState<Conversation | null>(null);

  const removeThread = useCallback(
    async (thread: Conversation) => {
      try {
        // `thread: true` so the whole conversation goes rather than its newest
        // turn alone, which would leave the predecessors behind as a stump.
        // Every branch of it goes too: branches are the versions of a message
        // a person pages between, not chats of their own.
        //
        // A *fork* of this conversation is a different row and survives. The
        // backend follows Git's rule for the history the two share: a Run the
        // fork still reaches is kept and stops being a chat, rather than being
        // deleted out from under it.
        await deleteRun(thread.latest.run_id, { thread: true });
        if (thread.ownRuns.some((run) => run.run_id === activeRunId))
          router.push("/chat");
        void refresh();
      } catch (err) {
        toast.error("Could not delete that conversation", {
          description: describeApiError(err),
        });
      }
    },
    [activeRunId, refresh, router],
  );

  return (
    <div
      className={cn(
        // The width is the only thing that animates, and it animates because
        // the panel is going somewhere: a fade alone reads as a bug, and an
        // instant snap makes the conversation jump sideways under the reader.
        // Both states are mounted and cross-faded so the header bar never
        // blinks out mid-slide.
        "relative h-full shrink-0 overflow-hidden border-l border-border bg-sidebar/40 transition-[width] duration-200 ease-out",
        collapsed ? "w-12" : "w-64",
        className,
      )}
    >
      {collapsible && (
        <div
          inert={!collapsed}
          className={cn(
            "absolute inset-y-0 left-0 flex w-12 flex-col transition-opacity duration-150",
            collapsed ? "opacity-100 delay-75" : "opacity-0",
          )}
        >
          <div className="bar justify-center px-0">
            <PanelButton collapsed onClick={() => setOpen(true)} />
          </div>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                asChild
                size="icon-sm"
                variant="ghost"
                className="mx-auto mt-2"
                aria-label="New chat"
              >
                <Link href="/chat">
                  <SquarePenIcon />
                </Link>
              </Button>
            </TooltipTrigger>
            <TooltipContent side="right">New chat</TooltipContent>
          </Tooltip>
        </div>
      )}

      <div
        inert={collapsed}
        className={cn(
          "absolute inset-y-0 left-0 flex flex-col transition-opacity duration-150",
          // Fixed at the open width so the text inside does not reflow while
          // the panel slides; the sheet has no rail beside it and fills.
          collapsible ? "w-64" : "w-full",
          collapsed ? "opacity-0" : "opacity-100 delay-75",
        )}
      >
        <div className="bar justify-between">
          <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
            Conversations
          </span>
          <div className="flex items-center gap-0.5">
            <Button
              asChild
              size="icon-xs"
              variant="ghost"
              aria-label="New chat"
            >
              <Link href="/chat">
                <SquarePenIcon />
              </Link>
            </Button>
            {collapsible && (
              <PanelButton collapsed={false} onClick={() => setOpen(false)} />
            )}
          </div>
        </div>

        <ScrollArea className="min-h-0 flex-1">
          <div className="flex flex-col gap-3 p-2">
            {loading && (
              <div className="flex flex-col gap-2 p-1">
                {[0, 1, 2].map((index) => (
                  <Skeleton key={index} className="h-12 w-full rounded-lg" />
                ))}
              </div>
            )}

            {!loading && error !== null && (
              <Alert variant="destructive">
                <TriangleAlertIcon />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            {!loading && error === null && groups.length === 0 && (
              <p className="flex items-center gap-2 p-2 text-caption text-muted-foreground">
                <MessagesSquareIcon className="size-3.5 shrink-0" aria-hidden />
                Nothing here yet.
              </p>
            )}

            {groups.map(([label, dayThreads]) => (
              <div key={label} className="flex flex-col gap-0.5">
                <div className="px-2 py-1 text-micro font-medium text-muted-foreground/80">
                  {label}
                </div>
                {dayThreads.map((thread) => (
                  <HistoryRow
                    key={thread.key}
                    thread={thread}
                    // Any Run of this conversation marks the row active: a
                    // person on turn three, or on a second branch of turn two,
                    // is still reading this chat. Matched against its own Runs
                    // and not the history a fork replays, which would otherwise
                    // light up both rows at once.
                    active={thread.ownRuns.some(
                      (run) => run.run_id === activeRunId,
                    )}
                    onDelete={(skipConfirm) =>
                      skipConfirm
                        ? void removeThread(thread)
                        : setPendingDelete(thread)
                    }
                  />
                ))}
              </div>
            ))}
          </div>
        </ScrollArea>
      </div>

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete this conversation?</DialogTitle>
            <DialogDescription>
              {pendingDelete !== null && pendingDelete.branchCount > 1
                ? `All ${pendingDelete.branchCount} branches of it go, every version of every
                   message. `
                : pendingDelete !== null && pendingDelete.ownRuns.length > 1
                  ? `All ${pendingDelete.ownRuns.length} exchanges disappear from this list. `
                  : "It disappears from this list. "}
              The record of what happened is kept, and stays available to
              whoever operates this platform.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (pendingDelete !== null) void removeThread(pendingDelete);
                setPendingDelete(null);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/** The one control that opens and shuts the panel, in both states, so its
 *  label and its keyboard stop are the same thing wherever it is. */
function PanelButton({
  collapsed,
  onClick,
}: {
  collapsed: boolean;
  onClick: () => void;
}) {
  const label = collapsed ? "Show conversations" : "Hide conversations";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          onClick={onClick}
          aria-label={label}
          aria-expanded={!collapsed}
        >
          {/* Right-facing, because the panel is on the right. A left
              chevron here says the panel will appear on the other side of
              the screen from where it actually does. */}
          {collapsed ? <PanelRightOpenIcon /> : <PanelRightCloseIcon />}
        </Button>
      </TooltipTrigger>
      {/* Collapsed, this button is the rightmost thing on the screen, so a
          tooltip to its right opens off the edge. */}
      <TooltipContent side={collapsed ? "left" : "bottom"}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
}

function HistoryRow({
  thread,
  active,
  onDelete,
}: {
  thread: Conversation;
  active: boolean;
  onDelete: (skipConfirm: boolean) => void;
}) {
  const { head, latest, runs } = thread;
  const lifecycle = toLifecycle(latest.state);
  // This branch's opening message, not the latest: it reads as the
  // conversation's title, and a title that changed on every reply would make
  // the list unscannable. For a forked branch it is the question asked
  // instead, which is the only thing that tells two branches of one
  // conversation apart at a glance.
  const title =
    head.message.trim().length > 0
      ? truncate(head.message.trim(), 70)
      : head.name;

  return (
    <div className="group/row relative">
      <button
        type="button"
        aria-label={`Delete conversation: ${title}`}
        title="Delete. Hold ctrl to skip the confirmation."
        onClick={(event) => {
          // Inside the row but outside the Link, so this never navigates.
          event.preventDefault();
          event.stopPropagation();
          onDelete(event.ctrlKey || event.metaKey);
        }}
        className="absolute top-1.5 right-1.5 z-10 hidden rounded p-1 text-muted-foreground transition-colors group-hover/row:block hover:bg-destructive/10 hover:text-destructive"
      >
        <Trash2Icon className="size-3.5" />
      </button>

      <Link
        // Opens the newest turn: that is where the conversation is, and what
        // the next message continues from.
        href={`/chat/${latest.run_id}`}
        className={cn(
          "flex flex-col gap-1 rounded-lg px-2 py-1.5 transition-colors hover:bg-sidebar-accent",
          active && "bg-sidebar-accent",
        )}
      >
        <span className="line-clamp-2 pr-5 text-caption font-medium text-foreground">
          {title}
        </span>
        <span className="flex items-center gap-1.5 text-micro text-muted-foreground">
          <StatusDot state={lifecycle} />
          {lifecycle === "done" ? head.name : statusLabel(lifecycle)}
          <span aria-hidden>·</span>
          {relativeTime(latest.started_at)}
          {runs.length > 1 && (
            <>
              <span aria-hidden>·</span>
              {runs.length} turns
            </>
          )}
          {thread.branchCount > 1 && (
            <>
              <span aria-hidden>·</span>
              <span className="inline-flex items-center gap-0.5">
                <GitBranchIcon className="size-3" aria-hidden />
                {thread.branchCount} branches
              </span>
            </>
          )}
          {thread.forked && (
            <>
              <span aria-hidden>·</span>
              <span className="inline-flex items-center gap-0.5">
                <GitForkIcon className="size-3" aria-hidden />
                fork
              </span>
            </>
          )}
        </span>
      </Link>
    </div>
  );
}
