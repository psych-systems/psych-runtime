#!/usr/bin/env node
/**
 * Copy the library's own documentation into the docs site.
 *
 * Nothing under `content/docs/next/` that this script writes is authored here.
 * The library owns those files, they are reviewed with the code that makes them
 * true, and a docs site that keeps its own second copy is a docs site that is
 * wrong within a month.
 *
 * It fails on a missing source rather than leaving the previous copy in place.
 * A rename in the library should break the docs build loudly, because the
 * alternative is last month's page staying up and looking current.
 */

import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..");
const OUT = resolve(HERE, "..", "site", "content", "docs", "next");

/** @type {{from: string, to: string, title: string, description: string}[]} */
const PAGES = [
  {
    from: "CHANGELOG.md",
    to: "changelog.mdx",
    title: "Changelog",
    description:
      "Written at commit time rather than reconstructed at release. Every breaking change is here.",
  },
  {
    from: "docs/api.md",
    to: "reference/api.mdx",
    title: "The public API",
    description: "Everything a consumer calls, and the worked example the test suite executes.",
  },
];

const DESIGN_NOTES = [
  ["durability-and-leases", "Durability and leases"],
  ["record-log-and-reducer", "The record log and its reducer"],
  ["spec-versioning-and-tool-access", "Spec versioning and tool access"],
  ["tool-disclosure-and-prompt-budget", "Tool disclosure and the prompt budget"],
  ["metering-and-telemetry", "Metering and telemetry"],
  ["subagents-and-delegation", "Subagents and delegation"],
  ["code-execution-and-sandboxing", "Code execution and sandboxing"],
  ["mcp-2026-07-28", "MCP 2026-07-28"],
];

for (const [slug, title] of DESIGN_NOTES) {
  PAGES.push({
    from: `docs/design-notes/${slug}.md`,
    to: `design/${slug}.mdx`,
    title,
    description: "Why this area is shaped the way it is, and what breaks under the alternative.",
  });
}

/**
 * The agent skills, published as ordinary pages.
 *
 * They were written for a coding agent and they read perfectly well to a
 * person: each is one feature, the working code, and the gotchas that bite. A
 * second set of feature guides written by hand for humans would be the same
 * content maintained twice, and the second copy is always the stale one.
 *
 * Their frontmatter is replaced rather than reused. A skill's description is
 * tuned for retrieval -- long, keyword-dense, written to make an agent reach
 * for it -- and reads as noise under a heading on a web page.
 */
const SKILLS_DIR = resolve(REPO, ".agents", "skills");
const skillNames = (await readdir(SKILLS_DIR, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort();

/**
 * Titles for people, keyed by skill.
 *
 * A skill's H1 names the feature ("Approvals", "Stores"), which is right for
 * an agent choosing a file by name and wrong for a person choosing a page by
 * what they are trying to do. The page keeps the skill's body word for word
 * and takes its title from here. A skill with no entry keeps its H1, so adding
 * a skill never breaks the build; it just gets a plainer title until somebody
 * names the task.
 */
const TITLES = {
  "psych-quickstart": "First run, in detail",
  "psych-agents": "Define an agent",
  "psych-workflows": "Run fixed steps as a workflow",
  "psych-builder": "Build a Spec with the fluent builder",
  "psych-code-tools": "Connect Python functions as tools",
  "psych-http-tools": "Call an HTTP endpoint as a tool",
  "psych-mcp": "Connect MCP servers",
  "psych-mcp-oauth": "Authenticate to an MCP server with OAuth 2.1",
  "psych-a2a": "Work with other agents over A2A",
  "psych-sandbox": "Run code the model writes",
  "psych-agent-skills": "Load instructions on demand with skills",
  "psych-memory": "Remember facts across runs",
  "psych-subagents": "Delegate to subagents",
  "psych-approvals": "Require approval for selected tool calls",
  "psych-interrupts": "Stop or steer a running agent",
  "psych-suspend-resume": "Pause a run and resume it later",
  "psych-compaction": "Keep a long conversation inside the window",
  "psych-streaming": "Stream results and reconnect",
  "psych-report": "Inspect a run: report, status, answer",
  "psych-pricing": "Track token usage and cost",
  "psych-telemetry": "Export spans with OpenTelemetry",
  "psych-stores": "Persist and recover runs",
  "psych-blobs": "Handle large tool results",
  "psych-multitenancy": "Serve multiple tenants safely",
  "psych-testing": "Test agents without a network",
  psych: "Psych for coding agents",
};

for (const name of skillNames) {
  PAGES.push({
    from: `.agents/skills/${name}/SKILL.md`,
    // The router skill is written for an agent deciding which file to read
    // next, so it is published under Development rather than as the guides'
    // front page, which a person reads.
    to: name === "psych" ? "development/coding-agents.mdx" : `guides/${name.replace(/^psych-/, "")}.mdx`,
    title: TITLES[name] ?? null, // null: taken from the page's own H1
    description: null, // written below, per page
    isSkill: true,
    skillName: name,
  });
}

/**
 * MDX treats `{` and `<` as syntax. The library's Markdown is full of both,
 * inside prose as often as inside code, so every synced page is escaped rather
 * than trusted. Fenced blocks are left exactly as written: a snippet that gets
 * "helpfully" rewritten is a snippet that no longer runs.
 */
function escapeForMdx(markdown) {
  const parts = markdown.split(/(^```[\s\S]*?^```)/gm);
  return parts
    .map((part, index) =>
      index % 2 === 1
        ? part
        : part.replace(/(?<!`)\{(?![^`]*`)/g, "\\{").replace(/<(?![a-zA-Z/!])/g, "&lt;"),
    )
    .join("");
}

/**
 * Turn one skill into a page: drop its agent-facing frontmatter, lift the H1 as
 * the title, and take the first sentence of the body as the description.
 */
function fromSkill(body, name, title) {
  const withoutFrontmatter = body.replace(/^---\n[\s\S]*?\n---\n/, "");
  const heading = title ?? withoutFrontmatter.match(/^#\s+(.+)$/m)?.[1]?.trim() ?? name;
  const firstSentence =
    withoutFrontmatter
      .replace(/^#\s+.*$/m, "")
      .split(/\n\s*\n/)
      .map((block) => block.trim())
      .find((block) => block && !block.startsWith("#") && !block.startsWith("```")) ?? "";
  const flattened = firstSentence.replace(/\s+/g, " ").replace(/\*\*/g, "").replace(/`/g, "");
  const description =
    flattened.length <= 180
      ? flattened
      : `${flattened.slice(0, 180).replace(/\s+\S*$/, "")}...`;
  return { title: heading, description: description || heading, withoutFrontmatter };
}

function frontmatter({ title, description }, source) {
  return [
    "---",
    `title: ${JSON.stringify(title)}`,
    `description: ${JSON.stringify(description)}`,
    "---",
    "",
    `{/* Generated by web/scripts/sync-from-repo.mjs from ${source}. Edit that file. */}`,
    "",
  ].join("\n");
}

let written = 0;
const checkOnly = process.argv.includes("--check");
const stale = [];
for (const page of PAGES) {
  const source = join(REPO, page.from);
  if (!existsSync(source)) {
    console.error(
      `sync: ${page.from} does not exist. It was renamed or removed in the library; ` +
        `update PAGES in this script rather than leaving a stale page published.`,
    );
    process.exit(1);
  }

  const body = (await readFile(source, "utf8")).replace(/\r\n/g, "\n");
  const meta = page.isSkill ? fromSkill(body, page.skillName, page.title) : page;
  const content = page.isSkill ? meta.withoutFrontmatter : body;

  // Drop the source's own H1: the frontmatter title renders it already, and two
  // would put the same words on screen twice.
  const withoutTitle = content.trimStart().replace(/^#\s+.*\n+/, "");
  const target = join(OUT, page.to);
  const expected =
    frontmatter({ title: meta.title, description: meta.description }, page.from) +
      escapeForMdx(withoutTitle);
  if (checkOnly) {
    const current = existsSync(target) ? (await readFile(target, "utf8")).replace(/\r\n/g, "\n") : null;
    if (current !== expected) stale.push(page.to);
  } else {
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, expected, "utf8");
  }
  written += 1;
}

if (checkOnly) {
  if (stale.length) {
    console.error(
      "sync: the committed docs are out of date with the library.\n" +
        "Run `node web/scripts/sync-from-repo.mjs` and commit the result. This is the\n" +
        "check that stops a changelog entry or a design note from being published a\n" +
        "release late.\n\n" +
        stale.join("\n"),
    );
    process.exit(1);
  }
  console.log("sync: docs match the library");
} else {
  console.log(`sync: wrote ${written} pages into content/docs/next from the library`);
}
