import { test } from "node:test";
import assert from "node:assert/strict";
import { cp, mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

test("documentation sync accepts CRLF and checks stale output without rewriting it", async () => {
  const fixture = await mkdtemp(join(tmpdir(), "psych-docs-"));
  try {
    for (const path of ["docs", ".agents/skills", "CHANGELOG.md"]) {
      await cp(join(repo, path), join(fixture, path), { recursive: true });
    }
    await mkdir(join(fixture, "web/scripts"), { recursive: true });
    const script = join(fixture, "web/scripts/sync-from-repo.mjs");
    await cp(join(repo, "web/scripts/sync-from-repo.mjs"), script);
    const run = (...args) => spawnSync(process.execPath, [script, ...args], { encoding: "utf8" });
    const initial = run();
    assert.equal(initial.status, 0, initial.stderr);
    const skills = join(fixture, ".agents/skills");
    for (const name of await readdir(skills)) {
      const path = join(skills, name, "SKILL.md");
      const body = await readFile(path, "utf8");
      await writeFile(path, body.replace(/\r?\n/g, "\r\n"));
    }
    assert.equal(run("--check").status, 0, "CRLF skills must produce the same documentation");
    const page = join(fixture, "web/site/content/docs/next/guides/agents.mdx");
    await writeFile(page, "stale documentation\n");
    const stale = run("--check");
    assert.equal(stale.status, 1);
    assert.match(stale.stderr, /guides\/agents.mdx/);
    assert.equal(await readFile(page, "utf8"), "stale documentation\n");
    await rm(page);
    assert.equal(run("--check").status, 1, "missing generated pages fail the check");
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }
});
