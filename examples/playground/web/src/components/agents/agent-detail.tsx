"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  CopyIcon,
  HistoryIcon,
  MessageSquareIcon,
  PencilIcon,
  PlugIcon,
  Trash2Icon,
} from "lucide-react";

import { groupConversations } from "@/lib/branches";
import { Copyable } from "@/components/activity/copyable";
import {
  ApiError,
  deleteAgent,
  externalUrl,
  getAgent,
  listAgentVersions,
  listRuns,
} from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel, truncate } from "@/lib/format";
import type { AgentSummary, AgentVersionSummary, RunSummary } from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import {
  DetailRow,
  EmptyState,
  Page,
  PageHeader,
  Section,
  TechnicalDetails,
} from "@/components/ui/page";
import { AgentSummaryTable } from "@/components/agents/agent-summary-table";
import { HashExplainer, VersionHash } from "@/components/agents/version-hash";

/**
 * One published agent.
 *
 * The page this replaces was six sections of prose explaining the runtime to
 * somebody who had already used it. What is here instead is the same
 * information at the density of the form that made it: a table of what is on
 * and what is off, chips for what it can reach, and the actions.
 */
export function AgentDetail({ agentId }: { agentId: string }) {
  const router = useRouter();
  const [agent, setAgent] = useState<AgentSummary | null>(null);
  const [versions, setVersions] = useState<AgentVersionSummary[] | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotFound(false);
    try {
      const [found, history, allRuns] = await Promise.all([
        getAgent(agentId).catch((err) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }),
        listAgentVersions(agentId).catch(() => []),
        listRuns(),
      ]);
      if (found === null) {
        setNotFound(true);
      } else {
        setAgent(found);
        setVersions(history);
        // By agent id, not by version hash. Filtering on the current hash
        // empties this list the moment somebody edits the agent; filtering on
        // every hash in the history makes two agents duplicated from one show
        // each other's conversations. A run dispatched before agents had
        // identities carries no agent id and falls back to the hash rather
        // than disappearing.
        const hashes = new Set(history.map((version) => version.version_hash));
        hashes.add(found.version_hash);
        setRuns(
          allRuns.filter((run) =>
            run.agent_id === "" ? hashes.has(run.version_hash) : run.agent_id === found.agent_id,
          ),
        );
      }
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    // Deferred a tick: `load` sets `loading` synchronously before its first
    // `await`, and running it inline would commit a second state update in
    // the pass this effect mounts in.
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load]);

  async function remove() {
    if (!agent) return;
    try {
      await deleteAgent(agent.agent_id);
      toast.success(`${agent.name} is no longer offered`);
      router.push("/agents");
    } catch (err) {
      toast.error("Could not remove the agent", { description: describeApiError(err) });
    }
  }

  return (
    <>
      <Page>
        <div>
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link href="/agents">
              <ArrowLeftIcon /> Agents
            </Link>
          </Button>
        </div>

        {loading && agent === null && !notFound && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-16 w-80" />
            <Skeleton className="h-64 w-full rounded-xl" />
          </div>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTriangleIcon />
            <AlertTitle>Couldn&apos;t load this agent</AlertTitle>
            <AlertDescription>
              <p>{error}</p>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => void load()}>
                <PlugIcon /> Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {notFound && !loading && (
          <EmptyState
            icon={AlertTriangleIcon}
            title="This agent is not being offered"
            description="It was either never published here or someone has since stopped offering it. Existing conversations are still available."
            action={
              <Button asChild size="sm" variant="outline">
                <Link href="/agents">Back to agents</Link>
              </Button>
            }
          />
        )}

        {agent && (
          <>
            <PageHeader
              title={agent.name}
              description={
                <>
                  {agent.description.trim() !== "" && <>{agent.description} </>}
                  Runs on <span className="font-technical">{agent.model}</span>, edited{" "}
                  {formatDayLabel(agent.updated_at)}.
                </>
              }
              actions={
                <>
                  <Button asChild>
                    <Link href={`/chat?agent=${encodeURIComponent(agent.agent_id)}`}>
                      <MessageSquareIcon /> Chat
                    </Link>
                  </Button>
                  {/* Edit, and it means edit. Publishing still mints an
                      immutable version and this agent moves to it;
                      conversations already underway keep the version they
                      opened on. */}
                  <Button asChild variant="outline">
                    <Link href={`/agents/new?edit=${encodeURIComponent(agent.agent_id)}`}>
                      <PencilIcon /> Edit
                    </Link>
                  </Button>
                  <Button asChild variant="outline">
                    <Link href={`/agents/new?from=${encodeURIComponent(agent.agent_id)}`}>
                      <CopyIcon /> Duplicate
                    </Link>
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Delete ${agent.name}`}
                    onClick={() => setConfirmDelete(true)}
                  >
                    <Trash2Icon />
                  </Button>
                </>
              }
            />

            <Section title="Instructions">
              {agent.instructions.trim() === "" ? (
                <p className="text-body text-muted-foreground">
                  None were written, so it answers with no guidance beyond the conversation itself.
                </p>
              ) : (
                <p className="rounded-xl bg-surface/60 p-4 text-body whitespace-pre-wrap">
                  {agent.instructions}
                </p>
              )}
            </Section>

            <Section
              title="This version"
              description="What it was published with. Editing publishes another; nothing here changes under a conversation already running."
            >
              <AgentSummaryTable agent={agent} />
            </Section>

            <Section title="Conversations">
              <RunList runs={runs} agentId={agent.agent_id} />
            </Section>

            <Section
              title="Versions"
              description="A conversation runs the version it opened on, whatever the agent points at now."
            >
              <VersionList versions={versions} />
            </Section>

            <TechnicalDetails>
              <DetailRow label="Agent id">
                <span className="font-technical">{agent.agent_id}</span>
              </DetailRow>
              <DetailRow label="Current version">
                <VersionHash hash={agent.version_hash} />
              </DetailRow>
              <DetailRow label="Published">{agent.published_at}</DetailRow>
              <DetailRow label="Created">{agent.created_at}</DetailRow>
              {/* This agent speaks A2A, so another agent can be pointed at it.
                  Whoever runs that one still needs a credential from you. */}
              <DetailRow label="A2A card">
                <Copyable value={externalUrl(`/a2a/v1/agents/${agent.agent_id}/agent-card.json`)} />
              </DetailRow>
              <HashExplainer />
            </TechnicalDetails>
          </>
        )}
      </Page>

      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Stop offering {agent?.name}?</DialogTitle>
            <DialogDescription>
              It disappears from the list with every version it has had, and nobody can start a new
              conversation with it. Conversations it already had stay readable.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              Keep it
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmDelete(false);
                void remove();
              }}
            >
              Stop offering it
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/** What this agent has been, newest first: the audit trail that makes a
 *  mutable pointer cost nothing. */
function VersionList({ versions }: { versions: AgentVersionSummary[] | null }) {
  if (versions === null || versions.length === 0) {
    return (
      <p className="text-body text-muted-foreground">
        One version, published when the agent was created.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1.5">
      {versions.map((version) => (
        <li
          key={version.version_hash}
          className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-lg border border-border px-3 py-2"
        >
          <span className="flex min-w-0 items-center gap-2">
            <HistoryIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <VersionHash hash={version.version_hash} />
          </span>
          <span className="flex items-center gap-2 text-caption text-muted-foreground">
            <span className="font-technical">{version.model}</span>
            <span>{formatDayLabel(version.published_at)}</span>
            {version.current && (
              <span className="rounded-md bg-primary/10 px-1.5 py-0.5 text-micro font-medium text-primary">
                Current
              </span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RunList({ runs, agentId }: { runs: RunSummary[] | null; agentId: string }) {
  if (runs === null) {
    return (
      <div className="flex flex-col gap-1.5">
        <Skeleton className="h-14 w-full rounded-lg" />
        <Skeleton className="h-14 w-full rounded-lg" />
      </div>
    );
  }
  if (runs.length === 0) {
    return (
      <EmptyState
        icon={MessageSquareIcon}
        title="Nobody has talked to this agent yet"
        description="Every conversation it has shows up here, with what it did and what it cost."
        action={
          <Button asChild size="sm">
            <Link href={`/chat?agent=${encodeURIComponent(agentId)}`}>
              <MessageSquareIcon /> Start one
            </Link>
          </Button>
        }
      />
    );
  }
  // Folded into conversations, not listed as Runs: a second message continues
  // the first Run rather than extending it, and listing them raw put the same
  // conversation on the page three times.
  const conversations = groupConversations(runs);
  return (
    <ul className="flex flex-col gap-1.5">
      {conversations.map((conversation) => (
        <li key={conversation.key}>
          <Link
            href={`/activity/${encodeURIComponent(conversation.latest.run_id)}`}
            className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2.5 transition-colors hover:bg-surface/60"
          >
            <span className="flex min-w-0 flex-col gap-0.5">
              {/* The branch's opening ask, which is what a person recognises
                  it by, never its latest turn. */}
              <span className="truncate text-body">
                {conversation.head.message.trim() === ""
                  ? "No opening message"
                  : truncate(conversation.head.message.trim(), 90)}
              </span>
              <span className="text-caption text-muted-foreground">
                {formatDayLabel(conversation.startedAt)}
                {conversation.runs.length > 1 ? ` · ${conversation.runs.length} messages` : ""}
              </span>
            </span>
            {/* The newest turn's state is the conversation's state. */}
            <StatusPill state={toLifecycle(conversation.latest.state)} size="sm" />
          </Link>
        </li>
      ))}
    </ul>
  );
}
