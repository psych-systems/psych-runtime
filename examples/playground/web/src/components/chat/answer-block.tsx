"use client";

import { useRef, useState } from "react";

import { AgentMark } from "@/components/chat/agent-mark";
import { AnswerMarkdown } from "@/components/chat/answer-markdown";
import { CopyMessageButton } from "@/components/chat/copy-message";
import { useRevealedText } from "@/components/chat/use-revealed-text";

/**
 * What the agent concluded.
 *
 * The one thing on the screen that is set in reading type. Everything else the
 * Run did is a step towards this and is sized like a step: the collapsed work
 * line above it, the tool calls inside that. A person who asked a question
 * should be able to find the sentence answering them without reading anything
 * else on the page.
 */
export function AnswerBlock({
  text,
  /** Whether the Run was still working when this block first rendered. */
  reveal,
}: {
  text: string;
  reveal: boolean;
}) {
  // Frozen at mount. An answer that lands while someone is watching arrives at
  // a reading rate; the same answer reopened from the history list tomorrow is
  // simply there. Freezing matters because the Run settles a moment after its
  // last turn is recorded, and a live `reveal` would flip to false mid
  // sentence and slam the rest of the answer onto the screen at once.
  const [revealOnArrival] = useState(reveal);
  const shown = useRevealedText(text, revealOnArrival);
  const complete = shown.length === text.length;
  const rendered = useRef<HTMLDivElement>(null);

  return (
    <div className="flex w-full gap-3">
      <AgentMark />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        {/* While the text is still arriving it is shown as plain preformatted
            text with a caret. Rendering half-written Markdown reflows on
            almost every frame: a list is a paragraph until its second item
            lands, a table is three lines of pipes until its separator row
            does. Parsing the finished answer once, at the end, is what makes
            the arrival read as typing rather than as a layout fighting
            itself. */}
        {complete ? (
          <div ref={rendered}>
            <AnswerMarkdown>{shown}</AnswerMarkdown>
          </div>
        ) : (
          <p className="text-prose whitespace-pre-wrap text-foreground">
            {shown}
            <span
              aria-hidden
              className="ml-px inline-block h-[1em] w-0.5 translate-y-0.5 animate-pulse rounded-full bg-primary align-baseline"
            />
          </p>
        )}

        {/* Only once the text has finished arriving. Copying a sentence that
            is still being written hands over half of it, and the node it would
            read carries the caret. */}
        {complete && (
          <CopyMessageButton source={rendered} text={text} className="-ml-2 self-start" />
        )}
      </div>
    </div>
  );
}
