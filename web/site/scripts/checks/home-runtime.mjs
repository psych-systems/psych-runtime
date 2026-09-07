import assert from "node:assert/strict";
import { chromium } from "playwright";
const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {});
try {
  for (const width of [320, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    await page.goto((process.env.BASE ?? "http://localhost:4599") + "/", { waitUntil: "networkidle" });
    const overview = page.locator(".runtime-window");
    await overview.getByRole("button", { name: "Follow the next step" }).click();
    assert.equal(await overview.locator('.runtime-progress li[data-active="true"]').textContent(), "02Psych checks access");
    await overview.getByRole("button", { name: "Pause & resume", exact: true }).click();
    assert.match(await overview.locator(".runtime-event").textContent(), /Waiting for approval/);
    assert.match(await overview.locator(".runtime-event").textContent(), /has not executed/);
    await overview.getByRole("button", { name: "Approve this step" }).click();
    assert.match(await overview.locator(".runtime-event").textContent(), /Decision recorded. Run resumes/);
    await overview.getByRole("button", { name: "Reset illustration" }).click();
    assert.match(await overview.locator(".runtime-event").textContent(), /Waiting for approval/);
    await overview.getByRole("button", { name: "Recover", exact: true }).click();
    assert.match(await overview.locator(".runtime-explanation").textContent(), /unless the tool is explicitly safe to retry/);
    assert.match(await overview.locator(".runtime-event").textContent(), /durable store/);
    await overview.getByRole("button", { name: "See recovery" }).focus();
    await page.keyboard.press("Enter");
    assert.match(await overview.locator(".runtime-event").textContent(), /New worker reads the saved history/);
    await overview.getByRole("button", { name: "Execute", exact: true }).click();
    assert.equal(await overview.locator('.runtime-progress li[data-active="true"]').textContent(), "01Model chooses a tool");
    for (const link of await page.locator(".home a[href^='/']").all()) {
      const response = await page.request.get(new URL(await link.getAttribute("href"), page.url()).toString());
      assert.equal(response.status(), 200, `Broken homepage link: ${response.url()}`);
    }
    await page.close();
    console.log(`Runtime overview: execution, approval, reset, recovery and links pass at ${width}px`);
  }
} finally { await browser.close(); }
