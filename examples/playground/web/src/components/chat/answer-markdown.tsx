"use client";

import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * The agent's answer, rendered.
 *
 * An agent set to answer concisely is asked for "bullet points or a small
 * table when you are presenting more than two or three facts", and it obliges:
 * it returns Markdown. Rendering that as preformatted text put `- Accounts and
 * account attributes` on the screen, dash and all, which is the raw material
 * of a readable answer rather than a readable answer. Asking for structure and
 * then showing the syntax is worse than never asking.
 *
 * ## Raw HTML is not enabled, and that is the point
 *
 * `react-markdown` does not render embedded HTML unless `rehype-raw` is added,
 * and it is deliberately not added. This text is model output, and model output
 * is downstream of whatever a tool returned, which is downstream of whatever
 * someone put in a record the agent read. Treating it as a document to display
 * is right; treating it as markup to execute would make every tool result a
 * script-injection surface.
 *
 * ## Only what an answer needs
 *
 * The component map below covers what the answer-style instruction actually
 * asks for and what prose needs: paragraphs, lists, tables, emphasis, code,
 * links, small headings. Everything renders on the app's own type scale rather
 * than the browser's defaults, so an answer sits in the page instead of on it.
 */
export function AnswerMarkdown({ children }: { children: string }) {
  return (
    <div className="text-prose text-foreground">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
          ul: ({ children }) => (
            <ul className="mb-3 flex list-disc flex-col gap-1 pl-5 last:mb-0">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-3 flex list-decimal flex-col gap-1 pl-5 last:mb-0">{children}</ol>
          ),
          li: ({ children }) => <li className="pl-0.5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          code: ({ children }) => (
            <code className="rounded bg-surface px-1 py-0.5 font-technical text-caption text-surface-foreground">
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="mb-3 overflow-x-auto rounded-lg border border-border bg-surface p-3 font-technical text-caption last:mb-0">
              {children}
            </pre>
          ),
          // A table is the other shape the answer style asks for, so it gets
          // real furniture rather than the browser's unstyled default. It
          // scrolls inside its own box: a wide table must never make the whole
          // conversation scroll sideways.
          table: ({ children }) => (
            <div className="mb-3 w-full overflow-x-auto last:mb-0">
              <table className="w-full border-collapse text-body">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="border-b border-border">{children}</thead>,
          th: ({ children }) => (
            <th className="px-3 py-1.5 text-left font-medium text-muted-foreground">{children}</th>
          ),
          td: ({ children }) => (
            <td className="border-b border-border/60 px-3 py-1.5 align-top">{children}</td>
          ),
          blockquote: ({ children }) => (
            <blockquote className="mb-3 border-l-2 border-border pl-3 text-muted-foreground last:mb-0">
              {children}
            </blockquote>
          ),
          // Headings in a chat answer are a paragraph lead, not a document
          // outline: the answer sits inside a page that already has a heading
          // hierarchy, so they are styled by weight rather than by size.
          h1: heading,
          h2: heading,
          h3: heading,
          h4: heading,
          h5: heading,
          h6: heading,
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              // An answer's links point wherever a tool result pointed, which
              // is not this application's to vouch for.
              rel="noopener noreferrer nofollow"
              className="text-primary underline underline-offset-2 hover:no-underline"
            >
              {children}
            </a>
          ),
          hr: () => <hr className="my-4 border-border" />,
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}

function heading({ children }: { children?: ReactNode }) {
  return <p className="mb-2 font-semibold text-foreground last:mb-0">{children}</p>;
}
