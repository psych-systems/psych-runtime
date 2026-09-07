"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  CheckCircle2Icon,
  CopyIcon,
  PencilIcon,
  Loader2Icon,
  MessageSquareIcon,
  WrenchIcon,
} from "lucide-react";

import { ApiError, createAgent, getConfig, listAgents, listModels, listTools } from "@/lib/api";
import { HttpToolsField } from "@/components/agents/http-tools-field";
import { SpawnEnvelopeFields, SubagentRosterField, DEFAULT_SPAWN } from "@/components/agents/subagents-field";
import {
  DEFAULT_MODEL_OPTIONS,
  ModelOptionsFields,
  modelOptionsAreDefault,
} from "@/components/agents/model-options-fields";
import {
  DEFAULT_SUSPENSION,
  SuspensionFields,
  suspensionIsDefault,
} from "@/components/agents/suspension-fields";
import { describeApiError } from "@/lib/errors";
import { useSettings } from "@/components/settings/use-settings";
import type {
  AgentSummary,
  ConfigResponse,
  CreateAgentRequest,
  CreateAgentResponse,
  HttpToolIn,
  McpServerIn,
  McpServerPreset,
  ModelOptionsIn,
  SpawnIn,
  SubagentRefIn,
  SuspensionIn,
  ToolInfo,
  ValidationProblem,
  SkillIn,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import { CheckList } from "@/components/agents/check-list";
import { ConnectionsField, type ConnectionChoice } from "@/components/agents/connections-field";
import { LimitsFields } from "@/components/agents/limits-fields";
import { CompactionFields } from "@/components/agents/compaction-fields";
import {
  DEFAULT_COMPACTION,
  validateCompaction,
  type CompactionFormState,
} from "@/components/agents/compaction";
import { SkillsField } from "@/components/agents/skills-field";
import { DEFAULT_LIMITS, validateLimits, type LimitsFormState } from "@/components/agents/limits";
import { ApprovalPicker } from "@/components/agents/approval-picker";
import { AnswerStylePicker } from "@/components/agents/answer-style-picker";
import { CHANGES_THE_AGENT, type AnswerStyleChoice } from "@/components/agents/answer-style";
import { selectorLabel, type ToolClass } from "@/components/agents/policy";
import { VersionHash } from "@/components/agents/version-hash";
import { ModelField } from "@/components/agents/model-field";
import { FieldError, IssuesSummary } from "@/components/settings/validation";

/** The same pattern the runtime checks a Spec name against, so a name this
 *  form accepts is one the publish accepts. */
const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

function choiceToMcpServer(choice: ConnectionChoice, preset: McpServerPreset): McpServerIn {
  return {
    name: preset.name,
    url: preset.url,
    // Carried through now. It used to be dropped, so an agent built from a
    // connection that speaks the older transport was published pointing at
    // plain HTTP and connected somewhere the tested connection never did.
    transport: preset.transport,
    credential: preset.credential ?? null,
    allow: choice.allow,
    optional: preset.optional,
    oauth: preset.oauth
      ? {
          grant: preset.oauth.grant,
          preregistered_client_id: preset.oauth.preregistered_client_id ?? null,
          client_secret_credential: preset.oauth.client_secret_credential ?? null,
        }
      : null,
  };
}

type FieldKey = "name" | "instructions" | "model" | "temperature" | "tools" | "connections";

/**
 * Which field owns a rejected publish's `path`.
 *
 * Two different shapes arrive here. The request body's own validation names
 * a field directly (`name`, `limits.max_turns`), while the publish check
 * names it under the agent's own name (`support-triage.tools.lookup_order`).
 * Stripping that prefix is what lets the second kind land on a field at all;
 * anything still unrecognised goes to the summary rather than being guessed
 * onto a field it might not belong to.
 */
function fieldKeyForPath(path: string, agentName: string): FieldKey | string | null {
  const stripped =
    agentName !== "" && path.startsWith(`${agentName}.`) ? path.slice(agentName.length + 1) : path;

  if (stripped === "name" || stripped === "instructions") return stripped;
  if (stripped === "model" || stripped.startsWith("model.fallbacks")) return "model";
  if (stripped === "model.model") return "model";
  if (stripped === "model.temperature") return "temperature";
  if (stripped.startsWith("limits.")) return stripped;
  // Passed through whole for the same reason the limits are: CompactionFields
  // looks each of its inputs up by that key, so a rejected number lands on the
  // box that holds it rather than in the summary at the top of the form.
  if (stripped.startsWith("compaction.")) return stripped;
  if (stripped === "tools" || stripped.startsWith("tools.")) return "tools";
  if (stripped.startsWith("http_tools")) return stripped;
  if (stripped.startsWith("subagents")) return stripped;
  if (stripped === "description") return "description";
  if (stripped.startsWith("model_options") || stripped.startsWith("model.")) return "model";
  if (stripped.startsWith("mcp_servers") || stripped.startsWith("mcp.") || stripped === "mcp")
    return "connections";
  // `skills.<name>.<field>`, passed through whole: SkillsField looks each of
  // its inputs up by that key, so a dangling `[[skill:x]]` link lands on the
  // body that wrote it rather than in the summary at the top of the form.
  if (stripped === "skills" || stripped.startsWith("skills.")) return stripped;
  return null;
}

export function CreateAgentForm() {
  const router = useRouter();
  // Two prefill modes, and the difference is which agent ends up pointing at
  // what gets published.
  //
  // `?edit=<agent_id>` publishes a new version and moves that agent to it.
  // `?from=<agent_id>` publishes a new version and creates a second agent
  // pointing at it, leaving the original where it is.
  //
  // Both prefill identically, from the source agent's current version, and
  // both mint an immutable content-hashed version. Nothing about that changed;
  // what changed is that an agent is now a name that can be repointed, so
  // "edit" is finally a thing this form can honestly offer.
  const params = useSearchParams();
  const editing = params.get("edit");
  const duplicateOf = editing ?? params.get("from");
  const { settings, loading: settingsLoading } = useSettings();

  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [modelsDetail, setModelsDetail] = useState<string | null>(null);
  const [allAgents, setAllAgents] = useState<AgentSummary[]>([]);

  useEffect(() => {
    void listTools()
      .then(setTools)
      .catch((err) => setToolsError(describeApiError(err)));
    // For the delegation roster. Best-effort: a list that will not load
    // leaves the roster empty rather than blocking the publish.
    void listAgents()
      .then(setAllAgents)
      .catch(() => undefined);
    void getConfig()
      .then(setConfig)
      .catch(() => undefined);
    // Swallowed on failure rather than surfaced: the picker is a convenience,
    // and a provider that is down must not stop somebody publishing an agent
    // against a model they already know the name of.
    void listModels()
      .then((response) => {
        setModels(response.models);
        setModelsDetail(response.detail);
      })
      .catch(() => undefined);
  }, []);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [modelOptions, setModelOptions] = useState<ModelOptionsIn>(DEFAULT_MODEL_OPTIONS);
  const [httpTools, setHttpTools] = useState<HttpToolIn[]>([]);
  const [roster, setRoster] = useState<SubagentRefIn[]>([]);
  const [spawn, setSpawn] = useState<SpawnIn>(DEFAULT_SPAWN);
  const [suspension, setSuspension] = useState<SuspensionIn>(DEFAULT_SUSPENSION);
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [temperatureEnabled, setTemperatureEnabled] = useState(false);
  const [temperature, setTemperature] = useState(1);
  const [answerStyle, setAnswerStyle] = useState<AnswerStyleChoice>(null);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [skills, setSkills] = useState<SkillIn[]>([]);
  const [mayAskQuestions, setMayAskQuestions] = useState(false);
  const [tasksEnabled, setTasksEnabled] = useState(false);
  const [componentsEnabled, setComponentsEnabled] = useState(false);
  const [subagentsEnabled, setSubagentsEnabled] = useState(false);
  const [compactionEnabled, setCompactionEnabled] = useState(false);
  const [compaction, setCompaction] = useState<CompactionFormState>(DEFAULT_COMPACTION);
  const [connections, setConnections] = useState<ConnectionChoice[]>([]);
  const [peers, setPeers] = useState<string[]>([]);
  const [limits, setLimits] = useState<LimitsFormState>(DEFAULT_LIMITS);
  const [approvalMode, setApprovalMode] = useState<"backend" | "custom">("backend");
  const [approvalClasses, setApprovalClasses] = useState<ToolClass[]>(["write", "destructive"]);

  const [duplicatedFrom, setDuplicatedFrom] = useState<string | null>(null);
  useEffect(() => {
    if (duplicateOf === null || duplicatedFrom === duplicateOf) return;
    // Deferred a tick: this sets state in reaction to a URL param rather than
    // during the commit that read it.
    const id = setTimeout(() => {
      void listAgents()
        .then((all) => {
          const source = all.find((agent) => agent.agent_id === duplicateOf);
          if (!source) return;
          setDuplicatedFrom(duplicateOf);
          setName(source.name);
          setDescription(source.description);
          setInstructions(source.instructions);
          setModelOverride(source.model);
          setModelOptions(source.model_options ?? DEFAULT_MODEL_OPTIONS);
          setHttpTools(source.http_tools.map((tool) => ({ ...tool })));
          setRoster(
            source.subagents.map((ref) => ({
              name: ref.name,
              description: ref.description,
              agent_id: ref.agent_id,
            }))
          );
          setSpawn(source.spawn ?? DEFAULT_SPAWN);
          setSuspension(source.suspension ?? DEFAULT_SUSPENSION);
          // The numbers as published, not this form's defaults: an edit that
          // silently reset a budget would publish a different agent.
          if (Object.keys(source.limits).length > 0) {
            setLimits({ ...DEFAULT_LIMITS, ...(source.limits as Partial<LimitsFormState>) });
          }
          if (source.approval_selectors !== null) {
            setApprovalMode("custom");
            setApprovalClasses(
              source.approval_selectors
                .map((selector) => selector.replace(/^@/, ""))
                .filter((cls): cls is ToolClass =>
                  cls === "read-only" || cls === "write" || cls === "destructive"
                )
            );
          }
          setAnswerStyle(source.answer_style);
          // Bodies and all: the list carries them for exactly this reason, and
          // duplicating an agent that silently lost its skills' instructions
          // would be worse than not offering to duplicate it.
          setSkills(source.skills.map((skill) => ({ ...skill })));
          setSelectedTools([...source.tools]);
          setMayAskQuestions(source.may_ask_questions);
          setTasksEnabled(source.tasks_enabled);
          setComponentsEnabled(source.components_enabled);
          setSubagentsEnabled(source.subagents_enabled);
          // Restored whole, not as a flag: an edit that turned compaction back
          // on with this form's starting numbers would publish an agent that
          // summarises on different terms from the one somebody opened.
          setCompactionEnabled(source.compaction !== null);
          if (source.compaction !== null) {
            setCompaction({
              trigger_tokens: source.compaction.trigger_tokens,
              keep_recent_turns:
                source.compaction.keep_recent_turns ?? DEFAULT_COMPACTION.keep_recent_turns,
              model: source.compaction.model ?? "",
              max_summary_tokens:
                source.compaction.max_summary_tokens ?? DEFAULT_COMPACTION.max_summary_tokens,
              summary_instructions: source.compaction.summary_instructions ?? "",
            });
          }
          setConnections(source.mcp_servers.map((server) => ({ name: server, allow: [] })));
          setPeers([...source.a2a_peers]);
        })
        .catch(() => {
          // The form works unprefilled, and the notice below says what was
          // and was not recovered either way.
        });
    }, 0);
    return () => clearTimeout(id);
  }, [duplicateOf, duplicatedFrom]);

  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [issues, setIssues] = useState<ValidationProblem[]>([]);
  const [result, setResult] = useState<CreateAgentResponse | null>(null);

  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NAME_PATTERN.test(trimmedName);
  const model = (modelOverride ?? config?.model ?? "").trim();
  const presets = useMemo(() => settings?.mcp_servers ?? [], [settings]);
  const peerPresets = useMemo(() => settings?.a2a_peers ?? [], [settings]);
  const limitErrors = useMemo(() => validateLimits(limits), [limits]);
  // Only when it is on: numbers behind a closed section must not be able to
  // block a publish that never sends them.
  const compactionErrors = useMemo(
    () => (compactionEnabled ? validateCompaction(compaction) : {}),
    [compactionEnabled, compaction]
  );

  const fieldErrors: Record<string, string> = { ...limitErrors, ...compactionErrors };
  const consumedPaths = new Set<string>();
  for (const issue of issues) {
    const key = fieldKeyForPath(issue.path, trimmedName);
    if (key !== null) {
      consumedPaths.add(issue.path);
      if (!(key in fieldErrors)) fieldErrors[key] = issue.message;
    }
  }

  const canPublish =
    !submitting &&
    trimmedName !== "" &&
    nameValid &&
    instructions.trim() !== "" &&
    model !== "" &&
    Object.keys(limitErrors).length === 0 &&
    Object.keys(compactionErrors).length === 0;

  async function handleSubmit() {
    setSubmitting(true);
    setFormError(null);
    setIssues([]);
    setResult(null);
    try {
      const presetsByName = new Map(presets.map((preset) => [preset.name, preset]));
      const mcp: McpServerIn[] = [];
      for (const choice of connections) {
        const preset = presetsByName.get(choice.name);
        if (preset) mcp.push(choiceToMcpServer(choice, preset));
      }
      const body: CreateAgentRequest = {
        agent_id: editing,
        name: trimmedName,
        description: description.trim(),
        instructions,
        model,
        temperature: temperatureEnabled ? temperature : null,
        model_options: modelOptionsAreDefault(modelOptions) ? null : modelOptions,
        tools: selectedTools,
        http_tools: httpTools,
        skills,
        mcp,
        subagents: roster,
        // Only when it is on, and only when it differs: an envelope of
        // defaults and a null both publish the library's defaults, and
        // sending null keeps the hash of an agent edited from an older form.
        spawn:
          subagentsEnabled && JSON.stringify(spawn) !== JSON.stringify(DEFAULT_SPAWN)
            ? spawn
            : null,
        suspension: suspensionIsDefault(suspension) ? null : suspension,
        // By name: the saved preset supplies the address, credential name and
        // grants, and what lands in the Spec is a copy of it rather than a
        // reference, so editing the preset later cannot change this agent.
        a2a: peerPresets
          .filter((peer) => peers.includes(peer.name))
          .map((peer) => ({
            name: peer.name,
            url: peer.url,
            credential: peer.credential ?? null,
            scheme: peer.scheme,
            tenant: peer.tenant ?? null,
            allow: peer.allow,
            optional: peer.optional,
            extensions: peer.extensions,
          })),
        limits: { ...limits },
        approval_selectors:
          approvalMode === "custom" ? approvalClasses.map(selectorLabel) : null,
        may_ask_questions: mayAskQuestions,
        tasks_enabled: tasksEnabled,
        components_enabled: componentsEnabled,
        subagents_enabled: subagentsEnabled,
        // Null rather than an object of zeroes when it is off: absent is what
        // the runtime reads as "this agent does not compact", and the two are
        // different agents with different version hashes.
        compaction: compactionEnabled
          ? {
              trigger_tokens: compaction.trigger_tokens,
              keep_recent_turns: compaction.keep_recent_turns,
              model: compaction.model.trim() === "" ? null : compaction.model.trim(),
              max_summary_tokens: compaction.max_summary_tokens,
              summary_instructions:
                compaction.summary_instructions.trim() === ""
                  ? null
                  : compaction.summary_instructions.trim(),
            }
          : null,
        answer_style: answerStyle,
      };
      setResult(await createAgent(body));
    } catch (err) {
      if (err instanceof ApiError) {
        setFormError(err.message);
        setIssues(err.issues);
      } else {
        setFormError(describeApiError(err));
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    return <PublishedCard result={result} onKeepEditing={() => setResult(null)} router={router} />;
  }

  return (
    <div className="flex flex-col gap-6">
      {duplicatedFrom !== null && (
        <Alert>
          {editing !== null ? <PencilIcon /> : <CopyIcon />}
          <AlertTitle>
            {editing !== null
              ? "Editing an agent you already published"
              : "Copied from an agent you already published"}
          </AlertTitle>
          <AlertDescription>
            <p>
              Its name, instructions, model, answer style, tools, skills, connections and long
              conversation settings came across. Its limits, approval rules and per connection
              tool lists did not: none of those are reported back once an agent is published, so
              they start from the defaults below and each connection starts at everything it
              offers. Check them before publishing.
            </p>
            {editing !== null && (
              <p className="mt-1.5">
                Publishing writes a new version and points this agent at it. Conversations already
                running keep the version they opened on, so nothing changes underneath them.
              </p>
            )}
          </AlertDescription>
        </Alert>
      )}

      {formError && (
        <Alert variant="destructive">
          <AlertTitle>This agent was not published</AlertTitle>
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      )}
      <IssuesSummary issues={issues} consumedPaths={consumedPaths} />

      <Card>
        <CardHeader>
          <CardTitle>Identity</CardTitle>
          <CardDescription>What it is called, and what it is for.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-name">Name</Label>
            <Input
              id="agent-name"
              placeholder="support-triage"
              value={name}
              onChange={(event) => setName(event.target.value)}
              aria-invalid={fieldErrors.name !== undefined || !nameValid}
            />
            {!nameValid ? (
              <p className="text-caption text-status-failed">
                Letters, numbers, dots, dashes and underscores, starting with a letter.
              </p>
            ) : (
              <p className="text-caption text-muted-foreground">
                How you will recognise it in the list and in a conversation.
              </p>
            )}
            <FieldError message={fieldErrors.name} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-description">What it is for</Label>
            <Input
              id="agent-description"
              value={description}
              placeholder="Answers questions about orders and issues refunds."
              onChange={(event) => setDescription(event.target.value)}
            />
            <p className="text-micro text-muted-foreground">
              One line for a person browsing the list, and for another agent reading this
              one&apos;s card. Not part of the prompt.
            </p>
            <FieldError message={fieldErrors.description} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-instructions">Instructions</Label>
            <Textarea
              id="agent-instructions"
              className="min-h-44"
              placeholder="You help the support team triage incoming tickets. Look up the customer's order before answering, and never promise a refund without checking the policy first."
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              aria-invalid={fieldErrors.instructions !== undefined}
            />
            <p className="text-caption text-muted-foreground">
              Written to the agent, in the second person. Say what it does, what it should check
              first, and what it must never do.
            </p>
            <FieldError message={fieldErrors.instructions} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Model</CardTitle>
          <CardDescription>Which model writes its replies.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-model">Model</Label>
            {/* Was a `<datalist>`, which met the requirement on paper and
                failed it in the browser: no click-to-open, prefix-only
                filtering that hid `azure/gpt-5.6-luna` from somebody typing
                `gpt`, and a chevron that did nothing when pressed.
                `ModelField` keeps the property that actually mattered -- any
                id stays typeable, because `known_models()` returning nothing
                is ordinary for a proxy (DESIGN.md §19) -- with a control that
                opens when clicked. */}
            <ModelField
              value={modelOverride ?? config?.model ?? ""}
              onChange={setModelOverride}
              models={models}
              detail={
                modelsDetail ??
                (config?.provider_label
                  ? `Filled in from ${config.provider_label}, the provider currently in use.`
                  : "Filled in from the provider currently in use.")
              }
              placeholder={config?.model ?? "Loading the configured model"}
              invalid={fieldErrors.model !== undefined}
            />
            <FieldError message={fieldErrors.model} />
          </div>

          <div className="flex items-start justify-between gap-4 rounded-lg border border-border px-3 py-2.5">
            <div className="flex flex-col gap-0.5">
              {/* Named for what it does rather than "response style", which
                  is now the name of a different control two cards below and
                  had people setting a number when they wanted bullet points. */}
              <p className="text-body font-medium">Set how varied its wording is</p>
              <p className="text-caption text-muted-foreground">
                Leave off to use the model&apos;s own. Lower is more predictable, higher is more
                varied.
              </p>
            </div>
            <Switch checked={temperatureEnabled} onCheckedChange={setTemperatureEnabled} />
          </div>
          {temperatureEnabled && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="agent-temperature">Temperature</Label>
              <Input
                id="agent-temperature"
                type="number"
                className="tabular w-32"
                min={0}
                max={2}
                step={0.1}
                value={temperature}
                aria-invalid={fieldErrors.temperature !== undefined}
                onChange={(event) => {
                  const parsed = event.target.valueAsNumber;
                  if (Number.isFinite(parsed)) setTemperature(parsed);
                }}
              />
              <FieldError message={fieldErrors.temperature} />
            </div>
          )}
          <div className="flex flex-col gap-2 border-t border-border pt-4">
            <span className="text-body font-medium">More about the model</span>
            <ModelOptionsFields value={modelOptions} onChange={setModelOptions} models={models} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Answer style</CardTitle>
          <CardDescription>How it shapes the reply it finishes on.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <AnswerStylePicker value={answerStyle} onChange={setAnswerStyle} />
          <p className="text-caption text-muted-foreground">{CHANGES_THE_AGENT}</p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Tools</CardTitle>
          <CardDescription>
            Things this agent can do on its own, beyond writing a reply. Leave everything unpicked
            and it answers from the conversation alone.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {toolsError ? (
            <Alert variant="destructive">
              <AlertDescription>{toolsError}</AlertDescription>
            </Alert>
          ) : tools === null ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-9 w-full rounded-lg" />
              <Skeleton className="h-40 w-full rounded-lg" />
            </div>
          ) : tools.length === 0 ? (
            <p className="text-body text-muted-foreground">
              This installation has no built-in tools. Connections below are the other way to give
              an agent something to use.
            </p>
          ) : (
            <CheckList
              options={tools.map((tool) => ({
                value: tool.name,
                label: tool.name,
                hint: tool.description,
                mono: true,
              }))}
              selected={selectedTools}
              onChange={setSelectedTools}
              searchPlaceholder="Search tools"
              emptyText="No tool matches that search."
              countLabel={(total) => (total === 1 ? "1 tool" : `${total} tools`)}
            />
          )}
          <FieldError message={fieldErrors.tools} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>HTTP tools</CardTitle>
          <CardDescription>
            REST endpoints the agent may call directly, with a key it never sees. Nobody writes a
            function; the schema you give is what the model is shown.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HttpToolsField
            value={httpTools}
            onChange={setHttpTools}
            fieldErrors={fieldErrors}
            secrets={settings?.secrets ?? []}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Asking you questions</CardTitle>
          <CardDescription>
            Whether this agent may stop and ask you something when it cannot carry on without an
            answer. The conversation waits until you reply, so leave it off for anything that runs
            unattended.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <label className="flex items-start gap-3 rounded-lg border border-border px-3 py-2.5">
            <input
              type="checkbox"
              className="mt-1"
              checked={mayAskQuestions}
              onChange={(event) => setMayAskQuestions(event.target.checked)}
            />
            <span className="flex flex-col gap-0.5">
              <span className="text-body">Let it ask</span>
              <span className="text-caption text-muted-foreground">
                It gets a tool for asking, and can offer you options to pick from. You can always
                answer in your own words instead.
              </span>
            </span>
          </label>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Keeping a plan</CardTitle>
          <CardDescription>
            Whether this agent writes down the steps it intends to take and ticks them off as it
            works. Useful when a job has several parts; noise when it has one.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <label className="flex items-start gap-3 rounded-lg border border-border px-3 py-2.5">
            <input
              type="checkbox"
              className="mt-1"
              checked={tasksEnabled}
              onChange={(event) => setTasksEnabled(event.target.checked)}
            />
            <span className="flex flex-col gap-0.5">
              <span className="text-body">Let it keep a plan</span>
              <span className="text-caption text-muted-foreground">
                The plan shows in the conversation as it changes, so a long job is readable while
                it runs rather than only afterwards.
              </span>
            </span>
          </label>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Showing things, not only saying them</CardTitle>
          <CardDescription>
            Whether this agent may answer with a product, a set of them to compare, an order, an
            itinerary, a chart or a single figure, alongside what it writes. Worth it when the
            answer has a shape; noise when it is a sentence.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <label className="flex items-start gap-3 rounded-lg border border-border px-3 py-2.5">
            <input
              type="checkbox"
              className="mt-1"
              checked={componentsEnabled}
              onChange={(event) => setComponentsEnabled(event.target.checked)}
            />
            <span className="flex flex-col gap-0.5">
              <span className="text-body">Let it show things</span>
              <span className="text-caption text-muted-foreground">
                It sends the data and this console draws it, so what appears follows the console
                rather than the agent. Links and images have to be http or https; anything else is
                refused before it reaches the screen.
              </span>
            </span>
          </label>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Subagents</CardTitle>
          <CardDescription>
            Whether this agent may write its own helpers and run them in the background while it
            keeps working. What you are granting is a ceiling, not a roster: a helper it writes can
            only ever be given the tools this agent already has and the model it already runs on,
            so turning this on cannot widen what the agent can reach.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <label className="flex items-start gap-3 rounded-lg border border-border px-3 py-2.5">
            <input
              type="checkbox"
              className="mt-1"
              checked={subagentsEnabled}
              onChange={(event) => setSubagentsEnabled(event.target.checked)}
            />
            <span className="flex flex-col gap-0.5">
              <span className="text-body">Let it start subagents</span>
              <span className="text-caption text-muted-foreground">
                Each one gets its own conversation and its own budget, and reports back when it is
                done. You can watch them, message one, or stop one from the conversation.
              </span>
            </span>
          </label>
          {subagentsEnabled && (
            <div className="mt-3">
              <SpawnEnvelopeFields
                value={spawn}
                onChange={setSpawn}
                tools={[...selectedTools, ...httpTools.map((tool) => tool.name).filter(Boolean)]}
                models={models}
              />
            </div>
          )}
          <div className="mt-4 flex flex-col gap-2 border-t border-border pt-4">
            <span className="text-body font-medium">Named helpers</span>
            <p className="text-caption text-muted-foreground">
              The other kind of helper: agents you already have, embedded into this one by name.
              This agent hands a task over and waits for the answer, and the whole tree is pinned
              by one version, so editing a helper later does not change this agent until you
              publish it again.
            </p>
            <SubagentRosterField
              value={roster}
              onChange={setRoster}
              agents={allAgents}
              selfId={editing}
              fieldErrors={fieldErrors}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How long it may wait</CardTitle>
          <CardDescription>
            A conversation that stops for a person, a webhook or a helper holds no worker and costs
            nothing while it waits. These say when waiting becomes giving up.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <SuspensionFields value={suspension} onChange={setSuspension} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Long conversations</CardTitle>
          <CardDescription>
            Whether this agent writes a summary of its older messages and carries
            that forward instead of the messages themselves, so a conversation can
            keep going once it gets long. Leave it off and the conversation grows
            until the model refuses to read any more of it.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-start justify-between gap-4 rounded-lg border border-border px-3 py-2.5">
            <div className="flex flex-col gap-0.5">
              <p className="text-body font-medium">Summarise older messages</p>
              <p className="text-caption text-muted-foreground">
                Nothing is deleted. The replaced messages stay in the record and in
                the report, and you can still read every one of them here; what
                changes is only what the model is shown on the next reply.
              </p>
            </div>
            <Switch checked={compactionEnabled} onCheckedChange={setCompactionEnabled} />
          </div>
          {compactionEnabled && (
            <CompactionFields
              value={compaction}
              onChange={setCompaction}
              fieldErrors={fieldErrors}
              models={models}
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Skills</CardTitle>
          <CardDescription>
            Procedures the agent loads only when it needs one. Each skill&apos;s description sits in
            every prompt so the agent knows the skill exists; the instructions are fetched only
            when it decides to use them, so a long procedure costs nothing on the conversations
            that never touch it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <SkillsField
            value={skills}
            onChange={setSkills}
            fieldErrors={fieldErrors}
            library={settings?.skills ?? []}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Connections</CardTitle>
          <CardDescription>
            Outside systems this agent may reach. Each one you pick is copied into the agent as it
            is set up today, so a later change in Connections does not alter an agent you already
            published.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <ConnectionsField
            connections={presets}
            value={connections}
            onChange={setConnections}
            loading={settingsLoading && settings === null}
          />
          <FieldError message={fieldErrors.connections} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Other agents</CardTitle>
          <CardDescription>
            Agents elsewhere this one may hand work to, over A2A. Each is copied in as it is set
            up today, so a later change under Connections does not alter an agent you already
            published. What each peer can do is read from its own agent card as a conversation
            runs, not fixed here.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {peerPresets.length === 0 ? (
            <p className="text-caption text-muted-foreground">
              None saved. Add one under{" "}
              <Link
                href="/connections"
                className="font-medium text-primary underline underline-offset-2"
              >
                Connections
              </Link>{" "}
              and it appears here.
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {peerPresets.map((peer) => {
                const picked = peers.includes(peer.name);
                return (
                  <li key={peer.name}>
                    <button
                      type="button"
                      aria-pressed={picked}
                      onClick={() =>
                        setPeers(
                          picked
                            ? peers.filter((name) => name !== peer.name)
                            : [...peers, peer.name]
                        )
                      }
                      className={
                        "flex w-full flex-col items-start gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors " +
                        (picked
                          ? "border-primary bg-primary/10"
                          : "border-border hover:bg-surface/60")
                      }
                    >
                      <span className="text-body font-medium">{peer.name}</span>
                      <span className="text-caption text-muted-foreground">
                        {peer.description || peer.url}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Limits</CardTitle>
          <CardDescription>
            What one answer may spend before it is stopped. Every field is filled in with a safe
            default, and every one of them is there to keep a stuck conversation from running up a
            bill.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <LimitsFields value={limits} onChange={setLimits} fieldErrors={fieldErrors} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Approvals</CardTitle>
          <CardDescription>
            Which kinds of action stop and wait for a person before they happen.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant={approvalMode === "backend" ? "default" : "outline"}
              onClick={() => setApprovalMode("backend")}
            >
              Use this installation&apos;s rule
            </Button>
            <Button
              type="button"
              size="sm"
              variant={approvalMode === "custom" ? "default" : "outline"}
              onClick={() => setApprovalMode("custom")}
            >
              Decide for this agent
            </Button>
          </div>
          {approvalMode === "custom" ? (
            <ApprovalPicker selected={approvalClasses} onChange={setApprovalClasses} />
          ) : (
            // Deliberately names no specific rule. Whoever runs this
            // installation sets the default, it is not reported back to this
            // screen, and the old copy stated one as fact and would have been
            // wrong the moment an operator changed it.
            <p className="text-body text-muted-foreground">
              Whoever set up this installation chose which actions pause for a person. This agent
              follows that choice, including if it changes later. Pick the other option to decide
              here instead, and the agent keeps your choice for good.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center justify-end gap-2 pb-10">
        <Button variant="ghost" onClick={() => router.push("/agents")} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={() => void handleSubmit()} disabled={!canPublish}>
          {submitting && <Loader2Icon className="animate-spin" />}
          Publish agent
        </Button>
      </div>
    </div>
  );
}

/**
 * What comes back from a publish.
 *
 * `created` says whether an agent was created or an existing one was moved to
 * a new version. Both are ordinary outcomes and both get the same ways
 * forward. This card used to read "Nothing changed" whenever a publish
 * matched an existing version, which was the only thing it could say when the
 * console had no notion of an agent apart from a version of one.
 */
function PublishedCard({
  result,
  onKeepEditing,
  router,
}: {
  result: CreateAgentResponse;
  onKeepEditing: () => void;
  router: ReturnType<typeof useRouter>;
}) {
  return (
    <Card className="mx-auto w-full max-w-xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2Icon className="size-5 text-status-done" aria-hidden />
          {result.created ? `${result.name} is ready` : `${result.name} is updated`}
        </CardTitle>
        <CardDescription>
          {result.created
            ? "You can start talking to it now."
            : "New conversations run this from now on. Ones already going keep the version they opened on."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Button asChild>
            <Link href={`/chat?agent=${encodeURIComponent(result.agent_id)}`}>
              <MessageSquareIcon /> Chat with it
            </Link>
          </Button>
          <Button asChild variant="outline">
            <Link href={`/agents/${encodeURIComponent(result.agent_id)}`}>Open it</Link>
          </Button>
          <Button variant="outline" onClick={onKeepEditing}>
            <WrenchIcon /> Keep editing
          </Button>
          <Button variant="ghost" onClick={() => router.push("/agents")}>
            All agents
          </Button>
        </div>
        <TechnicalDetails>
          <DetailRow label="Agent id">
            <span className="font-technical">{result.agent_id}</span>
          </DetailRow>
          <DetailRow label="Version">
            <VersionHash hash={result.version_hash} />
          </DetailRow>
        </TechnicalDetails>
      </CardContent>
    </Card>
  );
}
