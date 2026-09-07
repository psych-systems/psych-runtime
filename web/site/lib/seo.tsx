import type { Metadata } from "next";
import { SITE } from "@/lib/site";

/**
 * The social card, one image for the whole site.
 *
 * `/og.png` is `app/opengraph-image.tsx` after `scripts/name-images.mjs` has
 * given it an extension. Referenced by path rather than left to Next's file
 * convention, because the convention emits an extensionless URL that a static
 * host serves with the wrong content type.
 */
export const SOCIAL_IMAGE = {
  url: "/og.png",
  width: 1200,
  height: 630,
  alt: "Psych Runtime. Run AI agents inside your Python application, beside a record log: run_admitted, attempt_started, model_call_finished, tool_call_finished, run_settled.",
} as const;

/**
 * Per-page metadata with the fields every page must carry: a unique title
 * and description, a canonical URL, and the Open Graph and Twitter cards. The
 * social image is one brand image for the whole site, rendered at build time
 * from the Trident and the palette (`app/opengraph-image.tsx`).
 */
export function pageMeta({
  title,
  description,
  path,
  absoluteTitle = false,
}: {
  title: string;
  description: string;
  path: string;
  /**
   * Skip the root layout's `%s · Psych Runtime` template. The homepage title
   * already names the project, and the template turned it into "Psych Runtime:
   * run AI agents inside your Python application · Psych Runtime".
   */
  absoluteTitle?: boolean;
}): Metadata {
  const url = `${SITE.url}${path === "/" ? "" : path}`;
  return {
    title: absoluteTitle ? { absolute: title } : title,
    description,
    alternates: { canonical: url },
    openGraph: {
      title: absoluteTitle ? title : `${title} · ${SITE.name}`,
      description,
      url,
      siteName: SITE.name,
      type: "website",
      images: [SOCIAL_IMAGE],
    },
    twitter: {
      card: "summary_large_image",
      title: absoluteTitle ? title : `${title} · ${SITE.name}`,
      description,
      images: [SOCIAL_IMAGE],
    },
  };
}

/** Structured data, rendered as an inert JSON script. */
export function JsonLd({ data }: { data: Record<string, unknown> }) {
  return (
    <script
      type="application/ld+json"
      // JSON is serialised here, so the only content is what this file built.
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c") }}
    />
  );
}

export const SOFTWARE_APPLICATION = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: SITE.name,
  applicationCategory: "DeveloperApplication",
  operatingSystem: "Linux, macOS, Windows",
  description: SITE.description,
  url: SITE.url,
  license: "https://www.apache.org/licenses/LICENSE-2.0",
  programmingLanguage: "Python",
  softwareRequirements: "Python 3.12 or newer",
  downloadUrl: SITE.pypi,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  isAccessibleForFree: true,
  author: { "@type": "Organization", name: SITE.org, url: SITE.github },
};

export const SOFTWARE_SOURCE_CODE = {
  "@context": "https://schema.org",
  "@type": "SoftwareSourceCode",
  name: SITE.name,
  codeRepository: SITE.github,
  programmingLanguage: "Python",
  runtimePlatform: "Python 3.12",
  license: "https://www.apache.org/licenses/LICENSE-2.0",
  url: `${SITE.url}/open-source`,
  description: SITE.description,
};

export function breadcrumbs(items: { name: string; path: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, i) => ({
      "@type": "ListItem",
      position: i + 1,
      name: item.name,
      item: `${SITE.url}${item.path}`,
    })),
  };
}
