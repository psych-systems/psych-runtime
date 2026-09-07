/**
 * Fold the reasoning out of long changelog entries.
 *
 * Every entry in `CHANGELOG.md` is written as a bold claim followed by the
 * reasoning behind it, which is the right shape for the repository: months
 * later, the "why" is the part nobody can reconstruct. On a web page it means
 * 108 entries whose median body is around 570 characters and whose longest is
 * over 4,000, so the reader scrolls past the answer to "what changed" looking
 * for it.
 *
 * This keeps the claim and the first paragraph visible and folds the rest into
 * a `<details>`. Nothing is removed, the Markdown in the repository is
 * untouched, and a reader who wants the argument is one click from all of it.
 *
 * Scoped by file path rather than applied everywhere: a guide's list items are
 * short and folding them would hide the content the page exists for.
 */
const FOLD_OVER = 320;

function textLength(node) {
  if (node.value) return node.value.length;
  return (node.children ?? []).reduce((n, c) => n + textLength(c), 0);
}

export default function remarkFoldChangelog() {
  return (tree, file) => {
    if (!String(file.path ?? "").endsWith("changelog.mdx")) return;

    const visit = (node) => {
      for (const child of node.children ?? []) {
        if (child.type === "listItem") fold(child);
        visit(child);
      }
    };

    const fold = (item) => {
      const kids = item.children ?? [];
      if (kids.length < 2) return;
      const tail = kids.slice(1);
      if (tail.reduce((n, c) => n + textLength(c), 0) < FOLD_OVER) return;
      item.children = [
        kids[0],
        {
          type: "mdxJsxFlowElement",
          name: "details",
          attributes: [{ type: "mdxJsxAttribute", name: "className", value: "why" }],
          children: [
            {
              type: "mdxJsxFlowElement",
              name: "summary",
              attributes: [],
              children: [{ type: "paragraph", children: [{ type: "text", value: "why" }] }],
            },
            ...tail,
          ],
        },
      ];
    };

    visit(tree);
  };
}
