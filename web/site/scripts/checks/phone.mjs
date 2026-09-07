/**
 * Nested scrolling, the sticky header and touch targets.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium, devices } from "playwright";

/**
 * The mobile behaviours a width sweep does not cover: whether a nested
 * scroller traps the page, whether the sticky header hides what you jump to,
 * whether code and tables scroll inside their own box, and whether the menu
 * behaves for a keyboard on a small screen.
 */
const BASE = process.env.BASE ?? "http://localhost:4599";
// A sandbox with a pre-installed browser sets PLAYWRIGHT_CHROMIUM rather than
// letting Playwright download one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const fail = [];
const note = (m) => console.log(m);
const check = (c, m) => (c ? note(`  ok   ${m}`) : (fail.push(m), note(`  FAIL ${m}`)));

for (const name of ["iPhone 13", "Pixel 7"]) {
  const ctx = await b.newContext({ ...devices[name] });
  const p = await ctx.newPage();
  note(`\n--- ${name} (${devices[name].viewport.width}x${devices[name].viewport.height}) ---`);

  // 1. The record log is a nested scroller. Scrolling it to its end must not
  //    then scroll the page (overscroll-behavior: contain).
  await p.goto(`${BASE}/playground`, { waitUntil: "networkidle" });
  const log = p.locator(".stepper .log");
  await log.scrollIntoViewIfNeeded();
  const pageBefore = await p.evaluate(() => window.scrollY);
  const logScrollable = await log.evaluate((e) => e.scrollHeight > e.clientHeight + 4);
  check(logScrollable, "the record log is its own scroller");
  await log.evaluate((e) => (e.scrollTop = e.scrollHeight));
  // Wheel well past the end of the nested scroller.
  await log.hover();
  for (let i = 0; i < 6; i++) await p.mouse.wheel(0, 400);
  await p.waitForTimeout(300);
  const pageAfter = await p.evaluate(() => window.scrollY);
  check(pageAfter === pageBefore, `overscrolling the log does not scroll the page (${pageBefore} -> ${pageAfter})`);

  // 2. Sticky header must not cover a heading jumped to by anchor.
  await p.goto(`${BASE}/capabilities`, { waitUntil: "networkidle" });
  const navH = await p.evaluate(() => document.querySelector(".nav")?.getBoundingClientRect().height ?? 0);
  await p.evaluate(() => (location.hash = "#observability"));
  await p.waitForTimeout(600);
  const top = await p.evaluate(() => document.querySelector("#observability h2")?.getBoundingClientRect().top ?? -999);
  check(top >= navH - 1, `the jumped-to heading clears the sticky header (top ${Math.round(top)} vs header ${Math.round(navH)})`);

  const capabilityBodyWidth = await p.locator(".cap-group .row > div").first().evaluate(
    (el) => ({ body: el.getBoundingClientRect().width, row: el.parentElement?.getBoundingClientRect().width ?? 0 }),
  );
  check(
    capabilityBodyWidth.body >= capabilityBodyWidth.row * 0.7,
    `capability copy keeps a readable column (${Math.round(capabilityBodyWidth.body)}px of ${Math.round(capabilityBodyWidth.row)}px)`,
  );

  // 3. Sticky header must not eat the viewport on a short screen.
  check(navH <= devices[name].viewport.height * 0.15, `the header is ${Math.round(navH)}px, under 15% of the viewport`);

  // 4. Code blocks and tables scroll inside their own box, not the page.
  await p.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
  const boxed = await p.evaluate(() => {
    const de = document.documentElement;
    const bad = [];
    // A <pre> wider than the viewport is fine when an ancestor scrolls it;
    // that is what "scrolls inside its own box" means. What is not fine is one
    // with no such ancestor, because then the page has to scroll instead.
    const scrolls = (el) => {
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        const ox = getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
      const ox = getComputedStyle(el).overflowX;
      return ox === "auto" || ox === "scroll";
    };
    for (const el of document.querySelectorAll("pre, .table-wrap, table")) {
      const r = el.getBoundingClientRect();
      if (r.right > de.clientWidth + 1 && !scrolls(el)) {
        bad.push(`${el.tagName}.${String(el.className).split(" ")[0]}`);
      }
    }
    return { bad, docScrolls: de.scrollWidth > de.clientWidth + 1 };
  });
  check(!boxed.docScrolls, "the docs page does not scroll horizontally");
  check(boxed.bad.length === 0, `no code block or table overflows the viewport${boxed.bad.length ? `: ${boxed.bad.join(", ")}` : ""}`);

  // A horizontally scrollable table must still pass vertical wheel gestures
  // to the page. Otherwise the whole section becomes a scroll trap.
  await p.goto(`${BASE}/runtime`, { waitUntil: "networkidle" });
  const table = p.locator(".table-wrap");
  await table.evaluate((e) => e.scrollIntoView({ block: "center" }));
  const tablePageBefore = await p.evaluate(() => window.scrollY);
  await table.hover();
  await p.mouse.wheel(0, 320);
  await p.waitForTimeout(300);
  const tablePageAfter = await p.evaluate(() => window.scrollY);
  check(tablePageAfter > tablePageBefore, `wheel scrolling over a table moves the page (${tablePageBefore} -> ${tablePageAfter})`);

  // 5. The menu: opens, is reachable by keyboard, closes on Escape, and the
  //    product group is a flat list rather than a button that does nothing.
  await p.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await p.locator(".nav-toggle").tap();
  await p.waitForTimeout(300);
  const linkCount = await p.locator(".nav-links a:visible").count();
  check(linkCount >= 6, `the open menu shows every destination (${linkCount} links)`);
  const groupBtn = await p.locator(".nav-menu-btn:visible").count();
  check(groupBtn === 0, "the product group is inline, not a button to open on a phone");
  await p.keyboard.press("Escape");
  await p.waitForTimeout(300);
  check((await p.locator(".nav-links a:visible").count()) === 0, "Escape closes the menu");

  // 6. Nothing under the 44px comfortable target on the pages with controls.
  for (const path of ["/", "/playground", "/docs/next/get-started"]) {
    await p.goto(BASE + path, { waitUntil: "networkidle" });
    const small = await p.evaluate(() => {
      const out = [];
      for (const el of document.querySelectorAll("a,button,summary,input")) {
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        if (getComputedStyle(el).display === "inline") continue; // inline links in prose
        if (Math.round(r.height) < 44) out.push(`${el.tagName}.${String(el.className).split(" ")[0]} ${Math.round(r.height)}px "${el.textContent?.trim().slice(0, 18)}"`);
      }
      return out;
    });
    check(small.length === 0, `${path}: every control is at least 44px tall${small.length ? ` — ${small.slice(0, 4).join(" | ")}` : ""}`);
  }
  await ctx.close();
}

console.log("\n=== FAILURES ===");
console.log(fail.length ? fail.map((f) => "  " + f).join("\n") : "  none");
await b.close();
process.exit(fail.length ? 1 : 0);
