# The Psych playground

A console you can click through: publish an agent, talk to it, watch the record
log, read the report. It exists so somebody can see Psych working before
reading any of it.

**It is not how Psych is used.** Psych is a library. A company imports it into
their own service, and DESIGN.md §1 refuses an HTTP server and a UI on purpose.
This directory is a consumer application that supplies both, and it is the only
place in this repository where either appears.

## Run it

```sh
docker compose up --build        # from a clone of this repository
# or, from the first tagged release:
# docker run --rm -p 3000:3000 -v psych-data:/data ghcr.io/psych-systems/playground
```

Then http://localhost:3000. Nothing else to install and nothing to configure:
sign up, and the Settings page is where a model provider goes. The console
treats having no provider as a designed state rather than an error, so it works
before you have one.

Runs are held in memory by default and do not survive a restart, and the
console says so. For a real database:

```sh
docker compose up --build
```

Same image, PostgreSQL behind it, and the volume keeps the settings and the
agent index across restarts. It also brings up Jaeger: open
http://localhost:16686, pick the `psych-playground` service, and a Run is there
as a span tree, with its Attempt, each Turn, and the model, tool and compaction
calls underneath.

## Traces

Psych opens spans on every Run, Attempt, Turn, model call, tool call and
compaction, and throws them away unless a consumer supplies a `Telemetry`.
That default is deliberate on the library's side: choosing an exporter is a
deployment's decision, not a library's.

This backend supplies one. Unset `PSYCH_PLAYGROUND_OTLP_ENDPOINT` and nothing
collects, which is what keeps `docker run` a single command; set it to an
OTLP/HTTP base such as `http://localhost:4318` and spans go there.
`docker compose up` sets it for you.

The console's Trace panel says which of the two you are in, because "nothing
is collecting" and "something is, but not where you are looking" are different
problems that look identical from a screen mentioning neither. That panel is
drawn from the record log rather than from a trace backend, so it works either
way; the spans are for your own pipeline.

Log lines are the same arrangement. Psych attaches a `NullHandler` and
configures nothing else, so this backend decides: `PSYCH_PLAYGROUND_LOG_LEVEL`,
`INFO` by default, which is where the lease, supervisor and notification lines
an operator wants live.

## Why Docker rather than uvx or npx

The playground is two runtimes in one product: a Python 3.12 backend and a
server-rendered Next.js console. Every language-native launcher has to solve
that seam and none of them solves it well.

`npx @psych/playground` is the shape people expect and the console is already a
Node project, but the backend is Python. An `npx` entry point that then needs a
Python interpreter, `uv` and a virtualenv has moved the setup rather than
removed it, and on a machine with only Node it fails in a way that reads as a
broken package.

`uvx psych-playground` is honest about the runtime and `uv` installs its own
Python, but it has to carry a prebuilt Node bundle inside a Python wheel. That
works and it is an odd shape, and it makes the wheel large for every consumer
who only ever wanted the library.

An image carries both, pinned, and the host needs nothing but Docker. It also
makes the two artifacts say what they are: **the wheel is the library, the
image is the demo.** Nobody should conclude from a `docker run` that Psych is a
server.

What Docker does not remove: the host needs Docker, which is one more thing for
somebody used to `npx`; state needs the volume or `docker rm` looks like data
loss; and the image has to be built for both `linux/amd64` and `linux/arm64` or
every Apple Silicon machine runs it under emulation and Psych's first
impression is that it is slow.

## One port, and why

The container publishes 3000 and nothing else. The console proxies `/api` and
`/a2a` to the backend on loopback inside the container, so the browser only
ever talks to one origin.

That is not tidiness. `NEXT_PUBLIC_PSYCH_API` is baked into the client bundle
at **build** time, so an image built once cannot know the port somebody maps it
to later. Telling the browser about a second origin also drags in CORS and, far
worse, `SameSite=Lax` cookies that are silently not sent: sign-in returns 200,
the console shows you as signed in, and every request after it is anonymous.
Proxying deletes the question instead of answering it. See
[`web/next.config.ts`](web/next.config.ts).

## Working on it

The image is for running the playground, not for developing it. From a
checkout:

```sh
uv sync --all-extras --group dev
scripts/dev-services.sh start                              # optional: real Postgres
uv run --extra playground python examples/playground/backend/run.py
```

and in a second shell:

```sh
cd examples/playground/web
npm install
npm run dev
```

The console runs on 3000 and talks to the backend on 8080 cross-origin, which
is why the backend's CORS allowlist covers 3000 and 3010. `next dev` and `next
start` both keep working: standalone output and the API proxy are behind
`PSYCH_STANDALONE` and `PSYCH_API_PROXY`, and only the Docker build sets them.

Configuration is documented by [`backend/app/config.py`](backend/app/config.py),
which is what actually runs. The backend's own [README](backend/README.md)
covers the API.
