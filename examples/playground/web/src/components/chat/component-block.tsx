"use client";

import {
  ArrowDownRightIcon,
  ArrowUpRightIcon,
  ArrowRightIcon,
  CheckIcon,
  CircleIcon,
  ExternalLinkIcon,
  LoaderIcon,
} from "lucide-react";

import { ComponentChart } from "@/components/chat/component-chart";
import { Badge } from "@/components/ui/badge";
import type {
  CardComponent,
  CarouselComponent,
  ComponentField,
  DetailComponent,
  MetricComponent,
  PsychComponent,
  TimelineComponent,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * What the agent showed, drawn in this console's own vocabulary.
 *
 * The library sends a card, a carousel, an order, a timeline, a chart or a
 * metric as **data and intent only**: no colour, no font, no spacing, no
 * markup (`psych_runtime/core/components.py` argues why at length). Everything
 * visible below is decided here, in the console's tokens, which is what makes
 * a rebrand a change to `globals.css` rather than a change to any agent.
 *
 * ## The two things this file must keep doing
 *
 * **Every link is opened, never trusted.** `href` and `image_url` arrive
 * validated as http or https by the library, which refuses `javascript:` and
 * `data:` outright, so nothing here has to re-derive that rule. What this file
 * adds is the second half: `rel="noreferrer noopener"` and `target="_blank"`,
 * because the destination is still a place the model chose.
 *
 * **Nothing is rendered as markup.** Every string goes in as a text child, so
 * a model that writes `<script>` gets a person reading the characters
 * `<script>`. There is no `dangerouslySetInnerHTML` in this file and there
 * should never be one.
 */
export function ComponentBlock({ component }: { component: PsychComponent }) {
  switch (component.kind) {
    case "card":
      return (
        <Surface>
          <CardBody card={component} />
        </Surface>
      );
    case "carousel":
      return <CarouselBody carousel={component} />;
    case "detail":
      return (
        <Surface>
          <DetailBody detail={component} />
        </Surface>
      );
    case "timeline":
      return (
        <Surface>
          <TimelineBody timeline={component} />
        </Surface>
      );
    case "chart":
      return (
        <Surface>
          <ComponentChart chart={component} />
        </Surface>
      );
    case "metric":
      return (
        <Surface>
          <MetricBody metric={component} />
        </Surface>
      );
  }
}

/** One panel, matching the plan panel beside it so a conversation reads as one
 *  surface rather than as a stack of unrelated widgets. */
function Surface({ children }: { children: React.ReactNode }) {
  return (
    <section className="flex w-full flex-col gap-3 rounded-xl border border-border bg-surface/40 p-3">
      {children}
    </section>
  );
}

function CardBody({ card, compact = false }: { card: CardComponent; compact?: boolean }) {
  return (
    <div className={cn("flex min-w-0 gap-3", compact && "flex-col")}>
      {card.image_url && (
        <div
          className={cn(
            "relative shrink-0 overflow-hidden rounded-lg bg-muted",
            compact ? "h-28 w-full" : "size-20"
          )}
        >
          {/* A plain `img`, and `next/image` deliberately not used. Its
              optimizer would need an allowlist of hosts, and the host here is
              whatever the agent found -- an allowlist of `**` is not an
              allowlist, and routing arbitrary URLs through the server would
              make this console fetch and cache anything a model pointed at.
              `referrerPolicy` keeps the conversation's URL off that request. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={card.image_url}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            className="absolute inset-0 size-full object-cover"
          />
        </div>
      )}
      <div className="flex min-w-0 flex-col gap-1.5">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-body font-medium">{card.title}</span>
          {card.subtitle && (
            <span className="text-caption text-muted-foreground">{card.subtitle}</span>
          )}
        </div>
        {card.badges.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {card.badges.map((badge, index) => (
              <Badge key={index} variant="secondary">
                {badge}
              </Badge>
            ))}
          </div>
        )}
        {card.fields.length > 0 && <FieldList fields={card.fields} />}
        {card.href && <VisitLink href={card.href} />}
      </div>
    </div>
  );
}

/**
 * Several cards to compare, scrolled sideways.
 *
 * Sideways rather than stacked because the intent the agent sent is "these are
 * alternatives": a vertical list of five products reads as a ranking, and the
 * agent said nothing about rank.
 */
function CarouselBody({ carousel }: { carousel: CarouselComponent }) {
  if (carousel.cards.length === 0) return null;
  return (
    <section className="flex w-full flex-col gap-2">
      {carousel.title && <h3 className="text-caption font-medium">{carousel.title}</h3>}
      <div className="-mx-1 flex snap-x snap-mandatory gap-2 overflow-x-auto px-1 pb-1">
        {carousel.cards.map((card, index) => (
          <div
            key={index}
            className="w-56 shrink-0 snap-start rounded-xl border border-border bg-surface/40 p-3"
          >
            <CardBody card={card} compact />
          </div>
        ))}
      </div>
    </section>
  );
}

function DetailBody({ detail }: { detail: DetailComponent }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-body font-medium">{detail.title}</span>
          {detail.subtitle && (
            <span className="text-caption text-muted-foreground">{detail.subtitle}</span>
          )}
        </div>
        {/* The status is whatever system owns the record calls it, so it is
            shown as its own words rather than mapped onto this console's run
            states. A "Shipped" order is not a completed run. */}
        {detail.status && <Badge variant="outline">{detail.status}</Badge>}
      </div>
      {detail.fields.length > 0 && <FieldList fields={detail.fields} />}
      {detail.total && (
        <div className="flex items-baseline justify-between gap-3 border-t border-border pt-2">
          <span className="text-caption text-muted-foreground">{detail.total.label}</span>
          <span className="tabular text-body font-medium">{detail.total.value}</span>
        </div>
      )}
      {detail.href && <VisitLink href={detail.href} />}
    </div>
  );
}

/**
 * An ordered sequence with a position in it.
 *
 * The same three states the plan panel uses, and drawn the same way on
 * purpose: a person reading a delivery and a person reading the agent's own
 * plan are reading the same shape, and two different visual languages for
 * "this step is the current one" would be one too many.
 */
function TimelineBody({ timeline }: { timeline: TimelineComponent }) {
  if (timeline.steps.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {timeline.title && <h3 className="text-caption font-medium">{timeline.title}</h3>}
      <ol className="flex flex-col gap-2">
        {timeline.steps.map((step, index) => (
          <li key={index} className="flex items-start gap-2">
            <StepIcon state={step.state} />
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex items-baseline justify-between gap-3">
                <span
                  className={cn(
                    "text-body",
                    step.state === "upcoming" && "text-muted-foreground",
                    step.state === "current" && "font-medium"
                  )}
                >
                  {step.label}
                </span>
                {step.at && (
                  <span className="tabular shrink-0 text-micro text-muted-foreground">
                    {step.at}
                  </span>
                )}
              </span>
              {step.description && (
                <span className="text-caption text-muted-foreground">{step.description}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function StepIcon({ state }: { state: TimelineComponent["steps"][number]["state"] }) {
  if (state === "done") {
    return <CheckIcon className="mt-1 size-3.5 shrink-0 text-status-done" aria-label="Done" />;
  }
  if (state === "current") {
    return (
      <LoaderIcon
        className="mt-1 size-3.5 shrink-0 animate-spin text-status-running"
        aria-label="Now"
      />
    );
  }
  return (
    <CircleIcon className="mt-1 size-3.5 shrink-0 text-muted-foreground/50" aria-label="Upcoming" />
  );
}

/**
 * One number, set large.
 *
 * The direction arrow says which way it moved and nothing more. The agent does
 * not say whether that is good news, and this console does not guess: up is
 * good for revenue and bad for latency, and a green arrow on a rising error
 * rate is worse than no arrow at all.
 */
function MetricBody({ metric }: { metric: MetricComponent }) {
  const Arrow =
    metric.direction === "up"
      ? ArrowUpRightIcon
      : metric.direction === "down"
        ? ArrowDownRightIcon
        : metric.direction === "flat"
          ? ArrowRightIcon
          : null;
  return (
    <div className="flex flex-col gap-1">
      <span className="text-caption text-muted-foreground">{metric.label}</span>
      <span className="flex items-baseline gap-1.5">
        <span className="text-2xl font-semibold">{metric.value}</span>
        {metric.unit && <span className="text-caption text-muted-foreground">{metric.unit}</span>}
      </span>
      {metric.delta && (
        <span className="flex items-center gap-1 text-caption text-muted-foreground">
          {Arrow && <Arrow className="size-3.5" aria-hidden />}
          {metric.delta}
        </span>
      )}
    </div>
  );
}

function FieldList({ fields }: { fields: ComponentField[] }) {
  return (
    <dl className="flex flex-col gap-0.5">
      {fields.map((field, index) => (
        <div key={index} className="flex items-baseline justify-between gap-3">
          <dt className="text-caption text-muted-foreground">{field.label}</dt>
          <dd className="tabular min-w-0 truncate text-caption">{field.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The URL is shown, not just linked: the destination was chosen by a model,
 *  and a person deserves to see where they are about to go. */
function VisitLink({ href }: { href: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className="flex w-fit items-center gap-1 text-caption text-primary hover:underline"
    >
      <span className="max-w-72 truncate">{hostOf(href)}</span>
      <ExternalLinkIcon className="size-3 shrink-0" aria-hidden />
    </a>
  );
}

function hostOf(href: string): string {
  try {
    return new URL(href).host;
  } catch {
    // The library already refused anything that is not http or https, so this
    // is a URL that parses there and not here. Showing it whole beats showing
    // nothing.
    return href;
  }
}
