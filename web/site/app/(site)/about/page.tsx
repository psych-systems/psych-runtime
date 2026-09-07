import Link from "next/link";
import { Trident } from "@/components/Trident";
import { ArrowRight, ArrowUpRight, ICON } from "@/components/icons";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "About",
  description:
    "Why Psych Runtime exists, who it is for, who maintains it, and where the name and the mark come from.",
  path: "/about",
});

export default function AboutPage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={88} y={34} size={46} tone="gold" opacity={0.32} drift />
        <Bloom x={10} y={80} size={34} tone="ember" opacity={0.22} />
        <Grain opacity={0.22} />
        <div className="shell page-head">
          <p className="eyebrow">about</p>
          <h1 className="display d1">
            The hard part starts <span className="serif">after the first tool call.</span>
          </h1>
          <p className="lede">
            Psych exists so teams can build agents and workflows without writing an execution
            engine for every product.
          </p>
        </div>
      </Stage>

      <section className="section-tight">
        <div className="shell cols-2">
          <div>
            <h2 className="display d3">Why it exists</h2>
            <div className="prose mt-1">
              <p>
                Getting a model to call a tool is the first step. Production work
                also has to survive a stopped process, wait for a person, keep
                tenants apart, and explain what the run costs.
              </p>
              <p>
                Every team building on agents writes that layer. This is that
                layer, written once, with the reasoning recorded next to it.
              </p>
            </div>
          </div>
          <div>
            <h2 className="display d3">Who it is for</h2>
            <div className="prose mt-1">
              <p>
                A company with a service, a database, an identity system and a
                frontend already. They want their users to create agents, run
                them, and see what happened. They do not want to write an agent
                loop, a durable execution engine, a token accounting system or
                an MCP client pool.
              </p>
              <p>
                They would also not thank a dependency that arrived with its
                own users, its own tables and its own web server.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="section sunk" aria-labelledby="framework-title">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">how it is shaped</p>
            <h2 id="framework-title" className="display d2">
              A library, not <span className="serif">a framework.</span>
            </h2>
            <div className="prose mt-2">
              <p>
                You call Psych; it calls back only through ports you supplied:
                your store, your model client, your sandbox, your telemetry. It
                never runs your agents for you, because the moment it did you
                would have a second service to deploy, version and page someone
                about.
              </p>
            </div>
          </div>
          <div className="prose">
            <p>
              Each design decision is written down with the failure it prevents:
              why a report is computed from the log rather than stored, why a
              contradictory log fails loudly, why a lease and a deadline are
              separate mechanisms.
            </p>
            <div className="btn-row mt-2">
              <Link className="btn btn-ghost btn-sm" href={docs("design")}>
                The design notes{" "}
                <ArrowRight className="arrow" size={ICON} aria-hidden />
              </Link>
              <Link className="btn btn-ghost btn-sm" href="/runtime">
                How it fits together{" "}
                <ArrowRight className="arrow" size={ICON} aria-hidden />
              </Link>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">the name and the mark</p>
            <h2 className="display d2">
              Psych, and the <span className="serif">trident.</span>
            </h2>
            <div className="prose mt-2">
              <p>
                The name is short for psyche, and the mark started from the
                shape of the Greek letter psi. That is where the idea came
                from rather than a claim about what the word means: the point
                was cognition and the machinery under it, not the clinic.
              </p>
              <p>
                Drawn as a system glyph, three prongs meet one stem. Read them
                as the model, the tools and the store you supply, with the
                record log they fold into running up the middle. Everything you
                read back comes up that stem.
              </p>
            </div>
          </div>
          <div
            className="card"
            style={{
              display: "grid",
              placeItems: "center",
              padding: 40,
              color: "var(--accent)",
            }}
          >
            <Trident
              size={168}
              title="The Psych Runtime mark: psi drawn as a trident"
            />
          </div>
        </div>
      </section>

      <section className="section sunk" aria-labelledby="maintained-title">
        <div className="shell cols-2">
          <div>
            <p className="eyebrow">who maintains it</p>
            <h2 id="maintained-title" className="display d2">
              In the open, under <span className="serif">Apache-2.0.</span>
            </h2>
          </div>
          <div className="prose">
            <p>
              Psych Runtime is developed by {SITE.org} in a single public
              repository: the library, the playground application, this site
              and the design document all live in it. Issues and pull requests
              are the whole process, and the contribution guide says what a
              change has to pass before it lands.
            </p>
            <p>
              It is pre-alpha at 0.x, and the public API still changes between
              minor releases. Names scheduled for removal ship at least one minor
              release warning first, and every breaking change is written into
              the changelog by the commit that makes it true.
            </p>
            <div className="btn-row mt-2">
              <a className="btn btn-ghost btn-sm" href={SITE.github} rel="noopener">
                {SITE.org}/{SITE.distribution}{" "}
                <ArrowUpRight size={ICON} aria-hidden />
              </a>
              <a
                className="btn btn-ghost btn-sm"
                href={`${SITE.github}/blob/main/CONTRIBUTING.md`}
                rel="noopener"
              >
                Contributing <ArrowUpRight size={ICON} aria-hidden />
              </a>
              <Link className="btn btn-ghost btn-sm" href="/changelog">
                Changelog{" "}
                <ArrowRight className="arrow" size={ICON} aria-hidden />
              </Link>
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
