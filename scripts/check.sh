#!/usr/bin/env bash
# The gate (DESIGN.md §22). All of it, in order, all blocking.
#
#   scripts/check.sh          the full gate
#   scripts/check.sh fast     lint and types only, what the pre-commit hook runs
set -euo pipefail
cd "$(dirname "$0")/.."

run() { echo "--- $* "; "$@"; }

run uv run ruff check .
run uv run ruff format --check .
run uv run mypy --strict .
run uv run lint-imports

# Documentation is generated from the library, not written beside it, and this
# is what makes that true rather than aspirational: change a docstring or a
# design note without regenerating and the gate goes red here, before the page
# has a chance to start describing a version that no longer exists.
run uv run python scripts/generate_docs.py --check
# The approval demonstration on the website steps through a log this executes
# rather than one somebody typed, for the same reason: a record type that
# changes here should redden the build, not a page.
run uv run python scripts/generate_site_fixtures.py --check
run node web/scripts/sync-from-repo.mjs --check
run node --test web/scripts/tests/sync-from-repo.test.mjs
run node web/scripts/check-site.mjs

# The console is typed against the library's own vocabulary, so a record kind or
# a suspend reason that changes here breaks it there. CI catches that when it
# builds the playground image, because `next build` typechecks, but that is a
# Docker build away from the change that caused it. Skipped rather than failed
# when the console's dependencies are not installed: plenty of work on the
# library never touches it, and making everyone run `npm install` to lint Python
# would be the wrong trade.
if [[ -x examples/playground/web/node_modules/.bin/next ]]; then
  # `next typegen` first, and not optional. Next generates `PageProps` and
  # `LayoutProps` into .next/types, and the app's pages and root layout
  # annotate themselves with them, so a bare `tsc --noEmit` fails on four
  # "Cannot find name" errors in a checkout that has never been built. That
  # made this step fail for anybody who had actually installed the console's
  # dependencies, which is to say it only passed because it was usually skipped.
  run npm --prefix examples/playground/web run typecheck
else
  echo "--- skipping the console typecheck: examples/playground/web dependencies are not installed"
fi

if [[ "${1:-}" == "fast" ]]; then
  echo "fast checks green"
  exit 0
fi

# pytest exits 5 when a directory collects nothing. A layer is legitimately
# empty only before its first ticket lands, so say so loudly rather than
# passing in silence.
layer() {
  echo "--- pytest tests/$1"
  set +e
  uv run pytest "tests/$1" -q
  local code=$?
  set -e
  if [ $code -eq 5 ]; then
    echo "!!! tests/$1 is EMPTY. Every feature ships with a test in this layer (DESIGN.md section 22)."
    return 5
  fi
  return $code
}

layer unit
layer functional
layer e2e

echo "gate green"
