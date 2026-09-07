import Link from "next/link";
import { CAPABILITIES, CAPABILITY_GROUPS } from "@/content/site/capabilities";
import { ArrowRight, ICON } from "@/components/icons";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "Capabilities",
  description:
    "What Psych Runtime handles while an agent works, with the mechanism and guide for each behavior.",
  path: "/capabilities",
});

export default function CapabilitiesPage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={90} y={26} size={44} tone="ember" opacity={0.3} drift />
        <Bloom x={10} y={78} size={34} tone="gold" opacity={0.24} />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">capabilities</p>
        <h1 className="display d1">
          What Psych handles <span className="serif">while an agent works.</span>
        </h1>
        <p className="lede">
          Browse the behavior you need. Every item below exists in the runtime today, and opens to
          show the mechanism that keeps it reliable.
        </p>
        <nav className="chip-row mt-2" aria-label="Capability groups">
          {CAPABILITY_GROUPS.map((g) => (
            <a className="chip" key={g.name} href={`#${g.name.toLowerCase()}`}>
              {g.name}
              <span>{CAPABILITIES.filter((c) => c.group === g.name).length}</span>
            </a>
          ))}
        </nav>
      </div>
      </Stage>

      <section className="shell section-tight">
        {CAPABILITY_GROUPS.map((group) => (
          <section
            key={group.name}
            id={group.name.toLowerCase()}
            aria-labelledby={`${group.name.toLowerCase()}-title`}
            className="cap-group"
          >
            <div className="section-head">
              <h2 id={`${group.name.toLowerCase()}-title`} className="display d3">
                {group.name}
              </h2>
              <p className="lede">{group.blurb}</p>
            </div>
            <div className="rows">
              {CAPABILITIES.filter((c) => c.group === group.name).map((c, i) => (
                <article className="row" id={c.slug} key={c.slug}>
                  <span className="n">{String(i + 1).padStart(2, "0")}</span>
                  <h3>{c.title}</h3>
                  <div>
                    <p>{c.guarantee}</p>
                    {/* The mechanism is the evidence, and most readers scanning a
                        list of guarantees do not want it inline. Folded, not cut. */}
                    <details className="how">
                      <summary>how it is kept</summary>
                      <ul>
                        {c.mechanism.map((m) => (
                          <li key={m}>{m}</li>
                        ))}
                      </ul>
                    </details>
                    <span className="ref">
                      {c.design}
                      {c.guide ? (
                        <>
                          {" · "}
                          <Link className="link" href={docs(c.guide)}>
                            guide
                          </Link>
                        </>
                      ) : null}
                    </span>
                  </div>
                </article>
              ))}
            </div>
          </section>
        ))}

        <div className="mt-3 prose">
          <p>
            <Link className="link" href="/runtime">
              What Psych leaves to your application
            </Link>
            .
          </p>
        </div>
        <div className="btn-row mt-2">
          <Link className="btn btn-flame" href="/examples">
            See it running <ArrowRight className="arrow" size={ICON} aria-hidden />
          </Link>
        </div>
      </section>
    </>
  );
}
