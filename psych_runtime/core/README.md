# psych_runtime.core

## Owns
The Spec and Version models, the Record types, the pure reducer that folds a
record log into run state, the typed corruption errors, and the two read
projections a consumer serves from: `status.py` (`RunStatus`, what a Run is
doing right now, in the words a screen uses) and `conversation.py` (the
messages, as the model sees them).

`answer.py` is the third: a Run split into what it concluded and how it got
there. The split is derived rather than decided, because asking the model
whether its own question was simple is a judgement it is unreliable at and
does not need to make. The answer is the turn the loop itself finished on
(DESIGN.md §5's one success condition) and the work is everything before it.

`status.py` exists because `RunStateView` is the reducer's own working object:
mutable, carrying bookkeeping whose purpose is to make the next fold cheap.
Serving it to a UI made every consumer write their own projection of it and
decide separately what a status is.

Compaction is half owned here, and the half is the whole read side: the
`CompactionApplied` Record, the boundary and the summaries the reducer folds
out of it, and `conversation.py` standing the summary in for the range it
replaced. Deciding to compact, choosing where to cut and calling a model to
write the summary are writes and belong to `psych_runtime.runtime`. The load-bearing
rule is in the Record's own docstring: the replaced records stay in the log, so
the report, the trace and the audit trail are untouched by a compaction.

`components.py` is the vocabulary an agent answers with when a sentence is
the wrong shape: a card, a carousel, an order, a timeline, a chart, a metric.
It carries data and intent and nothing else -- no colour, no font, no layout,
no markup -- because DESIGN.md §1 refuses a UI and Psych owns the payload while
the consumer owns the drawing. That is also what makes brand control free: the
consumer never receives styling to override because none was ever sent.
A Run's own subagents are folded here too (`reducer.ChildRun`): a background
child's spawn, every message its parent sent it and its ending are Records, so
the whole tree is reconstructable from the log alone and nothing about a running
tree lives in a process (DESIGN.md §17).

## Does not own
Any IO. No store calls, no clock reads, no randomness, no network. `psych_runtime.core`
imports nothing from the other subpackages and import-linter enforces that.

## Ports
Defines none. It is the data and logic layer every other package builds on.

## The rules that live here
A Spec references tools by name and never holds a callable (DESIGN.md §4). The
reducer is pure: same log in, same state out (§6). A log the protocol could not
have produced raises a typed corruption error and is never repaired (§6 rule 6).

`conversation.py` renders a structured (dict/list) tool result for the model
with `separators=(",", ":")`: whitespace `json.dumps` would
otherwise insert is billed on every turn the result stays in context, for
formatting that carries no information. A `str` result is left byte-for-byte
untouched -- it is text the tool itself chose to return, not a structure this
package serialises.
