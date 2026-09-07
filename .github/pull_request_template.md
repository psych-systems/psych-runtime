<!-- CONTRIBUTING.md says what a change has to pass. Read it first. -->

## Problem

<!-- What was wrong, or missing, and for whom. Link the issue if there is one. -->

## What changes for a user of the library

<!--
Behaviour before and after. "None, internal only" is a fine answer.
If this touches DESIGN.md, say which section and whether the design changes;
a change to the design is a discussion before it is a commit.
-->

## How it is verified

<!--
Name the end-to-end case that fails when this feature breaks. A unit test
alone does not close a feature. If you ran only part of the gate, say which
part and why.
-->

## Documentation

<!--
Which skill, README, design note or docstring changed with this, and whether
you regenerated the docs (`scripts/check.sh fast` fails if you did not).
-->

## Compatibility

<!--
Breaking change? Then it is in CHANGELOG.md under Unreleased, with the
deprecation warning CONTRIBUTING.md describes, unless it is a security fix.
-->

- [ ] `scripts/check.sh` is green against real Postgres, MySQL and DynamoDB Local
- [ ] No test makes a real network call
- [ ] Breaking changes are in `CHANGELOG.md` now, not reconstructed later
- [ ] Generated docs and fixtures are regenerated
