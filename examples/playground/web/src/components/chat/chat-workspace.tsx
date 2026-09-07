"use client";

import { useCallback, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2Icon, TriangleAlertIcon } from "lucide-react";
import { toast } from "sonner";

import { Composer } from "@/components/chat/composer";
import { ConversationHeader } from "@/components/chat/conversation-header";
import { ConversationSettings } from "@/components/chat/conversation-settings";
import { ConversationTimeline } from "@/components/chat/conversation-timeline";
import { ReaskDialog, type ReaskTarget } from "@/components/chat/reask-dialog";
import { SubagentPanel } from "@/components/chat/subagent-panel";
import { HistoryPanel } from "@/components/chat/history-panel";
import { NewChatHero } from "@/components/chat/new-chat-hero";
import { useConversation } from "@/components/chat/use-conversation";
import { useRunAnswer } from "@/components/chat/use-answer";
import { useBackendConfig } from "@/components/app-shell/backend-config-provider";
import { BackendUnreachableNotice } from "@/components/app-shell/health-indicator";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { useAgents } from "@/hooks/use-agents";
import { useLocalStorage } from "@/hooks/use-local-storage";
import { useRunHistory } from "@/hooks/use-run-history";
import { useRunStatus } from "@/hooks/use-run-status";
import { useSubagents } from "@/hooks/use-subagents";
import { useSession } from "@/components/auth/session-provider";
import { dispatchRun, interruptRun, resumeRun, sendToRun } from "@/lib/api";
import { branchChoice, newestOnBranch } from "@/lib/branches";
import { describeApiError } from "@/lib/errors";
import type { Lifecycle } from "@/components/ui/status";
import type { QueueKind } from "@/lib/types";

const AGENT_KEY = "psych.playground.agent";
// There is no tenant or principal key here any more. Both used to be typed
// into a popover, kept in this browser's local storage, and sent with every
// dispatch, which meant the isolation boundary was whatever the page claimed
// it was. Both now come from the signed-in session; see `ConversationSettings`.

/** The lifecycles during which the conversation owns the turn: no new message
 * can be sent, and stopping is the action on offer. `stopping` is in here on
 * purpose -- an abort is in the log but the Run has not settled, and a UI that
 * calls that "Working" offers a stop button that does nothing. */
const BUSY: ReadonlySet<Lifecycle> = new Set<Lifecycle>([
  "queued",
  "running",
  "waiting",
  "stopping",
]);

/**
 * One conversation, whether it is being written now or was reopened from
 * history.
 *
 * This component is mounted by the `(chat)` layout and stays mounted across
 * `/chat` -> `/chat/{id}`, which is the point: dispatching used to navigate
 * to a different page component, unmounting everything, throwing away the Run
 * that had just been dispatched and re-fetching it from the route. The route
 * is now a thing this reads, not a thing that recreates it.
 */
export function ChatWorkspace({ routeRunId }: { routeRunId: string | null }) {
  const router = useRouter();
  const { agents, loading: agentsLoading, error: agentsError } = useAgents();
  const { status: backendStatus } = useBackendConfig();

  const [runId, setRunId] = useState(routeRunId);
  // What `runId` continues, when this component is the one that dispatched
  // it. Lets the conversation extend the thread already on screen instead of
  // blanking while the backend confirms a chain we just built.
  const [continuesFrom, setContinuesFrom] = useState<string | null>(null);
  const [optimisticMessage, setOptimisticMessage] = useState<string | null>(
    null,
  );
  const [stopRequested, setStopRequested] = useState(false);
  // The message being asked again, and whether that is a branch or a fork, or
  // null when nothing is. Held here rather than in the timeline because the
  // answer to it is a navigation, and the timeline does not know what route it
  // is in.
  const [reaskTarget, setReaskTarget] = useState<ReaskTarget | null>(null);

  const [storedAgent, setStoredAgent] = useLocalStorage<string | null>(
    AGENT_KEY,
    null,
  );
  // `/chat?agent=<id>`, which is what "Chat" on an agent's card and detail
  // page link to. It used to carry a version hash that nothing here read, so
  // those buttons opened whichever agent was last used and looked broken to
  // anyone who noticed. Only honoured for a new conversation: an existing
  // thread's agent is settled by its log.
  const requestedAgent = useSearchParams().get("agent");
  const { account } = useSession();

  // Adjusted during render rather than in an effect: a URL that changed under
  // us (a history click, back/forward) must not render the previous
  // conversation for a frame. A URL that changed *because* we dispatched
  // already matches, and falls through doing nothing.
  const [prevRouteRunId, setPrevRouteRunId] = useState(routeRunId);
  if (routeRunId !== prevRouteRunId) {
    setPrevRouteRunId(routeRunId);
    if (routeRunId !== runId) {
      setRunId(routeRunId);
      setContinuesFrom(null);
      setOptimisticMessage(null);
      setStopRequested(false);
      setReaskTarget(null);
    }
  }

  const { conversation, loadingHistory, historyError, stream } =
    useConversation(runId, continuesFrom);
  // The stream's record count doubles as the refresh key: a Run that just did
  // something is re-read at once, and the poll inside the hook is only the
  // floor under states that change with no Record at all (a lease expiring).
  const { status, refresh: refreshStatus } = useRunStatus(
    runId,
    stream.records.length,
  );

  // The run list, for the versions of a message. It is what the history panel
  // beside this already reads, and it is the only place the tree is visible:
  // one Run's log says what it continues, never what continues it, because
  // `Store` has no such query (DESIGN.md §7).
  const { runs: history } = useRunHistory(runId);
  const versionsOf = useCallback(
    (messageRunId: string) => branchChoice(history ?? [], messageRunId),
    [history],
  );
  // Paging to a version opens the *end* of its branch, not the branched
  // message itself: a person switching to the other wording wants the answer
  // it led to and everything said after it, which is where that branch has
  // got to.
  const openVersion = useCallback(
    (versionRunId: string) =>
      router.push(`/chat/${newestOnBranch(history ?? [], versionRunId)}`),
    [history, router],
  );

  // Who an approval or an interrupt is recorded as. The account's own name,
  // not a free-text field: what gets written into the log should be who
  // actually clicked, and nobody should be able to sign somebody else's name
  // to a decision.
  const principalLabel = account?.display_name ?? "you";

  // The agent is whatever the conversation opened against, and only a new
  // conversation gets to choose. A thread whose agent changed halfway is two
  // conversations wearing one title.
  // Matched on the version this conversation opened against, not on the
  // agent's identity: an agent edited since then no longer lists that version,
  // and the honest answer is that this thread is running something the list no
  // longer shows. `send` does not depend on finding it -- see below.
  const conversationAgent =
    agents?.find((agent) => agent.version_hash === conversation.versionHash) ??
    null;
  const chosenAgent =
    agents?.find((agent) => agent.agent_id === requestedAgent) ??
    agents?.find((agent) => agent.agent_id === storedAgent) ??
    agents?.[0] ??
    null;
  const agentLocked = runId !== null;
  const activeAgent = agentLocked ? conversationAgent : chosenAgent;

  const lifecycle: Lifecycle | null = status?.lifecycle ?? null;
  const busy = lifecycle !== null && BUSY.has(lifecycle);
  // The library's own split of this Run, read once it has landed. See
  // `use-answer.ts` for why this confirms the stream rather than replacing it.
  const answer = useRunAnswer(runId, lifecycle !== null && !busy);
  // The subagents this Run started, if any. Keyed on the same record count as
  // the status read, and polled while the Run is busy: a child writes into its
  // own log, so the parent's stream says nothing at all while one works.
  const subagents = useSubagents(runId, busy, stream.records.length);
  const stopping = lifecycle === "stopping" || (stopRequested && busy);
  const offline = backendStatus === "offline";
  const noAgents =
    !agentsLoading && agentsError === null && (agents?.length ?? 0) === 0;

  // A message this component sent is echoed back by the Run's own log a moment
  // later. Until it is, show what was typed rather than an empty gap.
  const echoed = conversation.items.some(
    (item) => item.kind === "user_message" && item.runId === runId,
  );

  const send = useCallback(
    async (message: string) => {
      // Two different questions, and they get two different answers.
      //
      // A new conversation asks for an *agent*, and the backend resolves it to
      // whatever version that agent points at right now. Continuing a thread
      // pins the version the thread opened on, so editing the agent mid
      // conversation does not change what the next message runs. Both are
      // ordinary `POST /api/runs`; only which field is set differs.
      const target = agentLocked
        ? { version_hash: conversation.versionHash }
        : chosenAgent === null
          ? null
          : { agent_id: chosenAgent.agent_id };
      if (target === null || (agentLocked && conversation.versionHash === null))
        return;
      const previousRunId = runId;
      setOptimisticMessage(message);
      try {
        const { run_id } = await dispatchRun({
          ...target,
          message,
          // Continuity is the runtime's job, not something reassembled here:
          // a new Run continues the last one and inherits its history.
          continues_run_id: previousRunId,
        });
        setContinuesFrom(previousRunId);
        setStopRequested(false);
        setRunId(run_id);
        router.push(`/chat/${run_id}`);
      } catch (err) {
        setOptimisticMessage(null);
        toast.error("Could not send that message", {
          description: describeApiError(err),
        });
        throw err;
      }
    },
    [agentLocked, chosenAgent, conversation.versionHash, router, runId],
  );

  const sendMidRun = useCallback(
    async (message: string, queue: QueueKind) => {
      if (runId === null) return;
      try {
        const sent = await sendToRun(runId, { message, queue });
        toast.success(
          sent.queue === "steer"
            ? "Sent to the reply being written"
            : sent.queue === "follow_up"
              ? "Kept for after this reply"
              : "Kept for the next turn",
        );
      } catch (err) {
        toast.error("Could not send that", { description: describeApiError(err) });
        throw err;
      }
    },
    [runId],
  );

  const stop = useCallback(async () => {
    if (runId === null) return;
    setStopRequested(true);
    try {
      await interruptRun(runId, { reason: `stopped by ${principalLabel}` });
      refreshStatus();
    } catch (err) {
      setStopRequested(false);
      toast.error("Could not stop this", {
        description: describeApiError(err),
      });
    }
  }, [principalLabel, refreshStatus, runId]);

  const decide = useCallback(
    async (approved: boolean) => {
      if (runId === null) return;
      try {
        // `by` is not decoration: an approval of a destructive call whose log
        // cannot say who approved it is not an audit trail.
        await resumeRun(runId, { approved, by: principalLabel });
        // The Run leaves `waiting` with no Record of its own, so nothing in
        // the stream would tell this view the decision landed.
        refreshStatus();
      } catch (err) {
        toast.error("Could not send that decision", {
          description: describeApiError(err),
        });
        throw err;
      }
    },
    [principalLabel, refreshStatus, runId],
  );

  const sendAnswer = useCallback(
    async (answers: Record<string, string>) => {
      if (runId === null) return;
      try {
        // `answers`, keyed by each question's header, rather than one string:
        // the agent may have asked several at once, and the runtime pairs each
        // answer back with its question so the model never has to remember
        // which of its own questions came back.
        await resumeRun(runId, { payload: { answers }, by: principalLabel });
        // The Run leaves `waiting` with no Record of its own, so nothing in
        // the stream would tell this view the answer landed.
        refreshStatus();
      } catch (err) {
        toast.error("Could not send that answer", {
          description: describeApiError(err),
        });
        throw err;
      }
    },
    [principalLabel, refreshStatus, runId],
  );

  // The optimistic message is enough to render a conversation: a message just
  // sent should appear at once, not after the Run's first Record comes back.
  const showTimeline =
    runId !== null && (conversation.hasRecords || optimisticMessage !== null);
  const loadingConversation =
    runId !== null && !showTimeline && stream.status !== "error";

  const blockedReason = offline
    ? "Waiting for the backend"
    : noAgents
      ? "Create an agent to start a conversation"
      : null;

  return (
    <div className="flex min-h-0 flex-1">
      {/* The conversation itself is the leftmost thing on the screen, next to
          the app's own nav. The history sits on the right, where it is a place
          to go back to rather than something to read past on the way in. */}
      <div className="flex min-h-0 flex-1 flex-col">
        {offline && <BackendUnreachableNotice />}

        <ConversationHeader
          runId={runId}
          title={
            activeAgent?.name ?? (runId === null ? "New chat" : "Conversation")
          }
          lifecycle={lifecycle}
          settings={
            <ConversationSettings
              accountName={principalLabel}
              runId={runId}
              versionHash={conversation.versionHash}
            />
          }
        />

        {stream.status === "reconnecting" && conversation.hasRecords && (
          <p className="flex items-center gap-2 border-b border-border px-4 py-1.5 text-caption text-muted-foreground">
            <Loader2Icon className="size-3 animate-spin" aria-hidden />
            Reconnecting.
          </p>
        )}

        <ScrollArea className="min-h-0 flex-1">
          {/* The reading column widens a step on a large monitor and stops
              there. A measure that keeps growing turns an answer into a line
              of text the eye cannot track back from; one that never grows
              leaves a stripe down the middle of a 27 inch screen. */}
          <div className="mx-auto flex w-full max-w-3xl flex-col px-6 xl:max-w-4xl">
            {runId === null && (
              <NewChatHero
                agents={agents}
                loading={agentsLoading}
                offline={offline}
              />
            )}

            {loadingConversation && <ConversationSkeleton />}

            {runId !== null && stream.status === "error" && (
              <Alert variant="destructive" className="mt-6">
                <TriangleAlertIcon />
                <AlertDescription>
                  {stream.error ?? "Lost the connection to this conversation."}
                </AlertDescription>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-2 w-fit"
                  onClick={stream.retry}
                >
                  Try again
                </Button>
              </Alert>
            )}

            {runId !== null && historyError !== null && (
              <Alert variant="destructive" className="mt-6">
                <TriangleAlertIcon />
                <AlertDescription>
                  Earlier messages in this conversation could not be loaded.{" "}
                  {historyError}
                </AlertDescription>
              </Alert>
            )}

            {loadingHistory && showTimeline && (
              <p className="pt-6 text-caption text-muted-foreground">
                Loading earlier messages.
              </p>
            )}

            {/* The tree, below the conversation and above the composer:
                everything happening inside a subagent happens in somebody
                else's log, so without this a fanned-out run simply goes
                quiet for as long as its children take. */}
            {subagents.tree !== null && (
              <div className="pt-6">
                <SubagentPanel
                  tree={subagents.tree}
                  onChanged={subagents.refresh}
                />
              </div>
            )}

            {showTimeline && (
              <ConversationTimeline
                conversation={conversation}
                activeRunId={runId}
                lifecycle={lifecycle}
                live={busy}
                answer={answer}
                pendingApproval={status?.pending_approval ?? null}
                pendingQuestion={status?.pending_question ?? null}
                plan={status?.tasks ?? []}
                onAnswer={sendAnswer}
                onDecide={decide}
                decidedByLabel={principalLabel}
                failureMessage={status?.failure_message ?? null}
                optimisticMessage={echoed ? null : optimisticMessage}
                onReask={(target) => setReaskTarget(target)}
                versionsOf={versionsOf}
                onOpenVersion={openVersion}
              />
            )}
          </div>
        </ScrollArea>

        <div className="shrink-0 border-t border-border bg-background/80 p-3 backdrop-blur">
          <div className="mx-auto w-full max-w-3xl px-6">
            <Composer
              agents={agents}
              agentsLoading={agentsLoading}
              agentsError={agentsError}
              selectedAgent={activeAgent}
              onSelectAgent={setStoredAgent}
              agentLocked={agentLocked}
              onSend={send}
              onSendMidRun={runId === null ? null : sendMidRun}
              busy={busy}
              stopping={stopping}
              onStop={() => void stop()}
              blockedReason={blockedReason}
            />
          </div>
        </div>
      </div>

      <HistoryPanel activeRunId={runId} className="hidden md:flex" />

      {/* Both a branch and a fork open at a Run this component was not
          holding, so both are a plain navigation rather than a state change
          here: `routeRunId` changes, the adjustment above resets everything
          this component was holding, and the new Run's own thread is read from
          the backend. Nothing carries over from what was on screen, which is
          the point. */}
      <ReaskDialog
        target={reaskTarget}
        agents={agents}
        currentAgentId={activeAgent?.agent_id ?? null}
        onClose={() => setReaskTarget(null)}
        onStarted={(startedRunId) => router.push(`/chat/${startedRunId}`)}
      />
    </div>
  );
}

/**
 * What a conversation looks like while its earlier turns are being read.
 *
 * Shaped like the thing it stands in for -- a message on the right, an answer
 * on the left -- rather than a spinner, so the layout is already the right
 * height when the text lands and nothing jumps into place.
 */
function ConversationSkeleton() {
  return (
    <div className="flex w-full flex-col gap-8 py-8" aria-hidden>
      <div className="flex justify-end">
        <Skeleton className="h-9 w-48 rounded-2xl" />
      </div>
      <div className="flex gap-3">
        <Skeleton className="size-6 shrink-0 rounded-lg" />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-4 w-full max-w-sm" />
          <Skeleton className="h-4 w-32" />
        </div>
      </div>
    </div>
  );
}
