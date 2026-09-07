---
name: psych-http-tools
description: >-
  Give a Psych agent an HTTP API to call with `psych_runtime.HttpTool`: URL templating,
  how arguments map to path, query or JSON body per method, credentials through
  the SecretResolver port, and wiring `http=` on the Runtime. Use whenever
  someone wants an agent to call a REST endpoint without writing a Python
  function, wants end users to create tools at runtime with no redeploy, asks
  where an API key goes in a Spec, or hits "header cannot be set literally in a
  Spec" or an HttpTool that fails every call. Read before adding any HTTP tool,
  because a Spec is exported and reviewed in git, so a literal Authorization
  header is refused by design, and because an HttpTool with no `http=` on the
  Runtime publishes fine and then fails every call.
---

# HTTP tools

An `HttpTool` is a URL, a method, a JSON schema and a credential **name**.
Entirely data, which means an end user can create one at runtime through your
console with no code and no redeploy. That is the whole reason it exists as a
separate kind from a code tool.

```python
psych_runtime.HttpTool(
    name="check_stock",
    description="Check warehouse stock for a SKU.",
    url="https://api.example.com/warehouses/{warehouse}/stock/{sku}",
    method="GET",
    input_schema={
        "type": "object",
        "properties": {
            "warehouse": {"type": "string"},
            "sku": {"type": "string"},
            "include_reserved": {"type": "boolean"},
        },
        "required": ["warehouse", "sku"],
    },
    headers={"X-Api-Version": "2026-04"},
    credential="warehouse-api-key",
    timeout_seconds=30.0,
    interruptible=True,
)
```

## Where arguments go

The model sends one flat JSON object. Three rules, applied in order:

1. **Path parameters.** Every `{name}` token in `url` is filled from
   `arguments[name]`, URL-escaped, and removed from what is left. A token with
   no matching argument is a permanent failure rather than a retry, because
   sending the identical incomplete call again will not fix it.
2. **GET and DELETE.** Everything left becomes query string parameters.
3. **POST, PUT and PATCH.** Everything left becomes the JSON request body, as
   one object.

There is no way to mix a query parameter into POST, PUT or PATCH. A tool that
needs both states one value as a path parameter instead. The trade-off is
deliberate: you cannot express every possible API shape, and in exchange an end
user creating a tool at runtime never has to learn a second schema dialect to
say where an argument goes. This is not OpenAPI parameter binding: no
`in: header`, no `in: cookie`, no per-property override.

## Credentials, and the header that is refused

`credential` names a value your `SecretResolver` resolves **fresh for the
calling Scope** on every call. It is resolved and sent as
`Authorization: Bearer <value>`.

Setting `authorization`, `proxy-authorization` or `cookie` literally in
`headers` raises at validation. Specs are exported, reviewed in git and stored
unencrypted, so a literal credential in one is a credential in your repository.
If your API wants a different header shape, put the non-secret template in
`headers` and keep the secret half in `credential`, or front the call with a
proxy that adds the header it wants.

## Wiring it

An `HttpTool` needs an executor on the `Runtime`:

```python
from psych_runtime.tools.http import HttpToolExecutor

runtime = psych_runtime.Runtime(
    store=store,
    model=model,
    registry=registry,
    http=HttpToolExecutor(psych_runtime.HttpTransport(), secrets),
)
```

Without `http=`, a Spec naming an HTTP tool publishes fine and then fails every
call as data. That is the honest answer for a Runtime never given a way to make
the request. Pass a `ValidationContext` saying so and publish-time validation
refuses the Spec instead, which is the outcome you want.

Every request goes through `HttpTransport`, the same egress seam the model
client uses, so your egress policy sees it. Never construct an `httpx` client
for a tool: that is a second, uncontrolled path to the network, and a control
covering three of four routes is worse than none.

## Results and failures

A response the server calls JSON, small enough to fit the executor's cap, comes
back parsed. Otherwise the raw text does. A non-2xx becomes an `HttpToolError`
carrying `status_code`, and the agent loop classifies it: 5xx, 429 and 408
retry against `Limits.transient_retry_budget`, and a 4xx does not, because
sending the same bad request again will not help.

## Code tool or HTTP tool

Use an **HTTP tool** when the call is a plain request against an API and you
want it to be data an end user can author. Use a **code tool** when you need
retries with your own semantics, response shaping, a client library, or
anything the three argument-placement rules cannot express.

## Gotchas

- **`description` is required and has no default.** There is no docstring to
  fall back on.
- **`input_schema` is your responsibility.** Unlike a code tool, nothing derives
  it from type hints. An empty schema means the model has no idea what to send.
- **`interruptible=False` for anything with a side effect that must not be
  half-done,** the same rule as code tools.
- **`timeout_seconds` caps at 600 and `url` at 2048 characters.**
