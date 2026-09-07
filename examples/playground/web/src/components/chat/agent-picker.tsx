"use client";

import { useState } from "react";
import { BotIcon, CheckIcon, ChevronsUpDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { truncate } from "@/lib/format";
import type { AgentSummary } from "@/lib/types";

interface AgentPickerProps {
  agents: AgentSummary[] | null;
  loading: boolean;
  error: string | null;
  selected: AgentSummary | null;
  onSelect: (agentId: string) => void;
  /** A conversation is with one agent for its whole life. Once it has
   * started, this shows which one rather than offering a switch that would
   * silently change who is answering mid-thread. */
  locked: boolean;
}

/**
 * Which agent you are talking to.
 *
 * Rows are named and described, not identified: a Version hash tells a person
 * nothing and two agents with the same name are told apart by what they say
 * they do. The hash is on the agent's own page for whoever needs it.
 */
export function AgentPicker({
  agents,
  loading,
  error,
  selected,
  onSelect,
  locked,
}: AgentPickerProps) {
  const [open, setOpen] = useState(false);
  const available = agents ?? [];

  if (locked) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex max-w-64 items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-caption text-muted-foreground">
            <BotIcon className="size-3.5 shrink-0" aria-hidden />
            <span className="truncate">{selected?.name ?? "This conversation's agent"}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-64">
          This conversation is with {selected?.name ?? "one agent"}. Start a new chat to use a
          different one.
        </TooltipContent>
      </Tooltip>
    );
  }

  const label = loading
    ? "Loading agents"
    : error !== null
      ? "Agents unavailable"
      : (selected?.name ?? (available.length > 0 ? "Choose an agent" : "No agents yet"));

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={loading || error !== null || available.length === 0}
          className="max-w-64 justify-between gap-2 rounded-full pl-2.5"
        >
          <span className="flex min-w-0 items-center gap-1.5">
            <BotIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <span className="truncate">{label}</span>
          </span>
          <ChevronsUpDownIcon className="size-3.5 shrink-0 opacity-50" aria-hidden />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-0" align="start">
        <Command loop>
          <CommandInput placeholder="Search agents" />
          <CommandList>
            <CommandEmpty>No agent matches.</CommandEmpty>
            <CommandGroup>
              {available.map((agent) => {
                const active = agent.agent_id === selected?.agent_id;
                return (
                  <CommandItem
                    key={agent.agent_id}
                    value={`${agent.name} ${agent.instructions}`}
                    onSelect={() => {
                      onSelect(agent.agent_id);
                      setOpen(false);
                    }}
                    className="flex-col items-start gap-0.5 py-2"
                  >
                    <span className="flex w-full items-center gap-2">
                      <span className={cn("truncate font-medium", active && "text-foreground")}>
                        {agent.name}
                      </span>
                      {active && <CheckIcon className="ml-auto size-3.5 shrink-0" aria-hidden />}
                    </span>
                    <span className="line-clamp-2 w-full text-caption text-muted-foreground">
                      {agent.instructions.trim().length > 0
                        ? truncate(agent.instructions.trim().replace(/\s+/g, " "), 110)
                        : "No description."}
                    </span>
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
