/**
 * Everything about the built site that only a browser can tell you.
 *
 *     cd web && pnpm build:preview && pnpm verify
 *
 * Serves `out/` the way the host does, then runs each check in
 * `scripts/checks/` against it. Every one is runnable alone against any origin:
 *
 *     BASE=https://preview.psychruntime.com node scripts/checks/layout.mjs
 *
 * What each is for, in the order they would waste the most time if missed:
 *
 * - `csp` loads every route under the policy `scripts/csp-headers.mjs`
 *   generated for it. A hash one byte out does not fail the build; it fails in
 *   the browser as a page that never hydrates.
 * - `contrast` measures against each element's actually-painted background,
 *   folding in every ancestor's opacity, in both themes. Asserting AA and
 *   measuring it are different activities: this site had text at 1.6:1 that
 *   nobody had caught by eye.
 * - `layout` checks for horizontal overflow at five widths in two themes, that
 *   the hero paints on the first frame, and that the page does not scroll
 *   itself on load.
 * - `approval-branches` asserts what a visitor sees after approving and after
 *   denying. Both Runs settle `completed`, which is correct and, alone,
 *   indistinguishable from success.
 * - `navigation` drives browser back and forward, the theme surviving a
 *   reload, an unknown URL, and choosing a search result from the keyboard
 * - `phone` covers nested scrolling, the sticky header, and a 44px floor on
 *   every control, which a 32px floor passed and should not have.
 *
 * The static server has no single-page fallback on purpose. A server that
 * rewrites unknown paths to index.html makes every page under test the
 * homepage, and a sweep over eleven pages then passes eleven times on one.
 */
import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { staticServer } from "./lib/static-server.mjs";

const PORT = Number(process.env.VERIFY_PORT ?? 4599);
const CHECKS = ["csp", "contrast", "layout", "approval-branches", "home-runtime", "navigation", "phone"];

try {
  await access("out/index.html");
} catch {
  console.error("verify: out/ has no build. Run `pnpm build:preview` first.");
  process.exit(1);
}

await new Promise((r) => staticServer.listen(PORT, r));

const failed = [];
for (const name of CHECKS) {
  console.log(`\n${"=".repeat(60)}\n${name}\n${"=".repeat(60)}`);
  const code = await new Promise((resolve) => {
    spawn(process.execPath, [`scripts/checks/${name}.mjs`], {
      stdio: "inherit",
      env: { ...process.env, BASE: `http://localhost:${PORT}` },
    }).on("close", resolve);
  });
  if (code !== 0) failed.push(name);
}

staticServer.close();
console.log(`\n${"=".repeat(60)}`);
console.log(failed.length ? `verify: ${failed.join(", ")} failed` : `verify: ${CHECKS.length} checks green`);
process.exit(failed.length ? 1 : 0);
