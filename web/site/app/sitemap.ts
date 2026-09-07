import type { MetadataRoute } from "next";
import { source } from "@/lib/source";
import { SITE } from "@/lib/site";

export const dynamic = "force-static";

/**
 * Every route the build produces, marketing and docs alike. The docs entries
 * come from the same page tree the pages render from, so the sitemap cannot
 * list a page that does not exist.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const marketing: MetadataRoute.Sitemap = [
    { url: `${SITE.url}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE.url}/runtime`, changeFrequency: "monthly", priority: 0.9 },
    { url: `${SITE.url}/capabilities`, changeFrequency: "monthly", priority: 0.9 },
    { url: `${SITE.url}/examples`, changeFrequency: "monthly", priority: 0.8 },
    { url: `${SITE.url}/integrations`, changeFrequency: "monthly", priority: 0.8 },
    { url: `${SITE.url}/playground`, changeFrequency: "monthly", priority: 0.7 },
    { url: `${SITE.url}/open-source`, changeFrequency: "monthly", priority: 0.7 },
    { url: `${SITE.url}/changelog`, changeFrequency: "weekly", priority: 0.7 },
    { url: `${SITE.url}/about`, changeFrequency: "yearly", priority: 0.5 },
    { url: `${SITE.url}/docs`, changeFrequency: "weekly", priority: 0.9 },
  ];

  const docs: MetadataRoute.Sitemap = source
    .getPages()
    .filter((page) => !page.url.endsWith("/changelog"))
    .map((page) => ({
      url: `${SITE.url}${page.url}`,
      changeFrequency: "weekly" as const,
      priority: 0.6,
    }));

  return [...marketing, ...docs];
}
