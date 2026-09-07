"use client";

import Link from "next/link";
import { useState } from "react";
import { AlertTriangleIcon, BotIcon, PlugIcon, PlusIcon } from "lucide-react";
import { toast } from "sonner";

import { useAgents } from "@/hooks/use-agents";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, Page, PageHeader } from "@/components/ui/page";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { AgentCard } from "@/components/agents/agent-card";
import { deleteAgent } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { AgentSummary } from "@/lib/types";

export default function AgentsPage() {
  const { agents, loading, error, refresh } = useAgents();
  const [pendingDelete, setPendingDelete] = useState<AgentSummary | null>(null);

  async function removeAgent(agent: AgentSummary) {
    try {
      await deleteAgent(agent.agent_id);
      toast.success(`${agent.name} is no longer offered`);
      void refresh();
    } catch (err) {
      toast.error("Could not remove the agent", { description: describeApiError(err) });
    }
  }

  return (
    <>
      <Page>
        <PageHeader
          title="Agents"
          description="What you can talk to. Each one has its own instructions and its own set of things it can use."
          actions={
            <Button asChild>
              <Link href="/agents/new">
                <PlusIcon /> New agent
              </Link>
            </Button>
          }
        />

        {loading && agents === null && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Skeleton className="h-52 w-full rounded-xl" />
            <Skeleton className="h-52 w-full rounded-xl" />
          </div>
        )}

        {error && agents === null && !loading && (
          <Alert variant="destructive">
            <AlertTriangleIcon />
            <AlertTitle>Couldn&apos;t load your agents</AlertTitle>
            <AlertDescription>
              <p>{error}</p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void refresh()}>
                <PlugIcon /> Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {agents && agents.length === 0 && (
          <EmptyState
            icon={BotIcon}
            title="No agents yet"
            description="An agent is a set of instructions plus the tools and connections it is allowed to use. Write one, publish it, and it becomes something you can hold a conversation with."
            action={
              <Button asChild size="sm">
                <Link href="/agents/new">
                  <PlusIcon /> Create your first agent
                </Link>
              </Button>
            }
          />
        )}

        {agents && agents.length > 0 && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {agents.map((agent) => (
              <AgentCard key={agent.agent_id} agent={agent} onDelete={setPendingDelete} />
            ))}
          </div>
        )}
      </Page>

      <Dialog open={pendingDelete !== null} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Stop offering {pendingDelete?.name}?</DialogTitle>
            <DialogDescription>
              {/* Says what actually happens, in the two facts a person cares
                  about. The old copy explained that the Version stayed in the
                  Store, which answers a question nobody using this asked. */}
              It disappears from this list, with every version it has had, and nobody can start a
              new conversation with it. Every conversation it already had stays where it is and
              stays readable.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingDelete(null)}>
              Keep it
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (pendingDelete) void removeAgent(pendingDelete);
                setPendingDelete(null);
              }}
            >
              Stop offering it
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
