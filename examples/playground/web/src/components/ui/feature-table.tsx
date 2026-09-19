"use client";

import type { ReactNode } from "react";

import { HelpTip } from "@/components/ui/help";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

/**
 * Settings as a table rather than a stack of cards.
 *
 * One row per thing to decide: a name on the left, the control on the right,
 * and a "?" for anyone who wants the why. Groups are thin headings, not new
 * cards, so twenty settings read as one screen rather than twenty. A row may
 * carry a detail line under the label (a summary of what is set), and a
 * toggle row may reveal more fields beneath it only while it is on.
 */
export function FeatureTable({
  children,
  className,
  dense = false,
}: {
  children: ReactNode;
  className?: string;
  /** Tighter rows, for a table with many short items. */
  dense?: boolean;
}) {
  return (
    <div
      role="table"
      className={cn(
        "overflow-hidden rounded-xl border border-border bg-card",
        dense ? "[&_[role=row]]:py-2" : "[&_[role=row]]:py-3",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function FeatureGroup({
  title,
  help,
  children,
}: {
  title: string;
  help?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div role="rowgroup" className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-1.5 bg-muted/40 px-4 py-2">
        <span className="text-caption font-medium tracking-wide text-muted-foreground uppercase">
          {title}
        </span>
        {help && <HelpTip title={title}>{help}</HelpTip>}
      </div>
      <div className="divide-y divide-border/70">{children}</div>
    </div>
  );
}

export function FeatureRow({
  label,
  help,
  detail,
  control,
  children,
  htmlFor,
  className,
}: {
  label: ReactNode;
  /** Why this exists and what changes when it is set. Behind the "?". */
  help?: ReactNode;
  /** A short line under the label: the current value in words, a hint. */
  detail?: ReactNode;
  /** The control on the right: a Switch, a Select, an Input. */
  control?: ReactNode;
  /** Extra fields shown under the row, usually only while a toggle is on. */
  children?: ReactNode;
  htmlFor?: string;
  className?: string;
}) {
  return (
    <div role="row" className={cn("flex flex-col gap-3 px-4", className)}>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        {/* A floor on the label's width, so a wide control wraps under the
            label on a phone instead of squeezing it into a column of words. */}
        <div className="flex min-w-[11rem] flex-1 flex-col gap-0.5">
          <span className="inline-flex items-center gap-1.5">
            <label htmlFor={htmlFor} className="text-body font-medium">
              {label}
            </label>
            {help && <HelpTip title={typeof label === "string" ? label : undefined}>{help}</HelpTip>}
          </span>
          {detail && <span className="text-caption text-muted-foreground">{detail}</span>}
        </div>
        {control && <div className="flex shrink-0 items-center gap-2">{control}</div>}
      </div>
      {children && <div className="flex flex-col gap-3 pb-1 pl-0 sm:pl-4">{children}</div>}
    </div>
  );
}

/** A row whose control is a toggle. The most common row, so it has a name. */
export function ToggleRow({
  id,
  label,
  help,
  detail,
  checked,
  onCheckedChange,
  disabled,
  children,
}: {
  id: string;
  label: ReactNode;
  help?: ReactNode;
  detail?: ReactNode;
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  disabled?: boolean;
  /** Shown only while on. */
  children?: ReactNode;
}) {
  return (
    <FeatureRow
      htmlFor={id}
      label={label}
      help={help}
      detail={detail}
      control={
        <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} />
      }
    >
      {checked ? children : undefined}
    </FeatureRow>
  );
}
