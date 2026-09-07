#!/bin/bash
# Start the backend and the console, and keep them honest about each other.
#
# Two processes in one container goes against the usual one-process rule, and
# it is the right call for this image. The playground is a demo somebody runs
# for ten minutes: asking them to orchestrate two containers before they can
# see a chat window costs more than the purity is worth. The compose file next
# to this is there for anyone who wants the real shape.
#
# What the rule normally buys is that the container dies when the thing inside
# it dies. That is bought back explicitly below: if either process exits, this
# script kills the other and exits with its status, so a half-dead container
# never sits there answering some requests and not others.

set -euo pipefail

backend_port="${PSYCH_PLAYGROUND_PORT:-8080}"
console_port="${PORT:-3000}"

python /app/backend/run.py &
backend=$!

# Waited for rather than raced. Next starts in well under a second and the
# backend has a store to open, so without this the first page load reliably
# proxies to a closed port and the console opens showing "backend unreachable"
# for a beat. Bounded, because a backend that never comes up should say so and
# stop rather than hang forever on a container start.
for _ in $(seq 1 100); do
    if python -c "
import socket, sys
sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', ${backend_port})) == 0 else 1)
" 2>/dev/null; then
        break
    fi
    if ! kill -0 "${backend}" 2>/dev/null; then
        echo "psych: the backend exited before it started listening" >&2
        wait "${backend}"
        exit $?
    fi
    sleep 0.2
done

node /app/web/server.js &
console=$!

shutdown() {
    # Both, and do not care which is already gone: this runs on SIGTERM from
    # `docker stop`, and a process that has already exited makes `kill` noisy
    # rather than wrong.
    kill -TERM "${backend}" "${console}" 2>/dev/null || true
    wait "${backend}" "${console}" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

echo "psych playground: http://localhost:${console_port}"

# Whichever falls over first ends the container.
wait -n "${backend}" "${console}"
status=$?
kill -TERM "${backend}" "${console}" 2>/dev/null || true
wait "${backend}" "${console}" 2>/dev/null || true
exit "${status}"
