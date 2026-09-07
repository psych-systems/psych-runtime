/**
 * Forward `/api/*` to the backend, without buffering the response.
 *
 * This used to be a `rewrites()` entry in `next.config.ts`, and a rewrite
 * buffers: measured against this backend, a Next server with the rewrite
 * delivered **zero** SSE frames in six seconds where the backend itself
 * delivered seven immediately, releasing the whole body only when the upstream
 * response ended.
 *
 * The console is built on that stream. `useRunStream` reads
 * `GET /api/runs/{id}/stream` and the conversation is rebuilt from the records
 * it yields, so buffering it meant:
 *
 * - a Run waiting for approval rendered as an empty conversation, with no
 *   approval card and therefore no way to approve it -- the console's whole
 *   reason for existing, unreachable in the shipped image;
 * - a Run still working showed nothing until it finished, so every answer
 *   arrived at once instead of as it was written;
 * - only a settled Run rendered at all, because only a settled Run's stream
 *   ends and lets the buffer go.
 *
 * A Route Handler that returns the upstream `Response.body` hands the browser
 * the same stream the backend is writing, chunk for chunk.
 *
 * `/a2a` stays on a rewrite in `next.config.ts`. Nothing the console itself
 * draws comes through it, and leaving it there keeps this file to the one
 * prefix whose buffering was doing damage.
 */

const TARGET = process.env.PSYCH_API_PROXY?.replace(/\/$/, "");

/**
 * Hop-by-hop headers, plus the two that describe a body this process is about
 * to re-frame. Forwarding `content-length` from a response we stream, or
 * `content-encoding` for bytes `fetch` has already decoded, is how a proxy
 * produces a truncated body that looks like a network fault.
 */
const DROP_FROM_RESPONSE = new Set([
  "connection",
  "content-encoding",
  "content-length",
  "keep-alive",
  "transfer-encoding",
]);

const DROP_FROM_REQUEST = new Set(["connection", "host", "content-length"]);

async function forward(request: Request, path: string[]): Promise<Response> {
  if (TARGET === undefined) {
    // No proxy configured: the console is talking to the backend directly
    // (`NEXT_PUBLIC_PSYCH_API`), so nothing should be asking this origin for
    // `/api`. Saying so beats a confusing 500.
    return Response.json(
      { detail: "This console is not configured to proxy the backend." },
      { status: 404 },
    );
  }

  const incoming = new URL(request.url);
  const target = `${TARGET}/api/${path.map(encodeURIComponent).join("/")}${incoming.search}`;

  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!DROP_FROM_REQUEST.has(key.toLowerCase())) headers.set(key, value);
  });

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    // Streaming a request body without this throws in undici. Harmless for the
    // bodies this console sends, and required the moment one is a stream.
    ...(hasBody ? { duplex: "half" } : {}),
    redirect: "manual",
    cache: "no-store",
  } as RequestInit);

  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!DROP_FROM_RESPONSE.has(key.toLowerCase())) out.set(key, value);
  });
  // `Headers` collapses repeated Set-Cookie into one comma-joined value, which
  // is not a thing a browser can parse back into two cookies. This is the one
  // header that has to be copied out separately.
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

type Context = { params: Promise<{ path: string[] }> };

const handler = async (request: Request, context: Context): Promise<Response> =>
  forward(request, (await context.params).path);

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const HEAD = handler;
export const OPTIONS = handler;

/** Never prerendered or cached: every request here is somebody's own session. */
export const dynamic = "force-dynamic";
