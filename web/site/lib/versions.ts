/**
 * Which versions of the documentation exist, and which one a bare URL reaches.
 *
 * Docs are versioned by **minor**, not by patch. A patch release that changes
 * no documented behaviour would otherwise fork the whole tree for nothing, and
 * a reader landing on v0.2.3 rather than v0.2.1 learns nothing from the
 * difference. When a patch does change documented behaviour, it is corrected in
 * place on that minor's tree, which is what a reader on that version wants.
 *
 * `next` tracks main and is explicitly unreleased. It exists so a change to
 * behaviour and the change to its documentation land in the same pull request,
 * which is the only way documentation stays true.
 */

export type DocsVersion = {
  /** URL segment, and the directory under `content/docs/`. */
  readonly slug: string;
  /** What the picker shows. */
  readonly label: string;
  /** The library releases this tree documents. */
  readonly covers: string;
  readonly state: "current" | "next" | "maintenance";
};

// Maintained by `pnpm cut`, which rewrites this list from the directories that
// actually exist under content/docs. Do not add an entry by hand: a version
// listed here with no tree behind it is a 404 for every reader who picks it, and
// `lib/source.ts` now fails the build rather than shipping one.
export const VERSIONS: readonly DocsVersion[] = [
  { slug: "next", label: "next", covers: "unreleased, tracks main", state: "next" },
  { slug: "v0.1", label: "v0.1", covers: "0.1.x", state: "current" },
];

/** The version a bare `/docs` URL resolves to. */
export const CURRENT: DocsVersion =
  VERSIONS.find((version) => version.state === "current") ?? VERSIONS[0];

export function versionFor(slug: string | undefined): DocsVersion {
  return VERSIONS.find((version) => version.slug === slug) ?? CURRENT;
}

/**
 * Why a reader should be told they are not on the current version.
 *
 * Returns null for the current one, because a banner on every page of the
 * version most people want is noise that trains them to ignore the banner that
 * matters.
 */
export function banner(version: DocsVersion): string | null {
  if (version.state === "current") return null;
  if (version.state === "next") {
    // Before the first `pnpm cut` there is no released tree to send anyone to,
    // and pointing them at `next` while calling it "the released library" is a
    // lie a reader will act on. Say which case they are in instead.
    return CURRENT.state === "next"
      ? "This documents main. No version of the docs has been cut yet, so it may describe behaviour the published package does not have."
      : `This is the documentation for unreleased changes on main. For the released library, read ${CURRENT.label}.`;
  }
  return `This is ${version.label}, which is no longer the current release. The current documentation is ${CURRENT.label}.`;
}

/**
 * A link into the documentation from outside it.
 *
 * The marketing pages must not hard-code `/docs/next/...`: after the first
 * `pnpm cut` that path is the unreleased tree, and every link on the site would
 * quietly send readers to documentation for code they have not got. This
 * resolves against `CURRENT`, so those links follow the release.
 */
export function docs(path = ""): string {
  const tail = path.replace(/^\/+/, "");
  return tail ? `/docs/${CURRENT.slug}/${tail}` : "/docs";
}
