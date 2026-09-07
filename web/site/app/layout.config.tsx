import type { BaseLayoutProps } from "fumadocs-ui/layouts/shared";
import { TridentTile } from "@/components/Trident";
import { SITE } from "@/lib/site";

/**
 * The documentation nav.
 *
 * Deliberately short. This site's marketing pages persuade and the docs
 * instruct, and a reader mid-task reaches for the reference, the changelog and
 * the source, not for the capabilities page. The mark links home, which is the
 * one door back.
 */
export const baseOptions: BaseLayoutProps = {
  nav: {
    title: (
      <span className="docs-brand">
        <TridentTile size={26} />
        <span className="wordmark">
          Psych <span>Runtime</span>
        </span>
      </span>
    ),
    url: "/",
  },
  links: [
    { text: "Docs", url: "/docs", active: "nested-url" },
    { text: "Changelog", url: "/changelog" },
    { text: "PyPI", url: SITE.pypi, external: true },
  ],
  githubUrl: SITE.github,
};
