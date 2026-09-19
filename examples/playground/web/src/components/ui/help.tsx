"use client";

import { useState, type ReactNode } from "react";
import { CircleHelpIcon } from "lucide-react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * The one place an explanation lives: behind a small "?" beside the thing it
 * explains. Hovering shows the short line; clicking pins the full text open so
 * it can be read at leisure and copied. The page itself carries only labels,
 * so a person who already knows what a setting does is never made to read
 * about it again, and a person who does not is one hover away.
 */
export function HelpTip({
  title,
  children,
  short,
  className,
}: {
  /** A heading for the pinned popover. Defaults to nothing. */
  title?: string;
  /** The full explanation, shown when pinned. */
  children: ReactNode;
  /** One line for the hover. Defaults to the full explanation. */
  short?: ReactNode;
  className?: string;
}) {
  const [pinned, setPinned] = useState(false);
  const trigger = (
    <button
      type="button"
      aria-label={title ? `About ${title}` : "More about this"}
      className={cn(
        "inline-flex size-5 shrink-0 items-center justify-center rounded-full text-muted-foreground/70 transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
        pinned && "text-foreground",
        className,
      )}
    >
      <CircleHelpIcon className="size-4" aria-hidden />
    </button>
  );
  return (
    <Popover open={pinned} onOpenChange={setPinned}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>{trigger}</PopoverTrigger>
        </TooltipTrigger>
        {!pinned && (
          <TooltipContent side="top" className="max-w-xs text-left">
            {short ?? children}
          </TooltipContent>
        )}
      </Tooltip>
      <PopoverContent side="top" align="start" className="max-w-sm text-body">
        {title && <p className="mb-1.5 font-medium">{title}</p>}
        <div className="flex flex-col gap-2 text-caption text-muted-foreground [&_code]:font-technical [&_code]:text-foreground">
          {children}
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** A label with its help beside it, for a form field or a table row. */
export function LabelWithHelp({
  label,
  help,
  htmlFor,
  className,
}: {
  label: ReactNode;
  help?: ReactNode;
  htmlFor?: string;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <label htmlFor={htmlFor} className="text-body font-medium">
        {label}
      </label>
      {help && <HelpTip title={typeof label === "string" ? label : undefined}>{help}</HelpTip>}
    </span>
  );
}
