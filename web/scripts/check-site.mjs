#!/usr/bin/env node
/**
 * Keep the site honest about the library it advertises.
 *
 * A marketing page is the one document nobody re-reads after launch, and it
 * carries the things a visitor copies first: the install command and the code
 * samples. Both went stale in this repository once already, which is what this
 * exists to stop happening twice.
 *
 * It checks facts, not prose. The distribution name, the import name and the
 * called symbols are read out of the package itself; the argument, the tone
 * and the layout are nobody's business but the page's.
 *
 * Every file under `web/site/content/site/` and `web/site/lib/site.ts` is
 * checked: that is where the site keeps every string it states about the
 * package, deliberately away from the components that lay them out.
 */

import { readdir, readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..");
const CONTENT = resolve(HERE, "..", "site", "content", "site");

const pyproject = await readFile(resolve(REPO, "pyproject.toml"), "utf8");
const initFile = await readFile(resolve(REPO, "psych_runtime", "__init__.py"), "utf8");

const distribution = pyproject.match(/^name = "([^"]+)"/m)?.[1];
const version = initFile.match(/__version__ = "([^"]+)"/)?.[1];
const exported = new Set(
  [...initFile.matchAll(/^\s{4}"([A-Za-z_][A-Za-z0-9_]*)",$/gm)].map((match) => match[1]),
);

const files = [
  resolve(HERE, "..", "site", "lib", "site.ts"),
  ...(await readdir(CONTENT)).map((name) => join(CONTENT, name)),
];

const problems = [];
let checked = 0;

for (const file of files) {
  const text = await readFile(file, "utf8");
  const short = file.slice(REPO.length + 1);

  if (short.endsWith("lib/site.ts") && !text.includes(`install: "pip install ${distribution}"`)) {
    problems.push(
      `${short}: the install command does not say "pip install ${distribution}". ` +
        `That is the single most copied string on the site.`,
    );
  }

  if (/(?<![\w_.])import psych(?![\w_])/.test(text)) {
    problems.push(
      `${short}: a sample still imports \`psych\`. The import is \`psych_runtime\`; ` +
        "`psych` is an unrelated package on PyPI and would install someone else's code.",
    );
  }

  // Every psych_runtime.X the samples call must still be exported. A renamed
  // symbol should break the build here rather than in a visitor's terminal.
  const called = new Set(
    [...text.matchAll(/psych_runtime\.([A-Za-z_][A-Za-z0-9_]*)/g)]
      .map((m) => m[1])
      // Submodule paths (psych_runtime.store.postgres) are documented as
      // internal and are not on __all__ by design.
      .filter((name) => !["store", "testing", "memory", "sandbox", "a2a", "model", "core", "tools", "runtime"].includes(name)),
  );
  for (const symbol of called) {
    checked += 1;
    if (!exported.has(symbol)) {
      problems.push(
        `${short}: calls psych_runtime.${symbol}, which is not in __all__. ` +
          `Either the sample is out of date or the symbol should be public.`,
      );
    }
  }
}

if (!version) problems.push("psych_runtime/__init__.py has no __version__; the changelog page reads it.");

if (problems.length > 0) {
  console.error("check-site: the site no longer matches the library.\n");
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error(
    "\nFix the file named above. A site whose snippet does not run costs more trust than it ever bought.",
  );
  process.exit(1);
}

console.log(`check-site: the site matches ${distribution} ${version} (${checked} symbol references checked)`);
