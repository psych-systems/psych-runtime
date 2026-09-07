import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import Link from "next/link";
import defaultMdxComponents from "fumadocs-ui/mdx";
import { source } from "@/lib/source";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";
import { ArrowRight } from "@/components/icons";

export const metadata = pageMeta({
  title: "Changelog",
  description:
    "Every change to Psych Runtime, written at commit time rather than reconstructed at release. The public API is 0.x and breaking changes are recorded here the day they land.",
  path: "/changelog",
});

/**
 * The changelog is `CHANGELOG.md` in the repository root, copied into the docs
 * tree by `web/scripts/sync-from-repo.mjs` and compiled by the same MDX
 * pipeline as every docs page. This route renders that compiled page inside
 * the marketing shell, so there is one changelog and it is the library's.
 *
 * The section index on the left is read from the Markdown at build time: the
 * compiled page does not expose its headings, and the index is the one thing
 * this route adds.
 */
/** fumadocs derives heading ids this way, and the index has to agree. */
function slugify(heading: string) {
  return heading
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export default async function ChangelogPage() {
  const page = source.getPage(["next", "changelog"]);
  if (!page) {
    throw new Error(
      "content/docs/next/changelog.mdx is missing. Run `pnpm sync` from web/ to copy it from CHANGELOG.md.",
    );
  }
  const MDX = page.data.body;
  const markdown = await readFile(resolve(process.cwd(), "..", "..", "CHANGELOG.md"), "utf8");
  const releases = [...markdown.matchAll(/^## (.+)$/gm)].map((m) => m[1].trim());
  const version = (await readFile(resolve(process.cwd(), "..", "..", "psych_runtime", "__init__.py"), "utf8")).match(
    /__version__ = "([^"]+)"/,
  )?.[1];

  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={88} y={36} size={42} opacity={0.28} drift />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">changelog</p>
        <h1 className="display d1">What changed, <span className="serif">and why.</span></h1>
        <p className="lede">
          Written at commit time. Read <strong>Removed</strong> and <strong>Changed</strong> first:
          those are the sections with something for you to do.
        </p>
      </div>
      </Stage>

      <section className="shell section-tight">
        <div className="release">
          <nav className="release-meta sticky" aria-label="Releases">
            <span className="meta">package version {version}</span>
            <ul>
              {releases.map((r) => (
                <li key={r}>
                  <a href={`#${slugify(r)}`}>{r}</a>
                </li>
              ))}
            </ul>
            <span className="meta">
              source:{" "}
              <a className="link" href={`${SITE.github}/blob/main/CHANGELOG.md`} rel="noopener">
                CHANGELOG.md
              </a>
            </span>
          </nav>
          <div className="release-body">
            <MDX components={defaultMdxComponents} />
          </div>
        </div>
        <p className="small mt-3">
          Documentation is versioned by minor and cut from the tree that tracks main.{" "}
          <Link className="link" href="/docs">
            Read the docs <ArrowRight size={13} aria-hidden />
          </Link>
        </p>
      </section>
    </>
  );
}
