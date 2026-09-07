"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ICON, Pause, Play, RotateCcw, X } from "@/components/icons";
import {
  type Branch,
  MILESTONES,
  OPEN_SEQ,
  PROLOGUE,
  SCENARIO_INPUT,
  SCENARIO_MODEL,
  SUSPEND_SEQ,
  fold,
  scenario,
  usd,
} from "@/lib/scenario";

/**
 * Step one Run's log and watch its state come out of the fold.
 *
 * Two things make this a demonstration rather than an animation. The panel on
 * the right is computed on every render by `fold()`, the same shape
 * `psych_runtime.status()` uses on a real log, so scripting it would show the
 * opposite of the runtime's central idea. And the Run genuinely stops at the
 * approval: the "next" button is disabled at the suspension, because there is
 * no next record until a person decides. Both tails are logs a real Run wrote
 * (see lib/scenario.ts).
 */
export function RunStepper() {
  const [branch, setBranch] = useState<Branch | null>(null);
  const [upTo, setUpTo] = useState(1);
  const [playing, setPlaying] = useState(false);

  // Before a decision there is only the shared prologue; choosing appends a
  // tail. `total` is therefore what is *readable now*, not what will exist.
  const records = useMemo(() => (branch ? scenario(branch) : PROLOGUE), [branch]);
  const total = records.length;
  const current = records[Math.min(upTo, total) - 1];
  const derived = useMemo(() => fold(records, upTo), [records, upTo]);
  const waiting = !branch && upTo >= SUSPEND_SEQ;

  const listRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    if (!playing) return;
    if (upTo >= total) {
      setPlaying(false);
      return;
    }
    const id = setTimeout(() => setUpTo((n) => Math.min(total, n + 1)), 850);
    return () => clearTimeout(id);
  }, [playing, upTo, total]);

  // Stop the player at the gate rather than letting it sit on a disabled step.
  useEffect(() => {
    if (waiting) setPlaying(false);
  }, [waiting]);

  // Keep the current record in view by scrolling the list itself, never with
  // scrollIntoView: that scrolls the nearest scrollable ancestor, which is the
  // page, so loading the homepage jumped the visitor down to this component. It
  // also moves the sequential focus starting point, which sent the first Tab
  // press into the middle of the page instead of the skip link.
  //
  // Only when the row is actually outside the visible band, so a click on
  // `back` does not re-centre a row that was already perfectly readable.
  useEffect(() => {
    const list = listRef.current;
    const row = list?.querySelector<HTMLElement>("li.now");
    if (!list || !row) return;
    const top = row.offsetTop;
    const bottom = top + row.offsetHeight;
    const viewTop = list.scrollTop;
    const viewBottom = viewTop + list.clientHeight;
    if (top >= viewTop + 8 && bottom <= viewBottom - 8) return;
    list.scrollTop = Math.max(0, top - list.clientHeight / 2 + row.offsetHeight / 2);
  }, [upTo, branch]);

  function decide(next: Branch) {
    setBranch(next);
    setUpTo(SUSPEND_SEQ + 1);
  }

  function reset() {
    setPlaying(false);
    setBranch(null);
    setUpTo(1);
  }

  function goTo(seq: number) {
    setPlaying(false);
    setUpTo(Math.min(seq, total));
  }

  const pending = derived.pendingApproval;
  const order = derived.order;

  return (
    <div className="stepper panel">
      <div className="panel-head">
        <span>
          <strong>run_9b7c</strong> · tenant=acme · {SCENARIO_MODEL}
        </span>
        <span className="sim">simulated · records from a real Run, no live model</span>
      </div>

      <p className="stepper-ask">
        <span>customer</span>
        {SCENARIO_INPUT}
      </p>

      <div className="stepper-body">
        <div className="stepper-log">
          <ol className="log" ref={listRef} aria-label="Record log">
            {records.map((r) => (
              <li
                key={r.seq}
                data-tone={r.tone}
                className={r.seq === upTo ? "now" : r.seq > upTo ? "ahead" : undefined}
                aria-current={r.seq === upTo ? "step" : undefined}
              >
                <span className="seq">{String(r.seq).padStart(2, "0")}</span>
                <span className="type">{r.type}</span>
                <span className="detail">
                  {r.tool ? <span className="resolved">{r.tool}</span> : null}
                  {r.detail}
                </span>
              </li>
            ))}
            {waiting ? (
              <li className="log-end" aria-hidden>
                <span className="seq" />
                <span>the log ends here until somebody decides</span>
              </li>
            ) : null}
          </ol>
        </div>

        <aside className="stepper-side" aria-label="State derived from the log">
          <p className="stepper-note" aria-live="polite">
            <span>
              record {current.seq} · {current.type}
            </span>
            {current.note || "Nothing new to derive. The fold carries on."}
          </p>

          {waiting && pending ? (
            <div className="approval-card" role="group" aria-labelledby="approval-title">
              <p className="approval-kicker">approval required · simulated</p>
              <p id="approval-title" className="approval-what">
                {describeCall(pending.tool, pending.arguments)}
              </p>
              <dl className="approval-facts">
                <div>
                  <dt>call</dt>
                  <dd>
                    <code>
                      {pending.tool}({JSON.stringify(pending.arguments)})
                    </code>
                  </dd>
                </div>
                {order ? (
                  <div>
                    <dt>the order, from lookup_order</dt>
                    <dd>
                      {Object.entries(order)
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join(" · ")}
                    </dd>
                  </div>
                ) : null}
              </dl>
              <p className="approval-why">
                <code>{pending.tool}</code> is annotated <code>destructive</code> and this Runtime
                gates <code>@destructive</code>, so the call opened at record {OPEN_SEQ} has not run.
                The Run is persisted with its lease released and waits 24 hours for a decision.
              </p>
              <div className="approval-actions">
                <button type="button" className="btn btn-primary" onClick={() => decide("approved")}>
                  <Check size={ICON} aria-hidden /> Approve
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => decide("denied")}>
                  <X size={ICON} aria-hidden /> Deny
                </button>
              </div>
            </div>
          ) : null}

          {derived.decision === "denied" ? (
            <p className="refusal">
              <span>refused</span>
              <code>issue_refund</code> never ran. The call opened at record {OPEN_SEQ} was settled
              as an error, no money moved, and the model was told so rather than being allowed to
              try again.
            </p>
          ) : null}

          {derived.answer ? (
            <p className="stepper-answer">
              <span>answer · simulated</span>
              {derived.answer}
            </p>
          ) : null}

          <dl className="derived">
            <div>
              <dt>lifecycle</dt>
              <dd data-tone={derived.lifecycle}>{derived.lifecycle}</dd>
            </div>
            <div>
              <dt>attempt</dt>
              <dd>{derived.attempt ? `${derived.attempt} · ${derived.worker}` : "none yet"}</dd>
            </div>
            <div>
              <dt>turn</dt>
              <dd>{derived.turn || "none yet"}</dd>
            </div>
            <div>
              <dt>pending_approval</dt>
              <dd data-tone={derived.pendingApproval ? "waiting" : undefined}>
                {derived.pendingApproval ? derived.pendingApproval.tool : "None"}
              </dd>
            </div>
            <div>
              <dt>approval</dt>
              <dd data-tone={derived.decision === "denied" ? "denied" : derived.decision === "approved" ? "done" : undefined}>
                {derived.decision
                  ? `${derived.decision} by ${derived.resumedBy}`
                  : derived.pendingApproval
                    ? "not decided yet"
                    : "none asked for"}
              </dd>
            </div>
            <div>
              <dt>tokens</dt>
              <dd>
                {derived.usage.input + derived.usage.output + derived.usage.cache_read === 0
                  ? "none yet"
                  : `${derived.usage.input} in · ${derived.usage.output} out · ${derived.usage.cache_read} cached`}
              </dd>
            </div>
            <div>
              <dt>cost</dt>
              <dd>
                {derived.cost === null
                  ? derived.unpricedCalls
                    ? "None · no price known"
                    : "no priced call yet"
                  : `${usd(derived.cost.amount)} ${derived.cost.currency}${derived.unpricedCalls ? ` · ${derived.unpricedCalls} unpriced` : ""}`}
              </dd>
            </div>
            <div>
              <dt>tool_calls</dt>
              <dd>
                {derived.toolCalls.length === 0
                  ? "none yet"
                  : `${derived.toolCalls.length} · ${derived.toolCalls.filter((c) => c.outcome === "ok").length} ok, ${derived.toolCalls.filter((c) => c.outcome !== "ok").length} failed`}
              </dd>
            </div>
            <div>
              <dt>terminal_state</dt>
              <dd data-tone={derived.terminal ? "done" : undefined}>
                {derived.terminal ?? "not settled"}
              </dd>
            </div>
          </dl>

          <details className="raw">
            <summary>the record, as the store holds it</summary>
            <pre>{JSON.stringify(current.raw, null, 2)}</pre>
          </details>
        </aside>
      </div>

      <div className="stepper-controls">
        <button type="button" className="btn btn-ghost" onClick={reset} disabled={upTo === 1 && !branch}>
          <RotateCcw size={14} aria-hidden /> reset
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setUpTo((n) => Math.max(1, n - 1))}
          disabled={upTo === 1}
        >
          back
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => setUpTo((n) => Math.min(total, n + 1))}
          disabled={upTo >= total}
        >
          {waiting ? "waiting on you" : "next record"}
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setPlaying((p) => !p)}
          disabled={upTo >= total && !playing}
          aria-pressed={playing}
        >
          {playing ? <Pause size={ICON} aria-hidden /> : <Play size={ICON} aria-hidden />}
          {playing ? "pause" : "play"}
        </button>

        <nav className="stepper-jumps" aria-label="Jump to">
          {MILESTONES.map((m) => (
            <button
              key={m.seq}
              type="button"
              onClick={() => goTo(m.seq)}
              disabled={m.seq > total}
              aria-current={upTo === m.seq ? "true" : undefined}
            >
              {m.label}
            </button>
          ))}
          <span className="stepper-count">
            {upTo} / {total}
          </span>
        </nav>
      </div>
    </div>
  );
}

/**
 * The pending call in the words a person approving it needs, when the arguments
 * carry a recognisable amount; otherwise the call as the log spells it.
 */
function describeCall(tool: string, args: Record<string, unknown>): string {
  const cents = args.cents;
  const order = args.order_id;
  if (tool === "issue_refund" && typeof cents === "number" && typeof order === "string") {
    return `Refund $${(cents / 100).toFixed(2)} on order ${order}`;
  }
  return `${tool}(${JSON.stringify(args)})`;
}
