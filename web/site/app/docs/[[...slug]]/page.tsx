import { notFound } from "next/navigation";
import { DocsBody, DocsDescription, DocsPage, DocsTitle } from "fumadocs-ui/page";
import { DocsLayout } from "fumadocs-ui/layouts/docs";
import { baseOptions } from "@/app/layout.config";
import { treeFor } from "@/lib/tree";
import defaultMdxComponents from "fumadocs-ui/mdx";
import { Card, Cards } from "fumadocs-ui/components/card";
import { source } from "@/lib/source";
import { CURRENT, banner, versionFor } from "@/lib/versions";
import { JsonLd, SOCIAL_IMAGE, breadcrumbs } from "@/lib/seo";
import { SITE } from "@/lib/site";

export default async function Page(props: { params: Promise<{ slug?: string[] }> }) {
  const params = await props.params;
  const slug = params.slug ?? [CURRENT.slug];
  const page = source.getPage(slug);
  if (!page) notFound();

  const version = versionFor(slug[0]);
  const notice = banner(version);
  const MDX = page.data.body;

  // Breadcrumbs from the page tree: the version index, then each folder,
  // then the page. The last crumb is the page itself.
  const trail = [{ name: "Docs", path: "/docs" }];
  const folders = slug.slice(1, -1);
  folders.forEach((segment, i) => {
    const folderSlug = slug.slice(0, i + 2);
    const index = source.getPage(folderSlug);
    trail.push({
      name: index?.data.title ?? segment.replaceAll("-", " "),
      path: `/docs/${folderSlug.join("/")}`,
    });
  });
  if (slug.length > 1) trail.push({ name: page.data.title, path: page.url });

  return (
    <DocsLayout tree={treeFor(slug[0])} {...baseOptions}>
      <DocsPage toc={page.data.toc} full={page.data.full}>
        <JsonLd data={breadcrumbs(trail)} />
        <DocsTitle>{page.data.title}</DocsTitle>
        <DocsDescription>{page.data.description}</DocsDescription>
        <DocsBody>
          {notice ? <p className="psych-version-banner">{notice}</p> : null}
          <MDX components={{ ...defaultMdxComponents, Card, Cards }} />
        </DocsBody>
      </DocsPage>
    </DocsLayout>
  );
}

export function generateStaticParams() {
  // The empty slug is `/docs` itself, which `source.generateParams()` does not
  // emit because no content file sits at the root of the tree. Without it the
  // export has no /docs page at all and every link to the bare URL is a 404.
  //
  // It renders the current version's index rather than redirecting to it, so
  // there is no host-specific rule to keep in step with `CURRENT`. Both URLs
  // compile the same MDX and cannot disagree; `canonical` below tells a crawler
  // which of the two is the real one.
  return [{ slug: [] as string[] }, ...source.generateParams()];
}

export async function generateMetadata(props: { params: Promise<{ slug?: string[] }> }) {
  const params = await props.params;
  const slug = params.slug ?? [CURRENT.slug];
  const page = source.getPage(slug);
  if (!page) notFound();
  // The changelog is served at /changelog in the marketing shell; the docs
  // copy stays reachable but points crawlers at the one route.
  const canonical = slug[slug.length - 1] === "changelog" ? "/changelog" : `/docs/${slug.join("/")}`;
  const title = slug.length <= 1 ? "Documentation" : page.data.title;
  return {
    title,
    description: page.data.description,
    alternates: { canonical },
    openGraph: {
      title: `${title} · ${SITE.name}`,
      description: page.data.description,
      url: `${SITE.url}${canonical}`,
      siteName: SITE.name,
      type: "article",
      images: [SOCIAL_IMAGE],
    },
    twitter: {
      card: "summary_large_image",
      title: `${title} · ${SITE.name}`,
      description: page.data.description,
      images: [SOCIAL_IMAGE],
    },
  };
}
