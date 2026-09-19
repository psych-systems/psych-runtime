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
import { AddFromCatalogue } from "@/components/catalogue/catalogue-bits";
import { GetStartedCard } from "@/components/catalogue/get-started-card";
import { useCatalogue } from "@/hooks/use-catalogue";
import { PSYCH_CATALOGUE_ID } from "@/lib/types";
import { deleteAgent } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type { AgentSummary } from "@/lib/types";

export default function AgentsPage() {
  const { agents, loading, error, refresh } = useAgents();
  const catalogue = useCatalogue();
  const [pendingDelete, setPendingDelete] = useState<AgentSummary | null>(null);

  const catalogueAgents = catalogue.catalogue?.agents ?? [];
  // Which published agents came from the catalogue. The backend reports it on
  // the agent itself; the catalogue's own list is the fallback for a backend
  // that does not yet.
  const cataloguedById = new Map(
    catalogueAgents
      .filter((entry) => entry.agent_id !== null)
      .map((entry) => [entry.agent_id!, entry.catalogue_id]),
  );
  const psychId =
    catalogueAgents.find((entry) => entry.catalogue_id === PSYCH_CATALOGUE_ID)?.agent_id ?? null;
  const missingAgents = catalogueAgents.filter((entry) => entry.agent_id === null).length;
  const missingWorkflows = (catalogue.catalogue?.workflows ?? []).filter(
    (entry) => entry.workflow_id === null,
  ).length;

  // Psych first: it is the one that talks to the others, so a list that buries
  // it among its own specialists reads as a pile of agents with no way in.
  const ordered = [...(agents ?? [])].sort((a, b) => {
    const rank = (agent: AgentSummary) => (agent.agent_id === psychId ? 0 : 1);
    return rank(a) - rank(b);
  });

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
          description="What you can talk to."
          actions={
            <span className="flex flex-wrap items-center gap-2">
              <AddFromCatalogue
                kinds={["agents", "workflows"]}
                missing={missingAgents + missingWorkflows}
                onSeed={async (kinds) => {
                  await catalogue.seed(kinds);
                  // The grid reads the published list, not the catalogue, so
                  // seeding without this adds things nothing shows.
                  await refresh();
                }}
              />
              <Button asChild>
                <Link href="/agents/new">
                  <PlusIcon /> New agent
                </Link>
              </Button>
            </span>
          }
        />

        <GetStartedCard catalogue={catalogue.catalogue} />

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
            {ordered.map((agent) => (
              <AgentCard
                key={agent.agent_id}
                agent={agent}
                fromCatalogue={
                  (agent.catalogue_id ?? cataloguedById.get(agent.agent_id) ?? null) !== null
                }
                note={
                  agent.agent_id === psychId ? "Talks to every other agent for you." : null
                }
                onDelete={setPendingDelete}
              />
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
