---
name: psych-builder
description: >-
  Use Psych's fluent Python builders (`psych_runtime.agent()`, `psych_runtime.workflow()`) to
  construct an AgentSpec or WorkflowSpec while
  registering tool functions in one pass. Use whenever someone wants a shorter
  or more readable way to define a Psych agent, is converting a dict or YAML
  agent definition into Python, wants tool registration and Spec construction to
  happen together, or asks whether the builder and a hand-written Spec produce
  the same Version hash (they must). Read before writing repetitive
  `AgentSpec(...)` construction, since `.tool(fn)` both registers the function
  and grants its name, which is the step people forget when writing the Spec by
  hand.
---

# The builders

One of four authoring forms and privileged over none of them. `.build()`
produces exactly the `AgentSpec` or `WorkflowSpec` you would get from validating
an equivalent dict, so it hashes the same and runs identically. That equivalence
is tested, not aspirational.

Both entry points are on `psych` itself: `psych_runtime.agent(name)` and
`psych_runtime.workflow(name)`.

## An agent

```python
spec = (
    psych_runtime.agent("support")
    .description("Answers order questions and issues refunds.")
    .instructions("Help the customer with their order. See [[skill:refund-policy]].")
    .model("gpt-4o", temperature=0.2, fallbacks=("gpt-4o-mini",))
    .tool(lookup_order, annotations={"read-only"})
    .tool(issue_refund, interruptible=False, annotations={"destructive"})
    .http_tool(
        "check_stock",
        description="Check warehouse stock for a SKU.",
        url="https://api.example.com/stock/{sku}",
        method="GET",
        credential="warehouse-api-key",
    )
    .mcp_server("github", "https://mcp.example.com/github", allow=("search_issues",))
    .skill("refund-policy", "When a refund is allowed", body="Refunds within 30 days...")
    .subagent("researcher", "Digs through the knowledge base for policy details", research_spec)
    .limits(max_turns=12, deadline_seconds=300)
    .suspension(may_ask_questions=True)
    .build()
)
```

Every method returns `self`, so the Spec reads as the sequence of grants that
built it. `.build()` is replayable: calling it any number of times produces a
fresh, equally valid Spec from the same fields.

## The registry is the point

`.tool(fn)` does two things: registers `fn` in a `ToolRegistry` and appends a
`CodeTool(name=...)` to the Spec. What lands in the Spec is the registered name,
never the function. This is the ergonomic path that means you never hit the
Spec model's own rejection of a callable.

Share one registry across builders and with your `Runtime`:

```python
registry = psych_runtime.ToolRegistry()
spec = psych_runtime.agent("support", registry=registry).model("gpt-4o").tool(lookup_order).build()

runtime = psych_runtime.Runtime(store=store, model=model, registry=registry)
version = await psych_runtime.publish(
    store, spec, context=psych_runtime.ValidationContext(registered_tools=registry.names)
)
```

Read the builder's own registry with `.registry` when you did not pass one in.

`.tool_by_name("lookup_order")` grants a tool this builder did not register:
one registered directly against the registry, or one another process registers.
Two Workers on two machines can register different implementations under the
same name, and the Spec is the same Spec.

## Shared methods

Both builders have these:

| Method | Notes |
|---|---|
| `.tool(fn, name=, description=, interruptible=, safe_to_retry=, annotations=)` | Registers and grants. |
| `.tool_by_name(name, interruptible=)` | Grants only. |
| `.http_tool(name, description=, url=, method=, input_schema=, headers=, credential=, timeout_seconds=, interruptible=)` | Entirely data. |
| `.mcp_server(name, url, transport=, credential=, oauth=, allow=, optional=)` | Empty `allow` means every tool the server offers, still narrowed by the tenant at resolution time. |
| `.limits(...)` | Every `Limits` field. |
| `.suspension(...)` | Every `SuspensionPolicy` field. |

Agent-only: `.description()`, `.instructions()`, `.model()`, `.skill()`,
`.subagent()`.

Workflow-only: `.description()`, `.tool_step()`, `.agent_step()`,
`.workflow_step()`.

## A workflow

```python
spec = (
    psych_runtime.workflow("onboard-customer")
    .tool(create_account)
    .tool_step("create", "create_account", {"plan": "starter"})
    .agent_step("welcome", psych_runtime.agent("greeter").model("gpt-4o-mini"))
    .build()
)
```

`.agent_step()` and `.subagent()` accept a built Spec or another builder, so a
child can be composed inline without a separate variable.

## Gotchas

- **`.build()` raises `BuilderError` with no model.** Every other field has a
  sensible default; the model does not.
- **`.subagent()` needs a description of at least 20 characters.** The parent's
  delegation tool shows that text to choose between subagents, and vague
  descriptions are the most common cause of bad routing.
- **Step order is preserved; tool order is not.** `steps` is an ordered list.
  Tools, MCP servers, peers, skills and subagents are sorted sets on the Spec,
  so the order you called `.tool()` in does not change the Version hash.
- **The builder is not a DSL.** It produces a Spec and stops. It has no
  execution semantics of its own.
