/**
 * What Psych connects to, and only what is implemented.
 *
 * Each entry names the module or extra that carries it. Anything not in this
 * file is not claimed on the site, and adding an entry means pointing at code.
 */
/**
 * What kind of thing an entry is, because "integration" covers four unlike
 * cases and a reader deciding what they have to build needs them apart.
 *
 * - `built-in`: an adapter in the package. Import it and configure it.
 * - `extra`: in the package, behind a `pip install psych-runtime[...]` extra.
 * - `compatible`: works through an existing adapter because it speaks the same
 *   protocol. Nothing is named after it in the code.
 * - `yours`: a port you implement, or a template you copy. Psych ships the
 *   shape, not the thing.
 */
export type IntegrationKind = "built-in" | "extra" | "compatible" | "yours";

export type Integration = { name: string; kind: IntegrationKind; how: string; via?: string };
export type IntegrationGroup = {
  title: string;
  note: string;
  items: Integration[];
};

export const INTEGRATIONS: IntegrationGroup[] = [
  {
    title: "Model providers",
    note: "Psych speaks the OpenAI-compatible wire protocol rather than depending on a provider SDK. One client reaches anything that speaks it, and the model is a port, so a provider it does not speak is one adapter away.",
    items: [
      {
        name: "OpenAI", kind: "built-in",
        how: "OpenAICompatibleClient against api.openai.com.",
        via: "psych_runtime.OpenAICompatibleClient",
      },
      {
        name: "OpenAI-compatible gateways", kind: "compatible",
        how: "Point base_url at a compatible provider or your own gateway and the same client works.",
        via: "base_url=",
      },
      {
        name: "Your own adapter", kind: "yours",
        how: "Implement ModelClient for a provider with a different wire protocol. The fake model is one such adapter.",
        via: "psych_runtime.ModelClient",
      },
      {
        name: "Price table", kind: "built-in",
        how: "Curated rates for common models ship in DEFAULT_PRICES. Override or replace them with a PriceResolver.",
        via: "psych_runtime.DEFAULT_PRICES",
      },
    ],
  },
  {
    title: "Stores",
    note: "Four adapters against one contract suite, run against real databases in CI. The in-memory store is a real Store that keeps nothing across a process.",
    items: [
      { name: "In memory", kind: "built-in", how: "For tests and the first run. No persistence.", via: "psych_runtime.InMemoryStore" },
      { name: "PostgreSQL", kind: "extra", how: "asyncpg, with sequential forward-only migrations.", via: "pip install psych-runtime[postgres]" },
      { name: "MySQL", kind: "extra", how: "aiomysql, same contract, its own migrations.", via: "pip install psych-runtime[mysql]" },
      { name: "DynamoDB", kind: "extra", how: "aioboto3, conditional writes for the append and the claim.", via: "pip install psych-runtime[dynamodb]" },
    ],
  },
  {
    title: "Blob stores",
    note: "For tool results too large to sit inline in a record. Without one, a result over the limit records an explicit failure rather than a silent truncation.",
    items: [
      { name: "In memory", kind: "built-in", how: "Tests.", via: "psych_runtime.InMemoryBlobStore" },
      { name: "Filesystem", kind: "built-in", how: "A directory on disk.", via: "psych_runtime.store.blob_fs" },
      { name: "S3", kind: "built-in", how: "Any S3-compatible object store.", via: "psych_runtime.store.blob_s3" },
    ],
  },
  {
    title: "Tools and agents",
    note: "Tools are code, HTTP or MCP. Agents elsewhere are reached over A2A.",
    items: [
      {
        name: "MCP servers", kind: "built-in",
        how: "Streamable HTTP, and the earlier HTTP+SSE transport. Pooled by (scope, server, credential). Deferred catalogue disclosure for large servers. No stdio.",
        via: "psych_runtime.McpServer",
      },
      {
        name: "MCP OAuth 2.1", kind: "built-in",
        how: "client_credentials and authorization_code with PKCE, dynamic client registration, scope step-up on a 403. You supply the redirect port; Psych runs no browser.",
        via: "psych_runtime.McpOAuth",
      },
      {
        name: "HTTP endpoints", kind: "built-in",
        how: "A URL, a method, a JSON schema and a credential name. Creatable at runtime by end users.",
        via: "psych_runtime.HttpTool",
      },
      {
        name: "Agent2Agent", kind: "built-in",
        how: "Expose a Run as an A2A Task, or call a remote agent as a tool source.",
        via: "psych_runtime.A2APeer",
      },
    ],
  },
  {
    title: "Code execution",
    note: "In-process sandboxing is rejected, because RestrictedPython, trimmed builtins and AST filtering are all escapable. A separate process is the floor, not a ceiling: how much the two backends contain differs, and the difference matters.",
    items: [
      { name: "Subprocess", kind: "built-in", how: "A fresh interpreter under CPU, memory and file-size rlimits, dropped to an unprivileged uid. It shares the host filesystem, and its network denial is a self-report rather than enforcement.", via: "psych_runtime.sandbox.SubprocessSandbox" },
      { name: "Container", kind: "built-in", how: "One container per program. Filesystem and network isolation are the kernel's, not a promise the program can break.", via: "psych_runtime.sandbox.ContainerSandbox" },
    ],
  },
  {
    title: "Observability",
    note: "The library calls the OpenTelemetry API only. The [otel] extra installs the SDK for you to configure, and never an exporter: that is a deployment's decision.",
    items: [
      { name: "OpenTelemetry", kind: "extra", how: "Eight declared spans under gen_ai.* conventions, with conformance tests.", via: "pip install psych-runtime[otel]" },
      { name: "Your logger", kind: "compatible", how: "Psych attaches a NullHandler and configures nothing else.", via: "logging" },
    ],
  },
  {
    title: "Your application",
    note: "Psych refuses to own an HTTP server, so these are patterns the public API documents rather than integrations it ships.",
    items: [
      { name: "FastAPI", kind: "yours", how: "A template: routes plus a separate Worker process, sharing a Store.", via: "psych new api --template fastapi" },
      { name: "Django", kind: "yours", how: "Calling the async API from synchronous code.", via: "docs: The public API" },
      { name: "Celery and worker queues", kind: "yours", how: "Where a Worker lives when you already have a worker process.", via: "docs: The public API" },
      { name: "Coding agents", kind: "built-in", how: "26 guides covering the runtime's features, installable into your project.", via: "psych skills install" },
    ],
  },
];
