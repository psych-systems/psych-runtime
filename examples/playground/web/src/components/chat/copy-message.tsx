"use client";

import { useState, type RefObject } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Take a message away with its formatting intact.
 *
 * `Copyable` already covers a technical value an operator pastes into a log
 * query, where plain text is the whole point. A message is the other case: it
 * goes into a doc, a ticket or a chat that understands formatting, and pasting
 * `**asterisks**` and `- dashes` there hands someone the raw material of an
 * answer rather than the answer. So the clipboard carries `text/html` as well,
 * and a destination that only reads plain text still gets the Markdown source,
 * which is the readable half of the pair rather than tags with their angle
 * brackets showing.
 *
 * The HTML is read back off the node already on screen rather than by
 * re-rendering the Markdown to a second string. There is one renderer,
 * `AnswerMarkdown`, and a second path through it would be free to drift from
 * what the person is looking at while both kept passing their own tests.
 */

/**
 * Affordances, not content. A control that travelled into a document would
 * paste as a dead word in the middle of a sentence.
 */
const AFFORDANCE =
  "button, input, select, textarea, [role='button'], [data-copy-exclude]";

function renderedHtml(node: HTMLElement): string {
  const clone = node.cloneNode(true) as HTMLElement;
  for (const control of clone.querySelectorAll(AFFORDANCE)) control.remove();
  return clone.innerHTML;
}

/**
 * True when something landed on the clipboard. The rich write is attempted
 * first and its failure is not reportable on its own: `ClipboardItem` is
 * missing in some browsers and `write` rejects outright off a secure origin,
 * and in both cases the plain text is still worth having.
 */
async function writeBoth(html: string, text: string): Promise<boolean> {
  try {
    if (typeof ClipboardItem === "function") {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([text], { type: "text/plain" }),
        }),
      ]);
      return true;
    }
  } catch {
    // Deliberately quiet: the plain-text attempt below decides what to report.
  }

  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function CopyMessageButton({
  /** The node that is on screen. Its `innerHTML` is what gets copied. */
  source,
  /** The Markdown behind that node, for a plain-text destination. */
  text,
  className,
}: {
  source: RefObject<HTMLElement | null>;
  text: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    const node = source.current;
    // The button only renders alongside a mounted node, so a missing one means
    // the ref never attached rather than an empty message.
    const html = node === null ? text : renderedHtml(node);
    if (await writeBoth(html, text)) {
      // Confirmed on the button itself, as `Copyable` does, rather than with a
      // toast as well: two notices for one click is the app congratulating
      // itself, and the one that answers "did that message copy" is the one
      // attached to that message. Failure is the other way round, below: there
      // is nothing to read off a button that did nothing.
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } else {
      toast.error("Could not copy that message", {
        description: "This browser refused clipboard access.",
      });
    }
  }

  const label = copied ? "Copied" : "Copy this message";
  return (
    // The icon carries it, and the word beside it only made this button wider
    // than the branch and fork buttons it sits with. The name is still on the
    // button, as `aria-label`, because an icon with no accessible name is a
    // button nobody using a screen reader can act on -- and the tooltip is not
    // that name. The check mark is the confirmation, in the same slot.
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={label}
          // Hidden until the exchange is hovered: a button permanently under
          // every message is a timeline of buttons. Keyboard users never
          // hover, so focus reveals it too.
          className={cn(
            "size-7 text-muted-foreground opacity-0 transition-opacity group-hover/exchange:opacity-100 focus-visible:opacity-100",
            className,
          )}
          onClick={() => void copy()}
        >
          {copied ? (
            <CheckIcon className="size-3.5 text-status-done" aria-hidden />
          ) : (
            <CopyIcon className="size-3.5" aria-hidden />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
