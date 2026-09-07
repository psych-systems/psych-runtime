import type { MetadataRoute } from "next";
import { IS_PREVIEW } from "@/lib/env";
import { SITE } from "@/lib/site";

export const dynamic = "force-static";

export default function robots(): MetadataRoute.Robots {
  // A preview of the site is the site, byte for byte, so an indexed preview
  // competes with production for production's own results. Nothing crawls it.
  if (IS_PREVIEW) {
    return { rules: [{ userAgent: "*", disallow: "/" }] };
  }
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/api/"] }],
    sitemap: `${SITE.url}/sitemap.xml`,
    host: SITE.url,
  };
}
