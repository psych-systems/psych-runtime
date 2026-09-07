"use client";

import { useState } from "react";
import { Check, Copy, ICON } from "@/components/icons";

/**
 * The install command with a copy button. The one control every visitor uses,
 * so it degrades: without scripting the text is still selectable and the
 * button is inert rather than broken.
 */
export function InstallCommand({ command, className }: { command: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // A denied clipboard leaves the text selectable, which is the fallback.
    }
  }

  return (
    <div className={className ? `install ${className}` : "install"}>
      <code>{command}</code>
      <button
        type="button"
        onClick={copy}
        data-state={copied ? "done" : "idle"}
        aria-label={`Copy "${command}" to the clipboard`}
      >
        {copied ? <Check size={ICON} aria-hidden /> : <Copy size={ICON} aria-hidden />}
        <span className="sr-only" aria-live="polite">
          {copied ? "Copied" : ""}
        </span>
      </button>
    </div>
  );
}
