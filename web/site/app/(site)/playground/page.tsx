import Link from "next/link";
import { Code } from "@/components/Code";
import { RunStepper } from "@/components/RunStepper";
import { PLAYGROUND_RUN } from "@/content/site/code";
import { OPEN_SEQ, SETTLE_SEQ, SUSPEND_SEQ } from "@/lib/scenario";
import { ArrowRight, ArrowUpRight, ICON } from "@/components/icons";
import { Bloom, Grain } from "@/components/Atmosphere";
import { Stage } from "@/components/Stage";
import { pageMeta } from "@/lib/seo";
import { SITE } from "@/lib/site";

export const metadata = pageMeta({
  title: "Playground",
  description:
    "Step through a recorded Psych run, make an approval decision, and explore the full playground application.",
  path: "/playground",
});

const SCREENS = [
  ["Chat", "Talking to an agent, one conversation at a time."],
  ["Agents", "What you can talk to, and building another."],
  ["Connections", "MCP servers, their live state, credentials by name."],
  ["Activity", "Every conversation, its outcome, its trace and usage."],
  ["Capabilities", "The definition-of-done scenarios, run against the live runtime."],
  ["Settings", "Model access, secrets, appearance."],
];

export default function PlaygroundPage() {
  return (
    <>
      <Stage className="page-head-stage">
        <Bloom x={84} y={32} size={46} tone="ember" opacity={0.32} drift />
        <Bloom x={14} y={84} size={34} tone="gold" opacity={0.24} />
        <Grain opacity={0.22} />
        <div className="shell page-head">
        <p className="eyebrow">interactive run · simulated</p>
        <h1 className="display d1">
          Watch a run pause. <span className="serif">Then decide what happens.</span>
        </h1>
        <p className="lede">
          Step through a recorded example and see what Psych knows after each event. At the approval
          gate, choose either path and follow the run to its answer.
        </p>
      </div>
      </Stage>

      <section className="shell section-tight">
        <RunStepper />
        <div className="cols-2 mt-3">
          <p className="small">
            <strong style={{ color: "var(--text)" }}>Three things to catch.</strong>{" "}
            <code className="inline-code">tool_call_started</code> is written at record {OPEN_SEQ},{" "}
            <em>before</em> the suspension at record {SUSPEND_SEQ}: the call is opened, then the gate
            stops it, and the same <code className="inline-code">call_id</code> is settled at record{" "}
            {SETTLE_SEQ} by a different Worker. Denying settles that same call as an error the model
            reads and answers around, so the Run completes either way. And cost only moves on a model
            call, because usage is recorded per call and priced as it is written.
          </p>
          <p className="small">
            <strong style={{ color: "var(--text)" }}>What is real and what is not.</strong> No model
            is called to draw this page. The records are the log of a Run executed against the
            scripted fake model with these tools and this approval rule, read back with{" "}
            <code className="inline-code">records()</code> and checked into the repository; the
            token counts were scripted, and the runtime priced them itself. The panel on the right
            is folded from the records on the left the way{" "}
            <code className="inline-code">status()</code> folds a real log, and the build fails if
            that fold disagrees with <code className="inline-code">report()</code>.
          </p>
        </div>
      </section>

      <section className="section" aria-labelledby="shots-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">the example console</p>
              <h2 id="shots-title" className="display d2">
                The same decision, <span className="serif">in the console.</span>
              </h2>
            </div>
            <p className="lede">
              Screenshots of the application in{" "}
              <code className="inline-code">examples/playground/</code>, running locally against a
              scripted provider. The agent, the tools and the approval rule are the ones the
              command below gives you.
            </p>
          </div>
          <figure className="shot">
            <img
              src="/console/console-approval.png"
              width={2560}
              height={1440}
              alt="The console holding a refund for approval: the tool issue_refund, its arguments order_id A1 and cents 4200, and Approve and Decline buttons."
              loading="lazy"
            />
            <figcaption>
              The Run stopped before <code className="inline-code">issue_refund</code> and is
              waiting. The card names the call and its exact arguments, because approving a tool by
              name alone is approving something you cannot see.
            </figcaption>
          </figure>
          <figure className="shot">
            <img
              src="/console/console-approved.png"
              width={2560}
              height={1440}
              alt="The same conversation after approval: a line reading Approved by sam, then the agent's reply confirming the refund."
              loading="lazy"
            />
            <figcaption>
              After approving. Who decided is on the log, not just on the screen, and the agent
              carries on from where it stopped.
            </figcaption>
          </figure>
        </div>
      </section>

      <section className="section sunk" aria-labelledby="console-title">
        <div className="shell">
          <div className="section-head">
            <div>
              <p className="eyebrow">the example console</p>
              <h2 id="console-title" className="display d2">
                Run the console <span className="serif">locally.</span>
              </h2>
            </div>
            <p className="lede">
              An example application in the repository. It supplies the HTTP server and the UI
              that Psych does not, so you can see an agent published, talked to, and read back.
            </p>
          </div>
          <div className="cols-2">
            <div>
              <Code code={PLAYGROUND_RUN} lang="sh" title="run it" />
              <p className="small mt-2">
                Nothing else to install. It works before you configure a provider. Compose adds
                PostgreSQL and Jaeger, where a Run shows up as a span tree.
              </p>
              <div className="btn-row mt-2">
                <a className="btn btn-ghost btn-sm" href={`${SITE.github}/tree/main/examples/playground`} rel="noopener">
                  Source <ArrowUpRight size={ICON} aria-hidden />
                </a>
                <Link className="btn btn-ghost btn-sm" href="/examples">
                  No Docker? Use the tour <ArrowRight className="arrow" size={ICON} aria-hidden />
                </Link>
              </div>
            </div>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Screen</th>
                    <th scope="col">For</th>
                  </tr>
                </thead>
                <tbody>
                  {SCREENS.map(([a, b]) => (
                    <tr key={a}>
                      <td>{a}</td>
                      <td>{b}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

        </div>
      </section>
    </>
  );
}
