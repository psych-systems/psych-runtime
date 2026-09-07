/** Homepage claims, separate from the components that present them. */
export const HOME = {
  introduction: "Create agents and multi-step workflows that use your tools, wait for people, and recover from interruptions. Psych runs inside your Python application, with your model and infrastructure.",
  explanation: "A model chooses what to do next. Psych checks what it can access, executes the action, records the result, and carries the run forward. Your application stays in charge of permissions, people, and deployment.",
  modes: [
    { id: "execute", label: "Execute", title: "Turn decisions into actions.", description: "Psych calls the tools your agent is allowed to use and returns their results to the model. The loop continues until the agent finishes.", status: "Tool result recorded", action: "Follow the next step", steps: ["Model chooses a tool", "Psych checks access", "Tool executes", "Result returns to model"] },
    { id: "approval", label: "Pause & resume", title: "Leave room for a human.", description: "Require approval before selected tool calls. Psych saves the pending action and releases the worker while your application collects a decision.", status: "Waiting for approval", action: "Approve this step", steps: ["Model chooses a tool", "Approval required", "Run waits for you", "Continue after a decision"] },
    { id: "recover", label: "Recover", title: "A stopped worker is not the end.", description: "With a durable store, another worker can recover the run after its lease expires. Unfinished calls stay unknown unless the tool is explicitly safe to retry.", status: "Worker stopped", action: "See recovery", steps: ["Progress recorded", "Worker stops", "Lease expires", "Another worker recovers the run"] },
  ],
  features: [
    { title: "Agents", description: "Define the instructions, model, tools, and access once. Psych runs the model and tool loop for each request.", guide: "guides/agents" },
    { title: "Workflows", description: "Combine tools, agents, and nested workflows into named steps. A recovered run continues after its last completed step.", guide: "guides/workflows" },
    { title: "Tools", description: "Connect Python functions, HTTP endpoints, MCP servers, or other agents. Access is checked again on every turn.", guide: "guides/code-tools" },
    { title: "Memory", description: "Remember facts across runs for the same tenant and end user. Keep retrieval behind the system you already use.", guide: "guides/memory" },
  ],
  fit: "Import Psych into the application you already run. Keep your interface, authentication, database, and deployment. Supply the model, tools, and storage that fit your product.",
  faqs: [
    { question: "What is Psych Runtime?", answer: "Psych is an embeddable Python runtime for AI agents and workflows. It runs the model and tool loop, records progress, pauses when your application needs a person, and exposes the result through a small public API." },
    { question: "What can I build with it?", answer: "Use Psych behind customer-facing agents, internal automation, or a product where your own users create agents. It supplies execution. Your application supplies the interface, identity, and business rules." },
    { question: "How do workflows work?", answer: "A workflow sequences named tool, agent, or nested workflow steps. Completed step outputs are read from the run log, so recovery continues after the last completed step instead of running it again." },
    { question: "How do I deploy it?", answer: "Run Psych inside your service and worker processes. Both use the same Store. You choose and operate the model provider, database, sandbox, and telemetry that your deployment needs." },
    { question: "Is it ready for production?", answer: "Psych is pre-alpha at 0.x. The runtime exists and is tested, but the public API can change between minor versions. Start with a bounded use case you can test end to end." },
  ],
  availability: "Pre-alpha. The API is still evolving. Start with a guided example and a use case you can test.",
} as const;
