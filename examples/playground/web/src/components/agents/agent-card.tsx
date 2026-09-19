"use client";

import Link from "next/link";
import {
  CopyIcon,
  MessageSquareIcon,
  MoreHorizontalIcon,
  PencilIcon,
  Trash2Icon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CapabilityChips } from "@/components/agents/chips";
import { CatalogueChip } from "@/components/catalogue/catalogue-bits";
import { truncate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AgentSummary } from "@/lib/types";

/** The abilities worth a chip on a card: the ones that change what a
 *  conversation with this agent feels like. Off is not listed, because a card
 *  is a comparison and every agent would carry the same six words. */
function abilities(agent: AgentSummary): string[] {
  const on: string[] = [];
  if (agent.may_ask_questions) on.push("asks");
  if (agent.tasks_enabled) on.push("plans");
  if (agent.components_enabled) on.push("components");
  if (agent.subagents_enabled || agent.subagents.length > 0) on.push("helpers");
  if (agent.code_execution?.enabled) on.push("code");
  if (agent.compaction !== null) on.push("summarises");
  if (agent.answer_style === "concise") on.push("concise");
  return on;
}

/**
 * One agent, at the density of a list somebody is scanning: the name, one
 * line about what it is for, the model, and small chips for what is on.
 */
export function AgentCard({
  agent,
  fromCatalogue = false,
  note = null,
  onDelete,
}: {
  agent: AgentSummary;
  /** Came with the console rather than being written here. */
  fromCatalogue?: boolean;
  /** One line above the description, for an agent whose job needs saying. */
  note?: string | null;
  onDelete: (agent: AgentSummary) => void;
}) {
  const href = `/agents/${encodeURIComponent(agent.agent_id)}`;
  const line =
    agent.description.trim() !== ""
      ? agent.description.trim()
      : agent.instructions.trim() !== ""
        ? truncate(agent.instructions.trim(), 120)
        : "No description was written for this one.";

  return (
    <article className="flex flex-col gap-3 rounded-xl bg-card p-4 ring-1 ring-foreground/10 transition-shadow hover:ring-foreground/20">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link href={href} className="truncate text-base font-semibold hover:underline">
              {agent.name}
            </Link>
            {fromCatalogue && <CatalogueChip />}
          </span>
          {note !== null && <p className="text-caption text-foreground">{note}</p>}
          <p className="line-clamp-2 text-caption text-muted-foreground">{line}</p>
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
            {/* Edit publishes a new version and moves this agent to it.
                Duplicate is the separate thing it always was: a second agent,
                prefilled from this one. */}
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

      <div className="flex flex-wrap items-center gap-1.5">
        <span className="rounded-full bg-surface px-2.5 py-1 font-technical text-micro text-surface-foreground">
          {agent.model}
        </span>
        {abilities(agent).map((ability) => (
          <span
            key={ability}
            className={cn(
              "rounded-full border border-border px-2 py-0.5 text-micro text-muted-foreground",
            )}
          >
            {ability}
          </span>
        ))}
      </div>

      <CapabilityChips tools={agent.tools} connections={agent.mcp_servers} limit={4} />

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
    </article>
  );
}
