/**
 * How a tool is classified, and what an approval class means in words a
 * person who has never read the runtime would use.
 *
 * `classifyTool` mirrors `psych_runtime/tools/policy.py`'s `classify`, including the
 * deliberate divergence documented there: a tool nobody annotated counts as
 * "makes changes", not as exempt. A server that forgets to annotate a
 * destructive tool must not thereby escape approval.
 *
 * This is a copy rather than an import of the Tools page's version of the
 * same table. That page is being removed, and an Agents surface that stops
 * compiling when an unrelated directory is deleted is a worse dependency
 * than a duplicated twenty lines.
 */

export type ToolClass = "read-only" | "write" | "destructive";

export const ALL_TOOL_CLASSES: readonly ToolClass[] = ["read-only", "write", "destructive"];

export function classifyTool(annotations: readonly string[]): ToolClass {
  const set = new Set(annotations.map((a) => a.toLowerCase()));
  if (set.has("destructive")) return "destructive";
  if (set.has("write")) return "write";
  if (set.has("read-only")) return "read-only";
  return "write";
}

/** The wire form the backend's `approval_selectors` takes. Kept out of the
 * visible copy: a person picks "makes changes", the request carries
 * `@write`. */
export function selectorLabel(toolClass: ToolClass): string {
  return `@${toolClass}`;
}

export interface ToolClassCopy {
  title: string;
  blurb: string;
  /** What choosing it costs, said once, so nobody has to infer it from the
   *  word "approval". */
  consequence: string;
}

export const TOOL_CLASS_COPY: Record<ToolClass, ToolClassCopy> = {
  "read-only": {
    title: "Looking things up",
    blurb: "Reading a record, searching, fetching a page. Nothing changes.",
    consequence: "Asking here is safe but slow: most conversations pause several times.",
  },
  write: {
    title: "Making changes",
    blurb:
      "Creating or updating something: sending a message, filing a ticket, editing a record. A tool nobody labelled counts as this one.",
    consequence: "The conversation waits for you before anything is written.",
  },
  destructive: {
    title: "Deleting or replacing things",
    blurb: "Removing a record, overwriting a file, cancelling an order.",
    consequence: "The conversation waits for you before anything is destroyed.",
  },
};

export function toolClassTitle(toolClass: ToolClass): string {
  return TOOL_CLASS_COPY[toolClass].title;
}
