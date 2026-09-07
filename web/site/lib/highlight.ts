/**
 * A small, deterministic tokenizer for the code the marketing pages show.
 *
 * The site shows Python and shell, nothing else, and it shows them with four
 * colours. A full grammar engine would be a dependency carried for one
 * decorative effect, and it would run at build time against strings this file
 * already controls. This does the job in a page of code and its output is
 * plain spans, so the docs (which use fumadocs' own highlighter) and the
 * marketing pages can each pick the right tool without sharing a theme file.
 */

export type Token = { cls: string | null; text: string };

const PY_KEYWORDS = new Set([
  "import",
  "from",
  "async",
  "await",
  "def",
  "return",
  "with",
  "as",
  "if",
  "else",
  "elif",
  "for",
  "in",
  "not",
  "and",
  "or",
  "None",
  "True",
  "False",
  "class",
  "raise",
  "try",
  "except",
  "finally",
  "yield",
  "lambda",
  "pass",
  "global",
  "while",
  "break",
  "is",
]);

const PY_RE =
  /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?''')|(f?"(?:\\.|[^"\\\n])*"|f?'(?:\\.|[^'\\\n])*')|(@[\w.]+)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_]\w*\b)|([=+\-*/<>!|&%:,.()\[\]{}]+)|(\s+)|(.)/g;

export function tokenizePython(source: string): Token[] {
  const out: Token[] = [];
  let m: RegExpExecArray | null;
  PY_RE.lastIndex = 0;
  while ((m = PY_RE.exec(source)) !== null) {
    const [text, com, doc, str, dec, num, word, op] = m;
    if (com !== undefined || doc !== undefined) out.push({ cls: "tok-com", text });
    else if (str !== undefined) out.push({ cls: "tok-str", text });
    else if (dec !== undefined) out.push({ cls: "tok-dec", text });
    else if (num !== undefined) out.push({ cls: "tok-num", text });
    else if (word !== undefined) {
      if (PY_KEYWORDS.has(word)) out.push({ cls: "tok-kw", text });
      else if (word === "psych_runtime") out.push({ cls: "tok-name", text });
      else if (/^[A-Z]/.test(word)) out.push({ cls: "tok-fn", text });
      else out.push({ cls: null, text });
    } else if (op !== undefined) out.push({ cls: "tok-op", text });
    else out.push({ cls: null, text });
  }
  return out;
}

const SH_RE = /(#[^\n]*)|("(?:\\.|[^"\\\n])*"|'[^'\n]*')|(\s--?[\w-]+)|(\s+)|([^\s#"']+)/g;

export function tokenizeShell(source: string): Token[] {
  const out: Token[] = [];
  let m: RegExpExecArray | null;
  let lineStart = true;
  SH_RE.lastIndex = 0;
  while ((m = SH_RE.exec(source)) !== null) {
    const [text, com, str, flag, ws, word] = m;
    if (com !== undefined) out.push({ cls: "tok-com", text });
    else if (str !== undefined) out.push({ cls: "tok-str", text });
    else if (flag !== undefined) out.push({ cls: "tok-op", text });
    else if (ws !== undefined) out.push({ cls: null, text });
    else if (word !== undefined) out.push({ cls: lineStart ? "tok-kw" : null, text });
    lineStart = text.includes("\n") || (lineStart && ws !== undefined && !text.includes("\n"));
    if (word !== undefined) lineStart = false;
    if (text.endsWith("\n")) lineStart = true;
  }
  return out;
}

/** Program output: no tokens, just muted text, with `$ ` prompts in copper. */
export function tokenizeOutput(source: string): Token[] {
  return source.split("\n").flatMap((line, i, all) => {
    const tokens: Token[] = [];
    if (line.startsWith("$ ")) {
      tokens.push({ cls: "tok-kw", text: "$ " }, { cls: null, text: line.slice(2) });
    } else {
      tokens.push({ cls: "tok-out", text: line });
    }
    if (i < all.length - 1) tokens.push({ cls: null, text: "\n" });
    return tokens;
  });
}

export function tokenize(source: string, lang: "python" | "sh" | "output" | "text"): Token[] {
  if (lang === "python") return tokenizePython(source);
  if (lang === "sh") return tokenizeShell(source);
  if (lang === "output") return tokenizeOutput(source);
  return [{ cls: null, text: source }];
}
