import type { ReactNode } from "react";
import { SiteNav } from "@/components/SiteNav";
import { SiteFooter } from "@/components/SiteFooter";
import { Reveal } from "@/components/Reveal";
import "./site.css";

/**
 * The marketing shell: header, footer, and the scroll reveal. The docs have
 * their own layout under /docs because fumadocs owns the sidebar and search
 * there; the two share tokens, the Trident and the link list, not markup.
 */
export default function SiteLayout({ children }: { children: ReactNode }) {
  return (
    <div className="site">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <SiteNav />
      <main id="main">{children}</main>
      <SiteFooter />
      <Reveal />
    </div>
  );
}
