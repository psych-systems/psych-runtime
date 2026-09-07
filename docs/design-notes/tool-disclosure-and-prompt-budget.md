# Tool disclosure, large results and the prompt budget

Covers DESIGN.md §10.2 and §10.3 (resolution and catalog cache), §10.7
(unreachable connections), §10.8 (large tool results) and the prompt-cache
discipline in §19. Read it before touching `psych_runtime/tools/deferred.py`,
`psych_runtime/tools/large_results.py` or prompt assembly.

Everything here is one problem seen from three sides: the prompt has a budget,
the model needs to reach things that do not fit in it, and the parts that do fit
must stay byte-stable so the provider's prompt cache keeps hitting.

## Deferred disclosure is a property of a connection

Deferral is per MCP server connection, not per tool call and not an opaque
per-call token. A connection carries a `preload` flag. When it is off, that
server's tool schemas stay out of the system prompt entirely and the model reaches
them through a small set of always-present meta tools:

- list the permitted tool names on one deferred server
- read one tool's description and schemas
- call one tool, addressed as a plain `(server, tool)` pair

Plus one line per deferred server in the prompt saying it is there, capped at a
couple of hundred characters of description. That line is not decoration. A tool
that is silently absent is §10.7's own failure mode: the model cannot ask for what
it does not know exists, and it will confidently tell the user the capability does
not exist.

There is no handle in this scheme. Addressing is the pair, and it is validated by
the same machinery as any other call: an unknown server returns a tool error, an
unknown or unpermitted tool name resolves through the same narrowing an ordinary
call goes through and is refused there. Malformed arguments come back as a
validation error in the tool result, not as a typed corruption error, because a
model getting its arguments wrong is ordinary rather than impossible.

**Discovery is a door, not a bypass.** Listing shows a deferred server's
*permitted* names, and calling through the meta tool re-resolves through narrowing.
A model cannot reach past its grant by going through discovery.

Membership in the deferred set follows the connection's `preload` flag alone. A
server can be deferred and still have a few named tools eagerly injected; it stays
listed as deferred, because the model must still discover the rest.

The default is three-valued and decides from the catalogue size when unset. A
Spec's author cannot know the count, since it is discovered from the server and it
changes, so an explicit default would be a guess. `True` and `False` pin it for an
author who does know.

The payoff is prompt-prefix size, and it is large. A server with several hundred
tools costs megabytes of request bytes per turn when every schema is injected, and
a couple of kilobytes when they are not, with every tool still reachable.

## Disclosure tiers by catalogue size

When a server's permitted tool set is small, one real tool per remote tool with the
schema copied in is the best shape: the model sees ordinary tools. As the count
grows that stops paying, and two other shapes take over:

- a **facade**: one tool for the whole server, whose description carries a
  first-line index of every permitted tool and whose input schema is the generic
  `{tool, input}` pair.
- a **search** pair: one tool that keyword-searches the catalogue and one that
  calls by name, for a catalogue too large even to index in a description.

The tiering is a prompt-budget decision layered on an already-narrowed list. It is
not a security mechanism. Every tier exposes exactly the permitted set, and the
executor re-checks membership at call time anyway, so a stale resolved set cannot
outlive a permission change mid-Run.

## Resolution happens per turn

Resolve the tool surface at the start of every turn, not once at Run start. This is
what makes §23's "an MCP server connected mid-Run is usable on the next turn with no
restart" true.

The alternative, caching the resolved set for the life of the Run, buys stability of
the model's tool surface mid-conversation. That is a reasonable trade for a chat
product and it is the wrong one here: Psych's consumers connect servers from their
own UI while a Run is in flight, and a tool that appears only after a restart looks
broken.

## Catalog cache

The stored catalogue is a versioned, soft-delete table. A refresh writes every tool
it saw stamped with a new catalog version, then marks everything older as removed in
one statement. Tools are never hard-deleted, and the removal marker is what
narrowing's outermost bound filters on. That gives a cheap diff and keeps old
Versions readable.

Four triggers keep it fresh, and they are not interchangeable:

- sync on connect
- a background sweep on a TTL
- refresh on demand
- react to the server's `notifications/tools/list_changed`

On-demand refresh alone is the tempting minimum and it is not enough. It leaves the
catalogue as stale as the last time a human clicked something.

## Large tool results

### Measuring

Thresholds are counted in estimated tokens, using a cheap estimator rather than a
provider tokenizer. A threshold trigger does not need to be accurate, it needs to be
fast and monotone in size.

Keep that estimate away from anything that feeds metering. Cost is computed from
provider-reported usage. An estimate is never allowed to stand in for a billed
number.

### Deciding what to elide

Two thresholds, both in tokens: a per-result one and a per-batch one, with the
per-result threshold never allowed to exceed the batch threshold. That constraint is
checked at publish, not at run.

The algorithm over one turn's tool results:

1. Sum the whole batch. Under the per-result threshold, do nothing. The
   short-circuit is on the *sum*, so many small results that add up still get
   processed.
2. Elide every result that on its own is at or over the per-result threshold.
3. Sort what is left largest first and keep eliding until the running total drops
   under the batch threshold.

So a middling result is elided only when the batch as a whole is over budget and it
is one of the largest remaining.

### What the model sees

A head-and-tail preview, a fixed number of characters from each end joined by an
ellipsis line, plus guidance and a handle for reading the rest. If the content is
short enough that head and tail would overlap, it is returned unchanged; the preview
never truncates something already small.

Failures are treated differently from oversized successes: hard-truncated to a few
hundred characters with no preview marker and no guidance. A failing tool's message
is usually a stack trace, and the useful part is the top.

### The log always holds the whole thing

This is the rule that everything else hangs on. The append-only Record log holds the
full result unconditionally. Only the model's view is trimmed.

The tempting cheaper design writes the full content to a sandbox file and mutates the
result in place before it is persisted, so the original survives only when a sandbox
happens to be configured. That loses data outright for any Run without one, and it
makes recoverability depend on an unrelated capability. Psych does not do it. The
elision is a projection at prompt-assembly time over a log that still holds
everything.

### The reader tool

`read_tool_output(handle, offset, limit, grep)` reads the stored result out of the
log. Its boundary behaviour is defined rather than incidental:

- The **handle** addresses a tool-result Record in *this* Run's log. A handle that
  does not resolve, or resolves into another Run, raises the typed
  `invalid_deferred_handle` corruption error rather than returning a tool error
  string, because a Run calling a handle it never emitted is a protocol violation.
  Resolving across Runs would be a cross-tenant read.
- **offset past the end** returns an empty result, not an error. That matches how
  slicing and `readlines` behave, and an error there would teach the model to avoid
  a perfectly safe probe.
- **grep with no match** returns an empty result, not an error, for the same reason.
- **binary content** is decided explicitly rather than left to whatever the codec
  does. Reading and grepping bytes as if they were text produces confident nonsense.

Do not implement this by shelling into a sandbox with `grep` and `sed`. The reader is
in-process over the log and must work with no sandbox configured at all.

## Prompt assembly and cache stability

The prompt cache pays only for a byte-stable prefix, so assembly is ordered
outermost-stable to innermost-volatile. Four concentric zones:

1. The static system prompt sections: instructions, then the skills index (names and
   descriptions only, bodies load on demand), then the subagent roster, then any
   capability notes.
2. The system prompt's single dynamic tail. Anything per-user and mutable
   mid-conversation, memories being the usual case, goes last inside the system
   prompt so it cannot invalidate anything above it.
3. The conversation history.
4. One trailing transient message appended after the history, never persisted.

The current date and time belongs in zone 4, not in the system prompt. Putting a
clock at the top invalidates the entire prefix on every call, which is the single
most expensive mistake available in this area.

## The elision boundary is a cache decision

Tool outputs from *prior* turns are truncated at prompt-assembly time. The current
turn's outputs are sent in full, because the model just asked for them.

The boundary is deliberately "strictly before the last user message", not "before the
current step". That makes a message's elided-or-not status change exactly once per
turn and then stay byte-stable across every step within the turn, keeping the cached
prefix intact. A boundary that moved per step would re-elide a different message on
every model call and defeat the cache it exists to protect.

Storage is untouched by this pass. Only what is sent this call is trimmed; the
transcript, the report and compaction all still see the original.
