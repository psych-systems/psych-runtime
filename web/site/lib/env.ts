/**
 * Which build this is: the live site, or a preview of it.
 *
 * `PSYCH_SITE_ENV=preview` changes three things and nothing else, so what a
 * reviewer sees is the site rather than a variant of it:
 *
 *   1. Crawlers are told to stay out, in robots.txt, in a meta tag and in a
 *      response header. A preview that gets indexed competes with the real
 *      site for its own search results, and the damage outlasts the preview.
 *   2. Absolute URLs point at the preview origin, so a link shared from it
 *      resolves and its social card renders.
 *   3. A strip across the top says which build it is and when it was made.
 *
 * Everything else, every page, every byte of content, is identical to what a
 * promotion to production would publish.
 */
export const SITE_ENV = process.env.PSYCH_SITE_ENV === "preview" ? "preview" : "production";

export const IS_PREVIEW = SITE_ENV === "preview";

/** The origin this build will be served from. */
export const SITE_URL =
  process.env.NEXT_PUBLIC_SITE_URL?.replace(/\/$/, "") ?? "https://psychruntime.com";

/**
 * When the build ran, to the minute. A reviewer looking at two tabs needs to
 * know which is newer, and "did my change deploy" is the question a preview
 * exists to answer.
 */
export const BUILT_AT = new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC";
