# psych_runtime.telemetry

## Owns
The `Telemetry` port with a no-op default, the OpenTelemetry adapter using
`gen_ai.*` semantic conventions, the span schema and its conformance tests.

## Does not own
A collector, an exporter configuration or a dashboard.

## Ports
Defines `Telemetry`.

## The rules that live here
Span names and attributes are declared in a schema and checked by conformance
tests, so spans cannot drift from their contract as the code changes
(DESIGN.md §13.5).
