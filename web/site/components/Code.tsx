import { Fragment } from "react";
import { tokenize } from "@/lib/highlight";

type Lang = "python" | "sh" | "output" | "text";

/**
 * A code block on the night surface. Server-rendered: the tokens are spans in
 * the HTML, so there is no highlighter in the client bundle and nothing to
 * flash in after hydration.
 *
 * `highlight` takes 1-based line numbers to mark, for the passages the prose
 * beside the block is talking about.
 */
export function Code({
  code,
  lang = "python",
  title,
  highlight = [],
  label,
}: {
  code: string;
  lang?: Lang;
  title?: string;
  highlight?: number[];
  /** Accessible name for the region, when the title does not say enough. */
  label?: string;
}) {
  const lines = splitLines(code.replace(/\n$/, ""), lang);
  const marked = new Set(highlight);
  return (
    <figure className="code" aria-label={label ?? title} style={{ margin: 0 }}>
      {title ? (
        <figcaption className="code-head">
          <span>
            <span className="dot" aria-hidden />
            {title}
          </span>
          <span>{langLabel(lang)}</span>
        </figcaption>
      ) : null}
      <pre tabIndex={0}>
        <code>
          {lines.map((tokens, i) => (
            <span key={i} className={marked.has(i + 1) ? "line hl" : "line"}>
              {tokens.map((t, j) =>
                t.cls ? (
                  <span key={j} className={t.cls}>
                    {t.text}
                  </span>
                ) : (
                  <Fragment key={j}>{t.text}</Fragment>
                ),
              )}
              {tokens.length === 0 ? "\n" : null}
            </span>
          ))}
        </code>
      </pre>
    </figure>
  );
}

function langLabel(lang: Lang) {
  return { python: "python", sh: "shell", output: "stdout", text: "" }[lang];
}

/**
 * Tokens are produced for the whole source (so a triple-quoted string spans
 * lines correctly) and then cut at newlines, so each line can be its own
 * element and take a highlight.
 */
function splitLines(code: string, lang: Lang) {
  const tokens = tokenize(code, lang);
  const lines: { cls: string | null; text: string }[][] = [[]];
  for (const token of tokens) {
    const parts = token.text.split("\n");
    parts.forEach((part, i) => {
      if (i > 0) lines.push([]);
      if (part) lines[lines.length - 1].push({ cls: token.cls, text: part });
    });
  }
  // Every line ends with a newline so copy-paste keeps the structure.
  return lines.map((line) => (line.length ? [...line, { cls: null, text: "\n" }] : line));
}
