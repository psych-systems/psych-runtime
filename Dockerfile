# The Psych playground: the console and the backend it talks to, in one image.
#
# This packages `examples/playground`, which is a consumer application. It is
# not how Psych is used. Psych is a library that a company imports into their
# own service, and DESIGN.md §1 refuses an HTTP server and a UI on purpose;
# nothing in this file puts either into `psych`. The library ships as a wheel.
# This image exists so somebody can see the thing working before reading any
# code.
#
#   docker build -t psych-playground .
#   docker run --rm -p 3000:3000 -v psych-data:/data psych-playground
#
# One published port, and that is deliberate. The console proxies `/api` and
# `/a2a` to the backend on loopback inside the container, so the browser only
# ever talks to one origin. Publishing two ports would mean the client bundle
# had to know the second one at build time, which it cannot: see
# `examples/playground/web/next.config.ts` for the whole argument.

# ---------------------------------------------------------------------------
# The console, built once.
# ---------------------------------------------------------------------------
FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS web

WORKDIR /build
COPY examples/playground/web/package.json examples/playground/web/package-lock.json ./
# `npm ci` rather than `npm install`: it installs exactly the lockfile and
# fails if the two disagree, so a build cannot quietly resolve a different tree
# from the one that was tested.
RUN npm ci

COPY examples/playground/web/ ./
# Empty on purpose, and read at `src/lib/api.ts`: an empty base means "this
# origin". Baking a host and port here is the thing that breaks the moment
# somebody maps a different port, which is why the console proxies instead.
ENV NEXT_PUBLIC_PSYCH_API=""
ENV NEXT_TELEMETRY_DISABLED=1
# See `next.config.ts`: standalone output is behind a flag so that `next start`
# keeps working for contributors, and this is the one build that wants it.
ENV PSYCH_STANDALONE=1
# Set in both stages, and both are needed for different reasons.
#
# Here, at build time, because `next.config.ts` resolves the `/a2a` rewrite
# when the config is evaluated and a standalone build freezes the result into
# the server it emits.
#
# Again in the runtime stage below, because `/api` is no longer a rewrite: it
# is forwarded by a Route Handler that reads this variable per request, so that
# the backend's SSE stream reaches the browser as it is written instead of when
# it ends. See `examples/playground/web/src/app/api/[...path]/route.ts`.
#
# The address is the backend on loopback *inside* the container, the same in
# every container that runs this image. The address that varies is the one
# outside, and that is the host's port mapping, which nothing in the bundle has
# to know.
#
# The consequence to keep in mind: the runtime stage pins
# PSYCH_PLAYGROUND_PORT to match. Changing one without the other leaves the
# console proxying to a closed port.
ENV PSYCH_API_PROXY=http://127.0.0.1:8080
RUN npm run build

# ---------------------------------------------------------------------------
# Python dependencies, resolved into a virtualenv that gets copied whole.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS deps

# Build tools live here and nowhere near the final image. A wheel that needs
# compiling gets compiled once, and the compiler stays behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv

WORKDIR /src
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Manifests first, so a change to source code does not re-resolve every
# dependency. `--no-install-project` because the project itself is copied in
# below and installing it here would defeat the cache split.
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
RUN uv sync --frozen --no-dev --extra playground --extra mysql --extra dynamodb --no-install-project

COPY psych_runtime/ ./psych_runtime/
COPY .agents/skills/ ./.agents/skills/
# mysql and dynamodb alongside playground. The playground extra carries asyncpg
# and calls it "optional PostgresStore wiring", but the console is not optional
# about any of the four: app/scenarios/four_stores.py imports PostgresStore,
# MySQLStore and DynamoDBStore at module scope, so the backend cannot reach its
# first line without all three drivers present. Without these the image built,
# started, and died on ModuleNotFoundError: No module named 'aioboto3'.
RUN uv sync --frozen --no-dev --extra playground --extra mysql --extra dynamodb

# ---------------------------------------------------------------------------
# What actually ships: two runtimes, no toolchain.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

# Node is needed at run time because the console is a server-rendered Next
# app, not a static bundle. The npm CLI and the build toolchain are not.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 psych

# Use the same Node major that built the Next.js server. Debian bookworm's
# nodejs package is Node 18, below Next.js 16's supported runtime version.
COPY --from=web /usr/local/bin/node /usr/local/bin/node

# PYTHONPATH carries /app because the venv copied from `deps` cannot supply
# `psych_runtime` by itself. `uv sync` installs the project it is run on as an
# editable install, so /opt/venv holds a pointer back to /src/psych_runtime --
# a path that exists in that stage and in no other. Copying the venv whole
# moves the pointer and not the thing it points at, and `import psych_runtime`
# failed at the first line of the backend that needed it.
#
# The source is already here, at /app/psych_runtime, one COPY below. What was
# missing is that nothing put /app on the path: run.py lives at /app/backend, so
# sys.path[0] is /app/backend and its parent was never searched. This is the
# smaller of the two fixes -- the other is making the install non-editable,
# which turns on a uv flag this pinned version may or may not have -- and it
# gives the existing COPY the job it was plainly copied for.
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY --chown=psych:psych psych_runtime/ /app/psych_runtime/
COPY --chown=psych:psych examples/playground/backend/ /app/backend/
COPY --chown=psych:psych LICENSE NOTICE /app/

# Next's standalone output: a server plus only what it imports. `static` and
# `public` are not inside it and have to be placed by hand, which is the one
# fiddly part of this mode and the reason a missing stylesheet is the usual
# first symptom of getting it wrong.
COPY --from=web --chown=psych:psych /build/.next/standalone /app/web/
COPY --from=web --chown=psych:psych /build/.next/static /app/web/.next/static
COPY --from=web --chown=psych:psych /build/public /app/web/public

COPY --chown=psych:psych docker/entrypoint.sh /app/entrypoint.sh
RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

# Everything that must outlive the container. Owned by the runtime user, since
# a volume mounted over a root-owned directory is the classic way a non-root
# image fails on its first write.
RUN mkdir -p /data && chown psych:psych /data
VOLUME ["/data"]

ENV PSYCH_API_PROXY=http://127.0.0.1:8080 \
    PSYCH_PLAYGROUND_HOST=127.0.0.1 \
    PSYCH_PLAYGROUND_PORT=8080 \
    PSYCH_PLAYGROUND_STATE_FILE=/data/state.json \
    PSYCH_PLAYGROUND_INDEX_FILE=/data/index.json \
    PSYCH_PLAYGROUND_MEMORY_FILE=/data/memory.json \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The backend binds loopback, so nothing reaches it except through the
# console's proxy. One way in, one place to reason about.
EXPOSE 3000

USER psych

# tini as pid 1 so signals reach both children and neither is left as a
# zombie. Without it `docker stop` waits out its ten seconds and then kills the
# container, which reads as the app hanging on shutdown.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=2).status == 200 else 1)"
