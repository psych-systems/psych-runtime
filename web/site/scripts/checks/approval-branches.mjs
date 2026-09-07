/**
 * What a visitor sees after approving and after denying.
 *
 * Run by `scripts/verify.mjs`; runnable alone against any BASE.
 */
import { chromium } from "playwright";

/**
 * The denied branch, asserted rather than assumed.
 *
 * Both branches settle `completed`, which is correct for the Run's lifecycle
 * and, on its own, indistinguishable from success. These assertions are about
 * what a person actually sees: that the refusal is named, that issue_refund is
 * never reported as having run, and that no success wording survives anywhere
 * in the rendered page.
 */
const BASE = process.env.BASE ?? "http://localhost:4599";
// A sandbox with a pre-installed browser sets PLAYWRIGHT_CHROMIUM rather than
// letting Playwright download one.
const b = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
);
const fail = [];
const ok = [];
const check = (cond, msg) => (cond ? ok.push(msg) : fail.push(msg));

// Wording that must never appear on the denied branch, in any casing.
const SUCCESS_WORDS = [
  /refunded/i,
  /\brefund (issued|succeeded|complete)/i,
  /outcome=ok[^\n]*issue_refund/i,
  /issue_refund[^\n]*outcome=ok/i,
];

for (const choice of ["Approve", "Deny"]) {
  const ctx = await b.newContext({ viewport: { width: 1400, height: 1100 } });
  const p = await ctx.newPage();
  await p.goto(`${BASE}/playground`, { waitUntil: "networkidle" });
  await p.locator('.stepper-jumps button:has-text("Approval")').click();
  await p.locator(`.approval-actions button:has-text("${choice}")`).click();
  for (let i = 0; i < 40; i++) {
    const btn = p.locator(".stepper-controls .btn-primary");
    if (await btn.isDisabled()) break;
    await btn.click();
  }

  const state = await p.evaluate(() => {
    const rows = {};
    for (const d of document.querySelectorAll(".derived > div")) {
      rows[d.querySelector("dt").textContent.trim()] = d.querySelector("dd").textContent.trim();
    }
    return {
      rows,
      answer: document.querySelector(".stepper-answer")?.textContent?.trim() ?? null,
      refusal: document.querySelector(".refusal")?.textContent?.trim() ?? null,
      log: Array.from(document.querySelectorAll(".stepper .log li")).map((li) => li.textContent.trim()),
      stepperText: document.querySelector(".stepper").innerText,
    };
  });

  const refundRows = state.log.filter((l) => l.includes("issue_refund") || l.includes("call_7d1"));
  console.log(`\n--- ${choice} ---`);
  console.log("  approval row :", state.rows.approval);
  console.log("  tool_calls   :", state.rows.tool_calls);
  console.log("  terminal     :", state.rows.terminal_state);
  console.log("  refusal shown:", state.refusal ? "yes" : "no");
  console.log("  answer       :", state.answer);
  for (const r of refundRows) console.log("  log          :", r.slice(0, 110));

  if (choice === "Deny") {
    check(/denied by manager-7/.test(state.rows.approval ?? ""), "denied: panel names the refusal and who made it");
    check(state.refusal !== null, "denied: a refusal notice is shown");
    check(/failed/.test(state.rows.tool_calls) && /1 failed/.test(state.rows.tool_calls), "denied: exactly one tool call is counted as failed");
    check(!/Refunded/.test(state.answer ?? ""), "denied: the answer does not claim a refund");
    check(/not able to issue that refund/i.test(state.answer ?? ""), "denied: the answer says the refund did not happen");
    // issue_refund must never appear settled ok, anywhere in the log.
    check(!refundRows.some((l) => /outcome=ok/.test(l)), "denied: issue_refund is never settled ok");
    check(refundRows.some((l) => /failure\.kind=denied/.test(l)), "denied: issue_refund is settled as failure.kind=denied");
    check(!refundRows.some((l) => /refunded 4200/.test(l)), "denied: the refund result string never appears");
    for (const re of SUCCESS_WORDS) {
      check(!re.test(state.stepperText), `denied: no success wording matching ${re}`);
    }
  } else {
    check(/approved by manager-7/.test(state.rows.approval ?? ""), "approved: panel names the approval and who made it");
    check(state.refusal === null, "approved: no refusal notice");
    check(/0 failed/.test(state.rows.tool_calls), "approved: no tool call failed");
    check(/Refunded \$42\.00/.test(state.answer ?? ""), "approved: the answer states the refund");
    check(refundRows.some((l) => /outcome=ok/.test(l)), "approved: issue_refund is settled ok");
  }
  check(state.rows.terminal_state === "completed", `${choice}: the Run settles completed`);
  await ctx.close();
}

console.log("\n=== PASSED ===");
for (const m of ok) console.log("  ✓", m);
console.log("\n=== FAILED ===");
console.log(fail.length ? fail.map((m) => "  ✗ " + m).join("\n") : "  none");
await b.close();
process.exit(fail.length ? 1 : 0);
