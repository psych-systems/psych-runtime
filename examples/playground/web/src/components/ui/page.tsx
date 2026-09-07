/**
 * Page furniture, in one place.
 *
 * Every page in the old app opened with its own hand-built header: an
 * uppercase tracked eyebrow ("Registry", "Configuration", "Verification
 * suite"), a title, a paragraph, and its own idea of spacing -- stacked under
 * a second, static header bar from the shell that said "Agent runtime
 * playground" on every screen and did no work at all. Six pages, six
 * headers, six answers to "how much space goes under a title".
 *
 * These are that answer, once. `PageHeader` also owns the page's actions, so
 * the primary action of a screen is always in the same place.
 */

import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * A page, and the scrolling pane it lives in.
 *
 * `AppShell` clips at `SidebarInset` (`overflow-hidden`) for a reason its own
 * comment gives: a descendant positioned against that relative box counted
 * toward an ancestor's scroll height without making any layout box bigger, so
 * the whole console -- sidebar included -- shifted up by 47px when you
 * scrolled past the end of a long form. Clipping there creates a contract,
 * which that comment states: *every page owns its scrolling, a `flex-1
 * overflow-y-auto` pane that fills the viewport*.
 *
 * This did not hold up that end. It was one centred column with no pane, so
 * inside an ancestor that clips, anything past the fold was simply cut off --
 * and with no scroll container anywhere, no scrollbar to find either.
 *
 * `min-h-0` is as load-bearing as the overflow. A flex child defaults to
 * `min-height: auto` and refuses to shrink below its content, so without it
 * the pane grows to fit instead of scrolling and nothing ever measures as
 * overflowing.
 *
 * Two elements rather than one: the outer scrolls the full width so the
 * scrollbar sits at the edge of the pane, while the inner keeps the centred
 * `max-w-5xl` column. Putting `overflow-y-auto` on the centred element itself
 * would float the scrollbar in from the window edge, against every other
 * scrolling surface in the console.
 */
export function Page({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className={cn("mx-auto flex w-full max-w-6xl flex-col gap-7 px-4 py-6 sm:px-7 sm:py-8 lg:px-10", className)}>
        {children}
      </div>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-5 border-b border-border/70 pb-5">
      <div className="flex min-w-0 flex-col gap-2">
        <h1 className="text-3xl leading-tight font-semibold">{title}</h1>
        {description && (
          <p className="max-w-2xl text-prose text-muted-foreground">{description}</p>
        )}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  title?: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("flex flex-col gap-3.5", className)}>
      {(title || actions) && (
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-col gap-1">
            {title && <h2 className="text-base font-semibold">{title}</h2>}
            {description && (
              <p className="max-w-2xl text-caption text-muted-foreground">{description}</p>
            )}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/**
 * The one empty state.
 *
 * The old app had five: a dashed box with an icon and a line of text, sized
 * differently on each page, none of them offering the action that would fill
 * the space. An empty state is the first thing a new person sees, so it says
 * what this is for and gives them the one button that starts it.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface/40 px-6 py-14 text-center",
        className
      )}
    >
      <span className="flex size-10 items-center justify-center rounded-full bg-background text-muted-foreground">
        <Icon className="size-5" aria-hidden />
      </span>
      <div className="flex max-w-sm flex-col gap-1">
        <p className="font-medium">{title}</p>
        {description && <p className="text-caption text-muted-foreground">{description}</p>}
      </div>
      {action}
    </div>
  );
}

/**
 * A labelled value in a row of them: a count, a duration, a cost.
 *
 * Numbers stay out of prose (they were scattered through paragraphs and
 * badges before), and every one of them is tabular so a column of them lines
 * up.
 */
export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "muted" | "failed";
}) {
  return (
    <div className="flex min-w-24 flex-col gap-0.5">
      <span className="text-micro font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </span>
      <span
        className={cn(
          "tabular text-lg leading-tight font-semibold",
          tone === "muted" && "text-muted-foreground",
          tone === "failed" && "text-status-failed"
        )}
      >
        {value}
      </span>
      {hint && <span className="text-micro text-muted-foreground">{hint}</span>}
    </div>
  );
}

/**
 * The disclosure every technical detail lives behind on an everyday screen.
 *
 * The brief this app is built to: run ids, version hashes and sequence
 * numbers do not belong on a page someone uses to talk to an agent. They are
 * not *hidden* -- an operator needs them and guessing is worse -- they are one
 * click away, under a consistent label, on every surface that has any.
 */
export function TechnicalDetails({
  children,
  label = "Technical details",
  className,
}: {
  children: ReactNode;
  label?: string;
  className?: string;
}) {
  return (
    <details className={cn("group", className)}>
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-caption text-muted-foreground transition-colors hover:text-foreground">
        <svg
          viewBox="0 0 12 12"
          className="size-3 transition-transform group-open:rotate-90"
          aria-hidden
        >
          <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        {label}
      </summary>
      <div className="mt-2 flex flex-col gap-2 border-l border-border pl-3">{children}</div>
    </details>
  );
}

/** One `label: value` line inside a `TechnicalDetails`. */
export function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-caption">
      <span className="text-muted-foreground">{label}</span>
      <span className="min-w-0 break-all">{children}</span>
    </div>
  );
}
