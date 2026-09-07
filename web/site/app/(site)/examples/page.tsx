import Link from "next/link";
import { Code } from "@/components/Code";
import { APPROVAL, CLI, MCP, TOUR_OUTPUT, WORKFLOW } from "@/content/site/code";
import { ArrowRight, ArrowUpRight, ICON } from "@/components/icons";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "Examples",
  description:
    "Start with working Psych agents and workflows, then adapt the model, tools, and storage to your application.",
  path: "/examples",
});

const TEMPLATES = [
  {
    name: "minimal",
    task: "Ask an agent where an order is, and let it call a tool to find out.",
    cmd: "psych new demo && cd demo && python main.py",
    result: "Prints the answer, then the token totals and the terminal state. No API key, no database, no Docker.",
  },
  {
    name: "tour",
    task: "The same agent, plus the five features you reach for next: an approval, a skill loaded on demand, a fact remembered across Runs, a workflow, and the report for all of it.",
    cmd: "psych new demo --template tour && cd demo && python main.py",
    result: "Stops at issue_refund and prints what it is waiting on, then resumes, answers, and prints usage, cost and wall-clock latency.",
  },
  {
    name: "fastapi",
    task: "Put an agent behind your own HTTP API, with the Worker in a process of its own.",
    cmd: "psych new api --template fastapi",
    result: "Writes six routes and a separate worker.py that share a Store and nothing else. You run them; the CLI never does.",
  },
];

export default function ExamplesPage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={86} y={34} size={44} tone="gold" opacity={0.32} drift />
        <Bloom x={12} y={86} size={34} opacity={0.24} />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">examples</p>
        <h1 className="display d1">
          Start with a working agent. <span className="serif">Make it yours.</span>
        </h1>
        <p className="lede">
          Choose a small agent, a guided tour, or an API-shaped application. Each template runs as
          written, and the test suite executes all three on every change.
        </p>
      </div>
      </Stage>

      <section className="shell section-tight">
        <div className="cols-2">
          <div className="rows" style={{ borderTop: 0 }}>
            {TEMPLATES.map((t, i) => (
              <div className="row" key={t.name} style={{ gridTemplateColumns: "36px minmax(0,1fr)" }}>
                <span className="n">{String(i + 1).padStart(2, "0")}</span>
                <div>
                  <h3>{t.name}</h3>
                  <p className="mt-1">{t.task}</p>
                  <span className="ref">{t.cmd}</span>
                  <p className="mt-1 small">
                    <strong style={{ color: "var(--text)" }}>You get:</strong> {t.result}
                  </p>
                </div>
              </div>
            ))}
          </div>
          <div>
            <Code code={CLI} lang="sh" title="the rest of the command line" />
            <p className="small mt-2">
              That is all of it. <code className="inline-code">psych</code> writes files, copies
              files and reports what is installed. It has no command that starts a Worker or runs
              an agent: you own the process that does that.
            </p>
          </div>
        </div>
      </section>

      <section className="section" aria-labelledby="tour-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">the tour, running</p>
              <h2 id="tour-title" className="display d2">
                The tour, stopping <span className="serif">for approval.</span>
              </h2>
            </div>
            <p className="lede">
              Output from <code className="inline-code">--template tour</code> against the fake
              model. The Run stops before the destructive call and waits for a decision.
            </p>
          </div>
          <div className="cols-2">
            <Code code={TOUR_OUTPUT} lang="output" title="stdout" />
            <div>
              <Code code={APPROVAL} title="what the consumer writes" />
              <div className="btn-row mt-2">
                <Link className="btn btn-ghost btn-sm" href="/playground">
                  Step through this Run in the browser demo <ArrowRight className="arrow" size={ICON} aria-hidden />
                </Link>
                <Link className="btn btn-ghost btn-sm" href={docs("guides/approvals")}>
                  Approvals guide <ArrowRight className="arrow" size={ICON} aria-hidden />
                </Link>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="section sunk" aria-labelledby="next-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">next</p>
              <h2 id="next-title" className="display d2">
                Two more shapes.
              </h2>
            </div>
            <p className="lede">
              Every feature has a guide with working code.
            </p>
          </div>
          <div className="cols-2">
            <div>
              <Code code={MCP} title="an MCP server, as data" />
              <p className="small mt-1">
                The credential is a name your resolver reads per Scope. The connection pools by
                scope, server and credential, never by URL.{" "}
                <Link className="link" href={docs("guides/mcp")}>
                  MCP guide
                </Link>
              </p>
            </div>
            <div>
              <Code code={WORKFLOW} title="a workflow with a nested agent" />
              <p className="small mt-1">
                Step names are the memoisation key. After a crash it resumes from the last completed
                step.{" "}
                <Link className="link" href={docs("guides/workflows")}>
                  Workflows guide
                </Link>
              </p>
            </div>
          </div>
          <div className="btn-row mt-3">
            <a className="btn btn-ghost" href={`${SITE.github}/tree/main/examples/playground`} rel="noopener">
              The playground source <ArrowUpRight size={ICON} aria-hidden />
            </a>
          </div>
        </div>
      </section>
    </>
  );
}
