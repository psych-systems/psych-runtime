"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  CheckCircle2Icon,
  CopyIcon,
  Loader2Icon,
  MessageSquareIcon,
  PencilIcon,
  SparklesIcon,
  WrenchIcon,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DetailRow, Section, TechnicalDetails } from "@/components/ui/page";
import { HelpTip } from "@/components/ui/help";
import { IssuesSummary } from "@/components/settings/validation";
import { AgentEssentials } from "@/components/agents/agent-essentials";
import { CapabilitiesTable } from "@/components/agents/capabilities-table";
import { BehaviourTable } from "@/components/agents/behaviour-table";
import { AdvancedTable } from "@/components/agents/advanced-table";
import { VersionHash } from "@/components/agents/version-hash";
import { STARTERS, useAgentForm, type AgentFormState } from "@/components/agents/use-agent-form";
import type { CreateAgentResponse } from "@/lib/types";

/**
 * Publishing an agent, in four decisions and a button.
 *
 * The form this replaces was fourteen cards, each explaining itself in a
 * paragraph, and a person had to read all of it to publish any of it. What
 * changed is not the fields -- every one of them is still here and still
 * reaches the same request -- but where the words are: behind a "?" beside
 * the thing they explain, and on by default rather than off and undiscovered.
 */
export function CreateAgentForm() {
  const form = useAgentForm();

  if (form.result) {
    return (
      <PublishedCard
        result={form.result}
        onKeepEditing={() => form.setResult(null)}
        router={form.router}
      />
    );
  }

  return (
    <div className="flex flex-col gap-7 pb-24">
      {form.duplicateOf === null && <Starters form={form} />}

      {form.duplicatedFrom !== null && (
        <Alert>
          {form.editing !== null ? <PencilIcon /> : <CopyIcon />}
          <AlertTitle>
            {form.editing !== null ? "Editing a published agent" : "Copied from a published agent"}
          </AlertTitle>
          <AlertDescription>
            Its limits, approval rules and per-connection tool lists are not reported back once an
            agent is published, so those start from the defaults. Check them before publishing.
          </AlertDescription>
        </Alert>
      )}

      {form.formError && (
        <Alert variant="destructive">
          <AlertTitle>This agent was not published</AlertTitle>
          <AlertDescription>{form.formError}</AlertDescription>
        </Alert>
      )}
      <IssuesSummary issues={form.issues} consumedPaths={form.consumedPaths} />

      <Section title="Essentials" description="All a working agent needs.">
        <AgentEssentials form={form} />
      </Section>

      <Section title="What it can use" description="Everything is on unless you take it away.">
        <CapabilitiesTable form={form} />
      </Section>

      <Section title="How it behaves" description="Each of these is part of the published agent.">
        <BehaviourTable form={form} />
      </Section>

      <Section title="Limits and waiting">
        <AdvancedTable form={form} />
      </Section>

      <PublishBar form={form} />
    </div>
  );
}

/** Templates, as chips. The card of three sub-cards this replaces spent a
 *  third of the first screen on three sentences nobody reads twice. */
function Starters({ form }: { form: AgentFormState }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="inline-flex items-center gap-1.5 text-caption text-muted-foreground">
        <SparklesIcon className="size-3.5" aria-hidden />
        Start from
        <HelpTip title="Start from" short="Fills in the name, description and instructions.">
          A starting point, not a category. It fills the name, what it does and the instructions;
          everything else stays as it is and all of it is yours to change.
        </HelpTip>
      </span>
      {STARTERS.map((starter) => (
        <button
          key={starter.name}
          type="button"
          onClick={() => {
            form.setName(starter.name);
            form.setDescription(starter.description);
            form.setInstructions(starter.instructions);
          }}
          className="rounded-full border border-border bg-card px-3 py-1 text-caption transition-colors hover:border-primary/50 hover:bg-primary/5 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          {starter.label}
        </button>
      ))}
    </div>
  );
}

/** Publish, cancel, and everything still in the way. Sticky, so the button
 *  is reachable from anywhere on a long form. */
function PublishBar({ form }: { form: AgentFormState }) {
  const { issueCount, blockers, fieldErrors } = form;
  const flagged = Object.keys(fieldErrors).length;
  // Every blocker, not the first one: a count of two beside one reason reads
  // as a form keeping something back.
  const reasons = [...blockers];
  if (flagged > 0) {
    reasons.push(flagged === 1 ? "one field marked below" : `${flagged} fields marked below`);
  }
  return (
    <div className="sticky bottom-0 -mx-4 flex flex-wrap items-center justify-between gap-3 border-t border-border bg-background/95 px-4 py-3 backdrop-blur sm:-mx-7 sm:px-7 lg:-mx-10 lg:px-10">
      <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
        {issueCount === 0 ? (
          "Ready to publish."
        ) : (
          <>
            <span className="text-status-failed">
              {issueCount === 1 ? "1 thing to fix:" : `${issueCount} things to fix:`}
            </span>{" "}
            {reasons.join(", ")}
          </>
        )}
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <Button
          variant="ghost"
          onClick={() => form.router.push("/agents")}
          disabled={form.submitting}
        >
          Cancel
        </Button>
        <Button onClick={() => void form.handleSubmit()} disabled={!form.canPublish}>
          {form.submitting && <Loader2Icon className="animate-spin" />}
          Publish
        </Button>
      </span>
    </div>
  );
}

/**
 * What comes back from a publish. `created` says whether an agent was made
 * or an existing one was moved to a new version; both are ordinary outcomes
 * and both get the same ways forward.
 */
function PublishedCard({
  result,
  onKeepEditing,
  router,
}: {
  result: CreateAgentResponse;
  onKeepEditing: () => void;
  router: ReturnType<typeof useRouter>;
}) {
  return (
    <Card className="mx-auto w-full max-w-xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CheckCircle2Icon className="size-5 text-status-done" aria-hidden />
          {result.created ? `${result.name} is ready` : `${result.name} is updated`}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Button asChild>
            <Link href={`/chat?agent=${encodeURIComponent(result.agent_id)}`}>
              <MessageSquareIcon /> Chat
            </Link>
          </Button>
          <Button asChild variant="outline">
            <Link href={`/agents/${encodeURIComponent(result.agent_id)}`}>Open</Link>
          </Button>
          <Button variant="outline" onClick={onKeepEditing}>
            <WrenchIcon /> Keep editing
          </Button>
          <Button variant="ghost" onClick={() => router.push("/agents")}>
            All agents
          </Button>
        </div>
        <TechnicalDetails>
          <DetailRow label="Agent id">
            <span className="font-technical">{result.agent_id}</span>
          </DetailRow>
          <DetailRow label="Version">
            <VersionHash hash={result.version_hash} />
          </DetailRow>
        </TechnicalDetails>
      </CardContent>
    </Card>
  );
}
