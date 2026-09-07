"use client";

import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { toolsUsed, workSummary } from "@/components/chat/exchange";
import { ToolCallCard, toolLabel } from "@/components/chat/tool-call-card";
import { cn } from "@/lib/utils";
import type { AssistantTurnItem } from "@/lib/conversation";

/** Enough tool names to say what kind of work this was without the line
 *  becoming a list. Past this the count in the summary is the honest answer. */
const NAMED_TOOLS = 3;

/**
 * How the agent got there, folded away.
 *
 * Collapsed, because the work is not the answer: a conversation should read as
 * a conversation and the mechanics should be one click away rather than
 * gone. Open, it is the whole thing at full fidelity -- every tool call, the
 * arguments it was sent, what came back -- because a person who opens this is
 * checking the agent's work and a summary of a summary would waste the click.
 *
 * The line itself counts rather than characterises, which is the library's own
 * choice: "Looked up the order" would be a guess about what the tools did.
 */
export function WorkSection({
  work,
  /** `AnswerView.summary`, once the Run has settled and the library has been
   *  asked. Identical to the line derived from the Records, so the swap moves
   *  nothing; it is here so that if the two ever differ, the library wins. */
  summary,
}: {
  work: readonly AssistantTurnItem[];
  summary: string | null;
}) {
  const [open, setOpen] = useState(false);

  // No work at all is the "hi" case: one turn, straight to an answer. There is
  // nothing to collapse, so there is no section, not an empty box.
  if (work.length === 0) return null;

  const line = summary !== null && summary.length > 0 ? summary : workSummary(work);
  const tools = toolsUsed(work);
  const named = tools.slice(0, NAMED_TOOLS).map(toolLabel).join(", ");

  return (
    <section className="w-full pl-9">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="group flex w-full max-w-full items-center gap-1.5 rounded-md py-1 text-left text-caption text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronRightIcon
          className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-90")}
          aria-hidden
        />
        <span className="shrink-0 font-medium">{line}</span>
        {named.length > 0 && (
          <>
            <span aria-hidden className="shrink-0 opacity-50">
              ·
            </span>
            <span className="min-w-0 truncate opacity-70">
              {named}
              {tools.length > NAMED_TOOLS && ` and ${tools.length - NAMED_TOOLS} more`}
            </span>
          </>
        )}
        <span className="ml-auto hidden shrink-0 pl-2 opacity-0 transition-opacity group-hover:opacity-70 sm:block">
          {open ? "Hide" : "Show work"}
        </span>
      </button>

      {open && (
        <div className="mt-2 mb-1 flex flex-col gap-4 border-l border-border pl-3.5">
          {work.map((turn) => (
            <WorkTurn key={turn.key} turn={turn} />
          ))}
        </div>
      )}
    </section>
  );
}

/** One step of working: what the model said on the way, and what it ran.
 *
 * The text is usually empty -- a turn that calls tools often says nothing --
 * and when it is there it is the model narrating its own plan, which is the
 * most useful line in the section. */
function WorkTurn({ turn }: { turn: AssistantTurnItem }) {
  return (
    <div className="flex flex-col gap-2">
      {turn.text.length > 0 && (
        <p className="text-body whitespace-pre-wrap text-muted-foreground">{turn.text}</p>
      )}

      {turn.toolCalls.map((call) => (
        <ToolCallCard key={call.callId} call={call} />
      ))}

      {turn.callFailure && (
        <p className="text-caption text-status-failed">
          {turn.callFailure.message}
          {turn.willRetry && <span className="text-muted-foreground"> Trying again.</span>}
        </p>
      )}
    </div>
  );
}
