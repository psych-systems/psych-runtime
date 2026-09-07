# psych_runtime.report

## Owns
The run report: a typed projection over the record log giving the Spec version
and hash, the resolved system prompt as sent, every step, tool call and model
call in order, usage split by cache state, computed cost, the latency breakdown,
every suspension and resume, the terminal state, and the subagent tree with
each branch's tokens and cost rolled up (`subtree`).

`totals` covers one Run alone and `subtree` covers it plus every descendant.
Both are here because both get asked for: "what did this agent cost" and "what
did this request cost" stop being the same question the moment a tree exists.

## Does not own
Storage. The report is a projection and never a parallel copy of the log
(DESIGN.md §6).

## Ports
Defines none.

## Why it ships with Psych
It is the most visible thing a consumer gets on day one. If every consumer writes
their own projection they will each get the token arithmetic wrong in a different
way (§13.4).
