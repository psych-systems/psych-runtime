"use client";

import { useState } from "react";
import { GitBranchIcon, GitForkIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { branchRun, forkRun } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { AgentSummary } from "@/lib/types";

/** Sentinel for "whichever agent this conversation is already using". The
 *  empty string cannot be a `SelectItem` value, and an agent id is opaque, so
 *  the two cannot be told apart by shape. */
const SAME_AGENT = "@same";

export interface ReaskTarget {
  /** The Run whose message is being asked again. */
  runId: string;
  /** What was asked, prefilled so the common edit is a small one. */
  message: string;
  /** Which of the two operations this is.
   *
   *  They diverge at the same message and carry the same history. `branch`
   *  keeps both versions inside this chat, which is what the arrows on a
   *  message page between. `fork` puts the new one in a chat of its own, which
   *  is the only one of the two that may change agent. */
  mode: "branch" | "fork";
}

/**
 * Ask one message of this conversation again, without losing the answer it
 * already got.
 *
 * The message is prefilled because the two things people do here are rewording
 * a question and putting the same question to another agent, and both start
 * from what was asked rather than from an empty box.
 *
 * One dialog for both operations rather than two, because the difference
 * between them is one field and one sentence: everything a person types is the
 * same. Choosing another agent is offered on a fork and not on a branch, and
 * that is not a restriction for its own sake -- a conversation is with one
 * agent for its whole life, so a branch that switched would produce a chat
 * whose two halves were answered by different agents with nothing on screen
 * saying so. Nothing about the original changes either way.
 */
export function ReaskDialog({
  target,
  agents,
  currentAgentId,
  onClose,
  onStarted,
}: {
  /** The message being asked again, or null when the dialog is shut. */
  target: ReaskTarget | null;
  agents: AgentSummary[] | null;
  /** The agent this conversation is running, named in the "same agent" row so
   *  the default is a choice a person can see rather than a blank. */
  currentAgentId: string | null;
  onClose: () => void;
  /** The new Run. The caller navigates; this dialog does not, because it does
   *  not know what route it is inside. */
  onStarted: (runId: string) => void;
}) {
  const [message, setMessage] = useState("");
  const [agentId, setAgentId] = useState<string>(SAME_AGENT);
  const [sending, setSending] = useState(false);

  // Reset when a different message is picked, adjusted during render rather
  // than in an effect, as everywhere else in this console: an effect would
  // paint the previous message's text for a frame before replacing it, and a
  // person would watch their own question flicker.
  // https://react.dev/learn/you-might-not-need-an-effect
  const targetRunId = target?.runId ?? null;
  const [prevTargetRunId, setPrevTargetRunId] = useState<string | null>(null);
  if (targetRunId !== prevTargetRunId) {
    setPrevTargetRunId(targetRunId);
    setMessage(target?.message ?? "");
    setAgentId(SAME_AGENT);
    setSending(false);
  }

  const current =
    agents?.find((agent) => agent.agent_id === currentAgentId) ?? null;
  const canSend = message.trim().length > 0 && !sending;
  const forking = target?.mode === "fork";

  async function submit() {
    if (target === null || !canSend) return;
    setSending(true);
    try {
      const { run_id } = forking
        ? await forkRun(target.runId, {
            message: message.trim(),
            ...(agentId === SAME_AGENT ? {} : { agent_id: agentId }),
          })
        : await branchRun(target.runId, { message: message.trim() });
      onStarted(run_id);
      onClose();
    } catch (err) {
      setSending(false);
      toast.error(
        forking
          ? "Could not fork this conversation"
          : "Could not branch this message",
        { description: describeApiError(err) },
      );
    }
  }

  return (
    <Dialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {forking ? (
              <GitForkIcon className="size-4" aria-hidden />
            ) : (
              <GitBranchIcon className="size-4" aria-hidden />
            )}
            {forking ? "Fork into a new chat" : "Ask this again"}
          </DialogTitle>
          <DialogDescription>
            {forking
              ? "Everything before this message comes with it into a chat of its own. This one stays exactly as it is, and deleting either leaves the other alone."
              : "Everything before this message is kept. The old answer stays in this chat as another version of it, and you can page between the two."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="reask-message">Message</Label>
            <Textarea
              id="reask-message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              rows={4}
              autoFocus
            />
          </div>

          {forking && (
            <div className="flex flex-col gap-2">
              <Label htmlFor="fork-agent">Agent</Label>
              <Select value={agentId} onValueChange={setAgentId}>
                <SelectTrigger id="fork-agent" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={SAME_AGENT}>
                    {current === null
                      ? "The same agent"
                      : `${current.name} (the same agent)`}
                  </SelectItem>
                  {(agents ?? [])
                    .filter((agent) => agent.agent_id !== currentAgentId)
                    .map((agent) => (
                      <SelectItem key={agent.agent_id} value={agent.agent_id}>
                        {agent.name}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
              <p className="text-caption text-muted-foreground">
                A fork is a conversation of its own, so it can run a different
                agent. The same question, two answers, two chats you can hold
                open side by side.
              </p>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={!canSend}>
            {sending ? "Asking" : forking ? "Fork" : "Ask"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
