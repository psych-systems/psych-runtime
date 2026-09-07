"use client";

import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";

import { cn } from "@/lib/utils";

const COLLAPSE_THRESHOLD = 400;

function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    // A cyclic or otherwise unserialisable value still has a printable form,
    // and showing it beats showing nothing where a payload is expected.
    return String(value);
  }
}

/**
 * Arguments and results, in full.
 *
 * The trace is the one place these are shown whole rather than previewed, so
 * nothing here truncates: a long payload goes behind a disclosure that says
 * how much there is, and opens to all of it. Lives in `components/trace`
 * rather than borrowing the chat transcript's block, so the technical view
 * does not break when the chat surface is restyled.
 */
export function JsonView({ value, emptyLabel = "{}" }: { value: unknown; emptyLabel?: string }) {
  const text = stringify(value);
  const [open, setOpen] = useState(false);
  const isEmpty = text.length === 0 || text === "{}" || text === "null";

  if (isEmpty) {
    return <span className="font-technical text-caption text-muted-foreground italic">{emptyLabel}</span>;
  }

  if (text.length <= COLLAPSE_THRESHOLD) {
    return (
      <pre className="max-w-full rounded-md bg-muted/50 p-2 font-technical text-caption break-words whitespace-pre-wrap text-foreground/90">
        {text}
      </pre>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-fit items-center gap-1 text-caption text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronRightIcon className={cn("size-3 transition-transform", open && "rotate-90")} aria-hidden />
        {open ? "Collapse" : `Show all ${text.length.toLocaleString()} characters`}
      </button>
      {open && (
        <pre className="max-h-96 max-w-full overflow-y-auto rounded-md bg-muted/50 p-2 font-technical text-caption break-words whitespace-pre-wrap text-foreground/90">
          {text}
        </pre>
      )}
    </div>
  );
}
