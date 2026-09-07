#!/usr/bin/env node
/**
 * Add the noindex header to a preview build's `_headers`.
 *
 * `robots.txt` and a meta tag ask crawlers not to index; `X-Robots-Tag` tells
 * them at the response, which is the one of the three that also covers the
 * PNGs, the JSON and anything else served that is not an HTML page with a head
 * to put a tag in.
 *
 * It appends rather than replacing, so the security headers the production
 * build ships are still there: a preview served under a weaker policy is a
 * preview that cannot tell you the policy works.
 */

import { appendFile, readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "out");
const HEADERS = join(OUT, "_headers");

const RULE = `
# Preview build. Added by scripts/preview-headers.mjs, never present in production.
/*
  X-Robots-Tag: noindex, nofollow
`;

const existing = await readFile(HEADERS, "utf8").catch(() => {
  console.error(
    "preview-headers: out/_headers does not exist. Run `next build` first; " +
      "without it the preview is publicly indexable.",
  );
  process.exit(1);
});

if (existing.includes("X-Robots-Tag")) {
  console.log("preview-headers: already applied");
} else {
  await appendFile(HEADERS, RULE, "utf8");
  console.log("preview-headers: X-Robots-Tag: noindex added to out/_headers");
}
