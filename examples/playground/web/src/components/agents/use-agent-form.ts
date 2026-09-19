"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { ApiError, createAgent, getConfig, listAgents, listModels, listTools } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { useSettings } from "@/components/settings/use-settings";
import { DEFAULT_SPAWN } from "@/components/agents/subagents-field";
import {
  DEFAULT_MODEL_OPTIONS,
  modelOptionsAreDefault,
} from "@/components/agents/model-options-fields";
import { DEFAULT_SUSPENSION, suspensionIsDefault } from "@/components/agents/suspension-fields";
import { type ConnectionChoice } from "@/components/agents/connections-field";
import {
  DEFAULT_CODE_EXECUTION,
  codeExecutionFromSummary,
  codeExecutionToRequest,
  profileIsolation,
  validateCodeExecution,
  type CodeExecutionFormState,
} from "@/components/agents/code-execution";
import {
  DEFAULT_COMPACTION,
  validateCompaction,
  type CompactionFormState,
} from "@/components/agents/compaction";
import { DEFAULT_LIMITS, validateLimits, type LimitsFormState } from "@/components/agents/limits";
import { selectorLabel, type ToolClass } from "@/components/agents/policy";
import type { AnswerStyleChoice } from "@/components/agents/answer-style";
import type {
  AgentSummary,
  ConfigResponse,
  CreateAgentRequest,
  CreateAgentResponse,
  HttpToolIn,
  McpServerIn,
  McpServerPreset,
  ModelOptionsIn,
  SkillIn,
  SpawnIn,
  SubagentRefIn,
  SuspensionIn,
  ToolInfo,
  ValidationProblem,
} from "@/lib/types";

/** The same pattern the runtime checks a Spec name against, so a name this
 *  form accepts is one the publish accepts. */
export const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

export interface Starter {
  name: string;
  label: string;
  description: string;
  instructions: string;
}

export const STARTERS: readonly Starter[] = [
  {
    name: "general-assistant",
    label: "General assistant",
    description: "Handles everyday questions and coordinates work across available systems.",
    instructions:
      "You are the workspace's general assistant. Understand the goal before acting, use available tools and agents when they improve the result, state assumptions plainly, and finish with a clear answer or next action.",
  },
  {
    name: "research-analyst",
    label: "Research analyst",
    description: "Investigates questions and turns evidence into practical recommendations.",
    instructions:
      "Investigate the question using the sources and tools available to you. Separate verified facts from assumptions, reconcile conflicting evidence, and finish with a concise recommendation supported by what you found.",
  },
  {
    name: "operations-coordinator",
    label: "Operations coordinator",
    description: "Plans multi-step work and keeps execution moving to completion.",
    instructions:
      "Turn the requested outcome into a short plan, complete each step with the available tools, surface blockers early, and report what changed, what was verified, and what still needs attention.",
  },
] as const;

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
          issuer: preset.oauth.issuer ?? null,
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
 * names it under the agent's own name (`research-assistant.tools.search`).
 * Stripping that prefix is what lets the second kind land on a field at all;
 * anything still unrecognised goes to the summary rather than being guessed
 * onto a field it might not belong to.
 */
export function fieldKeyForPath(path: string, agentName: string): FieldKey | string | null {
  const stripped =
    agentName !== "" && path.startsWith(`${agentName}.`) ? path.slice(agentName.length + 1) : path;

  if (stripped === "name" || stripped === "instructions") return stripped;
  if (stripped === "model" || stripped.startsWith("model.fallbacks")) return "model";
  if (stripped === "model.model") return "model";
  if (stripped === "model.temperature") return "temperature";
  if (stripped.startsWith("limits.")) return stripped;
  // Passed through whole for the same reason the limits are: the compaction
  // fields look each of their inputs up by that key, so a rejected number
  // lands on the box that holds it rather than in the summary.
  if (stripped.startsWith("compaction.")) return stripped;
  if (stripped === "tools" || stripped.startsWith("tools.")) return "tools";
  if (stripped.startsWith("http_tools")) return stripped;
  if (stripped.startsWith("subagents")) return stripped;
  if (stripped === "description") return "description";
  if (stripped.startsWith("model_options") || stripped.startsWith("model.")) return "model";
  if (stripped.startsWith("mcp_servers") || stripped.startsWith("mcp.") || stripped === "mcp")
    return "connections";
  // `skills.<name>.<field>`, passed through whole, so a dangling
  // `[[skill:x]]` link lands on the body that wrote it.
  if (stripped === "skills" || stripped.startsWith("skills.")) return stripped;
  return null;
}

/**
 * Everything the new-agent form knows, in one place.
 *
 * Extracted from the form so the layout around it is presentation only and
 * the request body is built once, in one readable function. The defaults
 * below are the new ones: a person who publishes without touching anything
 * gets an agent that can ask, plan, show components, start helpers,
 * summarise itself and pause before risky actions, with every registered
 * tool and every saved connection. Everything remains reachable and every
 * one of them is still part of the version hash.
 */
export function useAgentForm() {
  const router = useRouter();
  // Two prefill modes, and the difference is which agent ends up pointing at
  // what gets published. `?edit=` moves this agent to the new version;
  // `?from=` leaves the original where it is.
  const params = useSearchParams();
  const editing = params.get("edit");
  const duplicateOf = editing ?? params.get("from");
  const prefilling = duplicateOf !== null;
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
    // Best-effort: a roster that will not load leaves the picker empty
    // rather than blocking the publish.
    void listAgents()
      .then(setAllAgents)
      .catch(() => undefined);
    void getConfig()
      .then(setConfig)
      .catch(() => undefined);
    // Swallowed on failure: the picker is a convenience, and a provider that
    // is down must not stop somebody publishing against a model they already
    // know the name of.
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
  // On by default, all of them. An agent that cannot ask, cannot plan and
  // cannot show a table is the least useful agent this console can publish,
  // and every one of these is a thing a person would have had to read three
  // paragraphs to discover.
  const [mayAskQuestions, setMayAskQuestions] = useState(true);
  const [tasksEnabled, setTasksEnabled] = useState(true);
  const [componentsEnabled, setComponentsEnabled] = useState(true);
  const [subagentsEnabled, setSubagentsEnabled] = useState(true);
  const [compactionEnabled, setCompactionEnabled] = useState(true);
  const [compaction, setCompaction] = useState<CompactionFormState>(DEFAULT_COMPACTION);
  // Off until the account is known to offer a sandbox profile: an agent
  // asking for `run_code` where nothing can contain it is an agent whose
  // first program fails.
  const [codeEnabled, setCodeEnabled] = useState(false);
  const [codeExecution, setCodeExecution] =
    useState<CodeExecutionFormState>(DEFAULT_CODE_EXECUTION);
  const [connections, setConnections] = useState<ConnectionChoice[]>([]);
  const [peers, setPeers] = useState<string[]>([]);
  const [limits, setLimits] = useState<LimitsFormState>(DEFAULT_LIMITS);
  const [approvalMode, setApprovalMode] = useState<"backend" | "custom">("custom");
  const [approvalClasses, setApprovalClasses] = useState<ToolClass[]>(["write", "destructive"]);

  const presets = useMemo(() => settings?.mcp_servers ?? [], [settings]);
  const peerPresets = useMemo(() => settings?.a2a_peers ?? [], [settings]);
  const sandboxProfiles = useMemo(
    () => settings?.runtime.sandbox_profile_names ?? [],
    [settings],
  );

  // ---------------------------------------------------------------------
  // Seeding a new agent.
  //
  // Three of the defaults need a list that arrives over the network, so they
  // cannot be an initial `useState`. Each seed runs once, and none of them
  // runs at all while prefilling: an agent published with no tools, no
  // connections or no sandbox must come back exactly as it was published.
  // ---------------------------------------------------------------------
  const seededTools = useRef(false);
  useEffect(() => {
    if (prefilling || seededTools.current || tools === null) return;
    seededTools.current = true;
    if (tools.length === 0) return;
    // Deferred a tick, like the prefill below: this reacts to a list that
    // arrived over the network rather than synchronising with one.
    const id = setTimeout(() => setSelectedTools(tools.map((tool) => tool.name)), 0);
    return () => clearTimeout(id);
  }, [prefilling, tools]);

  const seededConnections = useRef(false);
  useEffect(() => {
    if (prefilling || seededConnections.current || settings === null) return;
    seededConnections.current = true;
    const id = setTimeout(() => {
      // `allow` copied from the preset, never emptied: an empty allow-list
      // means "everything it offers" on the wire, so a connection somebody
      // already narrowed must not widen when an agent picks it up.
      setConnections(presets.map((preset) => ({ name: preset.name, allow: [...preset.allow] })));
      setPeers(peerPresets.map((peer) => peer.name));
      setCodeEnabled(sandboxProfiles.length > 0);
      if (sandboxProfiles.length === 0) return;
      const profile = sandboxProfiles.includes(DEFAULT_CODE_EXECUTION.profile)
        ? DEFAULT_CODE_EXECUTION.profile
        : sandboxProfiles[0];
      // Isolation asked for is isolation the profile must provide: asking for
      // more does not run the program weaker, it refuses the program. So the
      // default is what this host actually reaches, and `process` when that
      // is not reported, rather than the strongest level in the enum.
      const isolation = profileIsolation(settings.runtime, profile) ?? "process";
      setCodeExecution((current) => ({ ...current, profile, isolation }));
    }, 0);
    return () => clearTimeout(id);
  }, [prefilling, settings, presets, peerPresets, sandboxProfiles]);

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
            })),
          );
          setSpawn(source.spawn ?? DEFAULT_SPAWN);
          setSuspension(source.suspension ?? DEFAULT_SUSPENSION);
          // The numbers as published, not this form's defaults: an edit that
          // silently reset a budget would publish a different agent.
          if (Object.keys(source.limits).length > 0) {
            setLimits({ ...DEFAULT_LIMITS, ...(source.limits as Partial<LimitsFormState>) });
          }
          // Both branches, always. The default is now "decide here", so an
          // agent that follows the installation's rule would otherwise be
          // republished with explicit selectors and a different hash.
          if (source.approval_selectors !== null) {
            setApprovalMode("custom");
            setApprovalClasses(
              source.approval_selectors
                .map((selector) => selector.replace(/^@/, ""))
                .filter(
                  (cls): cls is ToolClass =>
                    cls === "read-only" || cls === "write" || cls === "destructive",
                ),
            );
          } else {
            setApprovalMode("backend");
          }
          setAnswerStyle(source.answer_style);
          // Bodies and all: duplicating an agent that silently lost its
          // skills' instructions would be worse than not offering to.
          setSkills(source.skills.map((skill) => ({ ...skill })));
          setSelectedTools([...source.tools]);
          setMayAskQuestions(source.may_ask_questions);
          setTasksEnabled(source.tasks_enabled);
          setComponentsEnabled(source.components_enabled);
          setSubagentsEnabled(source.subagents_enabled);
          // Restored whole, not as a flag: an edit that turned compaction
          // back on with this form's starting numbers would publish an agent
          // that summarises on different terms from the one somebody opened.
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
          setCodeEnabled(source.code_execution !== null && source.code_execution.enabled);
          if (source.code_execution !== null) {
            setCodeExecution(codeExecutionFromSummary(source.code_execution));
          }
          setConnections(source.mcp_servers.map((server) => ({ name: server, allow: [] })));
          setPeers([...source.a2a_peers]);
        })
        .catch(() => {
          // The form works unprefilled, and the notice on the page says what
          // was and was not recovered either way.
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

  const limitErrors = useMemo(() => validateLimits(limits), [limits]);
  // Only when it is on: numbers behind a closed toggle must not be able to
  // block a publish that never sends them.
  const compactionErrors = useMemo(
    () => (compactionEnabled ? validateCompaction(compaction) : {}),
    [compactionEnabled, compaction],
  );
  const codeErrors = useMemo(
    () => (codeEnabled ? validateCodeExecution(codeExecution) : {}),
    [codeEnabled, codeExecution],
  );

  const fieldErrors: Record<string, string> = {
    ...limitErrors,
    ...compactionErrors,
    ...codeErrors,
  };
  const consumedPaths = new Set<string>();
  for (const issue of issues) {
    const key = fieldKeyForPath(issue.path, trimmedName);
    if (key !== null) {
      consumedPaths.add(issue.path);
      if (!(key in fieldErrors)) fieldErrors[key] = issue.message;
    }
  }

  /** What still stops a publish, in the words the footer shows. The
   *  blockers are counted alongside the rejected fields: a bar reading
   *  "nothing to fix" beside a disabled button is the worst of both. */
  const blockers: string[] = [];
  if (trimmedName === "") blockers.push("give it a name");
  else if (!nameValid) blockers.push("use only letters, numbers, dots, dashes and underscores in the name");
  if (instructions.trim() === "") blockers.push("write its instructions");
  if (model === "") blockers.push("pick a model");

  const issueCount = blockers.length + Object.keys(fieldErrors).length;
  const canPublish = !submitting && issueCount === 0;

  /** Whether anything wrong lives inside the advanced table, which is
   *  collapsed by default. A rejected number nobody can see is a form that
   *  refuses to publish and will not say why. */
  const advancedHasIssue = Object.keys(fieldErrors).some(
    (key) =>
      key.startsWith("limits.") ||
      key === "temperature" ||
      key === "model" ||
      key.startsWith("suspension"),
  );

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
        // Incomplete rows dropped rather than sent: the roster toggle adds a
        // blank row to fill in, and toggling it on and changing your mind must
        // publish the same agent as never touching it.
        subagents: roster.filter((ref) => ref.agent_id !== "" && ref.name.trim() !== ""),
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
        approval_selectors: approvalMode === "custom" ? approvalClasses.map(selectorLabel) : null,
        may_ask_questions: mayAskQuestions,
        tasks_enabled: tasksEnabled,
        components_enabled: componentsEnabled,
        subagents_enabled: subagentsEnabled,
        // Null when off, the same as compaction: absent is "this agent does
        // not run programs", and the two are different version hashes.
        code_execution: codeEnabled ? codeExecutionToRequest(codeExecution) : null,
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

  return {
    router,
    editing,
    duplicateOf,
    duplicatedFrom,
    settings,
    settingsLoading,
    presets,
    peerPresets,
    sandboxProfiles,
    sandboxReason: settings?.runtime.sandbox_unavailable_reason ?? null,
    tools,
    toolsError,
    config,
    models,
    modelsDetail,
    allAgents,

    name,
    setName,
    nameValid,
    description,
    setDescription,
    instructions,
    setInstructions,
    model,
    modelOverride,
    setModelOverride,
    modelOptions,
    setModelOptions,
    temperatureEnabled,
    setTemperatureEnabled,
    temperature,
    setTemperature,
    answerStyle,
    setAnswerStyle,
    selectedTools,
    setSelectedTools,
    httpTools,
    setHttpTools,
    skills,
    setSkills,
    connections,
    setConnections,
    peers,
    setPeers,
    roster,
    setRoster,
    spawn,
    setSpawn,
    suspension,
    setSuspension,
    limits,
    setLimits,
    approvalMode,
    setApprovalMode,
    approvalClasses,
    setApprovalClasses,
    mayAskQuestions,
    setMayAskQuestions,
    tasksEnabled,
    setTasksEnabled,
    componentsEnabled,
    setComponentsEnabled,
    subagentsEnabled,
    setSubagentsEnabled,
    compactionEnabled,
    setCompactionEnabled,
    compaction,
    setCompaction,
    codeEnabled,
    setCodeEnabled,
    codeExecution,
    setCodeExecution,

    submitting,
    formError,
    issues,
    consumedPaths,
    fieldErrors,
    blockers,
    issueCount,
    canPublish,
    advancedHasIssue,
    result,
    setResult,
    handleSubmit,
  };
}

export type AgentFormState = ReturnType<typeof useAgentForm>;
