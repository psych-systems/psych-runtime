"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  BotIcon,
  CheckCircle2Icon,
  ChevronDownIcon,
  Loader2Icon,
  PlayIcon,
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
  CreateWorkflowRequest,
  CreateWorkflowResponse,
  Mapping,
  RetryPolicy,
  Step,
  StepKind,
  ToolInfo,
  WorkflowSummary,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import { VersionHash } from "@/components/agents/version-hash";
import { FieldError } from "@/components/settings/validation";
import { JsonField, MappingEditor } from "@/components/workflows/value-editors";
import {
  CONTROL_STEP_CHOICES,
  RetryFields,
  StepCatalogProvider,
  StepEditor,
} from "@/components/workflows/step-editor";
import {
  duplicateNames,
  emptyStep,
  flattenSteps,
  normalizeRetry,
  normalizeStep,
} from "@/components/workflows/step-model";
import { StepGraph } from "@/components/workflows/step-graph";
import { definitionNodes } from "@/components/workflows/definition-graph";
import { toast } from "sonner";

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

/**
 * A workflow, built by hand.
 *
 * The other authoring surface (DESIGN.md §5): a fixed shape rather than a
 * model deciding what to do next. What runs is exactly what is on this
 * screen, and each step that finishes is remembered, so a crash resumes at
 * the step that was running rather than from the top.
 *
 * The shape is a tree now -- a step may run branches at once, pick an arm,
 * walk a list, loop, park for an event or for a person -- and the editor is
 * recursive to match. The part that mattered most in making it so is what did
 * *not* change: "Add a tool step" still produces one row with a name and a
 * tool, and everything optional is behind the same disclosure on every kind.
 * The nine control kinds are one menu away rather than nine more buttons.
 *
 * The JSON tab is the escape hatch and the contract. It shows the exact
 * request body, and editing it edits the definition, so anything the visual
 * editor cannot yet express is still reachable -- and a definition written in
 * JSON survives a round trip through the visual editor because both sides
 * hold the same value.
 *
 * Agents and nested workflows are copied in at their current version when you
 * publish. Editing one later does not change this workflow until it is
 * published again, which is what lets a crashed run resume against the exact
 * steps it started with.
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
  const [steps, setSteps] = useState<Step[]>([]);
  const [inputSchema, setInputSchema] = useState<Record<string, unknown> | null>(null);
  const [initialState, setInitialState] = useState<Record<string, unknown> | null>(null);
  const [output, setOutput] = useState<Mapping>({});
  const [retry, setRetry] = useState<RetryPolicy | null>(null);
  const [loadedFrom, setLoadedFrom] = useState<string | null>(null);

  const [tab, setTab] = useState("build");
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState<string | null>(null);

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
          // The definition comes back in exactly the shape the editor holds,
          // so nothing is translated here. That is deliberate: the old
          // three-way translation quietly dropped anything it did not know
          // about, and publishing afterwards wrote the loss back.
          setSteps(source.steps.map(normalizeStep));
          setInputSchema(source.input_schema ?? null);
          setInitialState(
            source.initial_state && Object.keys(source.initial_state).length > 0
              ? source.initial_state
              : null
          );
          setOutput(source.output ?? {});
          setRetry(source.retry ?? null);
        })
        .catch(() => undefined);
    }, 0);
    return () => clearTimeout(id);
  }, [editing, loadedFrom]);

  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NAME_PATTERN.test(trimmedName);

  const body: CreateWorkflowRequest = useMemo(
    () => ({
      workflow_id: editing,
      name: trimmedName,
      description: description.trim(),
      steps,
      input_schema: inputSchema,
      initial_state: initialState,
      output: Object.keys(output).length > 0 ? output : null,
      retry,
    }),
    [editing, trimmedName, description, steps, inputSchema, initialState, output, retry]
  );

  // Re-printed whenever the visual editor changes something, but only while
  // the JSON tab is not the one being typed in -- otherwise every keystroke
  // would reformat the text under the caret. Done while rendering rather than
  // in an effect: the JSON tab must never be opened onto a body one render
  // out of date.
  const [printed, setPrinted] = useState<CreateWorkflowRequest | null>(null);
  if (tab !== "json" && body !== printed) {
    setPrinted(body);
    setJsonText(JSON.stringify(body, null, 2));
    setJsonError(null);
  }

  const all = useMemo(() => flattenSteps(steps), [steps]);
  const duplicates = useMemo(() => duplicateNames(steps), [steps]);
  const unnamed = all.filter((step) => step.name.trim() === "").length;
  const badNames = all.filter(
    (step) => step.name.trim() !== "" && !NAME_PATTERN.test(step.name.trim())
  );
  const incomplete = all.filter(
    (step) =>
      (step.kind === "tool" && step.tool === "") ||
      (step.kind === "agent" && step.agent_id === "") ||
      (step.kind === "workflow" && step.workflow_id === "") ||
      (step.kind === "wait" && step.event.trim() === "")
  );

  const canPublish =
    !submitting &&
    trimmedName !== "" &&
    nameValid &&
    steps.length > 0 &&
    unnamed === 0 &&
    badNames.length === 0 &&
    duplicates.length === 0 &&
    incomplete.length === 0 &&
    jsonError === null;

  async function handleSubmit() {
    setSubmitting(true);
    setFormError(null);
    try {
      setResult(await createWorkflow(body));
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : describeApiError(err));
    } finally {
      setSubmitting(false);
    }
  }

  function applyJson(text: string) {
    setJsonText(text);
    if (text.trim() === "") {
      setJsonError("There is nothing here to publish.");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      setJsonError(err instanceof Error ? err.message : "Not valid JSON.");
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setJsonError("The request body is an object.");
      return;
    }
    const value = parsed as Partial<CreateWorkflowRequest>;
    if (!Array.isArray(value.steps)) {
      setJsonError("`steps` is missing, or is not a list.");
      return;
    }
    setJsonError(null);
    if (typeof value.name === "string") setName(value.name);
    if (typeof value.description === "string") setDescription(value.description);
    setSteps(value.steps.map(normalizeStep));
    setInputSchema(value.input_schema ?? null);
    setInitialState(value.initial_state ?? null);
    setOutput(value.output ?? {});
    setRetry(normalizeRetry(value.retry));
  }

  function addStep(kind: StepKind) {
    setSteps((current) => [...current, emptyStep(kind)]);
  }

  if (result) {
    return <PublishedCard result={result} onKeepEditing={() => setResult(null)} />;
  }

  return (
    <StepCatalogProvider
      catalog={{ tools, agents, workflows: others.filter((one) => one.workflow_id !== editing) }}
    >
      <div className="flex flex-col gap-6">
        {editing !== null && loadedFrom === editing && (
          <Alert>
            <WorkflowIcon />
            <AlertTitle>Editing {name}</AlertTitle>
            <AlertDescription>
              Publishing makes a new version and moves this workflow to it. A run already going
              keeps the version it started on.
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
                onChange={(event) => setName(event.target.value)}
              />
              <FieldError
                message={
                  nameValid
                    ? undefined
                    : "Letters, digits, dots, dashes and underscores, starting with a letter."
                }
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="wf-desc">What it is for</Label>
              <Input
                id="wf-desc"
                value={description}
                placeholder="Collects the latest inputs, checks them, and prepares a concise brief."
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
          </CardContent>
        </Card>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="build">Build</TabsTrigger>
            <TabsTrigger value="shape">Shape</TabsTrigger>
            <TabsTrigger value="json">JSON</TabsTrigger>
          </TabsList>

          <TabsContent value="build" className="flex flex-col gap-6">
            <Card>
              <CardHeader>
                <CardTitle>Steps</CardTitle>
                <CardDescription>
                  Top to bottom, each one seeing what the ones before it produced. A step may run
                  branches at once, pick an arm, walk a list, loop, wait for an event, or stop and
                  ask a person. Every finished step is remembered, so a crash picks up where it
                  left off.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {steps.length === 0 && (
                  <p className="text-caption text-muted-foreground">
                    No steps yet. A workflow needs at least one.
                  </p>
                )}
                {steps.map((step, index) => (
                  <StepEditor
                    key={index}
                    step={step}
                    path={`step-${index}`}
                    duplicateNames={duplicates}
                    onChange={(next) =>
                      setSteps(steps.map((one, i) => (i === index ? next : one)))
                    }
                    onRemove={() => setSteps(steps.filter((_, i) => i !== index))}
                    onMove={(by) => {
                      const target = index + by;
                      if (target < 0 || target >= steps.length) return;
                      const next = [...steps];
                      [next[index], next[target]] = [next[target], next[index]];
                      setSteps(next);
                    }}
                  />
                ))}

                <FieldError
                  message={
                    duplicates.length > 0
                      ? `${duplicates.map((one) => `"${one}"`).join(", ")} ${duplicates.length === 1 ? "is" : "are"} used more than once. A step name is what a resume, a breakpoint and a replay match on, so it has to be unique across the whole workflow.`
                      : undefined
                  }
                />
                <FieldError
                  message={
                    badNames.length > 0
                      ? "A step name is letters, digits, dots, dashes and underscores, starting with a letter."
                      : undefined
                  }
                />
                <FieldError
                  message={unnamed > 0 ? `${unnamed} step${unnamed === 1 ? " has" : "s have"} no name yet.` : undefined}
                />
                <FieldError
                  message={
                    incomplete.length > 0
                      ? `${incomplete.length} step${incomplete.length === 1 ? "" : "s"} still need${incomplete.length === 1 ? "s" : ""} a tool, an agent, a workflow or an event name.`
                      : undefined
                  }
                />

                <div className="flex flex-wrap gap-2">
                  <Button type="button" variant="outline" size="sm" onClick={() => addStep("tool")}>
                    <WrenchIcon /> Add a tool step
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={agents.length === 0}
                    onClick={() => addStep("agent")}
                  >
                    <BotIcon /> Add an agent step
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={others.filter((one) => one.workflow_id !== editing).length === 0}
                    onClick={() => addStep("workflow")}
                  >
                    <WorkflowIcon /> Add a workflow step
                  </Button>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button type="button" variant="outline" size="sm">
                        Add a control step <ChevronDownIcon />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className="max-w-72">
                      {CONTROL_STEP_CHOICES.map((choice) => (
                        <DropdownMenuItem key={choice.kind} onSelect={() => addStep(choice.kind)}>
                          <span className="flex flex-col gap-0.5">
                            <span className="font-medium">{choice.label}</span>
                            <span className="text-micro text-muted-foreground">{choice.blurb}</span>
                          </span>
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </CardContent>
            </Card>

            {steps.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>What it looks like</CardTitle>
                  <CardDescription>The same picture the workflow page shows.</CardDescription>
                </CardHeader>
                <CardContent>
                  <StepGraph nodes={definitionNodes(steps)} />
                </CardContent>
              </Card>
            )}
          </TabsContent>

          <TabsContent value="shape" className="flex flex-col gap-6">
            <Card>
              <CardHeader>
                <CardTitle>Input, state and output</CardTitle>
                <CardDescription>
                  What a run may be started with, what the shared state holds before the first step,
                  and what the workflow hands back when it is done.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <JsonField
                  id="wf-input-schema"
                  label="Input, as JSON Schema"
                  value={inputSchema}
                  placeholder='{"type":"object","properties":{"customer":{"type":"string"}},"required":["customer"]}'
                  description="Steps read these as input.<field>. Leave empty to accept anything."
                  rows={4}
                  onChange={(next) =>
                    setInputSchema(
                      next && typeof next === "object" && !Array.isArray(next)
                        ? (next as Record<string, unknown>)
                        : null
                    )
                  }
                />
                <JsonField
                  id="wf-initial-state"
                  label="Starting state"
                  value={initialState}
                  placeholder='{"seen": 0}'
                  description="Steps read and write these as state.<field>."
                  onChange={(next) =>
                    setInitialState(
                      next && typeof next === "object" && !Array.isArray(next)
                        ? (next as Record<string, unknown>)
                        : null
                    )
                  }
                />
                <div className="flex flex-col gap-1.5">
                  <Label>What the workflow returns</Label>
                  <MappingEditor
                    id="wf-output"
                    value={output}
                    addLabel="Add an output field"
                    emptyLabel="Nothing declared; the run returns the last step's output."
                    onChange={setOutput}
                  />
                </div>
                <RetryFields
                  path="wf-retry"
                  label="Retry every step inherits"
                  value={retry}
                  onChange={setRetry}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="json">
            <Card>
              <CardHeader>
                <CardTitle>The request body</CardTitle>
                <CardDescription>
                  Exactly what publishing sends. Edit it and the form above follows, so anything
                  the controls cannot yet express is still reachable here.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-2">
                <Textarea
                  aria-label="Workflow definition, as JSON"
                  value={jsonText}
                  spellCheck={false}
                  rows={24}
                  aria-invalid={jsonError !== null || undefined}
                  className="font-technical text-caption"
                  onChange={(event) => applyJson(event.target.value)}
                />
                <FieldError message={jsonError ?? undefined} />
                {jsonError === null && (
                  <p className="text-micro text-muted-foreground">
                    Parsed. {all.length} step{all.length === 1 ? "" : "s"} in all.
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>

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
    </StepCatalogProvider>
  );
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
