/**
 * Layout, the hero, the nav menu and the stepper's shape.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium } from "playwright";

const BASE = process.env.BASE ?? "http://localhost:4599";
const WIDTHS = [320, 375, 768, 1024, 1440];
const PAGES = ["/", "/runtime", "/capabilities", "/integrations", "/examples", "/playground", "/open-source", "/about", "/changelog", "/docs", "/docs/next/get-started"];

// A sandbox with a pre-installed browser sets PLAYWRIGHT_CHROMIUM rather than
// letting Playwright download one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const fail = [];
const note = (m) => console.log(m);

// 1. Overflow at every breakpoint, both themes.
for (const theme of ["light", "dark"]) {
  for (const w of WIDTHS) {
    const ctx = await b.newContext({ viewport: { width: w, height: 900 }, colorScheme: theme });
    const p = await ctx.newPage();
    for (const path of PAGES) {
      await p.goto(BASE + path, { waitUntil: "networkidle" });
      const over = await p.evaluate(() => {
        const de = document.documentElement;
        const bad = [];
        if (de.scrollWidth > de.clientWidth + 1) {
          for (const el of document.querySelectorAll("body *")) {
            const r = el.getBoundingClientRect();
            if (r.right > de.clientWidth + 1 || r.left < -1) {
              bad.push(`${el.tagName}.${(el.className || "").toString().split(" ")[0]} right=${Math.round(r.right)}`);
              if (bad.length > 3) break;
            }
          }
          return { w: de.scrollWidth, c: de.clientWidth, bad };
        }
        return null;
      });
      if (over) fail.push(`OVERFLOW ${theme} ${w} ${path}: ${over.w}>${over.c} ${over.bad.join(", ")}`);
    }
    await ctx.close();
  }
}
note(`overflow sweep done (${fail.length} problems)`);

// 2. Hero renders immediately: headline + primary action visible in the first frame.
{
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/", { waitUntil: "domcontentloaded" });
  const h1 = await p.locator("#hero-title").textContent();
  const opacity = await p.locator("#hero-title").evaluate((e) => getComputedStyle(e).opacity);
  const cta = await p.locator(".home-intro a").first().textContent();
  note(`hero h1=${JSON.stringify(h1?.trim().slice(0, 60))} opacity=${opacity} cta=${JSON.stringify(cta?.trim())}`);
  if (opacity !== "1") fail.push(`hero headline opacity ${opacity} on first paint`);
  await ctx.close();
}

// 3. The nav menu: keyboard, Escape, and no page jump.
{
  const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/", { waitUntil: "networkidle" });
  const y0 = await p.evaluate(() => window.scrollY);
  await p.locator(".nav-menu-btn").click();
  await p.waitForTimeout(300);
  const shown = await p.locator(".nav-panel a").first().isVisible();
  const expanded = await p.locator(".nav-menu-btn").getAttribute("aria-expanded");
  note(`aria-expanded after click: ${expanded}`);
  await p.keyboard.press("Escape");
  await p.waitForTimeout(250);
  const hidden = await p.locator(".nav-panel a").first().isVisible();
  const y1 = await p.evaluate(() => window.scrollY);
  note(`nav menu open=${shown} closedAfterEscape=${!hidden} scroll ${y0}->${y1}`);
  if (!shown) fail.push("nav menu did not open");
  if (hidden) fail.push("nav menu did not close on Escape");
  if (y1 !== y0) fail.push(`page scrolled on load: ${y0} -> ${y1}`);

  // First tab stop must be the skip link, from a fresh load.
  await p.goto(BASE + "/", { waitUntil: "networkidle" });
  await p.keyboard.press("Tab");
  const first = await p.evaluate(() => `${document.activeElement?.tagName}.${document.activeElement?.className}`);
  note(`first tab stop: ${first}`);
  if (!String(first).includes("skip-link")) fail.push(`first tab stop is ${first}`);
  await ctx.close();
}

// 4. The stepper: gate, approve, deny.
for (const choice of ["Approve", "Deny"]) {
  const ctx = await b.newContext({ viewport: { width: 1280, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/playground", { waitUntil: "networkidle" });
  await p.locator('.stepper-jumps button:has-text("Approval")').click();
  const card = await p.locator(".approval-card").isVisible();
  const what = await p.locator(".approval-what").textContent();
  const nextDisabled = await p.locator('.stepper-controls .btn-primary').isDisabled();
  note(`gate visible=${card} what=${JSON.stringify(what)} nextDisabled=${nextDisabled}`);
  if (!card) fail.push("approval card not shown at the gate");
  if (!nextDisabled) fail.push("stepping past the gate was possible without deciding");
  await p.locator(`.approval-actions button:has-text("${choice}")`).click();
  await p.waitForTimeout(150);
  const note1 = await p.locator(".stepper-note").textContent();
  // Run to the end and read the answer.
  for (let i = 0; i < 30; i++) {
    const btn = p.locator(".stepper-controls .btn-primary");
    if (await btn.isDisabled()) break;
    await btn.click();
  }
  const answer = await p.locator(".stepper-answer").textContent().catch(() => null);
  const terminal = await p.locator(".derived div:last-child dd").textContent();
  const cost = await p.locator(".derived div:nth-child(6) dd").textContent();
  const tokens = await p.locator(".derived div:nth-child(5) dd").textContent();
  note(`${choice}: decision-note=${JSON.stringify(note1?.slice(0, 50))}`);
  note(`${choice}: answer=${JSON.stringify(answer?.trim().slice(0, 80))} terminal=${terminal} cost=${cost} tokens=${tokens}`);
  if (!answer) fail.push(`${choice}: no answer shown at the end`);
  if (terminal !== "completed") fail.push(`${choice}: terminal_state=${terminal}`);
  await ctx.close();
}

// 5. Cost before any model call must read as words, not a dash.
{
  const ctx = await b.newContext({ viewport: { width: 1280, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/playground", { waitUntil: "networkidle" });
  const cost = await p.locator(".derived div:nth-child(6) dd").textContent();
  const tokens = await p.locator(".derived div:nth-child(5) dd").textContent();
  note(`record 1: cost=${JSON.stringify(cost)} tokens=${JSON.stringify(tokens)}`);
  if (cost?.trim() === "—") fail.push("cost still renders an unexplained dash");
  await ctx.close();
}

console.log("\n=== FAILURES ===");
console.log(fail.length ? fail.join("\n") : "none");
await b.close();
process.exit(fail.length ? 1 : 0);
