/**
 * Every route under the policy generated for it.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium } from "playwright";

/**
 * Every exported page, loaded under its own generated CSP, asserting that
 * nothing is blocked. A hash-based policy that is one byte out does not fail
 * the build: it fails in the browser, silently, as a page that does not
 * hydrate. This is the only check that catches that.
 */
const BASE = process.env.BASE ?? "http://localhost:4599";
// A sandbox with a pre-installed browser sets PLAYWRIGHT_CHROMIUM rather than
// letting Playwright download one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
const p = await ctx.newPage();

const violations = [];
await p.addInitScript(() => {
  window.__cspViolations = [];
  document.addEventListener("securitypolicyviolation", (e) => {
    window.__cspViolations.push(`${e.violatedDirective} blocked ${e.blockedURI} (${e.sourceFile ?? "inline"})`);
  });
});
p.on("console", (m) => {
  const t = m.text();
  if (/Content Security Policy|Refused to/i.test(t)) violations.push(`console: ${t.slice(0, 220)}`);
});

const routes = JSON.parse(process.env.ROUTES ?? "null") ?? (await (async () => {
  const { readdir } = await import("node:fs/promises");
  const { join, relative, sep } = await import("node:path");
  const walk = async (d) => {
    const out = [];
    for (const e of await readdir(d, { withFileTypes: true })) {
      const f = join(d, e.name);
      if (e.isDirectory()) out.push(...(await walk(f)));
      else if (e.name.endsWith(".html")) out.push(f);
    }
    return out;
  };
  const files = await walk("out");
  return files.map((f) => {
    const rel = relative("out", f).split(sep).join("/");
    return rel === "index.html" ? "/" : "/" + rel.replace(/(?:\/index)?\.html$/, "");
  });
})());

let hydrated = 0;
for (const route of routes) {
  const response = await p.goto(BASE + route, { waitUntil: "networkidle" });
  const policy = response?.headers()["content-security-policy"];
  if (!policy || !policy.includes("script-src")) {
    violations.push(`${route}: missing Content-Security-Policy response header`);
  }
  const found = await p.evaluate(() => window.__cspViolations ?? []);
  for (const v of found) violations.push(`${route}: ${v}`);
  // Hydration is the thing a broken script-src actually kills: the inline
  // payload never runs, so the flag the layout sets before paint is missing.
  const js = await p.evaluate(() => document.documentElement.dataset.js);
  if (js === "on") hydrated++;
  else violations.push(`${route}: did not hydrate (data-js=${js})`);
}

console.log(`checked ${routes.length} routes under their generated CSP; ${hydrated} hydrated`);
console.log("\n=== CSP VIOLATIONS ===");
console.log(violations.length ? violations.slice(0, 25).join("\n") : "none");
await b.close();
process.exit(violations.length ? 1 : 0);
