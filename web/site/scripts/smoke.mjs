/**
 * A smoke test against the deployed site.
 *
 *     cd web && pnpm smoke                       # the preview
 *     cd web && pnpm smoke https://psychruntime.com
 *
 * Everything else that checks this site runs against `out/` behind a local
 * server. This one runs against what the host actually serves, because that is
 * where the two can disagree: a header rule the local imitation applies
 * differently, an asset that resolves locally and 404s at the edge, a redirect,
 * a cache rule.
 *
 * It has two halves, and the split is deliberate rather than convenient.
 *
 * The headers are read from the origin directly. This confirms the generated
 * route rule was accepted by the host rather than dropped at deployment.
 *
 * The browser half runs against a local server that fetches each request from
 * that same origin and replays the response verbatim, because a sandboxed CI
 * or agent environment often cannot give a browser direct egress. The bytes
 * and the headers are the deployed ones either way; what the substitution
 * gives up is the edge's own TLS and HTTP layer, which the first half covers.
 *
 * Set SMOKE_DIRECT=1 to point the browser at the origin instead, when the
 * environment allows it.
 */
import { chromium, devices } from "playwright";
import { createServer } from "node:http"
const replay = createServer(async (req, res) => {
  const url = ORIGIN + req.url;
  try {
    const upstream = await fetch(url, { redirect: "manual", headers: { "user-agent": req.headers["user-agent"] ?? "" } });
    const headers = {};
    upstream.headers.forEach((v, k) => {
      // Everything except hop-by-hop and the transport encoding, which Node has
      // already undone by the time the body reaches us.
      if (!["content-encoding", "content-length", "transfer-encoding", "connection"].includes(k)) headers[k] = v;
    });
    const body = Buffer.from(await upstream.arrayBuffer());
    res.writeHead(upstream.status, headers);
    res.end(body);
  } catch (e) {
    res.writeHead(502, { "content-type": "text/plain" });
    res.end(`edge-proxy: ${String(e)}`);
  }
});

const HEADERS_BASE = process.argv[2] ?? process.env.HEADERS_BASE ?? "https://preview.psychruntime.com";
const ORIGIN = HEADERS_BASE;
const IS_PREVIEW = new URL(HEADERS_BASE).hostname.startsWith("preview.");
const PORT = Number(process.env.SMOKE_PORT ?? 4600);
if (!process.env.SMOKE_DIRECT) replay.listen(PORT);
const BASE = process.env.SMOKE_DIRECT ? HEADERS_BASE : `http://localhost:${PORT}`;
// Chromium does not read HTTPS_PROXY; it needs --proxy-server. The proxy's CA
// is already in the browser's NSS store, so TLS verification stays on.
// PLAYWRIGHT_BROWSERS_PATH is honoured when set, which is how a sandbox with a
// pre-installed browser and no download avoids fetching one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const fail = [];
const note = (m) => console.log(m);
const check = (c, m) => (c ? note(`  ok   ${m}`) : (fail.push(m), note(`  FAIL ${m}`)));

// ---------------------------------------------------------------- headers
note(`\n--- response headers, from ${HEADERS_BASE} ---`);
{
  const ctx = await b.newContext();
  for (const path of ["/", "/playground", "/docs/next/get-started", "/api/search"]) {
    const res = await ctx.request.get(HEADERS_BASE + path);
    const h = res.headers();
    const csp = h["content-security-policy"] ?? "";
    const script = csp.match(/script-src[^;]*/)?.[0] ?? "";
    check(res.status() === 200, `${path} is 200`);
    if (path === "/api/search") {
      check(/application\/json/.test(h["content-type"] ?? ""), `${path} is served as JSON`);
      continue;
    }
    check(csp !== "", `${path} carries a CSP`);
    check(!csp.includes(","), `${path} has one policy, not two comma-joined`);
    check(!/script-src[^;]*unsafe-inline/.test(csp), `${path} script-src has no 'unsafe-inline'`);
    check((script.match(/sha256-/g) ?? []).length >= 4, `${path} script-src carries hashes (${(script.match(/sha256-/g) ?? []).length})`);
    check((h["strict-transport-security"] ?? "").includes("max-age=31536000"), `${path} sets HSTS`);
    check(h["x-content-type-options"] === "nosniff", `${path} sets nosniff`);
    check(h["x-frame-options"] === "DENY", `${path} refuses framing`);
    const noindex = /noindex/.test(h["x-robots-tag"] ?? "");
    check(
      IS_PREVIEW ? noindex : !noindex,
      IS_PREVIEW ? `${path} tells crawlers to stay out` : `${path} allows production indexing`,
    );
  }
  await ctx.close();
}

// ------------------------------------------------------------- navigation
note(`\n--- navigation, against ${BASE} replaying the deployed bytes ---`);
{
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1000 } });
  const p = await ctx.newPage();
  const violations = [];
  p.on("console", (m) => /Content Security Policy|Refused to/i.test(m.text()) && violations.push(m.text().slice(0, 160)));
  p.on("pageerror", (e) => violations.push(`pageerror: ${String(e).slice(0, 160)}`));

  await p.goto(BASE, { waitUntil: "networkidle" });
  check((await p.title()).startsWith("Psych Runtime: build AI agents"), `the homepage title is ${JSON.stringify(await p.title())}`);

  // Every header destination, followed for real.
  await p.locator(".nav-menu-btn").hover();
  await p.waitForTimeout(400);
  const productLinks = await p.locator(".nav-panel a").evaluateAll((els) => els.map((e) => e.getAttribute("href")));
  for (const href of [...productLinks, "/examples", "/playground", "/docs"]) {
    const res = await p.goto(BASE + href, { waitUntil: "networkidle" });
    check(res.status() === 200 && p.url().includes(href), `nav: ${href} -> ${res.status()}`);
    check((await p.evaluate(() => document.documentElement.dataset.js)) === "on", `nav: ${href} hydrated`);
  }
  check(violations.length === 0, `no CSP violation or page error while navigating${violations.length ? `: ${violations[0]}` : ""}`);
  await ctx.close();
}

// ------------------------------------------------------ search navigation
note("\n--- search, and following a result ---");
{
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
  await p.keyboard.press("Control+k");
  await p.waitForTimeout(700);
  await p.keyboard.type("approval");
  await p.waitForTimeout(2500);
  const groups = await p.locator(".psych-result").count();
  const subs = await p.locator(".psych-result-subs li").count();
  check(groups > 0, `search returned ${groups} pages with ${subs} matches under them`);
  const excerpts = await p.locator(".psych-result-subs button").evaluateAll((els) => els.map((e) => e.innerText.trim().length));
  check(excerpts.every((n) => n <= 140), `every excerpt is one line (longest ${Math.max(0, ...excerpts)} chars)`);
  const first = p.locator(".psych-result > button").first();
  const label = (await first.innerText()).replace(/\s+/g, " ").trim().slice(0, 50);
  await first.click();
  await p.waitForTimeout(1500);
  check(/\/docs\//.test(p.url()), `following the first result "${label}" landed on ${p.url().replace(BASE, "")}`);
  check((await p.locator("h1").count()) > 0, "the result page rendered a heading");
  await ctx.close();
}

// ------------------------------------------------------ docs copy buttons
note("\n--- docs copy buttons ---");
{
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1000 }, permissions: ["clipboard-read", "clipboard-write"] });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
  const docsCopy = p.locator("figure button, pre ~ button, [data-copy]").first();
  if (await docsCopy.count()) {
    await docsCopy.click({ force: true });
    await p.waitForTimeout(400);
    const t = await p.evaluate(() => navigator.clipboard.readText().catch(() => ""));
    check(t.includes("psych"), `the docs code block copies its code (${JSON.stringify(t.trim().slice(0, 30))})`);
  } else {
    note("  --   no copy button on docs code blocks");
  }
  await ctx.close();
}

// -------------------------------------------------------- approval paths
note("\n--- both approval paths, deployed ---");
for (const choice of ["Approve", "Deny"]) {
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1100 } });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/playground`, { waitUntil: "networkidle" });
  await p.locator('.stepper-jumps button:has-text("Approval")').click();
  check(await p.locator(".approval-card").isVisible(), `${choice}: the Run stops at the gate`);
  check(await p.locator(".stepper-controls .btn-primary").isDisabled(), `${choice}: there is no next record until you decide`);
  await p.locator(`.approval-actions button:has-text("${choice}")`).click();
  for (let i = 0; i < 40; i++) {
    const btn = p.locator(".stepper-controls .btn-primary");
    if (await btn.isDisabled()) break;
    await btn.click();
  }
  const s = await p.evaluate(() => {
    const rows = {};
    for (const d of document.querySelectorAll(".derived > div")) rows[d.querySelector("dt").textContent.trim()] = d.querySelector("dd").textContent.trim();
    return { rows, answer: document.querySelector(".stepper-answer")?.textContent ?? "", refusal: !!document.querySelector(".refusal"), all: document.querySelector(".stepper").innerText };
  });
  if (choice === "Deny") {
    check(/denied by manager-7/.test(s.rows.approval), "denied: the panel names the refusal");
    check(s.refusal, "denied: a refusal notice is shown");
    check(!/refunded/i.test(s.all), "denied: the word 'refunded' appears nowhere in the component");
    check(/not able to issue that refund/i.test(s.answer), "denied: the answer says it did not happen");
  } else {
    check(/approved by manager-7/.test(s.rows.approval), "approved: the panel names the approval");
    check(/Refunded \$42\.00/.test(s.answer), "approved: the answer states the refund");
  }
  check(s.rows.terminal_state === "completed", `${choice}: settles completed`);
  await ctx.close();
}

// --------------------------------------------------------------- a phone
note("\n--- a phone, deployed ---");
{
  const ctx = await b.newContext({ ...devices["Pixel 7"] });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/playground`, { waitUntil: "networkidle" });
  const before = await p.evaluate(() => window.scrollY);
  const log = p.locator(".stepper .log");
  await log.scrollIntoViewIfNeeded();
  await log.evaluate((e) => (e.scrollTop = e.scrollHeight));
  await log.hover();
  for (let i = 0; i < 5; i++) await p.mouse.wheel(0, 400);
  await p.waitForTimeout(300);
  check((await p.evaluate(() => window.scrollY)) === (await p.evaluate(() => window.scrollY)), "the page settles");
  const small = await p.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll("a,button,summary")) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2 || getComputedStyle(el).display === "inline") continue;
      if (Math.round(r.height) < 44) out.push(`${el.tagName} ${Math.round(r.height)}px`);
    }
    return out;
  });
  check(small.length === 0, `every control is 44px or more${small.length ? `: ${small.slice(0, 3).join(", ")}` : ""}`);
  await p.locator(".nav-toggle").tap();
  await p.waitForTimeout(300);
  check((await p.locator(".nav-links a:visible").count()) >= 6, "the menu opens with every destination");
  await ctx.close();
}

console.log("\n=== FAILURES ===");
console.log(fail.length ? fail.map((f) => "  " + f).join("\n") : "  none");
await b.close();
replay.close();
process.exit(fail.length ? 1 : 0);
