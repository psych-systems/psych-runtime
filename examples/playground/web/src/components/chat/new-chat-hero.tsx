import Link from "next/link";
import { BotIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/page";
import type { AgentSummary } from "@/lib/types";
import { Trident } from "@/components/brand/trident";

/**
 * The first thing someone sees. Two cases only: there is something to talk to,
 * or there is not and the one action that fixes it is right there. An API path
 * is not an instruction a person can follow.
 */
export function NewChatHero({
  agents,
  loading,
  offline,
}: {
  agents: AgentSummary[] | null;
  loading: boolean;
  offline: boolean;
}) {
  if (!loading && !offline && (agents?.length ?? 0) === 0) {
    return (
      <div className="flex w-full flex-1 items-center py-10">
        <EmptyState
          className="w-full"
          icon={BotIcon}
          title="No agents yet"
          description="An agent is what you talk to here: a name, instructions and the tools it may use. Create one and it appears in the picker below."
          action={
            <Button asChild size="sm">
              <Link href="/agents/new">Create an agent</Link>
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2.5 py-16 text-center">
      <span
        aria-hidden
        className="flex size-11 items-center justify-center rounded-2xl bg-secondary font-heading text-xl font-semibold text-secondary-foreground"
      >
        <Trident className="size-7" />
      </span>
      <h2 className="font-heading text-3xl font-medium tracking-tight text-balance">
        What can I help with?
      </h2>
      <p className="max-w-sm text-body text-pretty text-muted-foreground">
        {loading
          ? "Loading your agents."
          : offline
            ? "Waiting for the backend to come back."
            : "Pick an agent and send it a message. It can ask before doing anything that changes something."}
      </p>
    </div>
  );
}
