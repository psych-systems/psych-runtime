#!/usr/bin/env node
/**
 * Freeze `content/docs/next` as a released version.
 *
 *     pnpm cut 0.2.0
 *
 * Docs are versioned by minor. `0.2.0` and `0.2.4` share one tree, because a
 * patch that changes no documented behaviour should not fork the whole thing,
 * and a reader gains nothing from landing on v0.2.4 rather than v0.2.1. A patch
 * that *does* change documented behaviour is corrected in place on that tree,
 * which is what a reader pinned to that minor actually wants to see.
 *
 * This script only copies files and rewrites one list. Publishing, tagging and
 * the changelog are the library's release process; this is the docs half of it,
 * kept separate so a docs mistake never blocks a release.
 */

import { cp, readFile, readdir, writeFile } from "node:fs/promises";
import { glob } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DOCS = resolve(HERE, "..", "site");
const CONTENT = join(DOCS, "content", "docs");
const VERSIONS_FILE = join(DOCS, "lib", "versions.ts");

const raw = process.argv[2];
if (!raw || !/^\d+\.\d+\.\d+$/.test(raw)) {
  console.error("usage: pnpm cut <major.minor.patch>   e.g. pnpm cut 0.2.0");
  process.exit(1);
}

const [major, minor] = raw.split(".");
const slug = `v${major}.${minor}`;
const target = join(CONTENT, slug);

// The library's version is the one in the code; this checks the two agree
// rather than letting the docs claim a release that was never published.
const initFile = join(HERE, "..", "..", "psych_runtime", "__init__.py");
const declared = (await readFile(initFile, "utf8")).match(/__version__ = "([^"]+)"/)?.[1];
if (declared !== raw) {
  console.error(
    `cut: psych_runtime.__version__ is ${declared}, not ${raw}.\n` +
      `Bump the library first. The docs must not announce a version the package does not carry.`,
  );
  process.exit(1);
}

if (existsSync(target)) {
  console.error(
    `cut: ${slug} already exists. A released tree is corrected in place, never re-cut: ` +
      `re-cutting would silently replace text a reader may already have been sent a link to.`,
  );
  process.exit(1);
}

await cp(join(CONTENT, "next"), target, { recursive: true });

// Rewrite the copied tree's own links. A page in v0.2 that links to
// /docs/next/get-started sends a reader pinned to a release into the
// unreleased tree, which is the one place they asked not to be. The pages
// only ever link within their own version, so the rewrite is total.
for await (const file of glob(`${target}/**/*.mdx`)) {
  const body = await readFile(file, "utf8");
  const rewritten = body.replaceAll("/docs/next/", `/docs/${slug}/`);
  if (rewritten !== body) await writeFile(file, rewritten, "utf8");
}

const existing = (await readdir(CONTENT, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory() && entry.name.startsWith("v"))
  .map((entry) => entry.name)
  .sort((a, b) => compare(b, a));

function compare(a, b) {
  const [aMajor, aMinor] = a.slice(1).split(".").map(Number);
  const [bMajor, bMinor] = b.slice(1).split(".").map(Number);
  return aMajor - bMajor || aMinor - bMinor;
}

const entries = [
  `  { slug: "next", label: "next", covers: "unreleased, tracks main", state: "next" },`,
  ...existing.map(
    (name, index) =>
      `  { slug: ${JSON.stringify(name)}, label: ${JSON.stringify(name)}, ` +
      `covers: ${JSON.stringify(`${name.slice(1)}.x`)}, ` +
      `state: ${JSON.stringify(index === 0 ? "current" : "maintenance")} },`,
  ),
];

const source = await readFile(VERSIONS_FILE, "utf8");
const rewritten = source.replace(
  /export const VERSIONS: readonly DocsVersion\[\] = \[[\s\S]*?\n\];/,
  `export const VERSIONS: readonly DocsVersion[] = [\n${entries.join("\n")}\n];`,
);
await writeFile(VERSIONS_FILE, rewritten, "utf8");

console.log(`cut: froze content/docs/next as ${slug}, and made it current`);
console.log(`next: commit, tag v${raw}, and let CI publish the wheel`);
