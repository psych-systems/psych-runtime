"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import { stepLabel } from "@/components/workflows/step-model";

/**
 * The shape of a workflow, drawn.
 *
 * One component for two callers: the definition page, which has a published
 * tree and no progress, and the run view, which has the same tree with a
 * status on every node. They were going to be two implementations of the same
 * picture, and the second one would have drifted.
 *
 * Deliberately no layout library and no measured coordinates. A graph built
 * from refs and a `ResizeObserver` has to re-measure on every font load, every
 * theme change and every disclosure someone opens, and gets it wrong once at
 * least. This is CSS: a sequence is a column with a rule down the gutter,
 * parallel branches are a flex row of columns with a rule across the top, and
 * a loop body is a bordered box with a label chip. The rules are borders and
 * pseudo-spacers, so they land wherever the boxes land.
 *
 * On a phone the fan collapses to a stack. Branches side by side at 360px
 * either overflow horizontally or squeeze to an unreadable column each, and
 * the label above the group already says they run together.
 */

export interface GraphNode {
  key: string;
  name: string;
  kind: string;
  /** One line identifying the step: which tool, which event, which list. */
  subtitle: string;
  /** Shown above the node: an iteration number, a case name. */
  badge?: string | null;
  /** The step's `when`, in words. */
  condition?: string | null;
  /** Drawn at the right of the node's header: a status pill, an attempt
   *  count, a menu. */
  trailing?: ReactNode;
  /** Extra lines inside the node, under the subtitle. */
  detail?: ReactNode;
  /** Makes the whole node a button. */
  onSelect?: () => void;
  selected?: boolean;
  tone?: "default" | "running" | "failed" | "waiting" | "done" | "muted";
  /** Child groups. `fan` draws them side by side, `stack` one under the
   *  other, each in its own labelled box. */
  layout?: "fan" | "stack";
  groups?: { key: string; label: string; nodes: GraphNode[] }[];
}

const TONE_RING: Record<NonNullable<GraphNode["tone"]>, string> = {
  default: "border-border",
  running: "border-status-running/50 bg-status-running/5",
  failed: "border-status-failed/50 bg-status-failed/5",
  waiting: "border-status-waiting/50 bg-status-waiting/5",
  done: "border-status-done/40",
  muted: "border-dashed border-border text-muted-foreground",
};

/** A whole workflow: its top-level steps, in order. */
export function StepGraph({
  nodes,
  className,
  emptyLabel = "No steps yet.",
}: {
  nodes: GraphNode[];
  className?: string;
  emptyLabel?: string;
}) {
  if (nodes.length === 0) {
    return <p className="text-caption text-muted-foreground">{emptyLabel}</p>;
  }
  return (
    <div className={cn("min-w-0", className)}>
      <Sequence nodes={nodes} />
    </div>
  );
}

/** Steps one after another, joined by a connector between each pair. */
function Sequence({ nodes }: { nodes: GraphNode[] }) {
  return (
    <ol className="flex min-w-0 flex-col">
      {nodes.map((node, index) => (
        <li key={node.key} className="flex min-w-0 flex-col">
          {index > 0 && <Connector />}
          <GraphNodeView node={node} />
        </li>
      ))}
    </ol>
  );
}

/** The thin line between two steps that run one after the other. */
function Connector() {
  return (
    <span className="ml-5 h-4 w-px shrink-0 bg-border" aria-hidden />
  );
}

function GraphNodeView({ node }: { node: GraphNode }) {
  const tone = node.tone ?? "default";
  const interactive = node.onSelect !== undefined;

  // The box is a `div` and the button is inside it, never the other way
  // round. `trailing` carries its own controls -- the run view puts a replay
  // button there -- and a button inside a button is invalid HTML that
  // assistive technology flattens or drops, which would take the replay
  // action away from anybody not using a mouse. Two tab stops, both real.
  const body = (
    <>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="rounded bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
          {stepLabel(node.kind)}
        </span>
        <span className="min-w-0 truncate font-technical text-body font-medium">
          {node.name || <span className="text-muted-foreground italic">unnamed</span>}
        </span>
      </div>
      {node.subtitle && (
        <span className="min-w-0 break-words text-caption text-muted-foreground">{node.subtitle}</span>
      )}
      {node.condition && (
        <span className="min-w-0 break-words font-technical text-micro text-muted-foreground">
          only when {node.condition}
        </span>
      )}
      {node.detail}
    </>
  );

  return (
    <div className="flex min-w-0 flex-col">
      {node.badge && (
        <span className="mb-1 w-fit rounded-md bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
          {node.badge}
        </span>
      )}
      <div
        className={cn(
          "flex min-w-0 items-start gap-2 rounded-lg border px-3 py-2 transition-colors",
          TONE_RING[tone],
          interactive && "hover:bg-surface/60",
          node.selected && "ring-[3px] ring-ring/50"
        )}
      >
        {interactive ? (
          <button
            type="button"
            onClick={node.onSelect}
            aria-pressed={node.selected ?? false}
            aria-label={`Show step ${node.name}`}
            className="flex min-w-0 flex-1 flex-col gap-1 rounded text-left focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            {body}
          </button>
        ) : (
          <div className="flex min-w-0 flex-1 flex-col gap-1">{body}</div>
        )}
        {node.trailing && (
          <span className="flex shrink-0 items-center gap-1.5">{node.trailing}</span>
        )}
      </div>

      {node.groups && node.groups.length > 0 && (
        <>
          <Connector />
          {node.layout === "fan" ? (
            <Fan groups={node.groups} />
          ) : (
            <Stack groups={node.groups} />
          )}
        </>
      )}
    </div>
  );
}

/**
 * Branches that run at the same time, or the arms of a branch.
 *
 * Side by side from `sm` up, with a rule across the top standing for the
 * split. Below `sm` they stack, because three columns at phone width is
 * either a horizontal scrollbar or three unreadable columns.
 */
function Fan({ groups }: { groups: NonNullable<GraphNode["groups"]> }) {
  return (
    <div className="ml-5 flex min-w-0 flex-col gap-3 border-l border-border pl-4 sm:ml-0 sm:flex-row sm:items-start sm:gap-4 sm:border-l-0 sm:border-t sm:pt-4 sm:pl-0">
      {groups.map((group) => (
        <div key={group.key} className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span className="w-fit rounded-md border border-border bg-surface/60 px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
            {group.label}
          </span>
          {group.nodes.length === 0 ? (
            <p className="rounded-lg border border-dashed border-border px-3 py-2 text-caption text-muted-foreground">
              empty
            </p>
          ) : (
            <Sequence nodes={group.nodes} />
          )}
        </div>
      ))}
    </div>
  );
}

/** A body that runs more than once: a `foreach`'s items, a `loop`'s turns.
 *  Each turn is its own boxed group, stacked, so the order reads downward. */
function Stack({ groups }: { groups: NonNullable<GraphNode["groups"]> }) {
  return (
    <div className="ml-5 flex min-w-0 flex-col gap-3 border-l border-border pl-4">
      {groups.map((group) => (
        <div
          key={group.key}
          className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-dashed border-border bg-surface/30 p-3"
        >
          <span className="w-fit rounded-md bg-muted px-1.5 py-0.5 text-micro font-medium text-muted-foreground">
            {group.label}
          </span>
          {group.nodes.length === 0 ? (
            <p className="text-caption text-muted-foreground">empty</p>
          ) : (
            <Sequence nodes={group.nodes} />
          )}
        </div>
      ))}
    </div>
  );
}
