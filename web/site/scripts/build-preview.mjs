import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";

const require = createRequire(import.meta.url);
const commands = [
  [require.resolve("next/dist/bin/next"), "build"],
  ["scripts/name-images.mjs"],
  ["scripts/preview-headers.mjs"],
  ["scripts/csp-headers.mjs"],
];

for (const args of commands) {
  const result = spawnSync(process.execPath, args, {
    stdio: "inherit",
    env: { ...process.env, PSYCH_SITE_ENV: "preview" },
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
