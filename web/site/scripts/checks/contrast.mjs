/**
 * Contrast in both themes, the docs sidebar, and search.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium } from "playwright";
const BASE = process.env.BASE ?? "http://localhost:4599";
// A sandbox with a pre-installed browser sets PLAYWRIGHT_CHROMIUM rather than
// letting Playwright download one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const fail = [];
const note = (m) => console.log(m);

const CONTRAST = `(() => {
  const lum = (r,g,b) => { const f=[r,g,b].map(v=>{v/=255;return v<=0.04045?v/12.92:Math.pow((v+0.055)/1.055,2.4)}); return 0.2126*f[0]+0.7152*f[1]+0.0722*f[2]; };
  const parse = (s) => { const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null; const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return { r:p[0], g:p[1], b:p[2], a: p.length>3?p[3]:1 }; };
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== "none") return "gradient";
      const c = parse(cs.backgroundColor);
      if (c && c.a > 0.85) return c;
      n = n.parentElement;
    }
    const c = parse(getComputedStyle(document.body).backgroundColor);
    return c && c.a > 0 ? c : { r:255,g:255,b:255,a:1 };
  };
  const out = [];
  for (const el of document.querySelectorAll("p,li,dd,dt,span,a,h1,h2,h3,h4,td,th,button,summary,code,strong,em,figcaption,label")) {
    if (el.closest("[aria-hidden='true']")) continue;
    if (el.closest(":disabled, [aria-disabled='true']")) continue;
    if (el.children.length && !Array.from(el.childNodes).some(n => n.nodeType===3 && n.textContent.trim())) continue;
    const text = el.textContent?.trim();
    if (!text) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.opacity === "0") continue;
    const fg = parse(cs.color); if (!fg) continue;
    const bg = bgOf(el);
    if (bg === "gradient") continue;
    // Composite the text colour over the background when it is translucent,
    // and fold in every ancestor opacity, which is what actually reaches the eye.
    let a = fg.a;
    let n = el;
    while (n && n !== document.documentElement) { a *= parseFloat(getComputedStyle(n).opacity); n = n.parentElement; }
    const mix = (f,bk) => a*f + (1-a)*bk;
    const L1 = lum(mix(fg.r,bg.r), mix(fg.g,bg.g), mix(fg.b,bg.b));
    const L2 = lum(bg.r,bg.g,bg.b);
    const ratio = (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
    const px = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight,10) >= 700;
    const need = (px >= 24 || (px >= 18.66 && bold)) ? 3 : 4.5;
    if (ratio < need) out.push({ sel: el.tagName + "." + String(el.className).split(" ").slice(0,2).join("."), text: text.slice(0,40), ratio: +ratio.toFixed(2), need, px, color: cs.color });
  }
  return { url: location.pathname, out };
})()`;

const PAGES = ["/", "/runtime", "/capabilities", "/integrations", "/examples", "/playground", "/open-source", "/about", "/changelog", "/docs", "/docs/next/get-started"];
for (const theme of ["light", "dark"]) {
  const ctx = await b.newContext({ viewport: { width: 1280, height: 1000 }, colorScheme: theme, reducedMotion: "reduce" });
  const p = await ctx.newPage();
  for (const path of PAGES) {
    await p.goto(BASE + path, { waitUntil: "networkidle" });
    const res = await p.evaluate(CONTRAST);
    const uniq = new Map();
    for (const x of res.out) uniq.set(x.sel + x.ratio, x);
    for (const x of uniq.values()) fail.push(`CONTRAST ${theme} ${res.url} ${x.sel} ${x.ratio}<${x.need} ${x.px}px ${x.color} :: ${JSON.stringify(x.text)}`);
  }
  await ctx.close();
}
note(`contrast sweep done`);

// Docs sidebar: no label appearing twice.
{
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/docs/next/get-started", { waitUntil: "networkidle" });
  const labels = await p.evaluate(() => {
    const aside = document.querySelector("#nd-sidebar, aside");
    if (!aside) return null;
    return Array.from(aside.querySelectorAll("a,button,p,h3,span"))
      .map(e => e.textContent?.trim())
      .filter(t => t && t.length < 40);
  });
  const counts = {};
  for (const l of labels ?? []) counts[l] = (counts[l] ?? 0) + 1;
  const dupes = Object.entries(counts).filter(([, n]) => n > 1);
  note(`sidebar duplicates: ${JSON.stringify(dupes)}`);
  await ctx.close();
}

// Search: grouped results.
{
  const ctx = await b.newContext({ viewport: { width: 1280, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto(BASE + "/docs/next/get-started", { waitUntil: "networkidle" });
  await p.keyboard.press("Control+k");
  await p.waitForTimeout(600);
  await p.keyboard.type("approval");
  await p.waitForTimeout(1500);
  const shape = await p.evaluate(() => {
    const groups = document.querySelectorAll(".psych-result");
    return {
      groups: groups.length,
      rows: document.querySelectorAll(".psych-result > button, .psych-result > a").length,
      subs: document.querySelectorAll(".psych-result-subs li").length,
      firstTitles: Array.from(groups).slice(0, 4).map(g => g.firstElementChild?.textContent?.trim().slice(0, 60)),
    };
  });
  note(`search "approval": ${JSON.stringify(shape, null, 1)}`);
  if (shape.groups === 0) fail.push("search returned no grouped results");
  await ctx.close();
}

// Touch target sizes on a phone.
{
  const ctx = await b.newContext({ viewport: { width: 375, height: 800 }, hasTouch: true, isMobile: true });
  const p = await ctx.newPage();
  await p.goto(BASE + "/playground", { waitUntil: "networkidle" });
  const small = await p.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll("a,button")) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      if (r.height < 32) out.push(`${el.tagName}.${String(el.className).split(" ")[0]} ${Math.round(r.width)}x${Math.round(r.height)} ${el.textContent?.trim().slice(0,20)}`);
    }
    return out;
  });
  note(`small touch targets on /playground @375: ${small.length ? small.join(" | ") : "none"}`);
  await ctx.close();
}

console.log("\n=== FAILURES ===");
console.log(fail.length ? fail.slice(0, 40).join("\n") : "none");
console.log(fail.length > 40 ? `...and ${fail.length - 40} more` : "");
await b.close();
process.exit(fail.length ? 1 : 0);
