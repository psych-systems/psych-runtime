"use client";

import { useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * An identifier you can take away with you.
 *
 * Every technical value on this surface is something an operator is about to
 * paste into a log query or a support thread. Before this they retyped a
 * 64 character hash by eye, which is how a run id ends up transposed in a
 * ticket and nobody can find the Run it names.
 */
export function Copyable({ value, className }: { value: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard permission denied, or an insecure origin. The value is
      // still on screen and selectable, so there is nothing to report.
    }
  }

  return (
    <button
      type="button"
      onClick={() => void copy()}
      title="Copy"
      className={cn(
        "group inline-flex max-w-full items-center gap-1.5 rounded-sm text-left align-baseline font-technical text-caption break-all transition-colors hover:text-foreground",
        className
      )}
    >
      <span className="min-w-0 break-all">{value}</span>
      {copied ? (
        <CheckIcon className="size-3 shrink-0 text-status-done" aria-hidden />
      ) : (
        <CopyIcon
          className="size-3 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100"
          aria-hidden
        />
      )}
      <span className="sr-only">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

/**
 * The same clipboard behaviour, for a value too long to show inline: a system
 * prompt, a diff, a payload. One owner for "did the copy land", so the button
 * on a panel header and the identifier in a detail row cannot report a copy
 * differently.
 */
export function CopyButton({
  value,
  label = "Copy",
  className,
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard permission denied, or an insecure origin. The text is still
      // on screen and selectable, so there is nothing to report.
    }
  }

  return (
    <button
      type="button"
      onClick={() => void copy()}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-caption text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        className
      )}
    >
      {copied ? (
        <CheckIcon className="size-3.5 text-status-done" aria-hidden />
      ) : (
        <CopyIcon className="size-3.5" aria-hidden />
      )}
      {copied ? "Copied" : label}
    </button>
  );
}
