import Link from "next/link";
import { Code } from "@/components/Code";
import { SiteFooter } from "@/components/SiteFooter";
import { SiteNav } from "@/components/SiteNav";
import "./(site)/site.css";
import { ArrowRight, ICON } from "@/components/icons";

export const metadata = { title: "Not found" };

const LOG = `$ psych_runtime.status(store, run_id)
psych_runtime.RunNotFound: no Run with that id in this Scope

# A missing page is the same shape: nothing in the log by that name.
# The reads that do resolve are below.
`;

/**
 * The 404. Exported as 404.html, which the host serves for any path the build
 * did not produce. It composes the site shell directly because it lives
 * outside the (site) route group.
 */
export default function NotFound() {
  return (
    <div className="site">
      <SiteNav />
      <main id="main" className="shell notfound">
        <div>
          <p className="eyebrow">404 · not in the log</p>
          <Code code={LOG} lang="output" title="stderr" />
          <h1 className="display d2">This page was never <span className="serif">admitted.</span></h1>
          <p className="lede">
            Nothing was dispatched at this address. Every route that resolves is in the header
            above.
          </p>
          <div className="btn-row mt-2">
            <Link className="btn btn-primary" href="/docs">
              Documentation <ArrowRight className="arrow" size={ICON} aria-hidden />
            </Link>
            <Link className="btn btn-ghost" href="/">
              Home <ArrowRight className="arrow" size={ICON} aria-hidden />
            </Link>
          </div>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}
