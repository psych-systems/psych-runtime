#!/usr/bin/env node
/**
 * Give the generated images a file extension.
 *
 * Next's image conventions export to extensionless paths (`out/opengraph-image`,
 * `out/apple-icon`). A static host decides content type from the extension, so
 * those files are served as bytes rather than as `image/png` and every social
 * preview silently shows nothing. Nobody notices, because the page itself is
 * fine and the failure is on someone else's server.
 *
 * So the build renames them and the metadata points at the renamed paths. Run
 * after `next build`; the package's `build` script does.
 */

import { readdir, rename, rm, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "out");

/** Generated path -> the name the metadata in `app/` references. */
const RENAMES = [
  ["opengraph-image", "og.png"],
  ["apple-icon", "apple-touch-icon.png"],
];

const problems = [];
for (const [from, to] of RENAMES) {
  const source = join(OUT, from);
  try {
    const info = await stat(source);
    if (info.isDirectory()) {
      // Next writes a directory when the route has a query-string variant.
      const [first] = await readdir(source);
      await rename(join(source, first), join(OUT, to));
      await rm(source, { recursive: true, force: true });
    } else {
      await rename(source, join(OUT, to));
    }
    console.log(`name-images: ${from} -> ${to}`);
  } catch (error) {
    problems.push(`${from}: ${error.message}`);
  }
}

if (problems.length > 0) {
  console.error(
    "name-images: an image the metadata references was not generated.\n  " +
      problems.join("\n  ") +
      "\nThe social preview and the touch icon are 404s until this is fixed.",
  );
  process.exit(1);
}
