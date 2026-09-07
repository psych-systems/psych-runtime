/**
 * Refuse to build the production site while the install path leads to
 * documentation for a version nobody can install.
 *
 * `next` tracks `main` and is explicitly unreleased: it exists so a change to
 * behaviour and the change to its documentation land in the same pull request.
 * That is right for the repository and wrong for a visitor, who arrives from
 * `pip install psych-runtime`, follows "Run the example", and gets a page that
 * may describe code the package they just installed does not have.
 *
 * The banner on those pages is honest, but a banner is a warning, not a gate,
 * and this is the kind of thing that ships because everyone assumed somebody
 * would remember. So it is checked here, against the built output rather than
 * the configuration: what matters is where the link on the page actually goes.
 *
 * The same visitor's first command is `pip install psych-runtime`, so the gate
 * also asks PyPI whether that distribution exists. A site that says "pip
 * install" for a package nobody can install is not ready either, however
 * good the pages are. `RELEASE_GATE_OFFLINE=1` skips only that lookup, for a
 * machine with no network; it does not skip the docs checks.
 *
 * Cutting a release tree (`pnpm cut`) and publishing the wheel are what clear
 * it. Until then the preview build (`pnpm build:preview`) is unaffected on
 * purpose: reviewing the site before a release is exactly what the preview is
 * for. `pnpm build` no longer runs this, so CI can prove the site builds
 * without pretending it is ready to ship; `pnpm deploy` runs it before the
 * upload, which is the only place it needs to be.
 */
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";

const OUT = resolve(process.cwd(), "out");
const DISTRIBUTION = "psych-runtime";

/** Pages a visitor reaches first, and the link on each that must not be unreleased. */
const ENTRY_POINTS = [
  ["index.html", "the homepage's primary action"],
  ["docs.html", "the bare /docs URL"],
];

const problems = [];

for (const [file, what] of ENTRY_POINTS) {
  let html;
  try {
    html = await readFile(join(OUT, file), "utf8");
  } catch {
    problems.push(`${file} is missing from out/, so ${what} could not be checked`);
    continue;
  }
  const hrefs = [...html.matchAll(/href="(\/docs\/[^"]+)"/g)].map((m) => m[1]);
  const unreleased = [...new Set(hrefs.filter((h) => h.startsWith("/docs/next")))];
  if (unreleased.length) {
    problems.push(
      `${what} (${file}) links into the unreleased docs tree: ${unreleased.slice(0, 3).join(", ")}` +
        (unreleased.length > 3 ? ` and ${unreleased.length - 3} more` : ""),
    );
  }
}

// The banner that says no release tree exists yet. If it survives into a
// production build, every docs page is telling visitors the docs may not match
// their install, which is not a state to ship the marketing site in.
try {
  const docs = await readFile(join(OUT, "docs.html"), "utf8");
  if (docs.includes("No version of the docs has been cut yet")) {
    problems.push("/docs still carries the “no version cut yet” banner");
  }
} catch {
  /* already reported above */
}

// The install command on the page must install something.
if (process.env.RELEASE_GATE_OFFLINE) {
  console.warn("release-gate: RELEASE_GATE_OFFLINE set, not checking PyPI");
} else {
  try {
    const res = await fetch(`https://pypi.org/pypi/${DISTRIBUTION}/json`);
    if (res.status === 404) {
      problems.push(`${DISTRIBUTION} is not on PyPI, so \`pip install ${DISTRIBUTION}\` fails for every visitor`);
    } else if (!res.ok) {
      problems.push(`PyPI answered ${res.status} for ${DISTRIBUTION}; could not confirm the install command works`);
    }
  } catch (err) {
    problems.push(`could not reach PyPI to confirm ${DISTRIBUTION} is installable (${err.message})`);
  }
}

if (problems.length) {
  console.error(
    "\nrelease-gate: the production site is not ready to ship.\n\n" +
      problems.map((p) => `  - ${p}`).join("\n") +
      "\n\n  Publish the release and cut its docs tree, so the install\n" +
      "  command works and a bare /docs link resolves to documentation that\n" +
      "  matches the published package:\n\n" +
      "      cd web && pnpm cut <version>\n\n" +
      "  To review the site before that, deploy the preview instead:\n\n" +
      "      cd web && pnpm deploy:preview\n",
  );
  process.exit(1);
}

console.log("release-gate: the package is installable and the install path leads to released documentation");
