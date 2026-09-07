/**
 * The four things a visitor does that nothing else here drives.
 *
 * Browser back, the theme choice surviving a reload, an unknown URL, and
 * choosing a search result from the keyboard. Each was reported as unverified
 * in an earlier handoff, which is a worse place for them than a check.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium } from "playwright";

const BASE = process.env.BASE ?? "http://localhost:4599";
const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);

const fail = [];
const ok = [];
const check = (cond, msg) => (cond ? ok.push(msg) : fail.push(msg));

const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
  reducedMotion: "reduce",
});
const page = await context.newPage();

// --- browser back ----------------------------------------------------------
//
// A single-page router that pushes state without handling popstate leaves the
// URL changed and the page not, which reads as the site being broken by the
// one control every browser has.

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
const homeHeading = (await page.locator("h1").first().innerText()).trim();

await page.goto(`${BASE}/runtime`, { waitUntil: "networkidle" });
const runtimeHeading = (await page.locator("h1").first().innerText()).trim();
check(runtimeHeading !== homeHeading, "the two pages have different headings to tell apart");

await page.goBack({ waitUntil: "networkidle" });
check(new URL(page.url()).pathname === "/", `back returns to / (got ${new URL(page.url()).pathname})`);
check(
  (await page.locator("h1").first().innerText()).trim() === homeHeading,
  "back renders the homepage, not just its URL",
);

await page.goForward({ waitUntil: "networkidle" });
check(
  (await page.locator("h1").first().innerText()).trim() === runtimeHeading,
  "forward renders /runtime again",
);

// Back out of the documentation too: it is a different layout and a different
// router path, and the docs are where somebody actually navigates a lot.
await page.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
await page.goto(`${BASE}/docs/next/guides`, { waitUntil: "networkidle" });
await page.goBack({ waitUntil: "networkidle" });
check(
  new URL(page.url()).pathname === "/docs/next/get-started",
  "back works inside the documentation",
);
check(
  (await page.locator("h1").first().innerText()).toLowerCase().includes("install"),
  "the docs page it went back to rendered its own heading",
);

// --- the theme choice survives a reload ------------------------------------
//
// next-themes writes localStorage and a class on <html>. A theme that resets
// on reload is worse than no toggle: the reader concludes their choice was
// ignored rather than that it was never stored.

await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
const before = await page.evaluate(() => document.documentElement.className);
const toggle = page.locator(".nav-end button").first();
await toggle.click();
await page.waitForTimeout(400);
const after = await page.evaluate(() => document.documentElement.className);
check(after !== before, `the toggle changes the theme class (${before} -> ${after})`);

const stored = await page.evaluate(() => {
  try {
    return window.localStorage.getItem("theme");
  } catch {
    return null;
  }
});
check(stored !== null, `the choice is stored (theme=${stored})`);

await page.reload({ waitUntil: "networkidle" });
check(
  (await page.evaluate(() => document.documentElement.className)) === after,
  "the theme survives a reload",
);

// And on another page, since the class is set by a script in <head> on each
// document rather than carried by the router.
await page.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
check(
  (await page.evaluate(() => document.documentElement.className)) === after,
  "the theme survives a move into the documentation",
);

// --- an unknown URL --------------------------------------------------------
//
// Checked against the built output the host serves, so a 404 that only exists
// as a designed page and never as a status is caught.

const missing = await page.goto(`${BASE}/no-such-page-here`, { waitUntil: "networkidle" });
check(missing.status() === 404, `an unknown path answers 404 (got ${missing.status()})`);
const notFoundText = await page.locator("body").innerText();
check(/404|not found/i.test(notFoundText), "the 404 page says so in words");
check(
  (await page.locator("a[href='/']").count()) > 0 ||
    (await page.locator("header a").count()) > 0,
  "the 404 page offers a way back",
);

// --- choosing a search result from the keyboard ----------------------------

await page.goto(`${BASE}/docs/next/get-started`, { waitUntil: "networkidle" });
await page.keyboard.press("Control+k");
await page.waitForTimeout(600);
const input = page.locator("input[type='search'], [role='dialog'] input").first();
check((await input.count()) > 0, "the search dialog opens on Control+K");

if ((await input.count()) > 0) {
  await input.fill("approval");
  await page.waitForTimeout(1400);
  // `.psych-result` is this site's own grouped row (components/DocsSearch.tsx).
  // fumadocs' internal markup is not a contract; ours is.
  const count = await page.locator(".psych-result").count();
  check(count > 0, `typing returns results (${count})`);

  // Arrow down then Enter: the whole point of a command palette is that a
  // person never has to reach for the mouse.
  await page.keyboard.press("ArrowDown");
  await page.waitForTimeout(200);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1600);
  const landed = new URL(page.url()).pathname;
  check(landed.startsWith("/docs/"), `Enter follows the highlighted result (${landed})`);
  check(
    (await page.locator("h1").count()) > 0,
    "the page it landed on rendered a heading",
  );
}

await browser.close();

console.log("\n=== PASSED ===");
for (const line of ok) console.log("  ok  ", line);
console.log("\n=== FAILURES ===");
console.log(fail.length ? fail.map((f) => `  ✗ ${f}`).join("\n") : "  none");
process.exit(fail.length ? 1 : 0);
