/**
 * Types for the Psych playground backend's REST contract.
 *
 * These mirror `examples/playground/backend/app/schemas.py` and the record /
 * report shapes in `psych.core.records` and `psych.report.model` exactly --
 * field for field, including which fields are nullable and why (a `cost` of
 * `null` means "unknown", never `0`; see `TotalsReport`). Other code under
 * `src/app/(agents)/**`, `src/app/(tools)/**` and `src/app/(settings)/**`
 * imports from here, so a change here is a change to their contract too.
 */

// `export type { McpTransport } from ...` at the foot of this file re-exports
// the name for importers; it does not bring it into this module's own scope,
// so the fields below that are typed by it need a real import too.
import type { McpTransport } from "@/components/settings/types";

// ---------------------------------------------------------------------------
// Scope, usage, cost -- shared primitives
// ---------------------------------------------------------------------------

export interface Scope {
  tenant: string;
  principal: string | null;
  labels: Record<string, string>;
}

/** Token counters for one model call. `cache_read`/`cache_write` are disjoint
 * from `input`; `cache_write_1h` is a subset of `cache_write`, not an addend. */
export interface Usage {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  cache_write_1h: number;
  reasoning: number;
}

/**
 * What one model call cost. `amount` is a decimal string (Psych computes it
 * with `Decimal`, and `jsonable_encoder` renders `Decimal` as a string, so do
 * not `Number()` it for anything but display -- summing across many calls as
 * floats reintroduces the rounding error `Decimal` exists to avoid).
 */
export interface Cost {
  amount: string;
  currency: string;
  model: string;
  /** Where the figure came from: `provider` when the gateway reported it with
   *  the response, `computed` when Psych derived it from a price table,
   *  `mixed` only on a total that summed both. A provider's number comes from
   *  the party doing the billing; a computed one is as good as whatever table
   *  was in force. */
  source: "computed" | "provider" | "mixed";
  input_amount?: string | null;
  output_amount?: string | null;
  cache_read_amount?: string | null;
  cache_write_amount?: string | null;
}

export type ToolOutcome = "ok" | "error" | "aborted" | "unknown";
export type TerminalState =
  | "completed"
  | "failed"
  | "aborted"
  | "abandoned"
  | "force_settled";
export type SuspendReason = "approval" | "question" | "external" | "children";
export type QueueKind = "steer" | "follow_up" | "next_run";

export interface ToolFailure {
  kind: string;
  /** Already written for a reader: what happened, whether retrying helps,
   *  and what to do instead. */
  message: string;
  /** Kept for whoever operates the platform. Only shown to the model when
   *  `traceback_is_for_the_model` says so, which is true only for a program
   *  the model itself wrote. */
  traceback: string | null;
  transient: boolean;
  traceback_is_for_the_model?: boolean;
}

export interface ModelTimings {
  queue_wait_seconds: number;
  time_to_first_token_seconds: number | null;
  stream_duration_seconds: number;
}

// ---------------------------------------------------------------------------
// Records -- the log. Discriminated union on `type`, matching
// psych_runtime/core/records.py's `Record` adapter exactly.
// ---------------------------------------------------------------------------

interface RecordBase {
  run_id: string;
  seq: number;
  at: string;
  scope: Scope;
  attempt_id: string | null;
}

export interface RunAdmittedRecord extends RecordBase {
  type: "run_admitted";
  version_hash: string;
  input: Record<string, unknown>;
  idempotency_key: string | null;
  deadline_at: string;
  parent_run_id: string | null;
  delegation_depth: number;
  /** The plan as the agent last left it. Empty for an agent that was not
   *  given the task tool, which is the default. */
  tasks: Task[];
  continues_run_id: string | null;
}

export interface AttemptStartedRecord extends RecordBase {
  type: "attempt_started";
  worker_id: string;
  attempt_number: number;
  reclaimed_expired_lease: boolean;
}

export interface RunSettledRecord extends RecordBase {
  type: "run_settled";
  state: TerminalState;
  output: Record<string, unknown> | null;
  failure: ToolFailure | null;
  orphaned_attempt_id: string | null;
}

export interface TurnStartedRecord extends RecordBase {
  type: "turn_started";
  turn: number;
  step_id: string | null;
}

export interface ModelCallStartedRecord extends RecordBase {
  type: "model_call_started";
  turn: number;
  model: string;
  prompt_tokens_estimate: number | null;
  tool_names: string[];
  /** The prompt this turn was sent, as sent. Recorded rather than rebuilt, so
   *  a turn whose advisories differed from its neighbour's shows its own text.
   *  Empty on a run recorded before this was captured. */
  system_prompt: string;
}

export interface ModelCallFinishedRecord extends RecordBase {
  type: "model_call_finished";
  turn: number;
  model: string;
  usage: Usage;
  cost: Cost | null;
  timings: ModelTimings;
  finish_reason: string;
  text: string;
  tool_calls: string[];
}

export interface ModelCallFailedRecord extends RecordBase {
  type: "model_call_failed";
  turn: number;
  model: string;
  failure: ToolFailure;
  will_retry: boolean;
}

export interface ToolCallStartedRecord extends RecordBase {
  type: "tool_call_started";
  call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  turn: number;
  step_id: string | null;
  interruptible: boolean;
  safe_to_retry: boolean;
}

export interface ToolCallFinishedRecord extends RecordBase {
  type: "tool_call_finished";
  call_id: string;
  outcome: ToolOutcome;
  result: unknown;
  failure: ToolFailure | null;
  duration_seconds: number;
  result_handle: string | null;
  preview: string | null;
  result_bytes: number;
  result_blob_key: string | null;
  result_content_type: string | null;
  /** Named streams and files a `run_code` call produced beside its result,
   *  each readable by handle. Empty for every ordinary tool. */
  attachments?: ResultAttachment[];
}

/** One output a tool call kept beside its result: a program's stdout, its
 *  returned value, a file it wrote. Never a host path. */
export interface ResultAttachment {
  /** `stdout`, `stderr`, `value`, `execution`, or `file:<relative path>`. */
  name: string;
  handle: string;
  content_type: string;
  size_bytes: number;
  /** How much the program actually produced; larger than `size_bytes` when
   *  the backend's capture cap cut it short. */
  observed_bytes: number;
  truncated: boolean;
  /** Base64 when the bytes sit in the record; null when they live in the
   *  blob store or were not kept. */
  data: string | null;
  stored: "inline" | "blob" | "preview_only";
  sha256: string | null;
  readable: boolean;
}

export interface StepStartedRecord extends RecordBase {
  type: "step_started";
  step_id: string;
  name: string;
  kind: "agent" | "tool" | "workflow" | "subagent";
  attempt_number: number;
  input: Record<string, unknown>;
}

export interface StepCompletedRecord extends RecordBase {
  type: "step_completed";
  step_id: string;
  output: Record<string, unknown> | null;
  failure: ToolFailure | null;
  child_run_id: string | null;
}

export interface AbortRequestedRecord extends RecordBase {
  type: "abort_requested";
  reason: string;
  requested_by: string | null;
}

export interface QueueEnqueuedRecord extends RecordBase {
  type: "queue_enqueued";
  queue: QueueKind;
  entry_id: string;
  payload: Record<string, unknown>;
}

export interface QueueCancelledRecord extends RecordBase {
  type: "queue_cancelled";
  entry_id: string;
}

export interface QueueConsumedRecord extends RecordBase {
  type: "queue_consumed";
  entry_id: string;
}

export interface SuspendedRecord extends RecordBase {
  type: "suspended";
  reason: SuspendReason;
  payload_schema: Record<string, unknown>;
  expires_at: string;
  pending_call_id: string | null;
  question: string | null;
}

export interface ResumedRecord extends RecordBase {
  type: "resumed";
  payload: Record<string, unknown>;
  approved: boolean | null;
  resumed_by: string | null;
}

export interface CompactionAppliedRecord extends RecordBase {
  type: "compaction_applied";
  reason: "threshold" | "manual" | "overflow";
  replaced_from_seq: number;
  replaced_to_seq: number;
  summary: string;
}

// ---------------------------------------------------------------------------
// Components -- what an agent answers with when a sentence is the wrong shape.
// Mirrors psych_runtime/core/components.py exactly. Data and intent only: there is no
// colour, font, size or layout in any of these, because the library ships the
// payload and this console owns the drawing.
// ---------------------------------------------------------------------------

/** One label/value pair: the unit every record-shaped component is built from. */
export interface ComponentField {
  label: string;
  value: string;
}

/** One entity worth showing rather than describing. `image_url` and `href` are
 *  guaranteed http or https by the library, which refuses everything else. */
export interface CardComponent {
  kind: "card";
  title: string;
  subtitle: string;
  image_url: string;
  /** Short standing facts ("In stock"), not styling: the text is the payload
   *  and whether it draws as a pill is this console's call. */
  badges: string[];
  fields: ComponentField[];
  href: string;
}

export interface CarouselComponent {
  kind: "carousel";
  title: string;
  cards: CardComponent[];
}

/** A record read on its own rather than scanned among others: an order, a
 *  booking, a confirmation. Hence `status` and `total`, which a card has no
 *  place for. */
export interface DetailComponent {
  kind: "detail";
  title: string;
  subtitle: string;
  status: string;
  fields: ComponentField[];
  total: ComponentField | null;
  href: string;
}

export interface TimelineComponentStep {
  label: string;
  /** When, in the agent's own words. A string rather than a timestamp because
   *  "Tuesday morning" is a real answer on an itinerary. */
  at: string;
  description: string;
  state: "done" | "current" | "upcoming";
}

export interface TimelineComponent {
  kind: "timeline";
  title: string;
  steps: TimelineComponentStep[];
}

/** `x` may be a label or a number; `y` is always a number. */
export interface ChartPoint {
  x: string | number;
  y: number;
}

export interface ChartSeries {
  name: string;
  points: ChartPoint[];
}

/** Numbers with a shape. `mark` is the agent's reading of the data -- "this is
 *  a share of a whole" -- rather than a drawing instruction, so the renderer
 *  stays free to draw it the way this console draws things. */
export interface ChartComponent {
  kind: "chart";
  title: string;
  mark: "line" | "bar" | "area" | "pie";
  series: ChartSeries[];
  x_label: string;
  y_label: string;
}

export interface MetricComponent {
  kind: "metric";
  label: string;
  value: string | number;
  unit: string;
  delta: string;
  /** Which way it moved, never whether that is good news: up is good for
   *  revenue and bad for latency, and only this console knows which. */
  direction: "up" | "down" | "flat" | null;
}

export type PsychComponent =
  | CardComponent
  | CarouselComponent
  | DetailComponent
  | TimelineComponent
  | ChartComponent
  | MetricComponent;

export interface ComponentShownRecord extends RecordBase {
  type: "component_shown";
  component: PsychComponent;
}

export type PsychRecord =
  | RunAdmittedRecord
  | AttemptStartedRecord
  | RunSettledRecord
  | TurnStartedRecord
  | ModelCallStartedRecord
  | ModelCallFinishedRecord
  | ModelCallFailedRecord
  | ToolCallStartedRecord
  | ToolCallFinishedRecord
  | StepStartedRecord
  | StepCompletedRecord
  | AbortRequestedRecord
  | QueueEnqueuedRecord
  | QueueCancelledRecord
  | QueueConsumedRecord
  | SuspendedRecord
  | ResumedRecord
  | CompactionAppliedRecord
  | ComponentShownRecord;

// ---------------------------------------------------------------------------
// GET /api/config
// ---------------------------------------------------------------------------

export interface McpServerHint {
  name: string;
  url: string;
  transport: string;
  oauth_grant: string | null;
}

export interface Tracing {
  enabled: boolean;
  /** The OTLP endpoint spans go to, or null when nothing collects them. Not a
   *  secret: it is an address inside the operator's own deployment. */
  endpoint: string | null;
  /** What to look for in the trace backend. A UI grouped by `service.name` is
   *  unnavigable if you do not know which name is yours. */
  service_name: string;
}

export interface ConfigResponse {
  /** The active provider's model, not the one the process booted with. */
  model: string;
  store: string;
  mcp_servers: McpServerHint[];
  has_api_key: boolean;
  /** Which provider is active, by the name a person gave it. */
  provider_label: string | null;
  /** The tenant this backend dispatches the caller's runs as and connects
   *  their servers under: their own account id. Named by the backend so the
   *  console does not derive it and then disagree about which connection a run
   *  will actually use. */
  account_id: string;
  /** Where this deployment's A2A door is, matching what its cards advertise. */
  a2a_base_url: string;
  /** Whether spans are being collected, and where they went.
   *
   *  Reported because "off" and "on but you are looking in the wrong place"
   *  are different problems and look identical from a screen that says
   *  nothing. Psych opens spans on every run either way; this is only about
   *  whether anything downstream is listening. */
  tracing: Tracing;
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

/** The one response that answers before sign-in. Says nothing about anybody. */
export interface HealthResponse {
  ok: boolean;
  store: string;
  /** Nobody has signed up yet, so the first screen offers to create an account
   *  rather than a sign-in form for accounts that do not exist. */
  needs_first_account: boolean;
}

export interface AccountResponse {
  id: string;
  email: string;
  display_name: string;
}

// ---------------------------------------------------------------------------
// POST /api/agents, GET /api/agents
// ---------------------------------------------------------------------------

export interface McpOAuthIn {
  grant?: "authorization_code" | "client_credentials";
  preregistered_client_id?: string | null;
  client_secret_credential?: string | null;
  issuer?: string | null;
}

export interface McpServerIn {
  name: string;
  url: string;
  /** Carried into the published agent. Dropped before, so an agent built
   *  from a preset declaring the legacy transport connected over plain HTTP
   *  instead -- and transport is part of the connection's identity. */
  transport?: McpTransport;
  credential?: string | null;
  allow?: string[];
  optional?: boolean;
  oauth?: McpOAuthIn | null;
}

export type AnswerStyle = "concise";

/** `GET /api/runs/{id}/answer` -- one tool call inside the collapsed work. */
export interface AnswerToolCall {
  call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  outcome: ToolOutcome | null;
  result: unknown;
  failure_message: string | null;
  duration_seconds: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface AnswerWorkTurn {
  turn: number;
  text: string;
  tool_calls: AnswerToolCall[];
  failure_message: string | null;
  at: string;
}

/**
 * A run split into what it concluded and how it got there.
 *
 * The split is derived from the log, never decided by the model: the answer is
 * the turn the loop itself finished on and the work is everything before it.
 * `finished` is false when the run never reached an answer, and then `text` is
 * empty because there is no answer, not because the agent said nothing. Show
 * the reason from `/status` instead of an empty space.
 */
export interface AnswerView {
  run_id: string;
  text: string;
  work: AnswerWorkTurn[];
  finished: boolean;
  /** One line for the collapsed section, or empty when there is no work. */
  summary: string;
  tool_call_count: number;
}

/**
 * One skill: a procedure the agent loads only when it needs it (DESIGN.md §16).
 *
 * `description` is in the system prompt on every turn, so it is charged for
 * constantly and belongs on one line. `body` is charged for only when the
 * model calls `load_skill`, so it can be as long as the procedure is. Getting
 * that backwards costs a long prompt and buys nothing.
 */
export interface SkillIn {
  name: string;
  description: string;
  body: string;
}

/**
 * One model's rates, per million tokens.
 *
 * Strings, not numbers, all the way to the backend: these are decimals that
 * get multiplied by token counts to produce money, and JavaScript's number is
 * binary floating point. The backend parses them as `Decimal`.
 */
/** One durable fact an agent kept past the conversation that taught it
 *  (DESIGN.md §15). Not conversation history: that is the log. */
export interface MemoryOut {
  id: string;
  content: string;
  created_at: string;
}

export interface MemoriesResponse {
  memories: MemoryOut[];
  /** Whose facts these are. The account's own id in this console; a consumer
   *  serving real end users sees a different one per customer. */
  end_user_id: string;
}

export interface ModelPrice {
  model: string;
  input: string;
  output: string;
  cache_read: string;
  cache_write: string;
  currency: string;
}

/**
 * `GET /api/models`: what the active provider will accept.
 *
 * An empty `models` is not an error. A provider that will not say what it
 * serves returns nothing (DESIGN.md §19), which is ordinary for a proxy, so
 * `detail` says why in words and the model field stays typeable.
 */
export interface ModelsResponse {
  models: string[];
  detail: string;
  provider_label: string | null;
}

/** One A2A peer on `POST /api/agents`. Declares nothing about what the peer
 *  can do: its skills come from its agent card at run time. `allow` narrows
 *  them; empty grants all of them. */
export interface A2APeerIn {
  name: string;
  url: string;
  credential?: string | null;
  scheme?: string;
  tenant?: string | null;
  allow?: string[];
  optional?: boolean;
  extensions?: string[];
}

/**
 * When an agent replaces its older conversation with a summary.
 *
 * Mirrors `psych.CompactionPolicy` field for field. `trigger_tokens` has no
 * default anywhere in the library, on purpose: Psych has no context window to
 * take a fraction of, so a number invented for it would be a guess about
 * somebody else's model. The form picks a starting one and says so; this type
 * requires it.
 *
 * The same shape comes back on `AgentSummary`, so the edit form is filled from
 * exactly what was published rather than from four numbers it guessed again.
 */
export interface CompactionIn {
  /** Compact once the provider's own input count for the last model call
   *  reaches this many tokens. Measured, never estimated, so the trigger is
   *  read one call late: leave room for one more turn under the real window. */
  trigger_tokens: number;
  /** How many recent turns stay verbatim below the summary. */
  keep_recent_turns?: number;
  /** Which model writes the summary. Null means the agent's own. */
  model?: string | null;
  /** Cap on the summary's length. A summary as long as what it replaced saves
   *  nothing. */
  max_summary_tokens?: number;
  /** What this agent additionally needs kept. Added to Psych's own summary
   *  rules rather than replacing them, so it can raise the floor and never
   *  lower it. Null adds nothing. */
  summary_instructions?: string | null;
}

/** One `psych.HttpTool`: a REST endpoint the agent may call with no Python
 *  function behind it. `credential` names a secret; a literal Authorization
 *  header is refused by the library itself. */
export interface HttpToolIn {
  name: string;
  description: string;
  url: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  input_schema: Record<string, unknown>;
  headers: Record<string, string>;
  credential: string | null;
  timeout_seconds: number;
  interruptible: boolean;
}

/** One author-written subagent: an existing agent, embedded into the parent
 *  under `name` so one version hash pins the whole tree. */
export interface SubagentRefIn {
  name: string;
  description: string;
  agent_id: string;
}

export interface SubagentRefOut extends SubagentRefIn {
  /** The child's own hash, which is what the parent actually pins. */
  version_hash: string;
}

/** The `SpawnEnvelope`: a permission for children the model composes at run
 *  time. A ceiling, never a roster. */
export interface SpawnIn {
  tools: string[];
  models: string[];
  max_depth: number;
  max_alive: number;
  may_message: boolean;
}

/** How long each kind of wait may last before the run is abandoned. */
export interface SuspensionIn {
  approval_expires_seconds: number;
  question_expires_seconds: number;
  external_expires_seconds: number;
  children_expires_seconds: number;
}

export type ReasoningEffort = "low" | "medium" | "high";

/** The rest of `psych.ModelRef`: sampling and the fallback chain. */
export interface ModelOptionsIn {
  top_p: number | null;
  max_output_tokens: number | null;
  reasoning_effort: ReasoningEffort | null;
  /** Tried in order on a transient failure. Never a router. */
  fallbacks: string[];
}

export interface CreateAgentRequest {
  /** Which existing agent this publishes a new version of. Omitted creates
   *  one. Naming one edits it: a version is still immutable and still
   *  content-hashed, and the agent's pointer moves to the new one. */
  agent_id?: string | null;
  name: string;
  /** What it is for, for a person browsing and for a peer reading its card. */
  description?: string;
  instructions: string;
  model: string;
  temperature?: number | null;
  model_options?: ModelOptionsIn | null;
  tools?: string[];
  http_tools?: HttpToolIn[];
  skills?: SkillIn[];
  mcp?: McpServerIn[];
  /** The delegation roster: children an author named, called through
   *  `delegate` and blocking. Distinct from `spawn`. */
  subagents?: SubagentRefIn[];
  /** The envelope published when `subagents_enabled` is on. Null publishes
   *  the library's defaults. */
  spawn?: SpawnIn | null;
  suspension?: SuspensionIn | null;
  /** Other agents this one may call over A2A. Part of the published spec and
   *  so part of its version hash: which agents an agent may delegate to is
   *  part of what it is. */
  a2a?: A2APeerIn[];
  limits?: Record<string, unknown> | null;
  approval_selectors?: string[] | null;
  /** Whether the agent may stop and ask the person a question. Off by
   *  default; joins the version hash, because an agent that can park a
   *  conversation is a different agent from one that cannot. */
  may_ask_questions?: boolean;
  /** Whether the agent keeps a plan it can update as it works. Off by
   *  default; joins the version hash. */
  tasks_enabled?: boolean;
  /** Whether the agent may answer with a card, a chart or a timeline as well
   *  as with prose. Off by default; joins the version hash. */
  components_enabled?: boolean;
  /** Whether the agent may write its own subagents and run them in the
   *  background. Off by default; joins the version hash, because what it
   *  publishes is a permission: which of the agent's own tools a child it
   *  writes may be given, and on which model. Never a list of children. */
  subagents_enabled?: boolean;
  /** Whether the agent may write and run programs, and on what terms. Null
   *  means it may not, whatever sandbox the installation has wired. */
  code_execution?: CodeExecutionIn | null;
  /** Whether the agent summarises its older conversation once a prompt gets
   *  large, instead of letting it reach the context window. Null, the default,
   *  means it does not. Joins the version hash: an agent shown a summary in
   *  place of its own older turns is not being shown the same conversation. */
  compaction?: CompactionIn | null;
  /** Changes what the model is told, so it is part of the published agent and
   *  part of its version hash. Omitted adds nothing to the prompt. */
  answer_style?: AnswerStyle | null;
}

export interface CreateAgentResponse {
  agent_id: string;
  version_hash: string;
  name: string;
  /** True when the agent was created, false when an existing one was edited. */
  created: boolean;
}

/**
 * One agent, described by the version it currently runs.
 *
 * `agent_id` is the identity and never changes. `version_hash` is what it
 * happens to run today and changes on every edit. Route, link and dispatch by
 * the first; show the second where somebody wants to know exactly what ran.
 */
export interface AgentSummary {
  agent_id: string;
  version_hash: string;
  name: string;
  description: string;
  answer_style: AnswerStyle | null;
  instructions: string;
  model: string;
  model_options: ModelOptionsIn | null;
  tools: string[];
  http_tools: HttpToolIn[];
  subagents: SubagentRefOut[];
  spawn: SpawnIn | null;
  suspension: SuspensionIn | null;
  /** Every limit as published, so the edit form restores the numbers. */
  limits: Record<string, number>;
  /** What this agent's runs are admitted under, or null for the installation
   *  default. */
  approval_selectors: string[] | null;
  /** Carried in full, bodies included, because the duplicate form is filled
   *  from this list: skills returned without their bodies would silently
   *  publish empty ones. */
  skills: SkillIn[];
  mcp_servers: string[];
  /** Peer names only. A published spec carries its own copy of every peer's
   *  URL and credential name, and this catalogue reports neither. */
  a2a_peers: string[];
  /** Reported back so the edit form can restore them. Both join the version
   *  hash, so a form that could not read them would publish a different agent
   *  from the one somebody meant to edit. */
  may_ask_questions: boolean;
  tasks_enabled: boolean;
  components_enabled: boolean;
  subagents_enabled: boolean;
  /** The agent's compaction policy, or null when it has none. Carried whole
   *  rather than as a bare flag, for the reason skills carry their bodies: an
   *  edit form told only that it was on would invent the numbers again. */
  compaction: CompactionIn | null;
  /** The agent's code-execution terms, or null when it may not run programs. */
  code_execution: CodeExecutionIn | null;
  /** When the current version was first published. Not "last edited": a
   *  version republished after a round trip through an earlier configuration
   *  keeps its original timestamp. */
  published_at: string;
  created_at: string;
  /** When the pointer last moved. This is the "last edited" a person means. */
  updated_at: string;
  version_count: number;
}

/** One entry in an agent's history. */
export interface AgentVersionSummary {
  version_hash: string;
  name: string;
  model: string;
  published_at: string;
  current: boolean;
}

// ---------------------------------------------------------------------------
// POST /api/runs, GET /api/runs
// ---------------------------------------------------------------------------

/** What a caller may say about a run they are starting.
 *
 * There is no `tenant`, and its absence is the point: the backend derives it
 * from the session. This interface used to carry one, and the popover that
 * filled it in made the isolation boundary a text box.
 */
export interface DispatchRequest {
  /** The agent to talk to. Resolved to whatever version it points at right
   *  now, and that hash is what the run pins. The normal way in. */
  agent_id?: string | null;
  /** The other way in, for pinning a version deliberately: replaying an older
   *  configuration, or continuing a thread on the version it started on.
   *  Exactly one of the two. */
  version_hash?: string | null;
  /** A workflow to run instead of an agent. Exclusive with the other two. */
  workflow_id?: string | null;
  message: string;
  /** A prior run this message continues, for a second turn in the same
   * conversation, omitted for a thread's first message. */
  continues_run_id?: string | null;
}

/** A new branch of an existing conversation, asked from one of its messages.
 *
 *  `agent_id` is optional and is the interesting half: a branch is an ordinary
 *  dispatch, so it may run a different agent, which is how one question gets
 *  two answers side by side. Omitted, the branch stays on the agent and the
 *  version the run it came from was running. */
/** `POST /api/runs/{run_id}/branch`: re-ask one message inside this chat.
 *
 *  No `agent_id`, deliberately. A branch is another wording of a question in
 *  one conversation, so the agent answering it does not change; putting one
 *  question to two agents is a fork. */
export interface BranchRequest {
  message: string;
}

/** `POST /api/runs/{run_id}/fork`: take this chat somewhere else, as a new one. */
export interface ForkRequest {
  message: string;
  agent_id?: string;
}

export interface DispatchResponse {
  run_id: string;
}

/** One entry of `GET /api/runs`, newest first. */
export interface RunSummary {
  run_id: string;
  /** Set instead of `agent_id` for a workflow's run. */
  workflow_id: string;
  kind: "agent" | "workflow";
  /** Which agent this run belongs to, recorded when it was dispatched.
   *
   *  Not derivable from `version_hash`: a version is content, so an agent
   *  duplicated and published unchanged shares one, and filtering by hash puts
   *  one agent's conversations under the other. Empty for a run dispatched
   *  before agents had identities. */
  agent_id: string;
  /** Which conversation this run belongs to.
   *
   *  The history list shows one row per conversation, not one per branch:
   *  every branch of a chat shares this, and only a fork takes a new one.
   *  Empty for a run dispatched before conversations had ids, which
   *  `lib/branches.ts` reads as the root of that run's own chain. */
  conversation_id: string;
  /** Which branch of its conversation this run is on.
   *
   *  A conversation branched at a message has two futures from that point, and
   *  `continues_run_id` alone cannot say which one a run is in: the branch
   *  point has two children and a walk forward would pick either. Empty for a
   *  run dispatched before conversations could branch. See `lib/branches.ts`. */
  branch_id: string;
  name: string;
  tenant: string;
  /** The store's coarse state. Pass it through `toLifecycle` rather than
   *  reading it directly: it is the runtime's vocabulary, not a person's. */
  state: string;
  started_at: string;
  version_hash: string;
  /** The opening message, so a history list has something to show without
   *  fetching every conversation. */
  message: string;
  settled_at: string | null;
  /** The run this one continues, or null when it opened its conversation. A
   *  list that ignores this shows every turn as a separate entry. */
  continues_run_id: string | null;
}

// ---------------------------------------------------------------------------
// Resume / interrupt
// ---------------------------------------------------------------------------

export interface ResumeRequest {
  /** Omitted when answering a question: a question takes words, not a
   *  decision, and sending `approved` for one would record a verdict on a
   *  call nobody was asked to approve. */
  approved?: boolean;
  /** The person's answer, for a Run waiting on `ask_question`. `answers` keys
   *  by each question's header when several were asked at once. */
  payload?: { answer?: string; answers?: Record<string, string> } | null;
  /** Who decided. Recorded on the run's log: an approval of a destructive
   *  call whose log cannot say who approved it is not an audit trail. */
  by?: string | null;
}

/** Where a message sent mid-run is parked (DESIGN.md §9). */
export interface SendRequest {
  message: string;
  queue: QueueKind;
}

export interface SendResponse {
  entry_id: string;
  queue: QueueKind;
}

// ---------------------------------------------------------------------------
// Workflows
// ---------------------------------------------------------------------------

export interface ToolStepIn {
  kind: "tool";
  name: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface AgentStepIn {
  kind: "agent";
  name: string;
  agent_id: string;
}

export interface NestedWorkflowStepIn {
  kind: "workflow";
  name: string;
  workflow_id: string;
}

export type WorkflowStepIn = ToolStepIn | AgentStepIn | NestedWorkflowStepIn;

export interface CreateWorkflowRequest {
  workflow_id?: string | null;
  name: string;
  description?: string;
  steps: WorkflowStepIn[];
  limits?: Record<string, number> | null;
}

export interface WorkflowStepOut {
  kind: "tool" | "agent" | "workflow";
  name: string;
  tool: string | null;
  arguments: Record<string, unknown>;
  agent_id: string | null;
  workflow_id: string | null;
  /** For an embedded agent or workflow: the child hash this version pins. */
  version_hash: string | null;
}

export interface WorkflowSummary {
  workflow_id: string;
  version_hash: string;
  name: string;
  description: string;
  steps: WorkflowStepOut[];
  tools: string[];
  limits: Record<string, number>;
  published_at: string;
  created_at: string;
  updated_at: string;
  version_count: number;
}

export interface CreateWorkflowResponse {
  workflow_id: string;
  version_hash: string;
  name: string;
  created: boolean;
}

// ---------------------------------------------------------------------------
// Runtime settings and A2A tokens
// ---------------------------------------------------------------------------

export type CostPolicy = "prefer_provider" | "computed" | "provider_only";

export interface SandboxLimitsIn {
  cpu_seconds: number;
  address_space_bytes: number;
  file_size_bytes: number;
  process_count: number;
  wall_seconds: number;
}

/** The `Runtime` knobs Psych leaves to the consumer, per account. None of
 *  them is part of a spec, so changing one moves no version hash. */
export type IsolationLevel = "isolated" | "process";
export type EnforcementState = "enforced" | "unverified" | "unavailable";

/** One extra sandbox profile this account offers its agents by name: a
 *  container image, or a remote service. A remote profile names the secret
 *  holding its token, never the token. */
export interface SandboxProfileIn {
  name: string;
  backend: "container" | "remote";
  enabled: boolean;
  hard_limits: SandboxLimitsIn;
  allow_network: boolean;
  image?: string | null;
  runtime?: "docker" | "podman" | null;
  base_url?: string | null;
  credential?: string | null;
}

/** A local backend this host could run, from detection alone. */
export interface SandboxBackend {
  name: string;
  isolation: IsolationLevel;
  available: boolean;
  reason: string;
}

export interface SandboxGuarantees {
  filesystem: EnforcementState;
  network: EnforcementState;
  process_tree: EnforcementState;
  identity: EnforcementState;
  cpu: EnforcementState;
  memory: EnforcementState;
  file_size: EnforcementState;
  process_count: EnforcementState;
  wall_clock: EnforcementState;
  environment: EnforcementState;
}

/** `POST /api/settings/sandbox/{name}/check`: what a profile's backend
 *  reports about itself, from its own probe. */
export interface SandboxProfileHealth {
  name: string;
  configured: boolean;
  backend: string;
  platform: string;
  isolation: IsolationLevel | null;
  guarantees: SandboxGuarantees;
  mechanisms: string[];
  network_grant_supported: boolean;
  artifacts_supported: boolean;
  ready: boolean;
  problems: string[];
  notes: string[];
  checked_in_ms: number;
}

export interface RuntimeSettingsIn {
  cost_policy: CostPolicy;
  blob_offload_bytes: number;
  catalogue_budget_chars: number;
  /** Whether the `default` profile (this host's own local backend) is offered. */
  sandbox_enabled: boolean;
  /** The `default` profile's ceiling; an agent's request can only lower it. */
  sandbox_limits: SandboxLimitsIn;
  sandbox_allow_network: boolean;
  sandbox_profiles: SandboxProfileIn[];
  egress_allow: string[];
  denied_tools: string[];
}

export interface RuntimeSettings extends RuntimeSettingsIn {
  /** Whether this host can offer the `default` (local) profile at all. */
  sandbox_available: boolean;
  sandbox_unavailable_reason: string | null;
  sandbox_platform: string;
  sandbox_backends: SandboxBackend[];
  /** Every profile an agent of this account may name. */
  sandbox_profile_names: string[];
}

export interface CodeExecutionLimitsIn {
  cpu_seconds?: number | null;
  wall_seconds?: number | null;
  memory_bytes?: number | null;
  file_size_bytes?: number | null;
  process_count?: number | null;
}

/** An agent's code-execution terms. Part of the published spec and so of its
 *  version hash: an agent that can run programs is a different agent. */
export interface CodeExecutionIn {
  enabled: boolean;
  profile: string;
  isolation: IsolationLevel;
  network: "denied" | "unrestricted";
  limits: CodeExecutionLimitsIn;
  /** Which of the agent's own tools a program may call. Null means all. */
  bindings: string[] | null;
  preview_bytes: number;
  max_output_bytes: number;
  preserve_output: "when_available" | "required" | "never";
  collect_artifacts: boolean;
  max_artifacts: number;
  max_artifact_bytes: number;
}

/** A window of one recorded output, through the reader the model uses. */
export interface ReadToolOutputResult {
  handle: string;
  binary: boolean;
  total_size_bytes: number;
  total_lines: number;
  offset: number;
  limit: number;
  pattern: string | null;
  total_matches: number | null;
  returned_lines: number;
  truncated: boolean;
  content: string;
  matches: { line_number: number; text: string }[];
}

export interface DemoSeedResponse {
  agent_id: string;
  run_ids: string[];
}

export interface A2ATokenRequest {
  secret_name: string;
  ttl_days?: number;
}

export interface A2ATokenResponse {
  secret_name: string;
  /** Shown once. Only its hash is kept. */
  token: string;
  expires_at: string;
}

export interface LocalPeerRequest {
  agent_id: string;
  name?: string | null;
  secret_name?: string;
  description?: string;
}

export interface InterruptRequest {
  reason?: string;
}

export interface OkResponse {
  ok: true;
}

// ---------------------------------------------------------------------------
// GET /api/runs/{id}/status -- psych.RunStatus, the library's own projection
// ---------------------------------------------------------------------------

/** Where a run is, in the words a screen uses. The backend derives this from
 *  the same fold a Worker uses to decide what to do next, so a UI and the
 *  runtime cannot disagree about whether a run is waiting. */
export type Lifecycle =
  | "queued"
  | "running"
  | "waiting"
  | "stopping"
  | "done"
  | "failed"
  | "stopped";

/** The call a waiting run needs a decision on. Everything an approval prompt
 *  needs, so nothing has to be parsed back out of the question text. */
export interface PendingApproval {
  call_id: string;
  tool: string;
  arguments: Record<string, unknown>;
  question: string | null;
  expires_at: string;
}

/** One suggested answer. A person may always reply in their own words
 *  instead, so nothing validates against these. */
export interface QuestionOption {
  label: string;
  description: string;
}

export interface AskedQuestion {
  question: string;
  /** A short chip label, or empty; fall back to the question itself. */
  header: string;
  /** Empty asks for free text. */
  options: QuestionOption[];
  multi_select: boolean;
}

/** A Run stopped because the agent asked something. The sibling of
 *  `PendingApproval`: both are a Run waiting on a person, but an approval
 *  takes yes or no and this takes words. */
export interface PendingQuestion {
  call_id: string;
  questions: AskedQuestion[];
  /** The same thing as one line, for anywhere that will not render options. */
  summary: string;
  expires_at: string;
}

export interface RunStatus {
  run_id: string;
  scope: Scope;
  version_hash: string;
  lifecycle: Lifecycle;
  /** The exact ending, for a caller that needs to tell abandoned from
   *  aborted. `lifecycle` is what to show. */
  terminal_state: TerminalState | null;
  output: Record<string, unknown> | null;
  /** Already written for a person to read: the library composes it through
   *  its own guidance module. The traceback stays in the log. */
  failure_message: string | null;
  failure_kind: string | null;
  suspend_reason: SuspendReason | null;
  suspend_expires_at: string | null;
  pending_approval: PendingApproval | null;
  pending_question: PendingQuestion | null;
  /** The agent's plan as it stands now, so a long job is readable while it
   *  runs rather than only once it has finished. */
  tasks: Task[];
  /** Everything the agent has shown, oldest first. Accumulates, unlike the
   *  plan above, which is one list revised. */
  components: PsychComponent[];
  turn: number;
  tool_calls: number;
  model_calls: number;
  /** The highest sequence written. What a stream reconnects with. */
  head_seq: number;
  attempt_count: number;
  current_attempt: string | null;
  /** The admission deadline, pushed back by however long the run has spent
   *  waiting: waiting for a person is not the run running too long. */
  deadline_at: string | null;
  continues_run_id: string | null;
  parent_run_id: string | null;
  has_dangling_tool_calls: boolean;
}

// ---------------------------------------------------------------------------
// GET /api/runs/{id}/report -- psych.report.model.RunReport
// ---------------------------------------------------------------------------

export interface ModelCallReport {
  turn: number;
  step_id: string | null;
  model: string;
  /** What this turn was told, and what it was offered. Per turn, because a
   *  withheld tool means turn three was genuinely told something turn one was
   *  not. */
  system_prompt: string;
  tool_names: string[];
  usage: Usage | null;
  /** Whether the provider response supplied these counters. */
  usage_reported: boolean | null;
  cost: Cost | null;
  timings: ModelTimings | null;
  finish_reason: string | null;
  text: string;
  tool_call_ids: string[];
  started_at: string;
  finished_at: string | null;
  failure: ToolFailure | null;
  will_retry: boolean;
  dangling: boolean;
}

export interface ToolCallReport {
  call_id: string;
  tool: string;
  turn: number;
  step_id: string | null;
  arguments: Record<string, unknown>;
  outcome: ToolOutcome | null;
  result: unknown;
  failure: ToolFailure | null;
  duration_seconds: number | null;
  result_handle: string | null;
  preview: string | null;
  result_bytes: number;
  attachments: ResultAttachment[];
  started_at: string;
  finished_at: string | null;
  interruptible: boolean;
  safe_to_retry: boolean;
  /** The `run_code` call whose program made this one, or null when the model
   *  made it itself. A program that loops over forty records produces forty
   *  of these and one `run_code`; without the edge a trace shows forty calls
   *  with no cause and one call with no effect. */
  parent_call_id: string | null;
}

export interface StepReport {
  step_id: string;
  name: string;
  kind: string;
  attempt_number: number;
  input: Record<string, unknown>;
  completed: boolean;
  output: Record<string, unknown> | null;
  failure: ToolFailure | null;
  child_run_id: string | null;
  child: RunReport | null;
}

export interface FailureStreakTrip {
  tool: string;
  call_id: string;
  streak: number;
  threshold: number;
  at: string;
}

export interface SuspensionReport {
  reason: SuspendReason;
  question: string | null;
  pending_call_id: string | null;
  suspended_at: string;
  expires_at: string;
  resumed_at: string | null;
  approved: boolean | null;
  payload: Record<string, unknown> | null;
}

export interface LatencyReport {
  wall_clock_seconds: number;
  model_seconds: number;
  tool_seconds: number;
  unaccounted_seconds: number;
}

/**
 * Run-wide totals. `cost` is `null` only when not one call in the Run had a
 * known price -- render "unknown", never `0`. `cost_is_incomplete` is `true`
 * whenever `unpriced_model_calls > 0`, i.e. `cost` is honest but partial even
 * when it is not `null`.
 */
export interface TotalsReport {
  usage: Usage;
  usage_source: "provider" | "partial" | "unknown";
  /** Finished calls omitted by the provider from the usage measurement. */
  unreported_usage_calls: number;
  cost: Cost | null;
  /** Priced calls whose figure the provider reported rather than Psych
   *  computing it. `cost.source` says what the total as a whole is made of. */
  provider_reported_costs: number;
  unpriced_model_calls: number;
  cost_is_incomplete: boolean;
  model_calls: number;
  failed_model_calls: number;
  tool_calls: number;
  /** Summaries written to keep the conversation under the context window. Not
   *  a turn and not inside `model_calls`: a compaction is a call the agent did
   *  not ask for. Its tokens and cost are already in `usage` and `cost`. */
  compaction_calls: number;
  latency: LatencyReport;
}

/** One step of an agent's plan. `active_form` is the present continuous shown
 *  while the step is running: the current line of a plan should read as an
 *  activity, not an instruction. Empty falls back to `title`. */
export interface Task {
  title: string;
  active_form: string;
  description: string;
  status: "pending" | "in_progress" | "completed";
}

export interface RunReport {
  run_id: string;
  scope: Scope;
  version_hash: string;
  spec_name: string;
  system_prompt: string;
  steps: StepReport[];
  tool_calls: ToolCallReport[];
  model_calls: ModelCallReport[];
  suspensions: SuspensionReport[];
  failure_streak_trips: FailureStreakTrip[];
  terminal_state: TerminalState | null;
  output: Record<string, unknown> | null;
  failure: ToolFailure | null;
  orphaned_attempt_id: string | null;
  admitted_at: string;
  settled_at: string | null;
  totals: TotalsReport;
}

// ---------------------------------------------------------------------------
// GET /api/runs/{id}/messages
// ---------------------------------------------------------------------------

export interface RunMessage {
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name?: string | null;
  seq: number;
  at: string;
}

/** One entry of `GET /api/oauth/pending`: an authorization_code grant waiting
 *  for someone to open a browser. Carries no token material and no `code` --
 *  only the URL to send a person to. */
export interface PendingAuthorization {
  state: string;
  authorization_url: string;
  tenant: string;
  started_at: string;
}

/** `POST /api/settings/mcp/{name}/test`: one real connection attempt. */
export interface McpTestResult {
  ok: boolean;
  /** The real error text when `ok` is false, never a generic "connection
   *  failed" -- a `text/html` reply means a redirect, login form or proxy
   *  block, which a person cannot tell from a bad credential otherwise. */
  detail: string;
  tool_count?: number | null;
  tools?: string[] | null;
  /** The exception's class name when `ok` is false, so a caller can say "no
   *  credential stored for this server" rather than relaying an exception. */
  error_type?: string | null;
}

/** One message of `GET /api/runs/{id}/thread`. `seq` is unique only within a
 *  single Run's log, so a thread flattening several Runs needs `run_id` to key
 *  messages apart. */
export interface ThreadMessage extends RunMessage {
  run_id: string;
}

/** One conversation's totals, summed across every Run in its chain by the
 *  backend. Summed there rather than here so money is added in one place. */
export interface ThreadTotals {
  messages: number;
  model_calls: number;
  failed_model_calls: number;
  tool_calls: number;
  /** Summaries written across the chain to keep it under the context window.
   *  Beside the call counts rather than inside them; the tokens are already
   *  counted above. */
  compaction_calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  /** Counted inside `output_tokens`, so never add it to the total. */
  reasoning_tokens: number;
  total_tokens: number;
  usage_source: "provider" | "partial" | "unknown";
  /** Calls whose provider response carried no usage counters. */
  unreported_usage_calls: number;
  /** A decimal string, never a number: this is money. `null` when not one
   *  call in the conversation had a known rate. */
  cost_amount: string | null;
  cost_currency: string | null;
  /** `provider` when the gateway reported the figure, `computed` when Psych
   *  derived it from a price table, `mixed` when the conversation used both. */
  cost_source: string | null;
  provider_reported_costs: number;
  cost_input_amount: string | null;
  cost_output_amount: string | null;
  cost_cache_read_amount: string | null;
  cost_cache_write_amount: string | null;
  unpriced_model_calls: number;
  /** True when at least one call had no rate, so the total is honest but
   *  partial. Keeps "$0.02" and "$0.02 plus three I could not price" apart. */
  cost_is_incomplete: boolean;
  /** First message to last settlement, gaps included: those gaps are a person
   *  reading and typing. Model and tool seconds are separate for that reason. */
  wall_clock_seconds: number;
  model_seconds: number;
  tool_seconds: number;
}

/** `GET /api/threads/{id}/report`: a whole conversation at once. Carries the
 *  totals and every Run's own report, so the conversation view and the
 *  per-message view come from one read and cannot disagree. */
export interface ThreadReport {
  run_ids: string[];
  totals: ThreadTotals;
  reports: RunReport[];
  messages: ThreadMessage[];
}

/** `GET /api/runs/{id}/thread`: one conversation across every Run in its chain.
 *  A thread is a chain of Runs rather than one long-lived Run, so
 *  this is what a chat view renders instead of one Run's `/messages`. */
export interface RunThread {
  /** Oldest first, ending at the Run that was asked for. */
  run_ids: string[];
  messages: ThreadMessage[];
}

// ---------------------------------------------------------------------------
// GET /api/tools
// ---------------------------------------------------------------------------

export interface ToolInfo {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  annotations: string[];
  interruptible: boolean;
  safe_to_retry: boolean;
  source: "code";
}

// ---------------------------------------------------------------------------
// GET /api/scenarios, POST /api/scenarios/{id}/run -- app.schemas' capability
// scenario shapes, and app.scenarios.base.{Assertion,ScenarioResult}.
// ---------------------------------------------------------------------------

/** What a scenario needs to actually run: any of these being unmet is why
 * `available` is `false` and `unavailable_reason` names it. */
export type ScenarioRequirement =
  | "postgres"
  | "mysql"
  | "dynamodb"
  | "model-provider"
  | "container-runtime";

/** One entry of `GET /api/scenarios`. */
export interface ScenarioSummary {
  id: string;
  title: string;
  proves: string;
  design_ref: string;
  requires: ScenarioRequirement[];
  available: boolean;
  unavailable_reason: string | null;
}

/** One SSE progress frame while `POST /api/scenarios/{id}/run` is still
 * working -- the scenario's own `emit(step, detail)` calls. */
export interface ScenarioProgressEvent {
  step: string;
  detail: string;
  at: string;
}

export interface ScenarioAssertion {
  claim: string;
  held: boolean;
  detail: string;
}

/** The stream's final `{"result": ...}` frame -- `app.scenarios.base
 * .ScenarioResult`, as JSON. `passed` is `false` both for a scenario that ran
 * and found its claims did not hold, and for one `check_availability`
 * refused to run at all -- `summary` and `assertions` say which. */
export interface ScenarioRunResult {
  passed: boolean;
  summary: string;
  assertions: ScenarioAssertion[];
  run_ids: string[];
  report?: Record<string, unknown> | null;
}

/** What `streamScenarioRun` yields: either one progress frame or the final
 * result, discriminated the way the caller actually needs to branch on it
 * (the wire frames themselves carry no `kind` -- see `streamScenarioRun`). */
export type ScenarioStreamEvent =
  | ({ kind: "progress" } & ScenarioProgressEvent)
  | { kind: "result"; result: ScenarioRunResult };

// ---------------------------------------------------------------------------
// Settings
//
// These were `Record<string, unknown>` with a comment saying the contract was
// still being written. It is written -- `app/schemas.py` has been typing it
// field for field the whole time -- and `components/settings/types.ts` already
// mirrored it correctly, so every settings call was casting through `unknown`
// to reach types that existed. One definition, re-exported here so a caller
// reaching for the wire shapes finds them where every other wire shape is.
// ---------------------------------------------------------------------------

export type {
  McpConnectionRecord,
  McpGrant,
  McpLiveConnection,
  McpOAuthPreset,
  McpServerPreset,
  McpServerPresetIn,
  McpTransport,
  PlaygroundSettings,
  ProviderIn,
  ProviderOut,
  ProviderTestResult,
} from "@/components/settings/types";

export type { PlaygroundSettings as SettingsResponse } from "@/components/settings/types";
export type { A2APeerPreset } from "@/components/settings/types";

/** The body of `PUT /api/settings/providers`. */
export interface ProviderUpdate {
  providers: import("@/components/settings/types").ProviderIn[];
}

/** The body of `PUT /api/settings/mcp`. */
export interface McpSettings {
  mcp_servers: import("@/components/settings/types").McpServerPreset[];
}

/** The body of `PUT /api/settings/secrets`. Values go out, never come back. */
export interface SecretsUpdate {
  secrets: Record<string, string>;
}

/** `PUT /api/settings/secrets` answers with names only. */
export interface SecretsResponse {
  secrets: string[];
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export interface ValidationProblem {
  path: string;
  message: string;
}

export interface ProblemResponse {
  detail?: unknown;
  issues?: ValidationProblem[];
}

/**
 * One subagent in a run's tree.
 *
 * Read out of the parent's own log: every spawn, every message the parent sent
 * it and its ending are records, so a tree watched live and a tree read back
 * next year are the same projection of the same thing.
 *
 * `state` is the child's own lifecycle rather than what its parent has heard.
 * The two differ for exactly as long as a notification is in flight, which is
 * the moment somebody is most likely to be watching.
 */
export interface SubagentNode {
  name: string;
  run_id: string;
  parent_run_id: string;
  state: string;
  terminal_state: string | null;
  purpose: string;
  task: string;
  deliverable: string;
  tools: string[];
  model: string;
  depth: number;
  spawned_at: string;
  finished_at: string | null;
  messages_sent: number;
  /** Its answer, or the last thing it said on the way to one. */
  latest: string;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  /** A decimal string, or null when the models had no known price. Never "0":
   *  a silent zero makes metering look correct and be wrong. */
  cost: string | null;
  subtree_input_tokens: number;
  subtree_output_tokens: number;
  subtree_cost: string | null;
  subtree_runs: number;
  /** Whether this branch's totals counted everything. False while it runs. */
  complete: boolean;
  children: SubagentNode[];
}

export interface SubagentTree {
  run_id: string;
  state: string;
  children: SubagentNode[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost: string | null;
  complete: boolean;
}

export interface SubagentRetryResult {
  run_id: string;
  retried_from: string;
  version_hash: string;
}
