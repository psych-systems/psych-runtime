import { defineDocs, defineConfig } from "fumadocs-mdx/config";
import remarkFoldChangelog from "./lib/remark-fold-changelog.mjs";

export const docs = defineDocs({
  dir: "content/docs",
});

export default defineConfig({
  mdxOptions: {
    // Psych's own docs are dense with identifiers. Keeping code untouched by
    // any smart-quote or typographic transform matters more here than it looks:
    // a curly quote inside a copied snippet is a SyntaxError somebody else
    // spends ten minutes on.
    remarkPlugins: [remarkFoldChangelog],
    rehypeCodeOptions: {
      themes: { light: "github-light", dark: "github-dark" },
      /**
       * Two colours in the stock GitHub themes miss WCAG AA against the card
       * the docs paint code on. Measured, not guessed: the light theme's
       * parameter orange `#e36209` is 3.46:1 on `#fffefb`, and the dark theme
       * reuses the light theme's comment grey `#6a737d`, which is 3.63:1 on
       * `#141a22`. Comments are the last thing that should be hard to read.
       *
       * Keyed by theme, because `#6a737d` is *also* the light theme's comment
       * colour, where it passes at 4.77:1: replacing it everywhere would fix
       * the dark theme by breaking the light one. Re-measure if either theme
       * changes.
       */
      colorReplacements: {
        "github-light": { "#e36209": "#a8460b" },
        "github-dark": { "#6a737d": "#939ca6" },
      },
    },
  },
});
