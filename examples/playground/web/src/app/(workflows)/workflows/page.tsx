"use client";

import Link from "next/link";
import { useState } from "react";
import { AlertTriangleIcon, MoreHorizontalIcon, PlugIcon, PlusIcon, Trash2Icon, WorkflowIcon } from "lucide-react";
import { toast } from "sonner";

import { useWorkflows } from "@/components/workflows/use-workflows";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { deleteWorkflow } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel } from "@/lib/format";
import type { WorkflowSummary } from "@/lib/types";

export default function WorkflowsPage() {
  const { workflows, loading, error, refresh } = useWorkflows();
  const [pendingDelete, setPendingDelete] = useState<WorkflowSummary | null>(null);

  async function remove(workflow: WorkflowSummary) {
    try {
      await deleteWorkflow(workflow.workflow_id);
      toast.success(`${workflow.name} is no longer offered`);
      void refresh();
    } catch (err) {
      toast.error("Could not remove the workflow", { description: describeApiError(err) });
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Page>
        <PageHeader
          title="Workflows"
          description="Fixed pipelines: steps in an order you chose, run by the same worker as an agent, each finished step remembered so a crash resumes rather than restarts. An agent with the create_workflow tool can write one of these for you."
          actions={
            <Button asChild>
              <Link href="/workflows/new">
                <PlusIcon /> New workflow
              </Link>
            </Button>
          }
        />

        {loading && workflows === null && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Skeleton className="h-36 w-full rounded-xl" />
            <Skeleton className="h-36 w-full rounded-xl" />
          </div>
        )}

        {error && workflows === null && !loading && (
          <Alert variant="destructive">
            <AlertTriangleIcon />
            <AlertTitle>Couldn&apos;t load your workflows</AlertTitle>
            <AlertDescription>
              <p>{error}</p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void refresh()}>
                <PlugIcon /> Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {workflows && workflows.length === 0 && (
          <EmptyState
            icon={WorkflowIcon}
            title="No workflows yet"
            description="A workflow is the other way to author work: not a model deciding what to do next, but a fixed order of tool calls, agents and other workflows. Build one here, or give an agent the create_workflow tool and ask it to."
            action={
              <Button asChild size="sm">
                <Link href="/workflows/new">
                  <PlusIcon /> Build your first workflow
                </Link>
              </Button>
            }
          />
        )}

        {workflows && workflows.length > 0 && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {workflows.map((workflow) => (
              <article
                key={workflow.workflow_id}
                className="flex flex-col gap-3 rounded-xl bg-card p-4 ring-1 ring-foreground/10 transition-shadow hover:ring-foreground/20"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-col gap-1">
                    <Link
                      href={`/workflows/${encodeURIComponent(workflow.workflow_id)}`}
                      className="truncate text-base font-semibold hover:underline"
                    >
                      {workflow.name}
                    </Link>
                    <p className="text-caption text-muted-foreground">
                      Edited {formatDayLabel(workflow.updated_at)}. {workflow.steps.length} step
                      {workflow.steps.length === 1 ? "" : "s"}.
                    </p>
                  </div>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button variant="ghost" size="icon-sm" aria-label={`More for ${workflow.name}`}>
                        <MoreHorizontalIcon />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem asChild>
                        <Link href={`/workflows/new?edit=${encodeURIComponent(workflow.workflow_id)}`}>Edit</Link>
                      </DropdownMenuItem>
                      <DropdownMenuItem variant="destructive" onSelect={() => setPendingDelete(workflow)}>
                        <Trash2Icon /> Stop offering
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                {workflow.description && (
                  <p className="text-body text-muted-foreground">{workflow.description}</p>
                )}
                <ol className="flex flex-wrap items-center gap-1 text-caption">
                  {workflow.steps.map((step, index) => (
                    <li key={step.name} className="flex items-center gap-1">
                      {index > 0 && <span className="text-muted-foreground">→</span>}
                      <span className="rounded-full border border-border px-2 py-0.5 font-technical">
                        {step.kind === "tool" ? step.tool : step.name}
                      </span>
                    </li>
                  ))}
                </ol>
              </article>
            ))}
          </div>
        )}
      </Page>

      <Dialog open={pendingDelete !== null} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Stop offering {pendingDelete?.name}?</DialogTitle>
            <DialogDescription>
              It disappears from this list. Every run it already had stays in Activity and stays
              readable.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingDelete(null)}>
              Keep it
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (pendingDelete) void remove(pendingDelete);
                setPendingDelete(null);
              }}
            >
              Stop offering it
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
