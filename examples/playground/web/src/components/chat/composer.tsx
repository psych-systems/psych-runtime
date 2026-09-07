"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import Link from "next/link";
import { ArrowUpIcon, SquareIcon } from "lucide-react";

import { AgentPicker } from "@/components/chat/agent-picker";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { AgentSummary, QueueKind } from "@/lib/types";

/** DESIGN.md §9's three queues, in the words a person picking one needs. */
const QUEUES: { kind: QueueKind; label: string; help: string }[] = [
  {
    kind: "steer",
    label: "Guide this run",
    help: "The agent reads this before its next model step.",
  },
  {
    kind: "follow_up",
    label: "Add a follow-up",
    help: "The current step finishes, then the agent continues with this.",
  },
  {
    kind: "next_run",
    label: "Save for next message",
    help: "Kept for the next run, even if you stop this one.",
  },
];

interface ComposerProps {
  agents: AgentSummary[] | null;
  agentsLoading: boolean;
  agentsError: string | null;
  selectedAgent: AgentSummary | null;
  onSelectAgent: (versionHash: string) => void;
  /** True once the conversation exists: the agent is settled and only the
   * message changes. */
  agentLocked: boolean;
  onSend: (message: string) => Promise<void>;
  /** A message into the run still executing, on one of the three queues.
   *  Offered while `busy`; null when the surface cannot send one. */
  onSendMidRun: ((message: string, queue: QueueKind) => Promise<void>) | null;
  /** The conversation is mid-answer: a message goes into a queue rather than
   *  starting a new run, and stopping is offered. */
  busy: boolean;
  /** A stop has been requested and the run has not settled yet. */
  stopping: boolean;
  onStop: () => void;
  /** Why nothing can be sent right now, said plainly. Null when it can. */
  blockedReason: string | null;
}

/**
 * Where a message is written.
 *
 * Enter sends, shift+Enter starts a line: the convention of every chat client,
 * and the one a person will try first. There is no tenant or principal here.
 * Those decide which isolated world the run executes in, which is a thing an
 * operator sets once, not a thing a person picks per message, so they live in
 * the conversation's settings instead.
 */
export function Composer({
  agents,
  agentsLoading,
  agentsError,
  selectedAgent,
  onSelectAgent,
  agentLocked,
  onSend,
  onSendMidRun,
  busy,
  stopping,
  onStop,
  blockedReason,
}: ComposerProps) {
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [queue, setQueue] = useState<QueueKind>("steer");

  const noAgents = !agentsLoading && agentsError === null && (agents?.length ?? 0) === 0;
  const midRun = busy && onSendMidRun !== null && !stopping;
  const canSend =
    (!busy || midRun) &&
    !sending &&
    blockedReason === null &&
    selectedAgent !== null &&
    message.trim().length > 0;

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) return;
    const text = message.trim();
    setSending(true);
    // Cleared before the request, not after: the message is already on screen
    // optimistically, and leaving it in the box invites sending it twice.
    setMessage("");
    try {
      if (midRun && onSendMidRun !== null) await onSendMidRun(text, queue);
      else await onSend(text);
    } catch {
      setMessage(text);
    } finally {
      setSending(false);
    }
  }

  const chosenQueue = QUEUES.find((q) => q.kind === queue) ?? QUEUES[0];

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form
      onSubmit={submit}
      className={
        "flex w-full flex-col gap-2 rounded-2xl border bg-card p-2.5 shadow-sm transition-colors " +
        (midRun ? "border-primary/45 ring-4 ring-primary/5" : "border-border focus-within:border-ring/60")
      }
    >
      {midRun && (
        <div className="flex items-center gap-2 px-1 pt-0.5 text-micro font-medium text-primary">
          <span className="size-1.5 animate-pulse rounded-full bg-primary" aria-hidden />
          Agent working · choose when it should read your note
        </div>
      )}
      <Textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={
          blockedReason ??
          (midRun
            ? chosenQueue.kind === "steer"
              ? "Guide what the agent is doing now"
              : chosenQueue.kind === "follow_up"
                ? "Add something after its current step"
                : "Write the next message now"
            : busy
              ? "Waiting for the answer"
              : `Message ${selectedAgent?.name ?? "an agent"}`)
        }
        disabled={blockedReason !== null || (busy && !midRun)}
        rows={1}
        className="max-h-48 min-h-11 resize-none rounded-none border-none bg-transparent! p-1 text-prose shadow-none focus-visible:border-transparent focus-visible:ring-0"
      />
      <div className="flex flex-wrap items-center gap-2">
        {noAgents ? (
          <Button asChild variant="outline" size="sm" className="rounded-full">
            <Link href="/agents/new">Create an agent</Link>
          </Button>
        ) : (
          <AgentPicker
            agents={agents}
            loading={agentsLoading}
            error={agentsError}
            selected={selectedAgent}
            onSelect={onSelectAgent}
            locked={agentLocked}
          />
        )}

        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {midRun && (
            <Select value={queue} onValueChange={(kind) => setQueue(kind as QueueKind)}>
              <SelectTrigger
                size="sm"
                className="min-w-40 rounded-full"
                aria-label="Where this message goes"
                title={chosenQueue.help}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent align="end">
                {QUEUES.map((q) => (
                  <SelectItem key={q.kind} value={q.kind}>
                    <span className="flex flex-col">
                      <span>{q.label}</span>
                      <span className="text-micro text-muted-foreground">{q.help}</span>
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {busy && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={onStop}
              disabled={stopping}
            >
              <SquareIcon className="fill-current" />
              {stopping ? "Stopping" : "Stop"}
            </Button>
          )}
          {(!busy || midRun) && (
            <Button type="submit" size="icon-sm" className="rounded-full" disabled={!canSend}>
              <ArrowUpIcon />
              <span className="sr-only">{midRun ? chosenQueue.label : "Send message"}</span>
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
