# psych_runtime.testing

## Owns
The scriptable fake model, and a stub MCP server, exported for consumers to test
their own agents against.

## Does not own
Any test that reaches the network. Every external call goes through an injectable
seam: `ModelClient`, `fetch_impl`, `Sandbox` (DESIGN.md §22).

## Ports
Implements `ModelClient` as `psych_runtime.testing.fake_model.FakeModel`.

## The rule that lives here
The fake scripts multi-step tool-calling runs including malformed tool calls,
unknown tool names, stalled streams, mid-token aborts, transient and permanent
provider failures at a settable status code, reasoning, per-turn usage with
cache fields, and a settable finish reason. A weak fake produces a weak test
suite, so the fake's own coverage is part of the gate (`test_fake_model.py`).

`FakeModel` records every `ModelRequest` it receives on `.requests`, so a test
can assert on prompt assembly and on the tool set actually being recomputed
each turn. Running `stream()` past the end of the script raises
`FakeModelScriptExhausted` rather than repeating the last turn, so a runaway
tool-calling loop fails the test instead of passing it by accident.

## The stub MCP server
`psych_runtime.testing.mcp_stub` serves the protocol over a real loopback socket:
`McpStubServer` records what it was asked on `.requests` and `.calls`, and can
be told to stall, to break a stream mid-response, to demand OAuth, or to return
each documented error code. `make_server` and `wire_tool` assemble a Spec
against one.

It lives here rather than in the test suite because it has two consumers and
only one of them is a test. `examples/playground` drives it for the capability
scenarios a person clicks through, and it used to import the class out of
`tests/functional/test_mcp.py` -- which cannot work in the playground's own
image, where `.dockerignore` keeps `tests` out of the build context. Nothing in
the module imports pytest, so depending on it costs an application nothing.
