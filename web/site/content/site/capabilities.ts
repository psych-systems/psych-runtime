/**
 * What the runtime does, stated as guarantees, each traceable to the
 * repository.
 *
 * `guarantee` is the sentence a visitor should be able to hold the library
 * to. `guide` is a path under the current docs version; the page resolves it. `mechanism` is how it is kept. `guide` is the docs page that carries the
 * working code, generated from the matching `.agents/skills/*` file, and
 * `design` the DESIGN.md section it comes from. Nothing here is aspirational:
 * every entry names a shipped module, a test layer, or a definition-of-done
 * item the e2e suite asserts.
 */
/**
 * Four groups, so eighteen guarantees can be read a few at a time. The order
 * follows what a reader needs first: get a Run to finish, give it something to
 * call, decide what it may do, then see what it did.
 */
export type CapabilityGroup = "Execution" | "Tools" | "Control" | "Observability";

export const CAPABILITY_GROUPS: readonly { name: CapabilityGroup; blurb: string }[] = [
  { name: "Execution", blurb: "Getting a Run from admitted to finished, and back on its feet after a crash." },
  { name: "Tools", blurb: "What an agent can reach, and where the code it writes runs." },
  { name: "Control", blurb: "Who may do what, what stops before it happens, and what fits in the prompt." },
  { name: "Observability", blurb: "What it cost, what it is doing now, and how you test any of it." },
];

export type Capability = {
  slug: string;
  group: CapabilityGroup;
  title: string;
  guarantee: string;
  mechanism: string[];
  guide?: string;
  design: string;
  home?: boolean;
};

export const CAPABILITIES: Capability[] = [
  {
    slug: "durable-execution",
    group: "Execution",
    title: "Recover a run after a worker stops",
    guarantee:
      "Kill a Worker mid-tool-call and another finishes the Run: it reclaims the expired lease, replays the log and settles the dangling call.",
    mechanism: [
      "A Worker claims a Run with a lease in one conditional write. Two concurrent claims cannot both win.",
      "The lease follows process liveness. A separate wall-clock deadline, enforced in the process holding the Attempt, catches the hang a lease cannot see.",
      "Supervision and execution are separate loops, so a stalled model stream can never block the code that would have timed it out.",
    ],
    guide: "guides/stores",
    design: "DESIGN.md §8, §23.2",
    home: true,
  },
  {
    slug: "record-log",
    group: "Execution",
    title: "Every read is computed from the log",
    guarantee:
      "Every question about a Run is a pure fold over its append-only log.",
    mechanism: [
      "Reports are recomputed on every read and never stored twice, so they cannot drift.",
      "Records are sequenced without gaps and appended under a conditional write. Exactly one Attempt holds the lease and may append.",
      "The reducer has no IO, no clock and no randomness. Same log in, same state out.",
      "A log the protocol could not have produced raises a typed CorruptLog with one of fourteen named reasons. It is never repaired.",
    ],
    guide: "guides/report",
    design: "DESIGN.md §6, §13",
    home: true,
  },
  {
    slug: "specs-and-versions",
    group: "Execution",
    title: "Agents are data, published as versions",
    guarantee:
      "An agent is a serialisable Spec of tool names, which publishing hashes into an immutable Version.",
    mechanism: [
      "A Spec never holds a callable, and the same Spec built in Python or from a dict hashes the same.",
      "Canonicalisation is chosen, not inherited: key order, float formatting, defaults materialised before hashing, server-assigned fields excluded.",
      "A Spec is validated once, at publish. A customer waiting on a response is the wrong place to discover a typo. Per-call checks that only the runtime can make, tool narrowing, approvals and limits, still happen every turn.",
      "Runs pin a Version for their whole life, so editing an agent never mutates a Run in flight.",
    ],
    guide: "guides/agents",
    design: "DESIGN.md §4, §23.1",
    home: true,
  },
  {
    slug: "tools",
    group: "Tools",
    title: "Four kinds of tool, resolved every turn",
    guarantee:
      "Python functions, HTTP endpoints, MCP servers and A2A peers are resolved at the start of every turn, never at boot.",
    mechanism: [
      "A source connected mid-Run is usable on the next turn without a restart, and invisible to another tenant's Run.",
      "Code tools take their schema from type hints and their description from the docstring, so neither can drift from the function.",
      "HTTP tools and MCP servers are data in the Spec. End users can create them at runtime without a redeploy.",
      "Three consecutive failures of one tool within a Run stop the model repeating it.",
    ],
    guide: "guides/code-tools",
    design: "DESIGN.md §10, §23.10",
    home: true,
  },
  {
    slug: "tenant-isolation",
    group: "Control",
    title: "Tool access only narrows",
    guarantee:
      "What the server offers contains what the tenant permits, contains what the Spec grants, contains what is callable now.",
    mechanism: [
      "One function computes that intersection and both the validator and the runtime call it, so the two cannot drift.",
      "A Scope threads through every call and is stamped on every Record. Every read path takes scope= and refuses another tenant's Run.",
      "MCP clients pool by (scope, server, credential) and never by URL, which is the one line that would otherwise send one tenant's token on another's call.",
      "Every outbound HTTP call, the model client included, goes through one egress seam with an EgressPolicy.",
    ],
    guide: "guides/multitenancy",
    design: "DESIGN.md §10.4, §10.5, §14",
    home: true,
  },
  {
    slug: "approvals",
    group: "Control",
    title: "Approval before selected tool calls",
    guarantee:
      "A Run suspends before any tool your selectors mark for approval, releases its lease and persists.",
    mechanism: [
      "The process that approves need not be the one that asked, and the decision and who made it are both in the log.",
      "Tools carry MCP-style annotations: read-only, write, destructive. An unannotated tool is treated as write, never exempt.",
      "status().pending_approval names the exact call waiting. resume(approved=True, by=...) records who decided.",
      "Suspension has four reasons: approval, question, external, children. One mechanism, not four.",
    ],
    guide: "guides/approvals",
    design: "DESIGN.md §10.9, §11",
    home: true,
  },
  {
    slug: "interrupts",
    group: "Control",
    title: "Stop or steer a running agent",
    guarantee:
      "An interrupt during a tool call stops the Run, and a message sent immediately after starts a new one carrying it.",
    mechanism: [
      "Both are visible in the log, in order, so a stop and the message racing it cannot be misread as either alone.",
      "An interruptible call is cancelled; a non-interruptible one finishes, so a refund is never half-issued.",
      "Three queues: steer this turn, follow up after it, or hand a message to the next Run.",
      "stream(after=N) resumes across the interrupt with no gap, because the log is the stream.",
    ],
    guide: "guides/interrupts",
    design: "DESIGN.md §9, §12, §23.3, §23.4",
  },
  {
    slug: "metering",
    group: "Observability",
    title: "Usage per call, cost only when priced",
    guarantee:
      "Usage is recorded per model call and split by cache state, and priced from a table you supply.",
    mechanism: [
      "A model with no known price records cost=None, never 0, because a silent zero makes metering look correct and be wrong.",
      "Input, output, cache read, cache write and reasoning tokens are separate fields, because they are billed at separate rates.",
      "A Run priced in two currencies raises inconsistent_cost rather than summing them.",
      "Latency splits wall-clock time into model, tool, queue and suspended time, and accounts for all of it.",
    ],
    guide: "guides/pricing",
    design: "DESIGN.md §13, §23.7",
    home: true,
  },
  {
    slug: "stores",
    group: "Execution",
    title: "Four stores, one contract",
    guarantee:
      "The same Spec runs identically on in-memory, PostgreSQL, MySQL and DynamoDB.",
    mechanism: [
      "The queue is the store: a runnable Run is one whose lease is unheld or expired, and there is no broker beside it.",
      "Every adapter implements three primitives: append at an expected sequence, claim under a condition, read a range.",
      "One contract suite runs against all four, against real databases. No store is mocked in the test suite.",
      "Migrations are sequential and forward-only.",
    ],
    guide: "guides/stores",
    design: "DESIGN.md §7, §23.8",
    home: true,
  },
  {
    slug: "workflows",
    group: "Execution",
    title: "Workflows resume from the last completed step",
    guarantee:
      "A workflow with a nested agent resumes from its last completed step after a crash and does not re-execute completed steps.",
    mechanism: [
      "Steps are tool calls, agents or nested workflows, sequenced deterministically.",
      "Step names are the memoisation key. A completed step's output is read back from the log rather than recomputed.",
    ],
    guide: "guides/workflows",
    design: "DESIGN.md §5, §23.5",
  },
  {
    slug: "subagents",
    group: "Execution",
    title: "Subagents with a depth and a fan-out limit",
    guarantee:
      "A parent's Spec embeds its subagents, so one Version hash pins the whole tree.",
    mechanism: [
      "A model may also compose a child at run time, within a SpawnEnvelope that joins the hash like every other permission.",
      "Tool access narrows down the tree and never widens.",
      "A parent with background children and no work of its own suspends on CHILDREN rather than holding its lease.",
    ],
    guide: "guides/subagents",
    design: "DESIGN.md §17",
  },
  {
    slug: "sandbox",
    group: "Tools",
    title: "Model-written code runs in a separate process",
    guarantee:
      "A program the model writes executes in a separate process, calls host tools through the normal tool path, and its traceback on failure reaches the model as data.",
    mechanism: [
      "In-process sandboxing is rejected outright: RestrictedPython, exec with trimmed builtins and AST filtering are all escapable.",
      "The two backends contain different amounts. The subprocess sets rlimits and drops to an unprivileged uid; it shares the host filesystem and its network denial is a self-report, not enforcement. The container's is a kernel guarantee.",
    ],
    guide: "guides/sandbox",
    design: "DESIGN.md §18, §23.9",
  },
  {
    slug: "memory-and-skills",
    group: "Tools",
    title: "Memory across runs, skills loaded on demand",
    guarantee:
      "Facts persist across Runs under a key of tenant and end user, erasable per person.",
    mechanism: [
      "A Skill's description sits in the prompt and its body loads only when the model asks, so a long policy is not billed every turn.",
      "end_user_id is required alongside memory: a default would point every end user at one bucket of facts.",
      "[[skill:name]] links in instructions are validated at publish.",
      "Retrieval, embeddings and vector stores are refused. You already have one.",
    ],
    guide: "guides/memory",
    design: "DESIGN.md §15, §16",
  },
  {
    slug: "compaction-and-blobs",
    group: "Control",
    title: "Long conversations and large results",
    guarantee:
      "History is summarised past a token threshold measured from real provider usage, leaving the log untouched.",
    mechanism: [
      "Oversized tool results are offloaded to a BlobStore and handed to the model as a readable handle.",
      "A CompactionApplied Record replaces a range in the model's view, not in the log.",
      "Elision (a Spec field) and offload (a Runtime field) are two thresholds on purpose, because they answer different questions.",
    ],
    guide: "guides/compaction",
    design: "DESIGN.md §10.8, §19",
  },
  {
    slug: "streaming",
    group: "Observability",
    title: "Stream and reconnect without gaps",
    guarantee:
      "A client reconnecting with after=N receives every later record and misses none, including across an interrupt.",
    mechanism: [
      "The log is the stream, so there is no separate stream state that could get out of step with it.",
      "stream() yields every Record and tails until the Run settles. stream_text() projects only the assistant's words for a chat UI.",
      "An optional Notifier accelerates delivery; correctness never depends on it.",
    ],
    guide: "guides/streaming",
    design: "DESIGN.md §12, §23.4",
  },
  {
    slug: "telemetry",
    group: "Observability",
    title: "OpenTelemetry spans for every step",
    guarantee:
      "Every Run, Attempt, Turn, model call, tool call and compaction opens a span under gen_ai.* conventions.",
    mechanism: [
      "The adapter imports the OpenTelemetry API only, and never an exporter: which one you ship is a deployment's decision.",
      "The span schema is declared in one place and conformance tests stop it drifting.",
      "pip install psych-runtime[otel] pulls the API and the SDK. The SDK is what a consumer configures; the library itself only calls the API.",
      "No collector, no dashboard. The playground wires Jaeger to show what a consumer's pipeline sees.",
    ],
    guide: "guides/telemetry",
    design: "DESIGN.md §13.5",
  },
  {
    slug: "a2a",
    group: "Tools",
    title: "Other agents over A2A",
    guarantee:
      "A Run can be exposed as an A2A Task, and a remote agent can be called as a tool source.",
    mechanism: [
      "The protocol and the mapping ship; the server does not, for the same reason nothing else here brings one.",
      "Agent Cards, JSON-RPC and REST envelopes, version and extension negotiation, JWS card signing, push notifications.",
      "A peer pool is keyed by (scope, peer, credential) for the same reason the MCP pool is.",
    ],
    guide: "guides/a2a",
    design: "docs/design-notes",
  },
  {
    slug: "testing",
    group: "Observability",
    title: "Tests without a network",
    guarantee:
      "Models, HTTP, MCP and A2A reach the network through injectable seams, and every test supplies its own.",
    mechanism: [
      "No test calls a model provider or any other outside service. Store tests do connect, to database containers CI starts itself.",
      "A scriptable fake model plays multi-step tool calling, malformed calls, stalled streams and streams that abort mid-token. FakeModel, LogBuilder and McpStubServer are exported for your own suite.",
      "Every feature ships with an end-to-end case that fails when it breaks. A green unit suite over a broken e2e case is a broken build.",
    ],
    guide: "guides/testing",
    design: "DESIGN.md §22",
  },
];
