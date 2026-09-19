/**
 * A published definition, as graph nodes.
 *
 * The definition side of `StepGraph`: no progress, no status, just the shape.
 * Kept apart from the run adapter because the two answer different questions
 * and share only the drawing.
 */

import type { Step } from "@/lib/types";
import type { GraphNode } from "@/components/workflows/step-graph";
import { describeCondition, stepSubtitle } from "@/components/workflows/step-model";

export function definitionNodes(steps: Step[], prefix = ""): GraphNode[] {
  return steps.map((step, index) => definitionNode(step, `${prefix}${index}`));
}

function definitionNode(step: Step, key: string): GraphNode {
  const base: GraphNode = {
    key,
    name: step.name,
    kind: step.kind,
    subtitle: stepSubtitle(step),
    condition: step.when ? describeCondition(step.when) : null,
  };

  switch (step.kind) {
    case "parallel":
      return {
        ...base,
        layout: "fan",
        groups: step.branches.map((branch, i) => ({
          key: `${key}.b${i}`,
          label: `branch ${i + 1}`,
          nodes: [definitionNode(branch, `${key}.b${i}.0`)],
        })),
      };
    case "branch":
      return {
        ...base,
        layout: "fan",
        groups: [
          ...step.cases.map((one, i) => ({
            key: `${key}.c${i}`,
            label: `${one.name || `case ${i + 1}`} · ${describeCondition(one.when)}`,
            nodes: [definitionNode(one.step, `${key}.c${i}.0`)],
          })),
          ...(step.otherwise
            ? [
                {
                  key: `${key}.else`,
                  label: "otherwise",
                  nodes: [definitionNode(step.otherwise, `${key}.else.0`)],
                },
              ]
            : []),
        ],
      };
    case "foreach":
      return {
        ...base,
        layout: "stack",
        groups: [
          {
            key: `${key}.body`,
            label: `for each item of ${step.items.path || "?"}`,
            nodes: [definitionNode(step.body, `${key}.body.0`)],
          },
        ],
      };
    case "loop":
      return {
        ...base,
        layout: "stack",
        groups: [
          {
            key: `${key}.body`,
            label: "each time round",
            nodes: [definitionNode(step.body, `${key}.body.0`)],
          },
        ],
      };
    default:
      return base;
  }
}
