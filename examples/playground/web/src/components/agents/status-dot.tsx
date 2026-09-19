"use client";

import type { ReactNode } from "react";
import { CheckIcon, MinusIcon } from "lucide-react";

import { FeatureRow } from "@/components/ui/feature-table";
import { cn } from "@/lib/utils";

/**
 * A read-only row saying whether one ability is on.
 *
 * Off is stated rather than omitted: "this agent cannot ask you anything" is
 * as much a fact about it as the reverse, and a page that only listed what
 * was on would read as a shorter agent rather than a narrower one.
 */
export function StatusDot({
  label,
  on,
  detail,
  help,
}: {
  label: string;
  on: boolean;
  detail?: ReactNode;
  help?: ReactNode;
}) {
  return (
    <FeatureRow
      label={label}
      help={help}
      detail={detail}
      control={
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-caption",
            on ? "bg-status-done/10 text-status-done" : "bg-surface text-muted-foreground",
          )}
        >
          {on ? (
            <CheckIcon className="size-3" aria-hidden />
          ) : (
            <MinusIcon className="size-3" aria-hidden />
          )}
          {on ? "On" : "Off"}
        </span>
      }
    />
  );
}
