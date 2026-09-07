import Link from "next/link";
import { TridentDiagram } from "@/components/Trident";
import { ArrowRight, ICON } from "@/components/icons";
import { ArrowDown, Check, Database, Inbox, Server } from "lucide-react";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { docs } from "@/lib/versions";

export const metadata = pageMeta({
  title: "The runtime",
  description:
    "See how Psych executes an agent run, saves its progress, and divides work between your request handler and workers.",
  path: "/runtime",
});

const LAYERS = [
  { name: "your app", body: "Routes, auth, cron, queue consumers. Turn a request into a Scope and call dispatch().", ours: false },
  { name: "psych_runtime", body: "The whole public surface. Every type you can hold is exported here, and a test fails when one is not.", ours: true },
  { name: "runtime/", body: "Worker, supervisor, Attempt, lease, agent loop, workflow engine, suspend, interrupts, subagents.", ours: true },
  { name: "tools/ model/", body: "Per-turn resolution, access narrowing, the four tool executors, the model client, pricing, one egress seam.", ours: true },
  { name: "core/", body: "Spec models, Version hashing, Record types, the pure reducer. No IO, no clock, no randomness.", ours: true },
  { name: "your infra", body: "Store, BlobStore, Sandbox, Telemetry, SecretResolver, Policy. Ports, with adapters in the box.", ours: false },
];

const REFUSED = [
  ["An HTTP server or any transport", "Functions you call from your own routes."],
  ["Auth, users, orgs, roles", "A Scope on every call and a Policy port you implement."],
  ["A database of its own", "A Store port, four adapters, one contract suite."],
  ["A scheduler or timer", "You call dispatch(). A Run waiting on a clock suspends as external."],
  ["A UI", "status() returns a serialisable RunStatus a screen can draw."],
  ["Prompt management, evals", "Instructions and skills live in the Spec you version."],
  ["A vector store or retrieval", "Nothing. You have one, and it belongs behind a tool."],
  ["Budgets and model routing", "Metering yes, enforcement no. Fallback models yes, a router no."],
];

export default function RuntimePage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={88} y={30} size={46} opacity={0.34} drift />
        <Bloom x={6} y={82} size={36} tone="gold" opacity={0.26} />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">agent execution</p>
        <h1 className="display d1">
          One request. Many steps. <span className="serif">One durable run.</span>
        </h1>
        <p className="lede">
          Psych turns an agent request into work a Worker can execute, pause, recover, and inspect.
          Your application keeps control of where it runs and what it can reach.
        </p>
      </div>
      </Stage>

      {/* Two processes */}
      <section className="section sunk" aria-labelledby="split-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">the path a request takes</p>
              <h2 id="split-title" className="display d2">
                Two processes, <span className="serif">one database.</span>
              </h2>
            </div>
            <p className="lede">
              The handler returns as soon as the Run is admitted. The Worker does the slow part in
              its own process, and the two share a Store and nothing else.
            </p>
          </div>
          <div className="process-map" role="img" aria-label="Your request handler dispatches a run to a shared store, then a separate worker claims and executes it">
            <div className="process-card">
              <span className="process-icon"><Inbox size={24} /></span>
              <span className="process-kicker">Your request handler</span>
              <strong>Admit the run</strong>
              <p>Validate the request, save it, and return its ID.</p>
              <span className="process-state"><Check size={13} /> The request can return</span>
            </div>
            <div className="process-link"><ArrowDown size={18} /><span>Shared store</span><Database size={22} /><ArrowDown size={18} /></div>
            <div className="process-card">
              <span className="process-icon"><Server size={24} /></span>
              <span className="process-kicker">Your worker process</span>
              <strong>Do the work</strong>
              <p>Claim the run, call the model and tools, and record each result.</p>
              <span className="process-state"><Check size={13} /> Scale workers separately</span>
            </div>
          </div>
        </div>
      </section>

      {/* The mark as the architecture */}
      <section className="section-tight">
        <div className="shell cols-2">
          <div className="card" style={{ padding: "clamp(20px,3vw,36px)" }}>
            <TridentDiagram />
          </div>
          <div>
            <p className="eyebrow">one log per run</p>
            <h2 className="display d2">
              Three ports in. <span className="serif">One log out.</span>
            </h2>
            <div className="prose mt-2">
              <p>
                You supply a model client, tools and a store. Psych folds what they do into one
                append-only log per Run, and everything you read back is derived from it.
              </p>
              <p>
                The report, the live stream, the status a screen shows and the step a crashed
                workflow resumes from are all computed from that log. Nothing is stored twice, so
                no two reads disagree.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Layers */}
      <section className="section" aria-labelledby="layers-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">inside the package</p>
              <h2 id="layers-title" className="display d2">
                What is yours, and{" "}
                <span className="serif">what is Psych&rsquo;s.</span>
              </h2>
            </div>
            <p className="lede">
              What to implement, and what to import. Dependencies point inward, and an import
              linter enforces it.
            </p>
          </div>
          <div className="rows">
            {LAYERS.map((l) => (
              <div className="row" key={l.name}>
                <span className="n">{l.ours ? "psych" : "yours"}</span>
                <h3 className="mono" style={{ fontSize: 15 }}>
                  {l.name}
                </h3>
                <p>{l.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Refusals */}
      <section className="section sunk" aria-labelledby="refuse-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">the boundary</p>
              <h2 id="refuse-title" className="display d2">
                Left to your application, <span className="serif">on purpose.</span>
              </h2>
            </div>
            <p className="lede">
              You already have each of these. A library that brought its own would argue with
              yours.
            </p>
          </div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Not in Psych</th>
                  <th scope="col">What it does instead</th>
                </tr>
              </thead>
              <tbody>
                {REFUSED.map(([a, b]) => (
                  <tr key={a}>
                    <td>{a}</td>
                    <td>{b}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="btn-row mt-3">
            <Link className="btn btn-flame" href="/capabilities">
              What it does handle <ArrowRight className="arrow" size={ICON} aria-hidden />
            </Link>
            <Link className="btn btn-ghost" href={docs("concepts/vocabulary")}>
              The vocabulary <ArrowRight className="arrow" size={ICON} aria-hidden />
            </Link>
          </div>
        </div>
      </section>
    </>
  );
}
