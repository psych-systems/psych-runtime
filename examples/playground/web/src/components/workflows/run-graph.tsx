"use client";

import type { StepView } from "@/lib/types";
import { formatDuration } from "@/lib/format";
import { stepLabel } from "@/components/workflows/step-model";
import type { GraphNode } from "@/components/workflows/step-graph";
import { StepStatusPill, stepDuration, stepTone } from "@/components/workflows/step-status";

/**
 * A run in progress, as graph nodes.
 *
 * The same picture the definition draws, with what happened on it. Two things
 * the definition adapter has no equivalent for:
 *
 * - a `foreach` or `loop` reports its body once per turn, so the children are
 *   grouped by `iteration` and drawn as a stack of labelled boxes rather than
 *   a fan. Forty items are forty boxes, in order, and each one is still a
 *   whole sub-graph;
 * - a `branch` reports only the arms it actually took, and `cases` says which
 *   they were, so the labels come from the run rather than the definition.
 */
export function runNodes(
  steps: StepView[],
  options: {
    onSelect: (step: StepView) => void;
    selectedId: string | null;
    /** Drawn at the right of a top-level node: the replay action. */
    topLevelAction?: (step: StepView) => React.ReactNode;
  },
  depth = 0
): GraphNode[] {
  return steps.map((step) => runNode(step, options, depth));
}

function runNode(
  step: StepView,
  options: Parameters<typeof runNodes>[1],
  depth: number
): GraphNode {
  const duration = stepDuration(step.started_at, step.completed_at);
  const action = depth === 0 ? options.topLevelAction?.(step) : null;

  return {
    key: step.step_id,
    name: step.name,
    kind: step.kind,
    subtitle: subtitle(step, duration),
    tone: stepTone(step.status),
    selected: options.selectedId === step.step_id,
    onSelect: () => options.onSelect(step),
    trailing: (
      <>
        {step.attempts > 1 && (
          <span className="tabular text-micro text-muted-foreground">
            attempt {step.attempts}
          </span>
        )}
        <StepStatusPill status={step.status} />
        {action}
      </>
    ),
    ...childGroups(step, options, depth),
  };
}

function subtitle(step: StepView, duration: number | null): string {
  const parts: string[] = [];
  if (step.cases.length > 0) parts.push(`took ${step.cases.join(", ")}`);
  if (step.child_run_id) parts.push("started its own run");
  if (step.status === "replayed" && step.replayed_from) parts.push("carried over from the earlier run");
  if (step.status === "retrying" && step.retry_at) {
    parts.push(`next attempt ${new Date(step.retry_at).toLocaleTimeString()}`);
  }
  if (step.failure) parts.push(step.failure.kind);
  if (duration !== null && step.status !== "pending") parts.push(formatDuration(duration));
  return parts.join(" · ");
}

function childGroups(
  step: StepView,
  options: Parameters<typeof runNodes>[1],
  depth: number
): Pick<GraphNode, "layout" | "groups"> {
  if (step.children.length === 0) return {};

  // A body that ran more than once reports an iteration on every child. One
  // box per turn, in order, is the only reading of forty `foreach` children
  // that stays legible.
  //
  // Gated on the parent's kind rather than on the children carrying an
  // iteration at all: a `parallel` nested inside a `foreach` body may inherit
  // its turn's number on every child, and that must still draw as a fan.
  const iterated =
    (step.kind === "foreach" || step.kind === "loop") &&
    step.children.some((child) => child.iteration !== null);
  if (iterated) {
    const byIteration = new Map<number, StepView[]>();
    for (const child of step.children) {
      const key = child.iteration ?? 0;
      byIteration.set(key, [...(byIteration.get(key) ?? []), child]);
    }
    return {
      layout: "stack",
      groups: [...byIteration.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([iteration, children]) => ({
          key: `${step.step_id}.i${iteration}`,
          label:
            step.kind === "foreach" ? `item ${iteration + 1}` : `time round ${iteration + 1}`,
          nodes: runNodes(children, options, depth + 1),
        })),
    };
  }

  if (step.kind === "parallel" || step.kind === "branch") {
    return {
      layout: "fan",
      groups: step.children.map((child, index) => ({
        key: `${step.step_id}.b${index}`,
        label:
          step.kind === "branch"
            ? (step.cases[index] ?? child.name ?? `arm ${index + 1}`)
            : `branch ${index + 1}`,
        nodes: runNodes([child], options, depth + 1),
      })),
    };
  }

  return {
    layout: "stack",
    groups: [
      {
        key: `${step.step_id}.body`,
        label: stepLabel(step.kind).toLowerCase(),
        nodes: runNodes(step.children, options, depth + 1),
      },
    ],
  };
}

/** Every step in the tree, parents before their children. Used for the step
 *  count and to find a step by id when a panel is open. */
export function flattenViews(steps: StepView[]): StepView[] {
  const out: StepView[] = [];
  const walk = (list: StepView[]) => {
    for (const step of list) {
      out.push(step);
      walk(step.children);
    }
  };
  walk(steps);
  return out;
}
