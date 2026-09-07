"use client";

import { AgentMark } from "@/components/chat/agent-mark";
import { toolLabel } from "@/components/chat/tool-call-card";
import type { AssistantTurnItem } from "@/lib/conversation";
import type { Lifecycle } from "@/components/ui/status";

/**
 * What is happening now, in the words of the thing that is happening.
 *
 * A spinner says the app is alive and nothing else. This says the agent is
 * running Search orders on turn three, which is the difference between waiting
 * and watching -- and it is the difference between a person giving it another
 * few seconds and a person reloading the page. Every word comes from a Record
 * that has already arrived: the tool by the name it was called with, the turn
 * by its number. Nothing here is guessed.
 *
 * It sits in the answer's own column so that when the answer arrives it takes
 * this line's place exactly, and nothing below it moves.
 */
export function WorkingIndicator({
  turn,
  lifecycle,
}: {
  /** The turn being written, when there is one. Null between turns and before
   *  the first one, which is a real state: the Run has been admitted and no
   *  Worker has claimed it yet. */
  turn: AssistantTurnItem | null;
  lifecycle: Lifecycle | null;
}) {
  const activity = describeActivity(turn, lifecycle);

  return (
    <div className="flex w-full gap-3" aria-live="polite">
      <AgentMark />
      <div className="flex min-h-6 min-w-0 flex-1 items-center gap-2">
        <Dots />
        <span className="min-w-0 truncate text-body text-muted-foreground">{activity.label}</span>
        {activity.detail !== null && (
          <span className="shrink-0 text-caption text-muted-foreground/70">{activity.detail}</span>
        )}
      </div>
    </div>
  );
}

/** Three dots rather than a spinner: a spinner reads as loading a page, this
 *  reads as someone writing. */
function Dots() {
  return (
    <span className="flex shrink-0 items-center gap-1" aria-hidden>
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60"
          style={{ animationDelay: `${index * 120}ms` }}
        />
      ))}
    </span>
  );
}

interface Activity {
  label: string;
  detail: string | null;
}

/**
 * The truthful sentence for the state the Run is actually in.
 *
 * Ordered by what a reader most needs to know. A tool that is running is the
 * answer to "what is taking so long", so it beats everything else; a stop that
 * has been asked for and not landed beats the work it is stopping, because a
 * person who pressed stop and still reads "Working" concludes stop is broken.
 */
function describeActivity(
  turn: AssistantTurnItem | null,
  lifecycle: Lifecycle | null
): Activity {
  const detail = turn !== null ? `Turn ${turn.turn}` : null;

  if (lifecycle === "stopping") return { label: "Stopping", detail: null };
  if (lifecycle === "queued") return { label: "Waiting for a worker", detail: null };

  const running = turn?.toolCalls.filter((call) => call.outcome === null) ?? [];
  if (running.length > 0) {
    const first = toolLabel(running[0].tool);
    return {
      label: running.length === 1 ? first : `${first} and ${running.length - 1} more`,
      detail,
    };
  }

  if (turn === null) return { label: "Getting started", detail: null };
  if (turn.willRetry) return { label: "Trying again", detail };
  if (turn.pending) return { label: "Thinking", detail };
  // The turn is closed, its tools have all answered, and the next turn has not
  // opened yet. The agent is reading what came back.
  if (turn.toolCalls.length > 0) return { label: "Reading the results", detail };
  return { label: "Working", detail };
}
