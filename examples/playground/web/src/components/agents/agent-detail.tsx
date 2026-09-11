"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  CopyIcon,
  HistoryIcon,
  MessageSquareIcon,
  PencilIcon,
  PlugIcon,
} from "lucide-react";

import { groupConversations } from "@/lib/branches";
import { Copyable } from "@/components/activity/copyable";
import { getAgent, listAgentVersions, listRuns, listTools } from "@/lib/api";
import { ApiError, externalUrl } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { formatDayLabel, truncate } from "@/lib/format";
import type {
  AgentSummary,
  AgentVersionSummary,
  McpServerPreset,
  RunSummary,
  SkillIn,
  ToolInfo,
} from "@/lib/types";
import { useSettings } from "@/components/settings/use-settings";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill, toLifecycle } from "@/components/ui/status";
import { DetailRow, EmptyState, Page, PageHeader, Section, TechnicalDetails } from "@/components/ui/page";
import { answerStyleCopy } from "@/components/agents/answer-style";
import { DEFAULT_LIMITS, LIMIT_FIELDS } from "@/components/agents/limits";
import { selectorLabel, toolClassTitle as classTitle } from "@/components/agents/policy";
import { classifyTool, toolClassTitle } from "@/components/agents/policy";
import { HashExplainer, VersionHash } from "@/components/agents/version-hash";

export function AgentDetail({ agentId }: { agentId: string }) {
  const [agent, setAgent] = useState<AgentSummary | null>(null);
  const [versions, setVersions] = useState<AgentVersionSummary[] | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [tools, setTools] = useState<ToolInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { settings } = useSettings();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotFound(false);
    try {
      const [found, history, allRuns, toolList] = await Promise.all([
        getAgent(agentId).catch((err) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }),
        listAgentVersions(agentId).catch(() => []),
        listRuns(),
        listTools(),
      ]);
      if (found === null) {
        setNotFound(true);
      } else {
        setAgent(found);
        setVersions(history);
        // By agent id, not by version hash.
        //
        // Two things go wrong with the hash. Filtering on the *current* hash
        // empties this list the moment somebody edits the agent, which is the
        // reading of "the agent" this whole change exists to fix. Filtering on
        // every hash in the history fixes that and introduces a worse one: a
        // version is content, so an agent duplicated and published unchanged
        // shares its hash, and both agents would then show each other's
        // conversations.
        //
        // A run dispatched before agents had identities carries no agent id
        // and cannot be attributed better than by hash, so it falls back to
        // exactly that rather than disappearing.
        const hashes = new Set(history.map((version) => version.version_hash));
        hashes.add(found.version_hash);
        setRuns(
          allRuns.filter((run) =>
            run.agent_id === "" ? hashes.has(run.version_hash) : run.agent_id === found.agent_id
          )
        );
      }
      setTools(toolList);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    // Deferred a tick rather than called directly: `load` sets `loading`
    // synchronously before its first `await`, and running that inline here
    // would mean this effect commits a second state update in the same pass
    // it mounts in. Matches `useAgents`' own fix for the identical shape.
    const id = setTimeout(() => void load(), 0);
    return () => clearTimeout(id);
  }, [load]);

  const toolsByName = new Map((tools ?? []).map((tool) => [tool.name, tool]));
  const connectionsByName = new Map(
    (settings?.mcp_servers ?? []).map((preset) => [preset.name, preset])
  );

  return (
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
            <Skeleton className="h-40 w-full rounded-xl" />
            <Skeleton className="h-40 w-full rounded-xl" />
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
                  Edited {formatDayLabel(agent.updated_at)}. Runs on{" "}
                  <span className="font-technical">{agent.model}</span>.
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
                      immutable version -- that is what lets a Run reload its
                      spec after a crash and resume the conversation it was
                      actually having -- and this agent moves to the new one.
                      Conversations already underway keep the version they
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
                </>
              }
            />

            <Section title="What it does">
              {agent.description.trim() !== "" && (
                <p className="text-body text-muted-foreground">{agent.description}</p>
              )}
              {agent.instructions.trim() === "" ? (
                <p className="text-body text-muted-foreground">
                  Nobody wrote instructions for this one, so it answers with no guidance beyond the
                  conversation itself.
                </p>
              ) : (
                <p className="rounded-xl bg-surface/60 p-4 text-body whitespace-pre-wrap">
                  {agent.instructions}
                </p>
              )}
              {/* Read from the published agent, not guessed at: the answer
                  style is part of what was published and the catalogue
                  reports it back, unlike the limits below. */}
              <ModelOptionsSummary agent={agent} />
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-caption font-medium text-muted-foreground">
                  Answer style
                </span>
                <span className="text-caption">
                  {answerStyleCopy(agent.answer_style).title}.{" "}
                  <span className="text-muted-foreground">
                    {answerStyleCopy(agent.answer_style).summary}
                  </span>
                </span>
              </div>
            </Section>

            <Section
              title="What it can use"
              description="What this version was published with. Editing the agent publishes another; nothing here changes under a conversation already running."
            >
              {agent.tools.length === 0 &&
              agent.http_tools.length === 0 &&
              agent.subagents.length === 0 &&
              agent.spawn === null &&
              agent.mcp_servers.length === 0 &&
              agent.a2a_peers.length === 0 &&
              agent.skills.length === 0 ? (
                <p className="text-body text-muted-foreground">
                  Nothing. It answers from the conversation alone, which is the safest an agent
                  gets.
                </p>
              ) : (
                <>
                  <ToolList tools={agent.tools} known={toolsByName} />
                  <HttpToolList tools={agent.http_tools} />
                  <SubagentList agent={agent} />
                  <SkillList skills={agent.skills} />
                  <ConnectionList names={agent.mcp_servers} today={connectionsByName} />
                  <PeerList names={agent.a2a_peers} />
                </>
              )}
            </Section>

            <Section
              title="Limits and approvals"
              description="The spending limits and the rule for which actions pause for a person, as this version was published."
            >
              <LimitsSummary agent={agent} />
            </Section>

            <Section
              title="Recent conversations"
              description="Everything anyone has said to this agent."
            >
              <RunList runs={runs} agentId={agent.agent_id} />
            </Section>

            <Section
              title="Reachable by other agents"
              description="This agent speaks A2A, so another agent can be pointed at it."
            >
              <div className="flex flex-col gap-2">
                <p className="text-body text-muted-foreground">
                  Hand this address to whoever runs the other agent. They will need a credential
                  from you as well: the card names the scheme it expects, and nothing here answers
                  without one.
                </p>
                <Copyable
                  value={externalUrl(`/a2a/v1/agents/${agent.agent_id}/agent-card.json`)}
                />
              </div>
            </Section>

            <Section
              title="History"
              description="Every version this agent has been. A conversation runs the version it opened on, whatever the agent points at now."
            >
              <VersionList versions={versions} />
            </Section>

            <TechnicalDetails>
              <DetailRow label="Model">
                <span className="font-technical">{agent.model}</span>
              </DetailRow>
              <DetailRow label="Agent id">
                <span className="font-technical">{agent.agent_id}</span>
              </DetailRow>
              <DetailRow label="Current version">
                <VersionHash hash={agent.version_hash} />
              </DetailRow>
              <DetailRow label="Published">{agent.published_at}</DetailRow>
              <DetailRow label="Created">{agent.created_at}</DetailRow>
              <HashExplainer />
            </TechnicalDetails>
          </>
        )}
    </Page>
  );
}

/** The agent's own tools, cross-checked against what this installation still
 *  offers. A name with nothing behind it is said plainly rather than dropped:
 *  an agent granted a tool that no longer exists is a fact worth knowing. */
function ToolList({ tools, known }: { tools: string[]; known: Map<string, ToolInfo> }) {
  if (tools.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">Tools</h3>
      <ul className="flex flex-col gap-2">
        {tools.map((name) => {
          const tool = known.get(name);
          return (
            <li key={name} className="rounded-lg border border-border px-3 py-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="font-technical text-body font-medium">{name}</span>
                {tool && (
                  <span className="text-caption text-muted-foreground">
                    {toolClassTitle(classifyTool(tool.annotations)).toLowerCase()}
                  </span>
                )}
              </div>
              <p className="text-caption text-muted-foreground">
                {tool ? tool.description : "This installation no longer offers a tool by this name."}
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * The agent's skills, description first.
 *
 * Descriptions are shown outright and bodies are behind a disclosure, which
 * mirrors what the agent itself sees: the description is in every prompt, the
 * body arrives only when the model asks for it. A page that dumped every body
 * would misrepresent the cost of the feature to whoever is reading it to
 * decide whether to use it.
 *
 * Unlike tools and connections, these are shown in full and without a caveat.
 * A skill is not a name pointing at something configured elsewhere that may
 * have changed; the text itself is inside the Version, so this is exactly what
 * the agent has.
 */
function SkillList({ skills }: { skills: SkillIn[] }) {
  if (skills.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">Skills</h3>
      <ul className="flex flex-col gap-2">
        {skills.map((skill) => (
          <li key={skill.name} className="rounded-lg border border-border px-3 py-2">
            <p className="font-technical text-body font-medium">{skill.name}</p>
            <p className="text-caption text-muted-foreground">{skill.description}</p>
            <details className="mt-1.5">
              <summary className="cursor-pointer text-caption text-muted-foreground hover:text-foreground">
                Instructions ({skill.body.length.toLocaleString()} characters, loaded on demand)
              </summary>
              <pre className="mt-1.5 max-h-64 overflow-auto rounded-md bg-surface p-2 font-technical text-caption whitespace-pre-wrap text-surface-foreground">
                {skill.body}
              </pre>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Connections, by name and nothing more.
 *
 * A published agent carries its own copy of every connection's address,
 * credential and transport, and the catalogue reports only the names. The
 * page this replaces rendered the current Connections entry's URL and OAuth
 * settings under the agent's heading, so an agent published against one
 * address appeared to point at whatever that name pointed at today. What is
 * shown here is only ever labelled as today's connection, never as the
 * agent's.
 */
function ConnectionList({
  names,
  today,
}: {
  names: string[];
  today: Map<string, McpServerPreset>;
}) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">Connections</h3>
      <ul className="flex flex-col gap-2">
        {names.map((name) => {
          const current = today.get(name);
          return (
            <li key={name} className="rounded-lg border border-border px-3 py-2">
              <p className="text-body font-medium">{name}</p>
              <p className="text-caption text-muted-foreground">
                {current
                  ? "A connection with this name is set up here today. The agent uses its own copy of the settings from when it was published, which this page cannot show."
                  : "Nothing with this name is set up here today. The agent still carries its own copy of the settings it was published with."}
              </p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * What this agent has been, newest first.
 *
 * The audit trail people assume immutability is *for*, and the reason a
 * mutable pointer costs nothing: editing freely and knowing exactly what ran
 * are only in tension while an agent and a version are the same object.
 */
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

/**
 * Peers, by name and nothing else.
 *
 * Same rule as connections: a published agent carries its own copy of every
 * peer's address and credential name, and the catalogue reports neither. What
 * a peer can actually do is not shown because this page does not know it. That
 * is read from the peer's own agent card while a conversation runs, so any
 * list here would be a guess about somebody else's system.
 */
function PeerList({ names }: { names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">Other agents</h3>
      <ul className="flex flex-col gap-2">
        {names.map((name) => (
          <li key={name} className="rounded-lg border border-border px-3 py-2">
            <p className="text-body font-medium">{name}</p>
            <p className="text-caption text-muted-foreground">
              Reached over A2A. Its skills are read from its own agent card as a conversation
              runs, so they are not fixed into this agent and are not listed here.
            </p>
          </li>
        ))}
      </ul>
    </div>
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
  // Folded into conversations, not listed as Runs. A second message continues
  // the first Run rather than extending it, so a three-message
  // exchange is three Runs; listing them raw put the same conversation on the
  // page three times, disagreeing with Conversations and Chat, which both fold.
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
                  it by, never its latest turn. Forked from a conversation this
                  agent had no part in, that ask is where its own involvement
                  begins, and the earlier turns are not this agent's to list. */}
              <span className="truncate text-body">
                {conversation.head.message.trim() === ""
                  ? "No opening message"
                  : truncate(conversation.head.message.trim(), 90)}
              </span>
              <span className="text-caption text-muted-foreground">
                {formatDayLabel(conversation.startedAt)}
                {conversation.runs.length > 1
                  ? ` · ${conversation.runs.length} messages`
                  : ""}
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


/** The rest of the model reference, only when any of it was set. */
function ModelOptionsSummary({ agent }: { agent: AgentSummary }) {
  const options = agent.model_options;
  if (options === null) return null;
  const rows: [string, string][] = [];
  if (options.top_p !== null) rows.push(["Top p", String(options.top_p)]);
  if (options.max_output_tokens !== null)
    rows.push(["Longest reply", `${options.max_output_tokens.toLocaleString()} tokens`]);
  if (options.reasoning_effort !== null) rows.push(["Reasoning effort", options.reasoning_effort]);
  if (options.fallbacks.length > 0) rows.push(["Fallbacks", options.fallbacks.join(", then ")]);
  if (rows.length === 0) return null;
  return (
    <dl className="grid gap-x-4 gap-y-1 text-caption sm:grid-cols-[auto_1fr]">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="font-medium text-muted-foreground">{label}</dt>
          <dd className="font-technical">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function HttpToolList({ tools }: { tools: AgentSummary["http_tools"] }) {
  if (tools.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">HTTP tools</h3>
      <ul className="flex flex-col gap-2">
        {tools.map((tool) => (
          <li key={tool.name} className="rounded-lg border border-border px-3 py-2">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="font-technical text-body font-medium">{tool.name}</span>
              <span className="font-technical text-caption text-muted-foreground">
                {tool.method} {tool.url}
              </span>
            </div>
            <p className="text-caption text-muted-foreground">{tool.description}</p>
            {tool.credential && (
              <p className="text-micro text-muted-foreground">
                Sends the secret <span className="font-technical">{tool.credential}</span> as a
                bearer token.
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Both kinds of helper: the named roster, and the envelope for helpers the
 *  model writes itself. */
function SubagentList({ agent }: { agent: AgentSummary }) {
  if (agent.subagents.length === 0 && agent.spawn === null) return null;
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-body font-medium">Helpers</h3>
      {agent.subagents.length > 0 && (
        <ul className="flex flex-col gap-2">
          {agent.subagents.map((ref) => (
            <li key={ref.name} className="rounded-lg border border-border px-3 py-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="font-technical text-body font-medium">{ref.name}</span>
                {ref.agent_id ? (
                  <Link
                    href={`/agents/${encodeURIComponent(ref.agent_id)}`}
                    className="text-caption text-primary underline underline-offset-2"
                  >
                    open the agent it was copied from
                  </Link>
                ) : (
                  <span className="text-caption text-muted-foreground">
                    copied from an agent no longer offered
                  </span>
                )}
              </div>
              <p className="text-caption text-muted-foreground">{ref.description}</p>
              <p className="font-technical text-micro text-muted-foreground">
                pinned at {ref.version_hash.slice(0, 12)}
              </p>
            </li>
          ))}
        </ul>
      )}
      {agent.spawn !== null && (
        <p className="rounded-lg bg-surface/60 px-3 py-2 text-caption text-muted-foreground">
          May write its own helpers: up to {agent.spawn.max_alive} alive at once,{" "}
          {agent.spawn.max_depth} deep, out of{" "}
          {agent.spawn.tools.length === 0 ? "all of its own tools" : agent.spawn.tools.join(", ")}
          , on{" "}
          {agent.spawn.models.length === 0 ? "its own model" : agent.spawn.models.join(" or ")}.
          {agent.spawn.may_message ? " It may message one while it runs." : " It may not message one."}
        </p>
      )}
    </div>
  );
}

/** Every limit as published, the ones that differ from the defaults first,
 *  and the approval rule this agent's conversations are admitted under. */
function LimitsSummary({ agent }: { agent: AgentSummary }) {
  const known = Object.keys(agent.limits).length > 0;
  const changed = LIMIT_FIELDS.filter(
    (field) => known && agent.limits[field.key] !== DEFAULT_LIMITS[field.key]
  );
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <span className="text-caption font-medium text-muted-foreground">Approvals</span>
        <span className="text-body">
          {agent.approval_selectors === null
            ? "Follows this installation's rule."
            : agent.approval_selectors.length === 0
              ? "Nothing pauses for a person."
              : `Pauses for a person before any ${agent.approval_selectors
                  .map((s) => classTitle(s.replace(/^@/, "") as Parameters<typeof classTitle>[0]).toLowerCase())
                  .join(" or ")} action.`}
        </span>
      </div>
      {agent.suspension !== null && (
        <div className="flex flex-col gap-1">
          <span className="text-caption font-medium text-muted-foreground">Waits at most</span>
          <span className="text-body">
            {Math.round(agent.suspension.approval_expires_seconds / 3600)}h for an approval,{" "}
            {Math.round(agent.suspension.question_expires_seconds / 3600)}h for an answer,{" "}
            {Math.round(agent.suspension.external_expires_seconds / 3600)}h on something outside,{" "}
            {Math.round(agent.suspension.children_expires_seconds / 60)}m on helpers.
          </span>
        </div>
      )}
      {!known ? (
        <p className="text-body text-muted-foreground">
          Published before limits were reported back. Edit the agent to see and set them.
        </p>
      ) : changed.length === 0 ? (
        <p className="text-body text-muted-foreground">Every limit is at its default.</p>
      ) : (
        <ul className="grid gap-x-4 gap-y-1 text-caption sm:grid-cols-2">
          {changed.map((field) => (
            <li key={field.key} className="flex items-baseline justify-between gap-2">
              <span className="text-muted-foreground">{field.label}</span>
              <span className="tabular font-technical">
                {agent.limits[field.key].toLocaleString()}{" "}
                <span className="text-muted-foreground">
                  (default {DEFAULT_LIMITS[field.key].toLocaleString()})
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="text-micro text-muted-foreground">
        {selectorLabel("write")} and {selectorLabel("destructive")} are the selectors the
        installation defaults to; an unannotated tool counts as a write.
      </p>
    </div>
  );
}
