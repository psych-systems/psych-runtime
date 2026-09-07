"use client";

import { useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * A version hash, truncated for the eye with the full value on hover and a
 * one-click copy.
 *
 * This never appears as primary content. It lives inside a
 * `TechnicalDetails` disclosure, because the person choosing which agent to
 * talk to has no use for it and the person debugging a run cannot do without
 * it.
 */
export function VersionHash({ hash, className }: { hash: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const short = hash.length > 16 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash;

  async function copy() {
    try {
      await navigator.clipboard.writeText(hash);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access denied (permissions, or a non-secure context). The
      // hash is still selectable text, so this is a nicety, not a feature.
    }
  }

  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="font-technical text-caption">{short}</span>
        </TooltipTrigger>
        <TooltipContent className="font-technical">{hash}</TooltipContent>
      </Tooltip>
      <Button size="icon-xs" variant="ghost" onClick={() => void copy()} aria-label="Copy full hash">
        {copied ? <CheckIcon className="text-status-done" /> : <CopyIcon />}
      </Button>
    </span>
  );
}

/** Why two agents can share a hash, for the disclosure it sits inside. */
export function HashExplainer() {
  return (
    <p className="text-caption text-muted-foreground">
      The hash is computed from the agent&apos;s own content. Publishing the same agent again
      returns this one instead of making a copy.
    </p>
  );
}
