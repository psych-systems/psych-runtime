# psych_runtime.builder

## Owns
The typed Python builder that produces Specs, one of the four authoring forms in
DESIGN.md §4 and privileged over none of the others.

## Does not own
A DSL with its own execution semantics. The builder produces a Spec and stops.

## Ports
Defines none.

## The rule that lives here
The builder may accept a function and register it as a side effect, but what
lands in the Spec is the registered name. The same agent built in Python and
built from a dict must produce the same Version hash (§23).
