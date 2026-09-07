/**
 * Line diffing for the per-turn system prompt, kept pure and separate from
 * the components that draw it.
 *
 * The prompt is recorded per turn as it was actually sent (`ModelCallReport
 * .system_prompt`), and the interesting question is almost never "what does
 * turn one say" but "what does turn three say that turn two did not". A tool
 * withheld by an approval selector, an MCP server that went down between
 * turns, a deferred catalogue that grew: all of them show up here as a few
 * changed lines and nowhere else in the product.
 *
 * No `Date.now()`, no fetching: same two strings in, same lines out, so the
 * badge on a row and the diff in the panel cannot disagree.
 */

export type PromptChange =
  /** Recorded before the runtime captured prompts. Not the same as empty. */
  | "unrecorded"
  /** The first recorded prompt in this run, so there is nothing to compare. */
  | "first"
  | "same"
  | "changed";

export type DiffLineKind = "same" | "added" | "removed";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
}

/**
 * Above this many lines on either side the quadratic table below stops being
 * free. A prompt that large is a generated dump rather than an instruction,
 * and a whole-block replace is both honest and instant. The panel says which
 * mode it is in, so nobody reads a coarse diff as a fine one.
 */
const MAX_DIFF_LINES = 800;

export function isTruncatedDiff(before: string, after: string): boolean {
  return before.split("\n").length > MAX_DIFF_LINES || after.split("\n").length > MAX_DIFF_LINES;
}

/**
 * A line-level diff, longest common subsequence, in the order a reader scans.
 *
 * Character-level diffing was deliberately not used: the lines that change
 * between turns are whole advisories ("tool `refund` is not available on this
 * turn"), and highlighting three characters inside one of them buries the
 * fact that a whole line appeared.
 */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");

  if (a.length > MAX_DIFF_LINES || b.length > MAX_DIFF_LINES) {
    return [
      ...a.map((text): DiffLine => ({ kind: "removed", text })),
      ...b.map((text): DiffLine => ({ kind: "added", text })),
    ];
  }

  // lcs[i][j] = length of the longest common subsequence of a[i:] and b[j:].
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0)
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "removed", text: a[i] });
      i++;
    } else {
      out.push({ kind: "added", text: b[j] });
      j++;
    }
  }
  while (i < a.length) out.push({ kind: "removed", text: a[i++] });
  while (j < b.length) out.push({ kind: "added", text: b[j++] });
  return out;
}

/** How many lines the diff actually changed, for a one line summary that
 *  does not require opening the diff to read. */
export function countChanges(lines: readonly DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.kind === "added") added++;
    else if (line.kind === "removed") removed++;
  }
  return { added, removed };
}

/**
 * Collapses long stretches of unchanged lines to a marker, the way a code
 * review does. A prompt is mostly unchanged between turns, and thirty screens
 * of identical text is how the four changed lines get missed.
 */
export function collapseUnchanged(
  lines: readonly DiffLine[],
  context = 3
): (DiffLine | { kind: "gap"; hidden: number })[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].kind === "same") continue;
    for (let j = Math.max(0, i - context); j <= Math.min(lines.length - 1, i + context); j++) {
      keep[j] = true;
    }
  }

  const out: (DiffLine | { kind: "gap"; hidden: number })[] = [];
  let hidden = 0;
  for (let i = 0; i < lines.length; i++) {
    if (keep[i]) {
      if (hidden > 0) {
        out.push({ kind: "gap", hidden });
        hidden = 0;
      }
      out.push(lines[i]);
    } else {
      hidden++;
    }
  }
  if (hidden > 0) out.push({ kind: "gap", hidden });
  return out;
}
