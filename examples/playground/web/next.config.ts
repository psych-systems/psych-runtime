import type { NextConfig } from "next";

/**
 * `PSYCH_API_PROXY` turns this server into the console's only origin.
 *
 * The console and the backend are two servers, and every way of telling the
 * browser where the second one is has been a source of bugs. `NEXT_PUBLIC_*`
 * is baked into the client bundle at **build** time, so an image built once
 * cannot know the port somebody maps it to later; and a second origin drags in
 * CORS and, worse, `SameSite=Lax` cookies that are silently not sent, which
 * looks like being signed in while every request is anonymous.
 *
 * Proxying removes the question rather than answering it. With this set, the
 * page fetches `/api/...` on its own origin, this server forwards to the
 * backend, and there is no second origin at all: no CORS, no cookie rule to
 * fall foul of, and one port to publish. It is how the Docker image runs.
 *
 * `/api` is forwarded by a Route Handler rather than by a rewrite below,
 * because a rewrite buffers and the console reads an SSE stream. See
 * `src/app/api/[...path]/route.ts`.
 *
 * Read here rather than in the bundle, and that is the whole point: Next
 * evaluates this file when the server boots, so the value is a run-time one. A
 * checkout running `next dev` against a backend on 8080 sets nothing and keeps
 * talking to it cross-origin, exactly as before.
 *
 * Both prefixes the backend serves are forwarded: `/a2a` here, `/api` by the
 * Route Handler. `/a2a` is not under `/api` and forgetting it would leave
 * agent cards reachable only by bypassing the console.
 */
const proxyTarget = process.env.PSYCH_API_PROXY?.replace(/\/$/, "");

/**
 * Standalone output: a self-contained server plus only the dependencies it
 * actually imports, which is what keeps the image from carrying the whole of
 * `node_modules`, most of which is build tooling nothing runs.
 *
 * Behind a flag because `next start` refuses to serve a standalone build and
 * says so, so turning it on unconditionally would break the command every
 * contributor runs. The Docker build sets it; nothing else does.
 */
const standalone = process.env.PSYCH_STANDALONE === "1";

const nextConfig: NextConfig = {
  ...(standalone ? { output: "standalone" as const } : {}),
  async rewrites() {
    if (!proxyTarget) return [];
    // `/api` is deliberately not here. A rewrite buffers the response, and
    // this console is built on an SSE stream: with `/api` rewritten, a Run
    // waiting for approval rendered as an empty conversation with no way to
    // approve it, and a working Run showed nothing until it finished.
    // `src/app/api/[...path]/route.ts` forwards it as a stream instead, and
    // says more about why.
    return [{ source: "/a2a/:path*", destination: `${proxyTarget}/a2a/:path*` }];
  },
};

export default nextConfig;
