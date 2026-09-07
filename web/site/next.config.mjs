import { createMDX } from "fumadocs-mdx/next";

const withMDX = createMDX();

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,

  // Every page here is content compiled at build time. Exporting to plain HTML
  // says so, and it removes a whole category of outage: there is no server to be
  // slow, to run out of memory, or to serve a page the build never produced.
  //
  // Two Next.js features are unavailable in this mode, and both have a better
  // home on the host: `redirects()` lives in `public/_redirects`, and response
  // headers live in `public/_headers`. Cloudflare reads both. Keeping them there
  // means the landing page and the docs are configured the same way instead of
  // one of them hiding its rules inside a JavaScript build.
  output: "export",
  images: { unoptimized: true },
};

export default withMDX(config);
