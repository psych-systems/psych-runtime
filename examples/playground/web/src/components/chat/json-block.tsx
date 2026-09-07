"use client";

import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

const COLLAPSE_THRESHOLD = 240;

function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** A compact, monospace rendering of a tool call's arguments or result.
 * Collapses long payloads behind a disclosure rather than either truncating
 * data the user asked to see or flooding the transcript with it. */
export function JsonBlock({ value, emptyLabel = "{}" }: { value: unknown; emptyLabel?: string }) {
  const text = stringify(value);
  const [open, setOpen] = useState(false);
  const isEmpty = text.length === 0 || text === "{}" || text === "null";

  if (isEmpty) {
    return <span className="font-technical text-caption text-muted-foreground italic">{emptyLabel}</span>;
  }

  if (text.length <= COLLAPSE_THRESHOLD) {
    return (
      <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words font-technical text-caption text-foreground/90">
        {text}
      </pre>
    );
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex items-center gap-1 text-caption text-muted-foreground hover:text-foreground">
        <ChevronRightIcon className={cn("size-3 transition-transform", open && "rotate-90")} />
        {open ? "Collapse" : `Show ${text.length.toLocaleString()} characters`}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre className="mt-1 max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words font-technical text-caption text-foreground/90">
          {text}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}
