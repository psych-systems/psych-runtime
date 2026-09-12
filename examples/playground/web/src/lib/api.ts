/**
 * The one client for the Psych playground backend. Every request in this
 * codebase goes through here -- no route handler anywhere else calls
 * `fetch()` against `NEXT_PUBLIC_PSYCH_API` directly, so the base URL, error
 * shape and auth story only ever have to change in one place.
 */

import type {
  DemoSeedResponse,
  ReadToolOutputResult,
  SandboxProfileHealth,
  A2ATokenRequest,
  A2ATokenResponse,
  AccountResponse,
  A2APeerPreset,
  CreateWorkflowRequest,
  CreateWorkflowResponse,
  LocalPeerRequest,
  RuntimeSettingsIn,
  SendRequest,
  SendResponse,
  WorkflowSummary,
  AgentSummary,
  AgentVersionSummary,
  AnswerView,
  ConfigResponse,
  CreateAgentRequest,
  CreateAgentResponse,
  DispatchRequest,
  DispatchResponse,
  BranchRequest,
  ForkRequest,
  InterruptRequest,
  McpServerPreset,
  McpServerPresetIn,
  MemoriesResponse,
  ModelPrice,
  ModelsResponse,
  ThreadReport,
  McpTestResult,
  HealthResponse,
  OkResponse,
  PendingAuthorization,
  PlaygroundSettings,
  ProblemResponse,
  ProviderIn,
  ProviderTestResult,
  PsychRecord,
  ResumeRequest,
  RunMessage,
  RunReport,
  RunStatus,
  RunSummary,
  SkillIn,
  RunThread,
  SubagentRetryResult,
  SubagentTree,
  ScenarioRunResult,
  ScenarioStreamEvent,
  ScenarioSummary,
  ToolInfo,
  ValidationProblem,
} from "@/lib/types";

/**
 * Where the backend is, as the bundle was built to believe.
 *
 * Set to the empty string, it means **this origin**: the server serving the
 * console forwards `/api` and `/a2a` to the backend itself, so every request
 * is same-origin and there is no second origin to get wrong. That is how the
 * Docker image is built, and it is why `??` rather than `||` -- an empty
 * string here is a decision, not a missing value.
 */
const CONFIGURED_API = process.env.NEXT_PUBLIC_PSYCH_API ?? "http://localhost:8080";

/** True when requests go to the page's own origin. See `CONFIGURED_API`. */
export const SAME_ORIGIN_API = CONFIGURED_API === "";

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * Where the backend is, with loopback spelled the way this page was opened.
 *
 * `NEXT_PUBLIC_PSYCH_API` is baked in at build time, so a bundle built with
 * `http://localhost:8080` is served to somebody who typed `127.0.0.1:3010` and
 * then makes cross-*site* requests: a browser treats `localhost` and
 * `127.0.0.1` as different sites, not merely different origins.
 *
 * That matters because the session cookie is `SameSite=Lax`, so it is simply
 * not attached to a cross-site request. The symptom is nasty: sign-in returns
 * 200, the console shows you as signed in, and every request after that is
 * anonymous, because the only thing that ever worked was the response body of
 * the sign-in itself.
 *
 * So when both the page and the configured backend are on loopback, the page's
 * own spelling wins. A backend genuinely elsewhere is left exactly as
 * configured.
 */
function resolveApiBase(): string {
  // Same-origin has no host to reconcile: a relative path is already spelled
  // however the page was opened, which is the whole reason to prefer it.
  if (SAME_ORIGIN_API) return "";
  if (typeof window === "undefined") return CONFIGURED_API;
  try {
    const configured = new URL(CONFIGURED_API);
    const here = window.location.hostname;
    if (!LOOPBACK.has(configured.hostname) || !LOOPBACK.has(here))
      return CONFIGURED_API;
    configured.hostname = here;
    return configured.toString().replace(/\/$/, "");
  } catch {
    // A base URL that will not parse is a configuration error, and failing
    // every request with a clear "could not reach" beats silently rewriting
    // something we do not understand.
    return CONFIGURED_API;
  }
}

export const API_BASE_URL: string = resolveApiBase();

/**
 * `path` as an address somebody outside this browser can use.
 *
 * `API_BASE_URL` is empty when the console proxies to the backend on its own
 * origin, which is right for every request the page makes and useless for a
 * value somebody copies and hands to another agent. Those need a scheme and a
 * host, and the page's own origin is the honest one to give: it is the address
 * that reached this console, so it is an address that reaches this backend.
 */
export function externalUrl(path: string): string {
  if (API_BASE_URL !== "") return `${API_BASE_URL}${path}`;
  if (typeof window === "undefined") return path;
  return `${window.location.origin}${path}`;
}

/** A non-2xx response the backend answered. Carries the same `detail` /
 * `issues` shape as every `ProblemResponse` in `app.schemas`. */
export class ApiError extends Error {
  readonly status: number;
  readonly issues: ValidationProblem[];

  constructor(
    status: number,
    detail: string,
    issues: ValidationProblem[] = [],
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.issues = issues;
  }
}

function normalizeProblem(
  problem: ProblemResponse | null,
  fallback: string,
): { detail: string; issues: ValidationProblem[] } {
  if (problem === null) return { detail: fallback || "Request failed", issues: [] };
  const explicitIssues = Array.isArray(problem.issues) ? problem.issues : [];
  if (typeof problem.detail === "string") {
    return { detail: problem.detail, issues: explicitIssues };
  }
  if (Array.isArray(problem.detail)) {
    const issues = problem.detail.flatMap((value): ValidationProblem[] => {
      if (!value || typeof value !== "object") return [];
      const item = value as Record<string, unknown>;
      if (typeof item.msg !== "string") return [];
      const path = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== "body").map(String).join(".")
        : "request";
      return [{ path: path || "request", message: item.msg }];
    });
    return {
      detail: issues.length > 0 ? "Check the highlighted fields." : fallback || "Request failed",
      issues: [...explicitIssues, ...issues],
    };
  }
  return { detail: fallback || "Request failed", issues: explicitIssues };
}

/** The backend never answered at all -- DNS, connection refused, timeout,
 * CORS. Distinct from `ApiError` because there is no status code or problem
 * body to show, only the URL that was tried. */
export class BackendUnreachableError extends Error {
  readonly url: string;

  constructor(url: string, cause?: unknown) {
    super(`could not reach the Psych backend at ${url}`);
    this.name = "BackendUnreachableError";
    this.url = url;
    this.cause = cause;
  }
}

/** Nobody is signed in, or the session aged out.
 *
 * Its own class rather than an `ApiError` with `status === 401` so a caller
 * can act on it without matching on a number, and so the one place that
 * redirects to the sign-in page reads as handling a session rather than
 * handling an error code.
 */
export class NotSignedInError extends Error {
  constructor(detail = "Sign in to continue.") {
    super(detail);
    this.name = "NotSignedInError";
  }
}

/** Sent on every request, including the SSE ones.
 *
 * The session is an httpOnly cookie, which script on this page deliberately
 * cannot read: an XSS bug should not become a stolen session. The cost is that
 * `fetch` will not send it across origins unless asked, and the console
 * (`:3010`) and the backend (`:8080`) are different origins. They are the same
 * *site*, so `SameSite=Lax` still lets the cookie ride; without this option it
 * simply would not be attached and every request would 401.
 */
const CREDENTIALS: RequestCredentials = "include";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      credentials: CREDENTIALS,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new BackendUnreachableError(API_BASE_URL, err);
  }

  if (!res.ok) {
    let problem: ProblemResponse | null = null;
    try {
      problem = (await res.json()) as ProblemResponse;
    } catch {
      // Not a JSON problem body (a proxy 502, say). Fall through.
    }
    const normalized = normalizeProblem(problem, res.statusText);
    if (res.status === 401) throw new NotSignedInError(normalized.detail);
    throw new ApiError(res.status, normalized.detail, normalized.issues);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function json(body: unknown): string {
  return JSON.stringify(body);
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export function getConfig(signal?: AbortSignal): Promise<ConfigResponse> {
  return request<ConfigResponse>("/api/config", { signal });
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

/** Liveness, and whether anybody has signed up yet.
 *
 * The one call that answers without a session, so it is what the console asks
 * before it knows who it is talking to. `getConfig` used to serve that purpose
 * and cannot: what it reports is per account.
 */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health", { signal });
}

/** Who is signed in. Throws `NotSignedInError` when nobody is. */
export function whoami(signal?: AbortSignal): Promise<AccountResponse> {
  return request<AccountResponse>("/api/auth/me", { signal });
}

export function signUp(
  email: string,
  password: string,
  displayName = "",
): Promise<AccountResponse> {
  return request<AccountResponse>("/api/auth/signup", {
    method: "POST",
    body: json({ email, password, display_name: displayName }),
  });
}

export function signIn(
  email: string,
  password: string,
): Promise<AccountResponse> {
  return request<AccountResponse>("/api/auth/signin", {
    method: "POST",
    body: json({ email, password }),
  });
}

export function signOut(): Promise<OkResponse> {
  return request<OkResponse>("/api/auth/signout", { method: "POST" });
}

// ---------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------

export function listAgents(signal?: AbortSignal): Promise<AgentSummary[]> {
  return request<AgentSummary[]>("/api/agents", { signal });
}

/** One agent, described by the version it currently runs. */
export function getAgent(
  agentId: string,
  signal?: AbortSignal,
): Promise<AgentSummary> {
  return request<AgentSummary>(`/api/agents/${encodeURIComponent(agentId)}`, {
    signal,
  });
}

/** What this agent has been, newest first. */
export function listAgentVersions(
  agentId: string,
  signal?: AbortSignal,
): Promise<AgentVersionSummary[]> {
  return request<AgentVersionSummary[]>(
    `/api/agents/${encodeURIComponent(agentId)}/versions`,
    { signal },
  );
}

export function listMemories(): Promise<MemoriesResponse> {
  return request<MemoriesResponse>("/api/memories");
}

export function forgetMemory(memoryId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `/api/memories/${encodeURIComponent(memoryId)}`,
    {
      method: "DELETE",
    },
  );
}

export function eraseMemories(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/api/memories", { method: "DELETE" });
}

export function getThreadReport(runId: string): Promise<ThreadReport> {
  return request<ThreadReport>(
    `/api/threads/${encodeURIComponent(runId)}/report`,
  );
}

export function listModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>("/api/models");
}

export function createAgent(
  body: CreateAgentRequest,
): Promise<CreateAgentResponse> {
  return request<CreateAgentResponse>("/api/agents", {
    method: "POST",
    body: json(body),
  });
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

export function listRuns(signal?: AbortSignal): Promise<RunSummary[]> {
  return request<RunSummary[]>("/api/runs", { signal });
}

export function dispatchRun(body: DispatchRequest): Promise<DispatchResponse> {
  return request<DispatchResponse>("/api/runs", {
    method: "POST",
    body: json(body),
  });
}

/**
 * Ask one message of a conversation again, differently, keeping both answers,
 * without leaving the conversation.
 *
 * The new run continues that message's *predecessor*, so everything said
 * before it is shared history and everything from it on is a second future.
 * The backend puts the new run on a new branch of the same conversation, which
 * is what keeps both findable afterwards rather than one of them being
 * followed at random. The history list still shows one chat.
 */
export function branchRun(
  runId: string,
  body: BranchRequest,
): Promise<DispatchResponse> {
  return request<DispatchResponse>(
    `/api/runs/${encodeURIComponent(runId)}/branch`,
    {
      method: "POST",
      body: json(body),
    },
  );
}

/**
 * Take a conversation somewhere else from one of its messages, as a new chat.
 *
 * The same divergence point as a branch and the same shared history. What
 * differs is that the new run opens a conversation of its own: its own row in
 * the history list, deleted on its own, and left standing when the chat it came
 * from is deleted. It may also run a different agent, which a branch may not.
 */
export function forkRun(
  runId: string,
  body: ForkRequest,
): Promise<DispatchResponse> {
  return request<DispatchResponse>(
    `/api/runs/${encodeURIComponent(runId)}/fork`,
    {
      method: "POST",
      body: json(body),
    },
  );
}

export function getRunReport(
  runId: string,
  signal?: AbortSignal,
): Promise<RunReport> {
  return request<RunReport>(`/api/runs/${encodeURIComponent(runId)}/report`, {
    signal,
  });
}

/**
 * What the run is doing right now, in the words a screen uses.
 *
 * `psych.status()`, served straight through: the same fold a Worker uses to
 * decide what to do next, so a UI and the runtime cannot disagree about
 * whether a run is waiting on a person. Prefer this over reading the record
 * stream for state -- the stream says what has happened, this says what is
 * true, and only this one carries `stopping` and the pending approval's
 * arguments.
 */
/** What the run concluded, with the work behind it collapsed. Prefer this over
 *  the raw record stream anywhere a person is reading a result: the stream is
 *  everything that happened, this is what it amounted to. */
export function getRunAnswer(
  runId: string,
  signal?: AbortSignal,
): Promise<AnswerView> {
  return request<AnswerView>(`/api/runs/${encodeURIComponent(runId)}/answer`, {
    signal,
  });
}

export function getRunStatus(
  runId: string,
  signal?: AbortSignal,
): Promise<RunStatus> {
  return request<RunStatus>(`/api/runs/${encodeURIComponent(runId)}/status`, {
    signal,
  });
}

export function getRunMessages(
  runId: string,
  signal?: AbortSignal,
): Promise<RunMessage[]> {
  return request<RunMessage[]>(
    `/api/runs/${encodeURIComponent(runId)}/messages`,
    { signal },
  );
}

/** The whole conversation this Run belongs to, not just this Run's own
 *  messages. Prefer this over `getRunMessages` anywhere a person is reading a
 *  chat: a second message starts a new Run, so `/messages` alone shows only
 *  the latest exchange. */
export function getRunThread(
  runId: string,
  signal?: AbortSignal,
): Promise<RunThread> {
  return request<RunThread>(`/api/runs/${encodeURIComponent(runId)}/thread`, {
    signal,
  });
}

export function resumeRun(
  runId: string,
  body: ResumeRequest,
): Promise<OkResponse> {
  return request<OkResponse>(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    body: json(body),
  });
}

export function interruptRun(
  runId: string,
  body: InterruptRequest = {},
): Promise<OkResponse> {
  return request<OkResponse>(
    `/api/runs/${encodeURIComponent(runId)}/interrupt`,
    {
      method: "POST",
      body: json(body),
    },
  );
}

/** A message into a run that is still executing, on one of the three queues.
 *  A message for a settled run is `dispatchRun` with `continues_run_id`. */
export function sendToRun(runId: string, body: SendRequest): Promise<SendResponse> {
  return request<SendResponse>(`/api/runs/${encodeURIComponent(runId)}/send`, {
    method: "POST",
    body: json(body),
  });
}

/** `psych.state()`: the reducer's working object, whole. For the debug view. */
export function getRunState(runId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/api/runs/${encodeURIComponent(runId)}/state`, {
    signal,
  });
}

/** One page of the raw log, for a client that wants it whole rather than streamed. */
export function getRunRecords(
  runId: string,
  options: { after?: number; limit?: number; signal?: AbortSignal } = {},
): Promise<PsychRecord[]> {
  const params = new URLSearchParams();
  if (options.after !== undefined) params.set("after", String(options.after));
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const query = params.toString();
  return request<PsychRecord[]>(
    `/api/runs/${encodeURIComponent(runId)}/records${query ? `?${query}` : ""}`,
    { signal: options.signal },
  );
}

// ---------------------------------------------------------------------------
// Workflows
// ---------------------------------------------------------------------------

export function listWorkflows(signal?: AbortSignal): Promise<WorkflowSummary[]> {
  return request<WorkflowSummary[]>("/api/workflows", { signal });
}

export function getWorkflow(workflowId: string, signal?: AbortSignal): Promise<WorkflowSummary> {
  return request<WorkflowSummary>(`/api/workflows/${encodeURIComponent(workflowId)}`, { signal });
}

export function createWorkflow(body: CreateWorkflowRequest): Promise<CreateWorkflowResponse> {
  return request<CreateWorkflowResponse>("/api/workflows", { method: "POST", body: json(body) });
}

export function deleteWorkflow(workflowId: string): Promise<OkResponse> {
  return request<OkResponse>(`/api/workflows/${encodeURIComponent(workflowId)}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export function listTools(signal?: AbortSignal): Promise<ToolInfo[]> {
  return request<ToolInfo[]>("/api/tools", { signal });
}

// ---------------------------------------------------------------------------
// Capabilities scenarios
// ---------------------------------------------------------------------------

export function listScenarios(
  signal?: AbortSignal,
): Promise<ScenarioSummary[]> {
  return request<ScenarioSummary[]>("/api/scenarios", { signal });
}

/**
 * Consume `POST /api/scenarios/{id}/run` as an async generator of
 * `ScenarioStreamEvent`s.
 *
 * Same non-`EventSource` approach as `streamRun` and for the same reason,
 * plus this endpoint is a `POST` in the first place, which `EventSource`
 * cannot issue at all. Unlike a run stream there is nothing to reconnect
 * to -- a dropped connection here loses the scenario's own progress for
 * good, since re-running means re-running the whole demonstration from
 * scratch -- so this does not retry; the caller sees the failure and offers
 * its own "run again".
 *
 * Yields one `{kind: "progress", ...}` per SSE frame carrying `{step,
 * detail, at}`, then one `{kind: "result", result}` for the frame carrying
 * `{result: ...}`, then returns when the server sends `event: done`.
 */
export async function* streamScenarioRun(
  scenarioId: string,
  signal?: AbortSignal,
): AsyncGenerator<ScenarioStreamEvent, void, void> {
  const url = `${API_BASE_URL}/api/scenarios/${encodeURIComponent(scenarioId)}/run`;

  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      signal,
      credentials: CREDENTIALS,
      headers: { Accept: "text/event-stream" },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new BackendUnreachableError(API_BASE_URL, err);
  }

  if (!res.ok || res.body === null) {
    let problem: ProblemResponse | null = null;
    try {
      problem = (await res.json()) as ProblemResponse;
    } catch {
      // Not a JSON problem body.
    }
    const normalized = normalizeProblem(problem, res.statusText);
    throw new ApiError(res.status, normalized.detail, normalized.issues);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const parsed = parseSseEvent(rawEvent);
        sepIndex = buffer.indexOf("\n\n");
        if (parsed === null) continue;
        if (parsed.event === "done") return;
        if (parsed.data.length > 0) {
          const payload = JSON.parse(parsed.data) as
            | { result: ScenarioRunResult }
            | ScenarioProgressEventWire;
          if ("result" in payload) {
            yield { kind: "result", result: payload.result };
          } else {
            yield {
              kind: "progress",
              step: payload.step,
              detail: payload.detail,
              at: payload.at,
            };
          }
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

interface ScenarioProgressEventWire {
  step: string;
  detail: string;
  at: string;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export function getSettings(signal?: AbortSignal): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings", { signal });
}

/** Replaces the whole provider list. A provider named by `id` keeps its
 *  stored key unless `api_key` is sent, so send the full list every time and
 *  omit `api_key` on the entries you are not changing. */
export function updateProviders(
  providers: ProviderIn[],
): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/providers", {
    method: "PUT",
    body: json({ providers }),
  });
}

export function activateProvider(providerId: string): Promise<OkResponse> {
  return request<OkResponse>(
    `/api/settings/providers/${encodeURIComponent(providerId)}/activate`,
    {
      method: "POST",
    },
  );
}

/** One real call to the provider, so a bad key reads differently from a bad
 *  URL or a model id the provider has never heard of. */
export function testProvider(providerId: string): Promise<ProviderTestResult> {
  return request<ProviderTestResult>(
    `/api/settings/providers/${encodeURIComponent(providerId)}/test`,
    { method: "POST" },
  );
}

/** Replaces the whole connection list. Each entry's stored last-connection
 *  record survives an edit that leaves its connection details alone. */
export function updateMcpSettings(
  mcpServers: Array<
    McpServerPresetIn & Partial<Pick<McpServerPreset, "last_connection" | "live">>
  >,
): Promise<PlaygroundSettings> {
  const writable = mcpServers.map((server) => {
    const { last_connection, live, ...preset } = server;
    void last_connection;
    void live;
    return preset;
  });
  return request<PlaygroundSettings>("/api/settings/mcp", {
    method: "PUT",
    body: json({ mcp_servers: writable }),
  });
}

/** Authorizations waiting for a browser. Polled while a connection is in
 *  flight: `authorize()` runs server-side inside the connect attempt, so the
 *  URL has nowhere else to surface. */
export function listPendingAuthorizations(
  signal?: AbortSignal,
): Promise<PendingAuthorization[]> {
  return request<PendingAuthorization[]>("/api/oauth/pending", { signal });
}

/** Connect to a preset for real and report what it offers. Goes through the
 *  same pool a Run uses, so a pass here means a Run would connect too. */
/** Close this account's connection to a server and forget its stored token.
 *  The server keeps existing as a preset; only the live connection goes. */
export function disconnectMcpServer(name: string): Promise<OkResponse> {
  return request<OkResponse>(
    `/api/settings/mcp/${encodeURIComponent(name)}/disconnect`,
    {
      method: "POST",
    },
  );
}

/** Connect for real, through the same pool a Run uses.
 *
 *  `force` drops the pooled connection and the stored token first, so this
 *  reconnects on a new one rather than reporting the one already open. For a
 *  browser sign-in grant that means signing in again, which is why it is not
 *  the default. */
export function testMcpServer(
  name: string,
  options?: { force?: boolean },
): Promise<McpTestResult> {
  const query = options?.force ? "?force=true" : "";
  return request<McpTestResult>(
    `/api/settings/mcp/${encodeURIComponent(name)}/test${query}`,
    { method: "POST" },
  );
}

/** Stop offering an agent, with every version it has had. No version is
 *  destroyed: they are immutable and stay in the store, so every conversation
 *  that ran one is still readable. This removes it from the catalogue only. */
export function deleteAgent(agentId: string): Promise<OkResponse> {
  return request<OkResponse>(`/api/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
}

/** Remove a chat from history. `thread` drops every Run in the conversation,
 *  which is what deleting a chat means to a person. The Records stay in the
 *  Store either way. */
export function deleteRun(
  runId: string,
  options?: { thread?: boolean },
): Promise<OkResponse> {
  const query = options?.thread ? "?thread=true" : "";
  return request<OkResponse>(`/api/runs/${encodeURIComponent(runId)}${query}`, {
    method: "DELETE",
  });
}

/** Merges into the stored secrets and into the live resolver, so a secret
 *  set here is usable by the very next connection without a restart. Never
 *  returns a value, only the names now stored. */
export function updateModelPrices(
  prices: ModelPrice[],
): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/prices", {
    method: "PUT",
    body: JSON.stringify({ prices }),
  });
}

/** Replace this account's skill library. Attaching one of these to an agent
 *  copies it into the published spec, so this never changes an agent that is
 *  already published. */
/** Replace this account's saved A2A peers. Presets: attaching one to an agent
 *  copies it into the published spec, so this never changes what an agent
 *  already published calls. */
export function updateA2APeers(
  peers: A2APeerPreset[],
): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/a2a", {
    method: "PUT",
    body: JSON.stringify({ a2a_peers: peers }),
  });
}

export function checkSandboxProfile(name: string): Promise<SandboxProfileHealth> {
  return request<SandboxProfileHealth>(
    `/api/settings/sandbox/${encodeURIComponent(name)}/check`,
    { method: "POST" }
  );
}

export function readRunAttachment(
  runId: string,
  handle: string,
  options: { offset?: number; limit?: number; pattern?: string } = {},
  signal?: AbortSignal
): Promise<ReadToolOutputResult> {
  const params = new URLSearchParams();
  if (options.offset !== undefined) params.set("offset", String(options.offset));
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.pattern) params.set("pattern", options.pattern);
  const query = params.toString();
  return request<ReadToolOutputResult>(
    `/api/runs/${encodeURIComponent(runId)}/attachments/${encodeURIComponent(handle)}${query ? `?${query}` : ""}`,
    { signal }
  );
}

export function attachmentDownloadUrl(runId: string, handle: string): string {
  return externalUrl(
    `/api/runs/${encodeURIComponent(runId)}/attachments/${encodeURIComponent(handle)}/download`
  );
}

export function seedCodeExecutionDemo(): Promise<DemoSeedResponse> {
  return request<DemoSeedResponse>("/api/demo/code-execution", { method: "POST" });
}

export function updateRuntimeSettings(body: RuntimeSettingsIn): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/runtime", {
    method: "PUT",
    body: json(body),
  });
}

/** Mint a long-lived bearer token another agent presents to call this
 *  account's agents over A2A, saved as a secret. The value comes back once. */
export function createA2AToken(body: A2ATokenRequest): Promise<A2ATokenResponse> {
  return request<A2ATokenResponse>("/api/settings/a2a/tokens", {
    method: "POST",
    body: json(body),
  });
}

/** One of this account's own agents, added as an A2A peer. */
export function addLocalPeer(body: LocalPeerRequest): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/a2a/peers/local", {
    method: "POST",
    body: json(body),
  });
}

export function updateSkills(skills: SkillIn[]): Promise<PlaygroundSettings> {
  return request<PlaygroundSettings>("/api/settings/skills", {
    method: "PUT",
    body: JSON.stringify({ skills }),
  });
}

export function updateSecrets(
  secrets: Record<string, string>,
): Promise<{ secrets: string[] }> {
  return request<{ secrets: string[] }>("/api/settings/secrets", {
    method: "PUT",
    body: json({ secrets }),
  });
}

export function deleteSecret(name: string): Promise<OkResponse> {
  return request<OkResponse>(
    `/api/settings/secrets/${encodeURIComponent(name)}`,
    {
      method: "DELETE",
    },
  );
}

// ---------------------------------------------------------------------------
// Run stream (SSE)
// ---------------------------------------------------------------------------

/**
 * Consume `GET /api/runs/{id}/stream` as an async generator of Records.
 *
 * Deliberately not `EventSource`: its built-in reconnect always replays from
 * the URL it was constructed with, which for this endpoint means replaying
 * from the original `after=` and duplicating every record a caller already
 * saw. This reads the response body directly instead, so a caller decides for
 * itself what `after` to reconnect with (see `useRunStream`, which reconnects
 * with `after=<last seq it saw>`).
 *
 * Yields one `PsychRecord` per SSE `data:` frame. Returns normally when the
 * server sends `event: done` (the run's stream ended, whether or not the Run
 * itself has settled -- reaching the end of a `?after=N` catch-up is also
 * `done`). Throws `BackendUnreachableError` if the connection could not be
 * established, `ApiError` if the server answered but not with a stream, and
 * whatever `fetch`/the reader throw if the connection drops mid-stream (a
 * proxy timeout, a killed backend) -- the caller decides whether and how to
 * retry.
 */
export async function* streamRun(
  runId: string,
  options: { after?: number; signal?: AbortSignal } = {},
): AsyncGenerator<PsychRecord, void, void> {
  const after = options.after ?? 0;
  const url = `${API_BASE_URL}/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`;

  let res: Response;
  try {
    res = await fetch(url, {
      signal: options.signal,
      credentials: CREDENTIALS,
      headers: { Accept: "text/event-stream" },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new BackendUnreachableError(API_BASE_URL, err);
  }

  if (!res.ok || res.body === null) {
    let problem: ProblemResponse | null = null;
    try {
      problem = (await res.json()) as ProblemResponse;
    } catch {
      // Not a JSON problem body.
    }
    const normalized = normalizeProblem(problem, res.statusText);
    throw new ApiError(res.status, normalized.detail, normalized.issues);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });

      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex !== -1) {
        const rawEvent = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const parsed = parseSseEvent(rawEvent);
        sepIndex = buffer.indexOf("\n\n");
        if (parsed === null) continue;
        if (parsed.event === "done") return;
        if (parsed.data.length > 0) {
          yield JSON.parse(parsed.data) as PsychRecord;
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

interface SseFrame {
  event: string;
  data: string;
}

/** Parses one `\n`-joined block between `\n\n` separators of an SSE stream.
 * Returns `null` for a keep-alive comment (`: ...`), which carries no field. */
function parseSseEvent(raw: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  let sawField = false;

  for (const line of raw.split("\n")) {
    if (line.length === 0 || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
      sawField = true;
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).replace(/^ /, ""));
      sawField = true;
    }
  }

  if (!sawField) return null;
  return { event, data: dataLines.join("\n") };
}

/**
 * The subagent tree under one run, with each branch's tokens and cost.
 *
 * Polled by the panel that renders it rather than streamed: the tree is a
 * projection over logs, and a run's own record stream (`/stream`) already wakes
 * the page whenever anything happens, so this is fetched when that says
 * something did.
 */
export async function fetchSubagents(
  runId: string,
  depth = 2,
): Promise<SubagentTree> {
  return request<SubagentTree>(`/api/runs/${runId}/subagents?depth=${depth}`);
}

/** Send a message to a running subagent. It arrives at its next turn. */
export async function messageSubagent(
  runId: string,
  childRunId: string,
  message: string,
): Promise<void> {
  await request<OkResponse>(
    `/api/runs/${runId}/subagents/${childRunId}/message`,
    {
      method: "POST",
      body: JSON.stringify({ message }),
    },
  );
}

/** Stop one subagent without stopping the run that started it. */
export async function stopSubagent(
  runId: string,
  childRunId: string,
  reason = "stopped from the console",
): Promise<void> {
  await request<OkResponse>(
    `/api/runs/${runId}/subagents/${childRunId}/interrupt`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

/**
 * Run a failed subagent's spec again.
 *
 * A new run of the same pinned version, not a second attempt at the old one: a
 * run is admitted once and its log is append-only, so the failed one stays in
 * the tree next to the retry.
 */
export async function retrySubagent(
  runId: string,
  childRunId: string,
): Promise<SubagentRetryResult> {
  return request<SubagentRetryResult>(
    `/api/runs/${runId}/subagents/${childRunId}/retry`,
    {
      method: "POST",
    },
  );
}
