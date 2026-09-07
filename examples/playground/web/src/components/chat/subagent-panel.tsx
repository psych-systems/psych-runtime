"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronRightIcon, ExternalLinkIcon, RotateCcwIcon, SquareIcon } from "lucide-react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { StatusPill, type Lifecycle } from "@/components/ui/status";
import { messageSubagent, retrySubagent, stopSubagent } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatTokens } from "@/lib/format";
import type { SubagentNode, SubagentTree } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The subagents a run started, while they are still running.
 *
 * An agent that fans work out to three children and waits is, without this,
 * an agent that has gone quiet: the parent's own log says "spawned" and then
 * nothing for a minute, because everything happening is happening in somebody
 * else's log. This is that minute, made readable.
 *
 * ## Why the controls are here rather than only inside the agent
 *
 * The parent can steer its own children -- it has a tool for it. A person
 * watching cannot, and the moment they most want to is the moment they can see
 * a child heading the wrong way. So stop, message and retry are offered here,
 * and each one goes through the same mechanism the agent's own tool uses: a
 * message is a steering-queue entry delivered at the child's next turn, a stop
 * is an abort record, and a retry is a new run of the same pinned version. The
 * console gets no privileged path the runtime does not have.
 *
 * ## What the numbers mean
 *
 * Two figures per child: its own, and its branch's. They differ once a child
 * spawns children of its own, and the branch total is what somebody means by
 * "what did that cost". A branch still running says so rather than showing a
 * total that is about to change.
 */
export function SubagentPanel({
  tree,
  onChanged,
}: {
  tree: SubagentTree;
  onChanged: () => void;
}) {
  if (tree.children.length === 0) return null;

  const running = countRunning(tree.children);

  return (
    <section className="flex w-full flex-col gap-2 rounded-xl border border-border bg-surface/40 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-caption font-medium">
          Subagents{running > 0 ? ` · ${running} working` : ""}
        </h3>
        <span className="tabular text-micro text-muted-foreground">
          {formatTokens(tree.total_input_tokens + tree.total_output_tokens)} tokens across{" "}
          {tree.children.length === 1 ? "1 branch" : `${tree.children.length} branches`}
          {tree.complete ? "" : " so far"}
        </span>
      </div>
      <ul className="flex flex-col gap-2">
        {tree.children.map((child) => (
          <SubagentRow
            key={child.run_id}
            node={child}
            parentRunId={tree.run_id}
            onChanged={onChanged}
          />
        ))}
      </ul>
    </section>
  );
}

function SubagentRow({
  node,
  parentRunId,
  onChanged,
}: {
  node: SubagentNode;
  parentRunId: string;
  onChanged: () => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const live = node.terminal_state === null;

  async function act(action: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-lg border border-border/70 bg-background/40">
      <div className="flex items-start gap-2 px-2.5 py-2">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="mt-0.5 rounded p-0.5 text-muted-foreground transition-colors hover:text-foreground"
          aria-expanded={open}
          aria-label={open ? `Hide ${node.name}` : `Show ${node.name}`}
        >
          <ChevronRightIcon className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </button>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-body font-medium">{node.name}</span>
            <StatusPill state={asLifecycle(node.state)} size="sm" />
            {node.messages_sent > 0 && (
              <span className="text-micro text-muted-foreground">
                {node.messages_sent === 1 ? "1 message sent" : `${node.messages_sent} messages sent`}
              </span>
            )}
          </div>
          <p className="text-caption text-muted-foreground">{node.purpose}</p>
          {node.latest && (
            <p className="line-clamp-2 text-caption text-foreground/80">{node.latest}</p>
          )}
          {node.error && <p className="text-caption text-status-failed">{node.error}</p>}
        </div>
        <span className="tabular shrink-0 text-micro text-muted-foreground">
          {formatTokens(node.subtree_input_tokens + node.subtree_output_tokens)}
          {node.complete ? "" : "+"}
        </span>
      </div>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border/70 px-2.5 py-2">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-micro">
            <Detail label="Model" value={node.model} />
            <Detail label="Tools" value={node.tools.join(", ") || "none"} />
            <Detail label="Asked for" value={node.deliverable} />
            <Detail
              label="Branch"
              value={`${node.subtree_runs} run${node.subtree_runs === 1 ? "" : "s"}${
                node.complete ? "" : ", still going"
              }`}
            />
          </dl>
          <p className="text-caption text-muted-foreground">{node.task}</p>

          <div className="flex flex-wrap items-center gap-2">
            <Link
              href={`/activity/${node.run_id}/trace`}
              className="flex items-center gap-1.5 rounded-md px-2 py-1 text-caption font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <ExternalLinkIcon className="size-3.5" />
              Its own log
            </Link>
            {live && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  void act(() => stopSubagent(parentRunId, node.run_id, "stopped from the console"))
                }
              >
                <SquareIcon className="size-3.5" />
                Stop it
              </Button>
            )}
            {!live && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const retried = await retrySubagent(parentRunId, node.run_id);
                    router.push(`/activity/${retried.run_id}/trace`);
                  })
                }
              >
                <RotateCcwIcon className="size-3.5" />
                Run it again
              </Button>
            )}
          </div>

          {live && (
            <form
              className="flex items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const text = message.trim();
                if (text === "") return;
                void act(async () => {
                  await messageSubagent(parentRunId, node.run_id, text);
                  setMessage("");
                });
              }}
            >
              <input
                type="text"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                placeholder="Tell it something. It reads this at its next turn."
                className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1 text-caption"
              />
              <Button type="submit" size="sm" variant="outline" disabled={busy}>
                Send
              </Button>
            </form>
          )}

          {error && <p className="text-caption text-status-failed">{error}</p>}

          {node.children.length > 0 && (
            <ul className="flex flex-col gap-2 border-l border-border/70 pl-2">
              {node.children.map((grandchild) => (
                <SubagentRow
                  key={grandchild.run_id}
                  node={grandchild}
                  parentRunId={node.run_id}
                  onChanged={onChanged}
                />
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="truncate text-right">{value}</dd>
    </div>
  );
}

/**
 * The backend answers in `psych.RunStatus.lifecycle`'s own vocabulary, which
 * is what `StatusPill` takes. This only guards against a value from a newer
 * backend than this bundle: showing "queued" beats crashing the panel.
 */
function asLifecycle(state: string): Lifecycle {
  const known: Lifecycle[] = ["queued", "running", "waiting", "stopping", "done", "failed", "stopped"];
  return known.includes(state as Lifecycle) ? (state as Lifecycle) : "queued";
}

function countRunning(nodes: SubagentNode[]): number {
  return nodes.reduce(
    (total, node) => total + (node.terminal_state === null ? 1 : 0) + countRunning(node.children),
    0,
  );
}
