/**
 * Generate hash-based CSP headers for every exported page without using
 * `script-src 'unsafe-inline'`.
 *
 * Next.js gives each page different inline RSC payloads. One rule per page
 * would exceed Cloudflare's 100-rule limit, so related documentation routes
 * share hashes. Large sections split by the first letter of the page slug.
 * The build fails before Cloudflare's 100-rule or 2,000-character limits.
 */
import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { join, relative, resolve, sep } from "node:path";

const OUT = resolve(process.cwd(), "out");
const HEADERS = join(OUT, "_headers");
const MAX_LINE = 2000;
const MAX_RULES = 90;
const BASE_CSP =
  "default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; " +
  "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; " +
  "base-uri 'self'; form-action 'self'; object-src 'none'";

async function htmlFiles(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await htmlFiles(full)));
    else if (entry.name.endsWith(".html")) out.push(full);
  }
  return out;
}

function routeFor(file) {
  const rel = relative(OUT, file).split(sep).join("/");
  if (rel === "index.html") return "/";
  return `/${rel.replace(/(?:\/index)?\.html$/, "")}`;
}

function inlineScriptHashes(html) {
  const hashes = new Set();
  for (const [, body] of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) {
    hashes.add(`sha256-${createHash("sha256").update(body, "utf8").digest("base64")}`);
  }
  return hashes;
}

function patternFor(route) {
  const match = route.match(
    /^\/docs\/(next|v\d+\.\d+)\/(changelog|concepts|design|development|get-started|guides|reference)(?:\/(.+))?$/,
  );
  if (!match) return route;
  const [, version, section, child] = match;
  const base = `/docs/${version}/${section}`;
  if (["changelog", "concepts", "development", "get-started"].includes(section)) {
    return `${base}*`;
  }
  if (!child) return base;
  const first = child[0].toLowerCase();
  const width = section === "reference" && first === "r" ? 2 : 1;
  return `${base}/${child.slice(0, width).toLowerCase()}*`;
}

const files = await htmlFiles(OUT);
if (files.length === 0) {
  throw new Error("csp-headers: out/ has no HTML. Run `next build` first.");
}

const grouped = new Map();
for (const file of files) {
  let html = await readFile(file, "utf8");
  html = html.replace(/\s*<meta http-equiv="Content-Security-Policy"[^>]*>/gi, "");
  await writeFile(file, html, "utf8");
  const pattern = patternFor(routeFor(file));
  const hashes = grouped.get(pattern) ?? new Set();
  for (const hash of inlineScriptHashes(html)) hashes.add(hash);
  grouped.set(pattern, hashes);
}

let largestLine = 0;
const generated = [];
for (const [pattern, hashes] of [...grouped].sort(([a], [b]) => a.localeCompare(b))) {
  const sources = [...hashes].sort().map((hash) => `'${hash}'`).join(" ");
  const line = `  Content-Security-Policy: ${BASE_CSP}; script-src 'self' ${sources}`;
  if (line.length > MAX_LINE) {
    throw new Error(
      `csp-headers: the header for ${pattern} is ${line.length} characters, over ` +
        `Cloudflare's ${MAX_LINE} character limit. Split that route group further.`,
    );
  }
  largestLine = Math.max(largestLine, line.length);
  generated.push(`${pattern}\n${line}`);
}

let headers = await readFile(HEADERS, "utf8");
headers = headers.replace(/\n?# BEGIN GENERATED CSP[\s\S]*?# END GENERATED CSP\s*/m, "\n");
const sharedRules = headers
  .split(/\r?\n/)
  .filter((line) => line && !/^\s/.test(line) && !line.startsWith("#")).length;
const ruleCount = sharedRules + generated.length;
if (ruleCount > MAX_RULES) {
  throw new Error(`csp-headers: ${ruleCount} _headers rules, over the ${MAX_RULES} build limit`);
}

headers = `${headers.trimEnd()}\n\n# BEGIN GENERATED CSP\n${generated.join("\n\n")}\n# END GENERATED CSP\n`;
await writeFile(HEADERS, headers, "utf8");
console.log(
  `csp-headers: ${files.length} documents in ${generated.length} policy rules; ` +
    `${ruleCount}/${MAX_RULES} total rules, largest header ${largestLine}/${MAX_LINE} chars`,
);
