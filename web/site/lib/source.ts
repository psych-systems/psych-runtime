import { loader } from "fumadocs-core/source";
import { docs } from "@/.source/server";
import { VERSIONS } from "@/lib/versions";

/**
 * One loader over every version, with the version as the first path segment.
 *
 * fumadocs supports i18n-style trees and a version is not a language: a page
 * missing from one version should 404 rather than fall back to another
 * version's text, because silently showing v0.1's behaviour to somebody reading
 * v0.2 is worse than an honest 404.
 */
export const source = loader({
  baseUrl: "/docs",
  source: docs.toFumadocsSource(),
});

// `pnpm cut` writes the version list from the directories on disk, so the two
// agree by construction. They stop agreeing the moment somebody edits the list
// by hand, and the symptom is subtle: the version picker offers a version, the
// bare /docs URL resolves to it, and every one of those pages is a 404. Failing
// here turns that into a build error in the change that caused it.
const orphans = VERSIONS.filter((version) => !source.getPage([version.slug]));
if (orphans.length > 0) {
  throw new Error(
    `lib/versions.ts lists ${orphans.map((v) => v.slug).join(", ")}, but ` +
      `content/docs has no tree for it. Run \`pnpm cut\` rather than editing the list.`,
  );
}
