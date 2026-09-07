# psych_runtime.model

## Owns
The `ModelClient` port, the OpenAI-compatible wire adapter, the usage and pricing
types, cache-deliberate prompt assembly and the transient-failure classifier.

## Does not own
Model routing. A Spec names its model; a fallback list is failover, not routing,
and no partial router belongs here (DESIGN.md §19). No provider SDK dependency:
Psych speaks the wire protocol.

## Ports
Defines `ModelClient` and `PriceResolver`.

## The rules that live here
Usage splits by cache state and is recorded per call, never aggregated at write
time (§13.1). A model with no known price records `cost=None`, never `0`, because
a silent zero makes metering look correct and be wrong (§13.2). Prompt assembly
order is fixed so a mid-conversation change invalidates the shortest prefix; a
change to that order is a performance change and gets reviewed as one (§19).

Every `ModelRequest` normalises its own `tools`: sorted by name, every schema's
keys sorted recursively, arrays left alone. This is bytes, not meaning -- see
`tool_normalize.py` for why it lives on `ModelRequest` rather than in the
resolver that usually builds it.
