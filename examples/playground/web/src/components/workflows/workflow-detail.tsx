"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  Loader2Icon,
  PencilIcon,
  PlayIcon,
  PlugIcon,
  WaypointsIcon,
} from "lucide-react";
import { toast } from "sonner";

import { ApiError, dispatchRun, getWorkflow, listWorkflowRuns } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel, truncate } from "@/lib/format";
import type { RunSummary, WorkflowSummary } from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import {
  DetailRow,
  EmptyState,
  Page,
  PageHeader,
  Section,
  TechnicalDetails,
} from "@/components/ui/page";
import { HashExplainer, VersionHash } from "@/components/agents/version-hash";
import { JsonView } from "@/components/trace/json-view";
import { relativeTime } from "@/components/chat/relative-time";
import { StepGraph } from "@/components/workflows/step-graph";
import { definitionNodes } from "@/components/workflows/definition-graph";
import { BreakpointPicker } from "@/components/workflows/breakpoint-picker";
import { flattenSteps } from "@/components/workflows/step-model";

/** The run states worth filtering a workflow's history by. `all` sends no
 *  filter at all rather than a word the backend would have to special-case. */
const STATE_FILTERS = [
  { value: "all", label: "Any state" },
  { value: "running", label: "Working" },
  { value: "waiting", label: "Waiting" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

export function WorkflowDetail({ workflowId }: { workflowId: string }) {
  const router = useRouter();
  const [workflow, setWorkflow] = useState<WorkflowSummary | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [stateFilter, setStateFilter] = useState("all");
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [message, setMessage] = useState("");
  const [inputText, setInputText] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [breakpoints, setBreakpoints] = useState<string[]>([]);
  const [stepMode, setStepMode] = useState(false);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const found = await getWorkflow(workflowId).catch((err) => {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      });
      if (found === null) setNotFound(true);
      else setWorkflow(found);
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

  // The run list is its own request, because the filter changes it and
  // nothing else on the page. Deferred a tick for the same reason the load
  // above is: this reacts to the filter changing rather than needing its
  // state update in the commit that changed it.
  useEffect(() => {
    const controller = new AbortController();
    const id = setTimeout(() => {
      void listWorkflowRuns(
        workflowId,
        stateFilter === "all" ? undefined : stateFilter,
        controller.signal
      )
        .then((found) => {
          setRuns(found);
          setRunsError(null);
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === "AbortError") return;
          setRuns([]);
          setRunsError(describeApiError(err));
        });
    }, 0);
    return () => {
      controller.abort();
      clearTimeout(id);
    };
  }, [workflowId, stateFilter]);

  const stepNames = useMemo(
    () => (workflow ? flattenSteps(workflow.steps).map((step) => step.name).filter(Boolean) : []),
    [workflow]
  );

  function parseInput(): Record<string, unknown> | null | false {
    if (inputText.trim() === "") return null;
    try {
      const parsed: unknown = JSON.parse(inputText);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        setInputError("The input is an object of fields.");
        return false;
      }
      setInputError(null);
      return parsed as Record<string, unknown>;
    } catch (err) {
      setInputError(err instanceof Error ? err.message : "Not valid JSON.");
      return false;
    }
  }

  async function run() {
    if (!workflow) return;
    const input = parseInput();
    if (input === false) return;
    setStarting(true);
    try {
      const { run_id } = await dispatchRun({
        workflow_id: workflow.workflow_id,
        message: message.trim() || `run ${workflow.name}`,
        input,
        breakpoints: breakpoints.length > 0 ? breakpoints : undefined,
        step_mode: stepMode || undefined,
      });
      router.push(`/activity/${run_id}/workflow`);
    } catch (err) {
      toast.error("Could not start it", { description: describeApiError(err) });
      setStarting(false);
    }
  }

  return (
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
          description="It was never published here or someone has since removed it. Runs it already had are still in Conversations."
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
            description="The message is what an agent step is asked. The input is what every step reads as input.<field>."
          >
            <div className="flex flex-col gap-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="wf-run-message">Message</Label>
                  <Input
                    id="wf-run-message"
                    value={message}
                    placeholder={`run ${workflow.name}`}
                    onChange={(event) => setMessage(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") void run();
                    }}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="wf-run-input">Input, as JSON</Label>
                  <Textarea
                    id="wf-run-input"
                    value={inputText}
                    spellCheck={false}
                    rows={3}
                    placeholder={inputPlaceholder(workflow)}
                    aria-invalid={inputError !== null || undefined}
                    className="font-technical text-caption"
                    onChange={(event) => {
                      setInputText(event.target.value);
                      setInputError(null);
                    }}
                  />
                  {inputError && <p className="text-caption text-status-failed">{inputError}</p>}
                </div>
              </div>

              <div className="flex flex-col gap-2">
                <Label>Stop before these steps</Label>
                <BreakpointPicker
                  stepNames={stepNames}
                  value={breakpoints}
                  onChange={setBreakpoints}
                />
                <label className="flex w-fit items-center gap-2 text-caption text-muted-foreground">
                  <input
                    type="checkbox"
                    className="size-3.5 accent-primary"
                    checked={stepMode}
                    onChange={(event) => setStepMode(event.target.checked)}
                  />
                  Stop before every step, not just these
                </label>
              </div>

              <div>
                <Button onClick={() => void run()} disabled={starting}>
                  {starting ? <Loader2Icon className="animate-spin" /> : <PlayIcon />} Run
                </Button>
              </div>
            </div>
          </Section>

          <Section
            title="What it does"
            description="Top to bottom. Branches that run together sit side by side; a body that runs more than once is boxed."
          >
            <StepGraph nodes={definitionNodes(workflow.steps)} />
          </Section>

          <Section
            title="Runs"
            actions={
              <Select value={stateFilter} onValueChange={setStateFilter}>
                <SelectTrigger size="sm" aria-label="Filter runs by state" className="w-40">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATE_FILTERS.map((one) => (
                    <SelectItem key={one.value} value={one.value}>
                      {one.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            }
          >
            {runsError !== null ? (
              <p className="text-body text-muted-foreground">{runsError}</p>
            ) : runs === null ? (
              <Skeleton className="h-16 w-full rounded-lg" />
            ) : runs.length === 0 ? (
              <p className="text-body text-muted-foreground">
                {stateFilter === "all"
                  ? "Nothing has run this yet."
                  : "No runs in that state."}
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {runs.map((one) => (
                  <li key={one.run_id}>
                    <Link
                      href={`/activity/${one.run_id}/workflow`}
                      className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2 transition-colors hover:bg-surface/60"
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <WaypointsIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="min-w-0 truncate text-body">
                          {truncate(one.message || `run ${workflow.name}`, 80)}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-2 text-caption text-muted-foreground">
                        {relativeTime(one.started_at)}
                        <StatusPill state={toLifecycle(one.state)} />
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
            <DetailRow label="Input schema">
              <JsonView value={workflow.input_schema} emptyLabel="anything" />
            </DetailRow>
            <DetailRow label="Starting state">
              <JsonView value={workflow.initial_state} emptyLabel="empty" />
            </DetailRow>
            <DetailRow label="Declared output">
              <JsonView value={workflow.output} emptyLabel="the last step's output" />
            </DetailRow>
            <DetailRow label="Versions">{workflow.version_count}</DetailRow>
            <DetailRow label="Published">{workflow.published_at}</DetailRow>
            <HashExplainer />
          </TechnicalDetails>
        </>
      )}
    </Page>
  );
}

/** An example body built from the declared input schema, so the box is not a
 *  guessing game about which fields this workflow expects. */
function inputPlaceholder(workflow: WorkflowSummary): string {
  const properties =
    (workflow.input_schema as { properties?: Record<string, unknown> } | null)?.properties ?? {};
  const keys = Object.keys(properties);
  if (keys.length === 0) return "{}";
  return JSON.stringify(Object.fromEntries(keys.map((key) => [key, "…"])), null, 2);
}
