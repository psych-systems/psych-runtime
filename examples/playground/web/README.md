# Psych playground web

The console for the playground backend: talk to an agent, build one, connect
the systems it can reach, and read back what any conversation actually did.

This is the "platform" DESIGN.md §1 says Psych deliberately does not ship. Every
screen here is something a consumer builds for themselves; none of it lives in
`psych_runtime`. It exists so the runtime can be driven by hand, and so the shape of a
product built on Psych is written down somewhere rather than imagined.

## Run it

```sh
npm install
npm run dev            # http://localhost:3000
```

It talks to the backend at `http://localhost:8080`. Point it elsewhere with
`NEXT_PUBLIC_PSYCH_API`:

```sh
NEXT_PUBLIC_PSYCH_API=http://127.0.0.1:9000 npm run dev
```

Start the backend first (`examples/playground/backend/README.md`); the app says
plainly when it cannot reach one, with the URL it tried, rather than rendering
an empty console.

## The five surfaces, and the line between them

| Route | For |
|---|---|
| `/chat` | Talking to an agent. One conversation at a time. |
| `/agents` | What you can talk to, and building another. |
| `/connections` | The systems agents reach: MCP servers, their live state, the credentials they use by name. |
| `/activity` | Every conversation with its outcome, and the door to one run's trace and usage. |
| `/settings` | Model access, secrets, appearance. |
| `/capabilities` | Separate on purpose: proving the runtime does what it claims, for someone evaluating it rather than using it. |

**Technical identifiers do not appear on everyday screens.** Run ids, version
hashes, attempt ids and sequence numbers are real and an operator needs them, so
they are never hidden outright. They sit behind the `TechnicalDetails`
disclosure (`src/components/ui/page.tsx`), under the same label on every surface
that has any, and they are first-class content on `/activity` where that is the
whole point of the page. A person asking an agent about an order should not have
to read a content hash to do it.

## What the shared pieces are

- `src/lib/api.ts`, the one client. Every request in the app goes through it,
  so the base URL, the error shape and the auth story change in one place.
  `ApiError` carries the backend's own `detail` and per-field `issues`;
  `BackendUnreachableError` is the distinct case of no answer at all.
- `src/lib/types.ts`, the wire contract, mirroring `backend/app/schemas.py`
  field for field, including which fields are null and why. A `cost` of `null`
  means "no known price", never zero; rendering `$0.00` there is a bug.
- `src/components/ui/page.tsx`, `Page`, `PageHeader`, `Section`, `EmptyState`,
  `Stat`, `TechnicalDetails`, `DetailRow`. Page furniture, once, so six screens
  do not answer "how much space goes under a title" six ways.
- `src/components/ui/status.tsx`, one status vocabulary: queued, running,
  waiting, stopping, done, failed, stopped. These are the words
  `psych_runtime.status()` itself answers with, so the console and the runtime cannot
  disagree about whether a run is waiting on a person.
- `src/app/globals.css`, the design tokens. One type scale (`text-micro`
  through `text-prose`), status colours that are their own tokens rather than
  aliases of the chart palette, and a light and dark palette.

## Streaming, and why not `EventSource`

`streamRun` reads the SSE body directly instead of using `EventSource`, because
`EventSource`'s built-in reconnect always replays from the URL it was
constructed with. For this endpoint that means replaying from the original
`after=` and duplicating every record already on screen. Reading the body
lets the caller reconnect with `after=<the last sequence it saw>`, which is what
DESIGN.md §12 promises and what `useRunStream` does.

A clean `event: done` on a run that has not settled is a backend idle timeout
rather than the end of the conversation, so the stream reattaches instead of
treating it as terminal.

## Conventions

- TypeScript strict, no `any`, no `# type: ignore` equivalents.
- Comments explain intent and trade-offs. Several of them name the specific bug
  the code prevents; keep that, it is why they are worth reading.
- `npm run lint` and `npx tsc --noEmit` are both expected green before a commit.
