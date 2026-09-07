import Link from "next/link";
import { ArrowUpRight, Check, Wrench, Database, BrainCircuit, GitBranch, Bot, Workflow } from "lucide-react";
import { Trident } from "@/components/Trident";
import { HomeRuntime } from "@/components/HomeRuntime";
import { HOME } from "@/content/site/home";
import { JsonLd, SOFTWARE_APPLICATION, pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";
import { docs } from "@/lib/versions";
import "./home.css";

export const metadata = pageMeta({ title: "Psych Runtime: build AI agents and workflows in Python", description: SITE.description, path: "/", absoluteTitle: true });

export default function Home() {
  return (
    <div className="home">
      <JsonLd data={SOFTWARE_APPLICATION} />
      <section className="shell home-intro" aria-labelledby="hero-title">
        <p className="home-eyebrow"><span /> Agent runtime for Python</p>
        <h1 id="hero-title">Build AI agents<br /><em>that finish the work.</em></h1>
        <p className="home-summary">{HOME.introduction}</p>
        <div className="home-actions">
          <a className="home-button home-button-dark" href="#see-psych">See how it works <span aria-hidden>↓</span></a>
          <Link className="home-button home-button-plain" href={docs("get-started")}>Start building <ArrowUpRight size={16} /></Link>
        </div>
      </section>
      <section className="shell home-showcase" id="see-psych" aria-label="Explore what the runtime does"><HomeRuntime /></section>
      <section className="shell home-understanding" aria-labelledby="understanding-title">
        <div><p className="home-eyebrow">The work between model calls</p><h2 id="understanding-title">From a model decision<br /><em>to a finished run.</em></h2></div>
        <p>{HOME.explanation}</p>
      </section>
      <section className="shell home-features" aria-label="More of the runtime">
        {HOME.features.map((feature, index) => (
          <article className={`home-feature home-feature-${index}`} key={feature.title}>
            <div className="home-feature-art" aria-hidden="true">
              {index === 0 ? <div className="agent-art"><span><Bot size={31} /></span><strong>Agent</strong><small>Model + tools + rules</small></div> : null}
              {index === 1 ? <div className="history-art"><span><Check size={14} /> Step completed <small>01</small></span><span><Workflow size={14} /> Agent step <small>02</small></span><span><GitBranch size={14} /> Continue workflow <small>03</small></span></div> : null}
              {index === 2 ? <div className="tools-art"><span>Python</span><span className="tool-tile"><Wrench size={31} /></span><span>MCP</span><span>HTTP</span></div> : null}
              {index === 3 ? <div className="memory-art"><Database size={30} /><span>customer prefers email</span><span>timezone is UTC+5:30</span><small>Scoped to one end user</small></div> : null}
            </div>
            <div className="home-feature-copy"><h3>{feature.title}</h3><p>{feature.description}</p><Link href={docs(feature.guide)} aria-label={`Read more: ${feature.title}`}><ArrowUpRight size={20} /></Link></div>
          </article>
        ))}
      </section>
      <section className="shell home-fit" aria-labelledby="fit-title">
        <div className="home-fit-copy"><p className="home-eyebrow">Inside your application</p><h2 id="fit-title">Keep your product.<br /><em>Add agent execution.</em></h2><p>{HOME.fit}</p><Link className="home-button home-button-plain" href="/runtime">Explore the architecture <ArrowUpRight size={16} /></Link></div>
        <div className="home-system" role="img" aria-label="Psych Runtime lives inside your application and connects to your model, tools, and store">
          <span className="home-system-label">Your application</span>
          <div className="home-system-core"><Trident size={38} /><strong>Psych</strong><span>Agent execution</span></div>
          <div className="home-system-lines" aria-hidden="true"><i /><i /><i /></div>
          <div className="home-system-ports"><span><BrainCircuit size={21} />Your model</span><span><Wrench size={21} />Your tools</span><span><Database size={21} />Your store</span></div>
        </div>
      </section>
      <section className="shell home-faq" aria-labelledby="faq-title">
        <div className="home-faq-head"><p className="home-eyebrow">The practical questions</p><h2 id="faq-title">Before you<br /><em>start building.</em></h2></div>
        <div className="home-faq-list">
          {HOME.faqs.map((item, index) => <details key={item.question} open={index === 0}><summary>{item.question}<span aria-hidden>+</span></summary><p>{item.answer}</p></details>)}
        </div>
      </section>
      <section className="shell home-closing" aria-labelledby="start-title">
        <div className="home-closing-copy"><p className="home-eyebrow">Build with Psych</p><h2 id="start-title">Your first agent<br /><em>starts with one tool.</em></h2><Link className="home-button home-button-dark" href={docs("get-started")}>Build your first agent <ArrowUpRight size={17} /></Link><p>{HOME.availability}</p></div>
        <div className="home-closing-mark" aria-hidden="true"><div /><Trident size={150} /></div>
      </section>
    </div>
  );
}
