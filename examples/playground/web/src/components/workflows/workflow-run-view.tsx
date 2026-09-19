"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  AlertCircleIcon,
  ArrowLeftIcon,
  MessagesSquareIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  WorkflowIcon,
} from "lucide-react";

import { useSession } from "@/components/auth/session-provider";
import { useRunStatus } from "@/hooks/use-run-status";
import { useRunStream } from "@/hooks/use-run-stream";
import { useWorkflowView } from "@/hooks/use-workflow-view";
import { formatClockTime, formatDuration } from "@/lib/format";
import type { StepView } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { EmptyState, Page, PageHeader, Section, Stat } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import { CopyButton } from "@/components/activity/copyable";
import { RunViewSwitcher } from "@/components/activity/run-view-switcher";
import { JsonView } from "@/components/trace/json-view";
import { StepGraph } from "@/components/workflows/step-graph";
import { flattenViews, runNodes } from "@/components/workflows/run-graph";
import { StepStatusPill, stepDuration } from "@/components/workflows/step-status";
import { stepLabel } from "@/components/workflows/step-model";
import { WaitingPanel } from "@/components/workflows/waiting-panel";
import { ReplayDialog } from "@/components/workflows/replay-dialog";

/**
 * A workflow run, as its own shape rather than as a log.
 *
 * The third view of a run, beside the activity summary and the trace, and the
 * only one that answers "where is it". A trace is a list of records in the
 * order they were written, which for a `foreach` over forty items is forty
 * indistinguishable rows; this is the definition with the run drawn onto it,
 * so a stuck branch, a retrying step and a skipped arm are each one glance.
 *
 * Live while the run is: the view is polled, and the record stream is attached
 * as well so anything that writes a record is picked up at once rather than up
 * to a poll late. Both stop when the run settles, because a finished tree does
 * not change.
 */
export function WorkflowRunView({ runId }: { runId: string }) {
  const { account } = useSession();
  const by = account?.display_name ?? "you";

  const { records } = useRunStream(runId);
  const { view, error, notAWorkflow, loading, refresh } = useWorkflowView(runId, records.length);
  const { status, refresh: refreshStatus } = useRunStatus(runId, records.length);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [replayFrom, setReplayFrom] = useState<string | null>(null);

  const all = useMemo(() => (view ? flattenViews(view.steps) : []), [view]);
  const selected = all.find((step) => step.step_id === selectedId) ?? null;
  const topLevelNames = view?.steps.map((step) => step.name).filter(Boolean) ?? [];
  const allNames = all.map((step) => step.name).filter(Boolean);

  const nodes = useMemo(
    () =>
      view
        ? runNodes(view.steps, {
            selectedId,
            onSelect: (step) =>
              setSelectedId((current) => (current === step.step_id ? null : step.step_id)),
            topLevelAction: (step) => (
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={`Replay from ${step.name}`}
                onClick={(event) => {
                  // The node itself is a button that opens the detail panel.
                  event.stopPropagation();
                  setReplayFrom(step.name);
                }}
              >
                <RotateCcwIcon />
              </Button>
            ),
          })
        : [],
    [view, selectedId]
  );

  function refreshAll() {
    refresh();
    refreshStatus();
  }

  if (loading && view === null && !notAWorkflow && error === null) {
    return (
      <Page>
        <Skeleton className="h-16 w-full rounded-md" />
        <Skeleton className="h-24 w-full rounded-xl" />
        <Skeleton className="h-72 w-full rounded-xl" />
      </Page>
    );
  }

  if (notAWorkflow) {
    return (
      <Page>
        <EmptyState
          icon={WorkflowIcon}
          title="This run is not a workflow"
          description="Only a run started from a workflow has a shape to draw. This one is a conversation with an agent."
          action={
            <Button asChild size="sm" variant="outline">
              <Link href={`/activity/${runId}`}>
                <ArrowLeftIcon /> Back to the run
              </Link>
            </Button>
          }
        />
      </Page>
    );
  }

  if (error !== null && view === null) {
    return (
      <Page>
        <PageHeader title="Workflow" description="This run could not be loaded." />
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>Couldn&apos;t load it</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refreshAll}>
            <RefreshCwIcon className="size-3.5" /> Try again
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link href={`/activity/${runId}`}>
              <ArrowLeftIcon className="size-3.5" /> Back to the run
            </Link>
          </Button>
        </div>
      </Page>
    );
  }

  if (view === null) return null;

  const lifecycle = status?.lifecycle ?? toLifecycle(view.terminal_state ?? "running");

  return (
    <Page>
      <PageHeader
        title={view.workflow}
        description={`${all.length} step${all.length === 1 ? "" : "s"} · ${view.completed} done${view.failed > 0 ? `, ${view.failed} failed` : ""}`}
        actions={
          <>
            <StatusPill state={lifecycle} />
            <RunViewSwitcher runId={runId} active="workflow" isWorkflow />
            <Button asChild variant="ghost" size="sm">
              <Link href={`/chat/${runId}`}>
                <MessagesSquareIcon className="size-3.5" />
                Open in chat
              </Link>
            </Button>
          </>
        }
      />

      {view.replays_run_id && (
        <Alert>
          <RotateCcwIcon />
          <AlertTitle>A replay</AlertTitle>
          <AlertDescription>
            Replayed from{" "}
            <Link
              href={`/activity/${view.replays_run_id}/workflow`}
              className="text-primary underline underline-offset-2"
            >
              {view.replays_run_id.slice(0, 16)}
            </Link>
            {view.replay_from_step ? ` at step ${view.replay_from_step}` : ""}.
          </AlertDescription>
        </Alert>
      )}

      {(view.breakpoints.length > 0 || view.step_mode) && (
        <p className="text-caption text-muted-foreground">
          {view.step_mode
            ? "Pauses before every step."
            : `Pauses before ${view.breakpoints.join(", ")}.`}
        </p>
      )}

      {view.waiting && (
        <WaitingPanel
          runId={runId}
          waiting={view.waiting}
          question={status?.pending_question ?? null}
          approval={status?.pending_approval ?? null}
          payloadSchema={status?.pending_wait?.payload_schema ?? null}
          by={by}
          onDone={refreshAll}
        />
      )}

      <div className="flex flex-wrap gap-8">
        <Stat label="Started" value={view.step_starts} />
        <Stat label="Done" value={view.completed} />
        <Stat label="Failed" value={view.failed} tone={view.failed > 0 ? "failed" : "muted"} />
      </div>

      <Section
        title="Where it is"
        description="Click a step for what went in and what came out. Replay from any top-level step."
      >
        <StepGraph nodes={nodes} emptyLabel="Nothing has started yet." />
      </Section>

      {selected && <StepPanel step={selected} onClose={() => setSelectedId(null)} />}

      <Section title="State" description="What the steps have written for each other.">
        <JsonView value={view.state} emptyLabel="empty" />
      </Section>

      {view.output !== null && (
        <Section title="Output" description="What the workflow handed back.">
          <JsonView value={view.output} emptyLabel="nothing" />
        </Section>
      )}

      <Section title="Input" description="What the run was started with.">
        <JsonView value={view.input} emptyLabel="nothing" />
      </Section>

      {replayFrom !== null && (
        <ReplayDialog
          runId={runId}
          fromStep={replayFrom}
          input={view.input}
          stepNames={allNames.length > 0 ? allNames : topLevelNames}
          open
          onOpenChange={(open) => {
            if (!open) setReplayFrom(null);
          }}
        />
      )}
    </Page>
  );
}

/** One step, in full: what it was given, what it produced, and why it
 *  failed if it did. Opened by clicking the step in the graph. */
function StepPanel({ step, onClose }: { step: StepView; onClose: () => void }) {
  const duration = stepDuration(step.started_at, step.completed_at);
  const json = JSON.stringify(step, null, 2);

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border bg-surface/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
          {stepLabel(step.kind)}
        </span>
        <span className="font-technical text-body font-medium">{step.name}</span>
        <StepStatusPill status={step.status} />
        <span className="ml-auto flex items-center gap-1">
          <CopyButton value={json} label="Copy JSON" />
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </span>
      </div>

      <dl className="grid gap-x-6 gap-y-1.5 text-caption sm:grid-cols-2">
        <Row label="Attempts">{step.attempts}</Row>
        <Row label="Took">{duration === null ? "not started" : formatDuration(duration)}</Row>
        <Row label="Started">
          {step.started_at ? formatClockTime(step.started_at) : "not yet"}
        </Row>
        <Row label="Finished">
          {step.completed_at ? formatClockTime(step.completed_at) : "not yet"}
        </Row>
        <Row label="Path">
          <span className="font-technical">{step.path.join(" › ") || "—"}</span>
        </Row>
        {step.iteration !== null && <Row label="Iteration">{step.iteration + 1}</Row>}
        {step.retry_at && <Row label="Next attempt">{formatClockTime(step.retry_at)}</Row>}
        {step.replayed_from && (
          <Row label="Carried over from">
            <span className="font-technical break-all">{step.replayed_from}</span>
          </Row>
        )}
        {step.child_run_id && (
          <Row label="Its own run">
            <Link
              href={`/activity/${step.child_run_id}`}
              className="text-primary underline underline-offset-2"
            >
              open it
            </Link>
          </Row>
        )}
        {step.cases.length > 0 && <Row label="Arms taken">{step.cases.join(", ")}</Row>}
      </dl>

      {step.failure && (
        <Alert variant="destructive">
          <AlertCircleIcon />
          <AlertTitle>{step.failure.kind}</AlertTitle>
          <AlertDescription>{step.failure.message}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
            In
          </span>
          <JsonView value={step.input} emptyLabel="nothing" />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
            Out
          </span>
          <JsonView value={step.output} emptyLabel="nothing" />
        </div>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}
