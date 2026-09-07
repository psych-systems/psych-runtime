import Link from "next/link";
import { ArrowRight, ArrowUpRight, ICON } from "@/components/icons";
import { Check, FileCode2, GitPullRequest, TestTube2 } from "lucide-react";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { JsonLd, SOFTWARE_SOURCE_CODE, pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "Open source",
  description:
    "Read the Psych Runtime source, run its complete test gate, and contribute under Apache-2.0.",
  path: "/open-source",
});

export default function OpenSourcePage() {
  return (
    <>
      <JsonLd data={SOFTWARE_SOURCE_CODE} />

      <Stage className="page-head-stage">
        <Bloom x={86} y={30} size={44} opacity={0.3} drift />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">open source</p>
        <h1 className="display d1">
          Read every decision. <span className="serif">Test every change.</span>
        </h1>
        <p className="lede">
          The runtime, playground, documentation, and design reasoning live together. Inspect the
          implementation, run the same checks as CI, and contribute under {SITE.license}.
        </p>
        <div className="btn-row mt-2">
          <a className="btn btn-flame" href={SITE.github} rel="noopener">
            {SITE.org}/{SITE.distribution} <ArrowUpRight size={ICON} aria-hidden />
          </a>
          <a className="btn btn-ghost" href={SITE.pypi} rel="noopener">
            PyPI <ArrowUpRight size={ICON} aria-hidden />
          </a>
        </div>
      </div>
      </Stage>

      <section className="section-tight">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">what a change has to pass</p>
            <h2 className="display d2">
              The <span className="serif">build gate.</span>
            </h2>
            <div className="prose mt-2">
              <p>
                Lint, format, strict types, an import linter that keeps dependencies pointing inward,
                and three test layers that run as separate CI jobs so a failure names the layer.
                Contributions run the same commands locally.
              </p>
              <p>
                Store tests run against real PostgreSQL, MySQL and DynamoDB, which CI starts as
                service containers. No store is mocked, and no test calls a model provider or any
                other outside service. A feature with no end-to-end case asserting its behaviour is
                not done, whatever the line coverage says.
              </p>
            </div>
          </div>
          <div className="gate-panel" aria-label="The checks every change passes">
            <span><FileCode2 size={19} /><strong>Code quality</strong><small>Lint, format, strict types</small><Check size={15} /></span>
            <span><TestTube2 size={19} /><strong>Real behavior</strong><small>Unit, functional, end to end</small><Check size={15} /></span>
            <span><GitPullRequest size={19} /><strong>Architecture</strong><small>Imports and generated docs</small><Check size={15} /></span>
            <p className="small mt-2">
              The docs are part of it. The reference is generated from the module and the pages are
              copied from the repository, so the build fails on a diff rather than letting a page
              fall behind the code.
            </p>
          </div>
        </div>
      </section>

      <section className="section sunk">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">for coding agents</p>
            <h2 className="display d2">
              Twenty-six skills, one <span className="serif">per feature.</span>
            </h2>
            <div className="prose mt-2">
              <p>
                Written for an agent that has never seen the codebase: the mental model, the
                vocabulary, the invariants that get a change rejected, and the working code. They are
                the same files the documentation guides are generated from, so there is one copy and
                it is the tested one.
              </p>
            </div>
            <div className="btn-row mt-2">
              <Link className="btn btn-ghost btn-sm" href={docs("guides")}>
                Read them as guides <ArrowRight className="arrow" size={ICON} aria-hidden />
              </Link>
            </div>
          </div>
          <div className="skills-stack" aria-label="Feature guides installed with the package">
            <span>psych <small>Start here</small></span>
            <span>approvals <small>Pause and resume</small></span>
            <span>stores <small>Durable runs</small></span>
            <span>tools <small>Code, HTTP and MCP</small></span>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">status</p>
            <h2 className="display d2">
              Pre-alpha, at <span className="serif">0.x.</span>
            </h2>
          </div>
          <div className="prose">
            <p>
              The public API still changes between minor releases. Breaking changes go in the
              changelog in the commit that makes them true.
            </p>
            <p>
              A name scheduled for removal ships at least one minor release emitting a{" "}
              <code className="inline-code">DeprecationWarning</code> that names its replacement, so
              deprecated in 0.N means removed no earlier than 0.N+2. Security fixes are the
              exception. The full policy is in{" "}
              <a className="link" href={`${SITE.github}/blob/main/CONTRIBUTING.md`} rel="noopener">
                CONTRIBUTING.md
              </a>
              .
            </p>
            <p>
              <a className="link" href={`${SITE.github}/blob/main/CONTRIBUTING.md`} rel="noopener">
                CONTRIBUTING.md
              </a>{" "}
              ·{" "}
              <a className="link" href={`${SITE.github}/issues`} rel="noopener">
                Issues
              </a>{" "}
              ·{" "}
              <Link className="link" href="/changelog">
                Changelog
              </Link>
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
