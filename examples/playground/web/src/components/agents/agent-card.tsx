"use client";

import Link from "next/link";
import { CopyIcon, PencilIcon, MessageSquareIcon, MoreHorizontalIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { DetailRow, TechnicalDetails } from "@/components/ui/page";
import { answerStyleCopy } from "@/components/agents/answer-style";
import { CapabilityChips } from "@/components/agents/chips";
import { HashExplainer, VersionHash } from "@/components/agents/version-hash";
import { formatDayLabel, truncate } from "@/lib/format";
import type { AgentSummary } from "@/lib/types";

/**
 * One agent, as a card rather than a row.
 *
 * The list this replaces was a table whose columns were Name, Model, Tools,
 * MCP servers, Published, Version and Actions: six facts about the runtime
 * and one about the agent. What a person picking someone to talk to actually
 * needs is the name, what it is for, what it can reach, and a way to start
 * talking, so those are the card and the rest is behind the disclosure.
 */
export function AgentCard({
  agent,
  onDelete,
}: {
  agent: AgentSummary;
  onDelete: (agent: AgentSummary) => void;
}) {
  const href = `/agents/${encodeURIComponent(agent.agent_id)}`;
  const instructions = agent.instructions.trim();

  return (
    <article className="flex flex-col gap-3 rounded-xl bg-card p-4 ring-1 ring-foreground/10 transition-shadow hover:ring-foreground/20">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <Link href={href} className="truncate text-base font-semibold hover:underline">
            {agent.name}
          </Link>
          <p className="text-caption text-muted-foreground">
            {/* When the agent was last edited, not when its current version
                was first published: a version republished after a round trip
                through an earlier configuration keeps its original timestamp,
                which is right for a version and wrong for an agent. */}
            Edited {formatDayLabel(agent.updated_at)}
            {agent.version_count > 1 && ` \u00b7 ${agent.version_count} versions`}
            {/* Only the non-default is worth a card's line. Saying "detailed"
                on every other card would price the one fact this is here to
                carry down to nothing. */}
            {agent.answer_style === "concise" &&
              ` \u00b7 ${answerStyleCopy(agent.answer_style).title} answers`}
          </p>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label={`More actions for ${agent.name}`}>
              <MoreHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem asChild>
              <Link href={href}>Open</Link>
            </DropdownMenuItem>
            {/* Edit is an edit now. It publishes a new version -- a version
                is still immutable and still content-hashed -- and moves this
                agent to it. Duplicate is the separate thing it always was:
                a second agent, prefilled from this one. */}
            <DropdownMenuItem asChild>
              <Link href={`/agents/new?edit=${encodeURIComponent(agent.agent_id)}`}>
                <PencilIcon /> Edit
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <Link href={`/agents/new?from=${encodeURIComponent(agent.agent_id)}`}>
                <CopyIcon /> Duplicate
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem variant="destructive" onSelect={() => onDelete(agent)}>
              <Trash2Icon /> Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {instructions === "" ? (
        <p className="text-body text-muted-foreground">No instructions were written for this one.</p>
      ) : (
        <p className="text-body text-muted-foreground">{truncate(instructions, 220)}</p>
      )}

      <CapabilityChips tools={agent.tools} connections={agent.mcp_servers} limit={5} />

      <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
        <Button asChild size="sm">
          <Link href={`/chat?agent=${encodeURIComponent(agent.agent_id)}`}>
            <MessageSquareIcon /> Chat
          </Link>
        </Button>
        <Button asChild size="sm" variant="ghost">
          <Link href={href}>Details</Link>
        </Button>
      </div>

      <TechnicalDetails>
        <DetailRow label="Model">
          <span className="font-technical">{agent.model}</span>
        </DetailRow>
        <DetailRow label="Version">
          <VersionHash hash={agent.version_hash} />
        </DetailRow>
        <HashExplainer />
      </TechnicalDetails>
    </article>
  );
}
