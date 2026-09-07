import Link from "next/link";
import { INTEGRATIONS } from "@/content/site/integrations";
import { ArrowRight, ICON } from "@/components/icons";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "Integrations",
  description:
    "Connect Psych Runtime to your model provider, tools, databases, sandboxes, and telemetry.",
  path: "/integrations",
});

export default function IntegrationsPage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={88} y={28} size={44} opacity={0.3} drift />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">integrations</p>
        <h1 className="display d1">
          Bring the systems you use.{" "}
          <span className="serif">Psych connects the run.</span>
        </h1>
        <p className="lede">
          Connect models, tools, storage, sandboxes, and telemetry through built-in adapters or
          small ports. Every entry below states what works today and what you need to supply.
        </p>
        <ul className="legend mt-2">
          <li><span className="kind" data-kind="built-in">built in</span> an adapter in the package</li>
          <li><span className="kind" data-kind="extra">extra</span> in the package, behind a pip extra</li>
          <li><span className="kind" data-kind="compatible">compatible</span> works through an existing adapter</li>
          <li><span className="kind" data-kind="yours">yours</span> a port you implement, or a template you copy</li>
        </ul>
      </div>
      </Stage>

      <section className="shell section-tight">
        <div className="integration-map" aria-label="Psych connects your model, tools, storage, and telemetry">
          <div className="integration-core"><span>Psych</span><small>Runtime</small></div>
          {INTEGRATIONS.slice(0, 4).map((group, index) => (
            <div className="integration-port" data-port={index + 1} key={group.title}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{group.title}</strong>
              <small>{group.items.slice(0, 3).map((item) => item.name).join(" · ")}</small>
            </div>
          ))}
        </div>
      </section>

      <section className="shell">
        {INTEGRATIONS.map((group) => (
          <div className="mt-3" key={group.title}>
            <div className="section-head" style={{ marginBottom: 16 }}>
              <div>
                <h2 className="display d3">{group.title}</h2>
              </div>
              <p className="small">{group.note}</p>
            </div>
            <div className="rows">
              {group.items.map((item) => (
                <div className="row plain" key={item.name}>
                  <h3>
                    {item.name}
                    <span className="kind" data-kind={item.kind}>
                      {item.kind === "built-in" ? "built in" : item.kind}
                    </span>
                    {item.via ? <span className="ref">{item.via}</span> : null}
                  </h3>
                  <p>{item.how}</p>
                </div>
              ))}
            </div>
          </div>
        ))}

        <div className="mt-3 prose">
          <p>
            <strong>Writing your own.</strong> A Store or BlobStore adapter is correct when it passes
            the shared contract suite, the same tests the shipped four pass. A model provider with a
            different wire protocol is one port.
          </p>
        </div>
        <div className="btn-row mt-2">
          <Link className="btn btn-flame" href={docs("guides/stores")}>
            Stores guide <ArrowRight className="arrow" size={ICON} aria-hidden />
          </Link>
          <Link className="btn btn-ghost" href={docs("reference/ports")}>
            Every port <ArrowRight className="arrow" size={ICON} aria-hidden />
          </Link>
        </div>
      </section>
    </>
  );
}
