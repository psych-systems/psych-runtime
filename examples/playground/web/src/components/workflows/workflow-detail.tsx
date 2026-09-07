"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  BotIcon,
  Loader2Icon,
  PencilIcon,
  PlayIcon,
  PlugIcon,
  WorkflowIcon,
  WrenchIcon,
} from "lucide-react";
import { toast } from "sonner";

import { ApiError, dispatchRun, getWorkflow, listRuns } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel, truncate } from "@/lib/format";
import type { RunSummary, WorkflowSummary } from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import { DetailRow, EmptyState, Page, PageHeader, Section, TechnicalDetails } from "@/components/ui/page";
import { HashExplainer, VersionHash } from "@/components/agents/version-hash";
import { JsonView } from "@/components/trace/json-view";
import { relativeTime } from "@/components/chat/relative-time";

export function WorkflowDetail({ workflowId }: { workflowId: string }) {
  const router = useRouter();
  const [workflow, setWorkflow] = useState<WorkflowSummary | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [found, allRuns] = await Promise.all([
        getWorkflow(workflowId).catch((err) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }),
        listRuns(),
      ]);
      if (found === null) setNotFound(true);
      else {
        setWorkflow(found);
        setRuns(allRuns.filter((run) => run.workflow_id === found.workflow_id));
      }
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load]);

  async function run() {
    if (!workflow) return;
    setStarting(true);
    try {
      const { run_id } = await dispatchRun({
        workflow_id: workflow.workflow_id,
        message: message.trim() || `run ${workflow.name}`,
      });
      router.push(`/activity/${run_id}`);
    } catch (err) {
      toast.error("Could not start it", { description: describeApiError(err) });
      setStarting(false);
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <Page>
        <div>
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link href="/workflows">
              <ArrowLeftIcon /> Workflows
            </Link>
          </Button>
        </div>

        {loading && workflow === null && !notFound && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-16 w-80" />
            <Skeleton className="h-40 w-full rounded-xl" />
          </div>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTriangleIcon />
            <AlertTitle>Couldn&apos;t load this workflow</AlertTitle>
            <AlertDescription>
              <p>{error}</p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void load()}>
                <PlugIcon /> Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {notFound && !loading && (
          <EmptyState
            icon={AlertTriangleIcon}
            title="This workflow is not being offered"
            description="It was never published here or someone has since removed it. Runs it already had are still in Activity."
            action={
              <Button asChild size="sm" variant="outline">
                <Link href="/workflows">Back to workflows</Link>
              </Button>
            }
          />
        )}

        {workflow && (
          <>
            <PageHeader
              title={workflow.name}
              description={
                workflow.description ||
                `Edited ${formatDayLabel(workflow.updated_at)}. ${workflow.steps.length} step${workflow.steps.length === 1 ? "" : "s"}.`
              }
              actions={
                <Button asChild variant="outline">
                  <Link href={`/workflows/new?edit=${encodeURIComponent(workflow.workflow_id)}`}>
                    <PencilIcon /> Edit
                  </Link>
                </Button>
              }
            />

            <Section
              title="Run it"
              description="The message is what an agent step is asked; a workflow of tool steps alone ignores it."
            >
              <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <Label htmlFor="wf-run-message">Message</Label>
                  <Input
                    id="wf-run-message"
                    value={message}
                    placeholder={`run ${workflow.name}`}
                    onChange={(e) => setMessage(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void run();
                    }}
                  />
                </div>
                <Button onClick={() => void run()} disabled={starting}>
                  {starting ? <Loader2Icon className="animate-spin" /> : <PlayIcon />} Run
                </Button>
              </div>
            </Section>

            <Section title="Steps" description="In this order, each remembered once it finishes.">
              <ol className="flex flex-col gap-2">
                {workflow.steps.map((step, index) => (
                  <li key={step.name} className="flex items-start gap-3 rounded-lg border border-border px-3 py-2">
                    <span className="tabular mt-0.5 text-caption text-muted-foreground">{index + 1}.</span>
                    <span className="mt-0.5 text-muted-foreground">
                      {step.kind === "tool" ? <WrenchIcon className="size-4" /> : step.kind === "agent" ? <BotIcon className="size-4" /> : <WorkflowIcon className="size-4" />}
                    </span>
                    <div className="flex min-w-0 flex-1 flex-col gap-1">
                      <div className="flex flex-wrap items-baseline gap-x-2">
                        <span className="font-technical text-body font-medium">{step.name}</span>
                        {step.kind === "tool" && (
                          <span className="font-technical text-caption text-muted-foreground">{step.tool}</span>
                        )}
                        {step.kind === "agent" && step.agent_id && (
                          <Link href={`/agents/${encodeURIComponent(step.agent_id)}`} className="text-caption text-primary underline underline-offset-2">
                            the agent it was copied from
                          </Link>
                        )}
                        {step.kind === "workflow" && step.workflow_id && (
                          <Link href={`/workflows/${encodeURIComponent(step.workflow_id)}`} className="text-caption text-primary underline underline-offset-2">
                            the workflow it was copied from
                          </Link>
                        )}
                      </div>
                      {step.kind === "tool" && <JsonView value={step.arguments} emptyLabel="no arguments" />}
                      {step.version_hash && (
                        <span className="font-technical text-micro text-muted-foreground">
                          pinned at {step.version_hash.slice(0, 12)}
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </Section>

            <Section title="Recent runs">
              {runs === null || runs.length === 0 ? (
                <p className="text-body text-muted-foreground">Nothing has run this yet.</p>
              ) : (
                <ul className="flex flex-col gap-2">
                  {runs.map((run) => (
                    <li key={run.run_id}>
                      <Link
                        href={`/activity/${run.run_id}`}
                        className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2 transition-colors hover:bg-surface/60"
                      >
                        <span className="min-w-0 truncate text-body">{truncate(run.message, 80)}</span>
                        <span className="flex shrink-0 items-center gap-2 text-caption text-muted-foreground">
                          {relativeTime(run.started_at)}
                          <StatusPill state={toLifecycle(run.state)} />
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </Section>

            <TechnicalDetails>
              <DetailRow label="Workflow id">
                <span className="font-technical">{workflow.workflow_id}</span>
              </DetailRow>
              <DetailRow label="Current version">
                <VersionHash hash={workflow.version_hash} />
              </DetailRow>
              <DetailRow label="Tools it holds">
                <span className="font-technical">{workflow.tools.join(", ") || "none"}</span>
              </DetailRow>
              <DetailRow label="Versions">{workflow.version_count}</DetailRow>
              <DetailRow label="Published">{workflow.published_at}</DetailRow>
              <HashExplainer />
            </TechnicalDetails>
          </>
        )}
      </Page>
    </div>
  );
}
