/**
 * Serve `out/` the way the host does: `/x` maps to `out/x.html`, no
 * single-page fallback, and every matching `_headers` rule applied.
 *
 * The fallback matters. A server that rewrites unknown paths to index.html
 * makes every page under test the homepage, and a sweep over eleven pages
 * then passes eleven times on one of them.
 */
// A static server that mirrors how the export is meant to be served: /x maps
// to out/x.html, with no single-page fallback. `serve -s` rewrites every
// unknown path to index.html, which silently made every page under test the
// homepage.
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

// Apply out/_headers the way Cloudflare would: every rule whose pattern matches
// contributes its headers. Without this the local check tests the pages with no
// CSP at all, which is the one thing worth checking.
const RULES = [];
const ROOT = "out";
{
  const txt = await readFile(join(ROOT, "_headers"), "utf8");
  let cur = null;
  for (const raw of txt.split("\n")) {
    if (!raw.trim() || raw.trimStart().startsWith("#")) continue;
    if (!/^\s/.test(raw)) { cur = { pattern: raw.trim(), headers: [] }; RULES.push(cur); continue; }
    const at = raw.indexOf(":");
    if (cur && at > 0) cur.headers.push([raw.slice(0, at).trim(), raw.slice(at + 1).trim()]);
  }
}
const matches = (pattern, url) => {
  if (pattern.startsWith("http")) return false;
  const re = new RegExp("^" + pattern.split("*").map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$");
  return re.test(url);
};

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".xml": "application/xml", ".txt": "text/plain", ".webmanifest": "application/manifest+json" };

const exists = async (p) => { try { return (await stat(p)).isFile(); } catch { return false; } };

const server = createServer(async (req, res) => {
  const url = decodeURIComponent(new URL(req.url, "http://x").pathname);
  const base = join(ROOT, normalize(url).replace(/^(\.\.[/\\])+/, ""));
  const candidates = [base, `${base}.html`, join(base, "index.html")];
  for (const c of candidates) {
    if (await exists(c)) {
      const body = await readFile(c);
      const headers = { "content-type": TYPES[extname(c)] ?? "application/octet-stream" };
      for (const rule of RULES) {
        if (!matches(rule.pattern, url)) continue;
        for (const [k, v] of rule.headers) {
          headers[k] = headers[k] && k.toLowerCase() !== "content-type" ? `${headers[k]}, ${v}` : v;
        }
      }
      res.writeHead(200, headers);
      return res.end(body);
    }
  }
  const nf = join(ROOT, "404.html");
  const body = (await exists(nf)) ? await readFile(nf) : "not found";
  res.writeHead(404, { "content-type": "text/html" });
  res.end(body);
});

export { server as staticServer };
