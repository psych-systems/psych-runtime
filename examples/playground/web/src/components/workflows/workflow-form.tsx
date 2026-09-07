"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  BotIcon,
  CheckCircle2Icon,
  Loader2Icon,
  PlayIcon,
  Trash2Icon,
  WrenchIcon,
  WorkflowIcon,
} from "lucide-react";

import {
  ApiError,
  createWorkflow,
  dispatchRun,
  getWorkflow,
  listAgents,
  listTools,
  listWorkflows,
} from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import type {
  AgentSummary,
  CreateWorkflowResponse,
  ToolInfo,
  WorkflowStepIn,
  WorkflowSummary,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import { VersionHash } from "@/components/agents/version-hash";
import { FieldError } from "@/components/settings/validation";
import { toast } from "sonner";

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

/**
 * A workflow, built by hand.
 *
 * The other authoring surface (DESIGN.md §5): a fixed order of steps rather
 * than a model deciding what to do next. Each step is a tool with its
 * arguments written down, one of your agents, or another workflow. What runs
 * is exactly what is on this screen, in this order, and each step that
 * finishes is remembered, so a crash resumes at the step that was running
 * rather than from the top.
 *
 * Agents and nested workflows are copied in at their current version when
 * you publish. Editing one later does not change this workflow until it is
 * published again, which is what lets a crashed run resume against the
 * exact steps it started with.
 */
export function WorkflowForm() {
  const router = useRouter();
  const params = useSearchParams();
  const editing = params.get("edit");

  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [others, setOthers] = useState<WorkflowSummary[]>([]);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [steps, setSteps] = useState<WorkflowStepIn[]>([]);
  const [argumentText, setArgumentText] = useState<Record<number, string>>({});
  const [loadedFrom, setLoadedFrom] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [result, setResult] = useState<CreateWorkflowResponse | null>(null);

  useEffect(() => {
    void listTools().then(setTools).catch(() => undefined);
    void listAgents().then(setAgents).catch(() => undefined);
    void listWorkflows().then(setOthers).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (editing === null || loadedFrom === editing) return;
    const id = setTimeout(() => {
      void getWorkflow(editing)
        .then((source) => {
          setLoadedFrom(editing);
          setName(source.name);
          setDescription(source.description);
          const restored: WorkflowStepIn[] = source.steps.map((step) =>
            step.kind === "tool"
              ? { kind: "tool", name: step.name, tool: step.tool ?? "", arguments: step.arguments }
              : step.kind === "agent"
                ? { kind: "agent", name: step.name, agent_id: step.agent_id ?? "" }
                : { kind: "workflow", name: step.name, workflow_id: step.workflow_id ?? "" }
          );
          setSteps(restored);
          setArgumentText(
            Object.fromEntries(
              restored.map((step, i) => [
                i,
                step.kind === "tool" && Object.keys(step.arguments).length > 0
                  ? JSON.stringify(step.arguments, null, 2)
                  : "",
              ])
            )
          );
        })
        .catch(() => undefined);
    }, 0);
    return () => clearTimeout(id);
  }, [editing, loadedFrom]);

  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NAME_PATTERN.test(trimmedName);
  const stepNames = steps.map((s) => s.name.trim());
  const duplicateStep = stepNames.find((n, i) => n !== "" && stepNames.indexOf(n) !== i);
  const stepsComplete = steps.every(
    (step) =>
      step.name.trim() !== "" &&
      NAME_PATTERN.test(step.name.trim()) &&
      (step.kind === "tool"
        ? step.tool !== ""
        : step.kind === "agent"
          ? step.agent_id !== ""
          : step.workflow_id !== "")
  );
  const invalidArguments = useMemo(
    () =>
      Object.entries(argumentText).some(([, text]) => {
        if (text.trim() === "") return false;
        try {
          const parsed: unknown = JSON.parse(text);
          return parsed === null || typeof parsed !== "object" || Array.isArray(parsed);
        } catch {
          return true;
        }
      }),
    [argumentText]
  );
  const canPublish =
    !submitting &&
    trimmedName !== "" &&
    nameValid &&
    steps.length > 0 &&
    stepsComplete &&
    duplicateStep === undefined &&
    !invalidArguments;

  function updateStep(index: number, next: WorkflowStepIn) {
    setSteps(steps.map((step, i) => (i === index ? next : step)));
  }

  function move(index: number, by: -1 | 1) {
    const target = index + by;
    if (target < 0 || target >= steps.length) return;
    const next = [...steps];
    [next[index], next[target]] = [next[target], next[index]];
    setSteps(next);
    setArgumentText((current) => ({
      ...current,
      [index]: current[target] ?? "",
      [target]: current[index] ?? "",
    }));
  }

  function remove(index: number) {
    setSteps(steps.filter((_, i) => i !== index));
    setArgumentText((current) => {
      const next: Record<number, string> = {};
      Object.entries(current).forEach(([key, text]) => {
        const i = Number(key);
        if (i < index) next[i] = text;
        else if (i > index) next[i - 1] = text;
      });
      return next;
    });
  }

  async function handleSubmit() {
    setSubmitting(true);
    setFormError(null);
    try {
      setResult(
        await createWorkflow({
          workflow_id: editing,
          name: trimmedName,
          description: description.trim(),
          steps: steps.map((step) => ({
            ...step,
            name: step.name.trim(),
          })),
        })
      );
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : describeApiError(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    return <PublishedCard result={result} onKeepEditing={() => setResult(null)} />;
  }

  return (
    <div className="flex flex-col gap-6">
      {editing !== null && loadedFrom === editing && (
        <Alert>
          <WorkflowIcon />
          <AlertTitle>Editing {name}</AlertTitle>
          <AlertDescription>
            Publishing makes a new version and moves this workflow to it. A run already going keeps
            the version it started on.
          </AlertDescription>
        </Alert>
      )}
      {formError && (
        <Alert variant="destructive">
          <AlertTitle>Not published</AlertTitle>
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Identity</CardTitle>
          <CardDescription>What this pipeline is called and what it is for.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wf-name">Name</Label>
            <Input
              id="wf-name"
              value={name}
              spellCheck={false}
              placeholder="weekly-research-brief"
              aria-invalid={!nameValid || undefined}
              onChange={(e) => setName(e.target.value)}
            />
            <FieldError message={nameValid ? undefined : "Letters, digits, dots, dashes and underscores, starting with a letter."} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="wf-desc">What it is for</Label>
            <Input
              id="wf-desc"
              value={description}
              placeholder="Collects the latest inputs, checks them, and prepares a concise brief."
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Steps, in order</CardTitle>
          <CardDescription>
            A tool runs with exactly the arguments written here. An agent step runs one of your
            agents against the message the run is started with, and a workflow step runs another
            workflow inside this one. Every step sees what the steps before it produced. Each
            finished step is remembered, so a crash picks up where it left off.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {steps.length === 0 && (
            <p className="text-caption text-muted-foreground">
              No steps yet. A workflow needs at least one.
            </p>
          )}
          {steps.map((step, index) => (
            <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
              <div className="flex items-start gap-2">
                <span className="tabular mt-2 w-6 shrink-0 text-caption text-muted-foreground">
                  {index + 1}.
                </span>
                <div className="grid min-w-0 flex-1 gap-3 sm:grid-cols-[8rem_1fr_1fr]">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`step-kind-${index}`}>Kind</Label>
                    <Select
                      value={step.kind}
                      onValueChange={(kind) =>
                        updateStep(
                          index,
                          kind === "tool"
                            ? { kind: "tool", name: step.name, tool: "", arguments: {} }
                            : kind === "agent"
                              ? { kind: "agent", name: step.name, agent_id: "" }
                              : { kind: "workflow", name: step.name, workflow_id: "" }
                        )
                      }
                    >
                      <SelectTrigger id={`step-kind-${index}`}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="tool">Tool</SelectItem>
                        <SelectItem value="agent">Agent</SelectItem>
                        <SelectItem value="workflow">Workflow</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`step-name-${index}`}>Step name</Label>
                    <Input
                      id={`step-name-${index}`}
                      value={step.name}
                      spellCheck={false}
                      placeholder="look_up"
                      aria-invalid={duplicateStep === step.name.trim() || undefined}
                      onChange={(e) => updateStep(index, { ...step, name: e.target.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`step-target-${index}`}>
                      {step.kind === "tool" ? "Tool" : step.kind === "agent" ? "Agent" : "Workflow"}
                    </Label>
                    {step.kind === "tool" ? (
                      <Select
                        value={step.tool || undefined}
                        onValueChange={(tool) => updateStep(index, { ...step, tool })}
                      >
                        <SelectTrigger id={`step-target-${index}`}>
                          <SelectValue placeholder="Pick a tool" />
                        </SelectTrigger>
                        <SelectContent>
                          {tools.map((tool) => (
                            <SelectItem key={tool.name} value={tool.name}>
                              <span className="font-technical">{tool.name}</span>
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : step.kind === "agent" ? (
                      <Select
                        value={step.agent_id || undefined}
                        onValueChange={(agent_id) => updateStep(index, { ...step, agent_id })}
                      >
                        <SelectTrigger id={`step-target-${index}`}>
                          <SelectValue placeholder="Pick an agent" />
                        </SelectTrigger>
                        <SelectContent>
                          {agents.map((agent) => (
                            <SelectItem key={agent.agent_id} value={agent.agent_id}>
                              {agent.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <Select
                        value={step.workflow_id || undefined}
                        onValueChange={(workflow_id) => updateStep(index, { ...step, workflow_id })}
                      >
                        <SelectTrigger id={`step-target-${index}`}>
                          <SelectValue placeholder="Pick a workflow" />
                        </SelectTrigger>
                        <SelectContent>
                          {others
                            .filter((w) => w.workflow_id !== editing)
                            .map((w) => (
                              <SelectItem key={w.workflow_id} value={w.workflow_id}>
                                {w.name}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                    )}
                  </div>
                </div>
                <div className="mt-6 flex shrink-0 flex-col gap-0.5">
                  <Button type="button" size="icon-sm" variant="ghost" aria-label="Move up" disabled={index === 0} onClick={() => move(index, -1)}>
                    <ArrowUpIcon />
                  </Button>
                  <Button type="button" size="icon-sm" variant="ghost" aria-label="Move down" disabled={index === steps.length - 1} onClick={() => move(index, 1)}>
                    <ArrowDownIcon />
                  </Button>
                  <Button type="button" size="icon-sm" variant="ghost" aria-label="Remove step" onClick={() => remove(index)}>
                    <Trash2Icon />
                  </Button>
                </div>
              </div>
              {step.kind === "tool" && (
                <div className="flex flex-col gap-1.5 pl-8">
                  <Label htmlFor={`step-args-${index}`}>Arguments, as JSON</Label>
                  <Textarea
                    id={`step-args-${index}`}
                    value={argumentText[index] ?? ""}
                    spellCheck={false}
                    placeholder={argumentPlaceholder(tools.find((t) => t.name === step.tool))}
                    className="min-h-16 font-technical text-caption"
                    onChange={(e) => {
                      const text = e.target.value;
                      setArgumentText((current) => ({ ...current, [index]: text }));
                      try {
                        const parsed: unknown = text.trim() === "" ? {} : JSON.parse(text);
                        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                          updateStep(index, { ...step, arguments: parsed as Record<string, unknown> });
                        }
                      } catch {
                        // Half-typed. The last valid value stands until it parses again.
                      }
                    }}
                  />
                </div>
              )}
            </div>
          ))}
          <FieldError message={duplicateStep ? `Two steps are both called "${duplicateStep}". Step names are what a resume matches on, so they must differ.` : undefined} />
          <FieldError message={invalidArguments ? "One step's arguments are not valid JSON yet." : undefined} />
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setSteps([...steps, { kind: "tool", name: "", tool: "", arguments: {} }])}>
              <WrenchIcon /> Add a tool step
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={agents.length === 0} onClick={() => setSteps([...steps, { kind: "agent", name: "", agent_id: "" }])}>
              <BotIcon /> Add an agent step
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={others.filter((w) => w.workflow_id !== editing).length === 0} onClick={() => setSteps([...steps, { kind: "workflow", name: "", workflow_id: "" }])}>
              <WorkflowIcon /> Add a workflow step
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center justify-end gap-2 pb-10">
        <Button variant="ghost" onClick={() => router.push("/workflows")} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={() => void handleSubmit()} disabled={!canPublish}>
          {submitting && <Loader2Icon className="animate-spin" />}
          Publish workflow
        </Button>
      </div>
    </div>
  );
}

function argumentPlaceholder(tool: ToolInfo | undefined): string {
  if (!tool) return "{}";
  const properties = (tool.input_schema as { properties?: Record<string, unknown> }).properties ?? {};
  const example = Object.fromEntries(Object.keys(properties).map((key) => [key, "..."]));
  return JSON.stringify(example);
}

function PublishedCard({
  result,
  onKeepEditing,
}: {
  result: CreateWorkflowResponse;
  onKeepEditing: () => void;
}) {
  const router = useRouter();
  const [running, setRunning] = useState(false);

  async function run() {
    setRunning(true);
    try {
      const { run_id } = await dispatchRun({
        workflow_id: result.workflow_id,
        message: `run ${result.name}`,
      });
      router.push(`/activity/${run_id}`);
    } catch (err) {
      toast.error("Could not start it", { description: describeApiError(err) });
      setRunning(false);
    }
  }

  return (
    <Card className="mx-auto w-full max-w-xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2Icon className="size-5 text-status-done" aria-hidden />
          {result.created ? `${result.name} is ready` : `${result.name} is updated`}
        </CardTitle>
        <CardDescription>
          Every step was checked at publish. Run it now, or find it under Workflows later.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => void run()} disabled={running}>
            {running ? <Loader2Icon className="animate-spin" /> : <PlayIcon />} Run it
          </Button>
          <Button asChild variant="outline">
            <Link href={`/workflows/${encodeURIComponent(result.workflow_id)}`}>Open it</Link>
          </Button>
          <Button variant="outline" onClick={onKeepEditing}>
            Keep editing
          </Button>
          <Button variant="ghost" onClick={() => router.push("/workflows")}>
            All workflows
          </Button>
        </div>
        <TechnicalDetails>
          <DetailRow label="Workflow id">
            <span className="font-technical">{result.workflow_id}</span>
          </DetailRow>
          <DetailRow label="Version">
            <VersionHash hash={result.version_hash} />
          </DetailRow>
        </TechnicalDetails>
      </CardContent>
    </Card>
  );
}
