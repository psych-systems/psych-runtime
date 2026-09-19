"use client";

import Link from "next/link";
import { CheckCircle2Icon, CircleIcon, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useLocalStorage } from "@/hooks/use-local-storage";
import { cn } from "@/lib/utils";
import { PSYCH_CATALOGUE_ID, type CatalogueResponse } from "@/lib/types";

const DISMISSED_KEY = "psych.playground.getStarted.dismissed";

interface Step {
  done: boolean;
  title: string;
  href: string;
  action: string;
}

/**
 * The three things between a new account and a working conversation.
 *
 * Shown until they are done or somebody says they do not want it, and each
 * step ticks itself off from the catalogue rather than from a flag this card
 * sets: a person who added a key on the Settings page before ever seeing this
 * finds that step already done.
 */
export function GetStartedCard({ catalogue }: { catalogue: CatalogueResponse | null }) {
  const [dismissed, setDismissed] = useLocalStorage(DISMISSED_KEY, false);

  if (catalogue === null || dismissed) return null;

  // The provider *in use* has to be able to answer. A key on one that is
  // standing by leaves chat failing exactly as before, so it does not count.
  const hasProvider =
    catalogue.provider_ready ??
    catalogue.providers.some((provider) => provider.configured && provider.active);
  const hasConnector = catalogue.connectors.some((connector) => connector.connected);
  const psych = catalogue.agents.find((agent) => agent.catalogue_id === PSYCH_CATALOGUE_ID);
  const hasPsych = (psych?.agent_id ?? null) !== null;

  const steps: Step[] = [
    {
      done: hasProvider,
      title: "Add a provider key",
      href: "/settings#providers",
      action: "Open Settings",
    },
    {
      done: hasConnector,
      title: "Connect a system you already use",
      href: "/connections",
      action: "Open Connections",
    },
    {
      done: hasPsych,
      title: "Talk to Psych",
      href: hasPsych ? `/chat?agent=${encodeURIComponent(psych!.agent_id!)}` : "/agents",
      action: hasPsych ? "Open Chat" : "Add the agents",
    },
  ];

  // Nothing left to do is the same as being dismissed, without making anybody
  // click to say so.
  if (steps.every((step) => step.done)) return null;

  const next = steps.find((step) => !step.done)!;

  return (
    <section
      aria-label="Get started"
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h2 className="text-base font-semibold">Get started</h2>
          <p className="text-caption text-muted-foreground">
            Three steps, and this console answers.
          </p>
        </div>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Hide Get started"
          onClick={() => setDismissed(true)}
        >
          <XIcon />
        </Button>
      </div>

      <ol className="flex flex-col gap-1.5">
        {steps.map((step, index) => (
          <li key={step.title} className="flex flex-wrap items-center gap-2">
            {step.done ? (
              <CheckCircle2Icon className="size-4 shrink-0 text-status-done" aria-hidden />
            ) : (
              <CircleIcon className="size-4 shrink-0 text-muted-foreground/60" aria-hidden />
            )}
            <span className={cn("text-body", step.done && "text-muted-foreground line-through")}>
              {index + 1}. {step.title}
            </span>
            {!step.done && (
              <Link
                href={step.href}
                className="text-caption font-medium text-primary underline underline-offset-2"
              >
                {step.action}
              </Link>
            )}
          </li>
        ))}
      </ol>

      <div>
        <Button size="sm" asChild>
          <Link href={next.href}>{next.action}</Link>
        </Button>
      </div>
    </section>
  );
}
