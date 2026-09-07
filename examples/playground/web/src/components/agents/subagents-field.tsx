"use client";

import Link from "next/link";
import { PlusIcon, Trash2Icon } from "lucide-react";

import { FieldError } from "@/components/settings/validation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { AgentSummary, SpawnIn, SubagentRefIn } from "@/lib/types";

export const DEFAULT_SPAWN: SpawnIn = {
  tools: [],
  models: [],
  max_depth: 2,
  max_alive: 3,
  may_message: true,
};

/**
 * The delegation roster: agents an author names, embedded whole into this
 * one. The parent is offered `delegate`, which calls the child and waits.
 *
 * Each row copies the named agent at its *current* version. Editing the child
 * later does not move this parent, and the detail page says which version
 * was pinned. The description is what the parent's model reads to decide
 * whether to hand work over, and the library refuses one under 20 characters
 * because vague descriptions are the usual cause of bad routing.
 */
export function SubagentRosterField({
  value,
  onChange,
  agents,
  selfId,
  fieldErrors,
}: {
  value: SubagentRefIn[];
  onChange: (next: SubagentRefIn[]) => void;
  agents: AgentSummary[];
  /** The agent being edited, which may not delegate to itself. */
  selfId: string | null;
  fieldErrors: Record<string, string>;
}) {
  const candidates = agents.filter((agent) => agent.agent_id !== selfId);

  function update(index: number, patch: Partial<SubagentRefIn>) {
    onChange(value.map((ref, i) => (i === index ? { ...ref, ...patch } : ref)));
  }

  return (
    <div className="flex flex-col gap-3">
      {value.length === 0 && (
        <p className="text-caption text-muted-foreground">
          No named helpers. Add one and this agent can hand a task to it and wait for the answer.
        </p>
      )}
      {value.map((ref, index) => {
        const error = fieldErrors[`subagents.${ref.name}.description`] ?? fieldErrors[`subagents.${index}`];
        return (
          <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
            <div className="flex items-start gap-2">
              <div className="grid min-w-0 flex-1 gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`sub-agent-${index}`}>Agent</Label>
                  <Select
                    value={ref.agent_id || undefined}
                    onValueChange={(agent_id) => {
                      const picked = candidates.find((a) => a.agent_id === agent_id);
                      update(index, {
                        agent_id,
                        name: ref.name || picked?.name || "",
                        description: ref.description || picked?.description || "",
                      });
                    }}
                  >
                    <SelectTrigger id={`sub-agent-${index}`}>
                      <SelectValue placeholder="Pick an agent" />
                    </SelectTrigger>
                    <SelectContent>
                      {candidates.map((agent) => (
                        <SelectItem key={agent.agent_id} value={agent.agent_id}>
                          {agent.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`sub-name-${index}`}>Called, inside this agent</Label>
                  <Input
                    id={`sub-name-${index}`}
                    value={ref.name}
                    spellCheck={false}
                    placeholder="researcher"
                    onChange={(e) => update(index, { name: e.target.value })}
                  />
                </div>
              </div>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                className="mt-6 shrink-0"
                aria-label={`Remove ${ref.name || "this helper"}`}
                onClick={() => onChange(value.filter((_, i) => i !== index))}
              >
                <Trash2Icon />
              </Button>
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between gap-2">
                <Label htmlFor={`sub-desc-${index}`}>When to hand work to it</Label>
                <span className="font-technical text-micro text-muted-foreground">
                  {ref.description.length} characters, at least 20
                </span>
              </div>
              <Input
                id={`sub-desc-${index}`}
                value={ref.description}
                placeholder="Deep research over the internal wiki; give it one question at a time."
                onChange={(e) => update(index, { description: e.target.value })}
              />
              <FieldError message={error} />
            </div>
          </div>
        );
      })}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={candidates.length === 0}
          onClick={() => onChange([...value, { name: "", description: "", agent_id: "" }])}
        >
          <PlusIcon /> Add a named helper
        </Button>
        {candidates.length === 0 && (
          <span className="text-caption text-muted-foreground">
            Publish another agent first; it will appear here. See{" "}
            <Link href="/agents" className="underline underline-offset-2">
              Agents
            </Link>
            .
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The envelope for helpers the model writes itself. A ceiling and never a
 * roster: the agent may compose a child out of at most these tools, on at
 * most these models, this deep and this many at once.
 */
export function SpawnEnvelopeFields({
  value,
  onChange,
  tools,
  models,
}: {
  value: SpawnIn;
  onChange: (next: SpawnIn) => void;
  /** The parent's own tool names, the most a child may be given. */
  tools: string[];
  models: string[];
}) {
  return (
    <div className="flex flex-col gap-4 rounded-lg border border-border p-3">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="spawn-depth">How deep helpers may go</Label>
          <Input
            id="spawn-depth"
            type="number"
            className="tabular"
            min={1}
            max={16}
            value={value.max_depth}
            onChange={(e) => onChange({ ...value, max_depth: Number(e.target.value) || 1 })}
          />
          <p className="text-micro text-muted-foreground">
            1 means a helper may not start helpers of its own.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="spawn-alive">Alive at once</Label>
          <Input
            id="spawn-alive"
            type="number"
            className="tabular"
            min={1}
            max={32}
            value={value.max_alive}
            onChange={(e) => onChange({ ...value, max_alive: Number(e.target.value) || 1 })}
          />
          <p className="text-micro text-muted-foreground">
            Each one is a separate conversation with its own bill.
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label>Tools a helper may be given</Label>
        {tools.length === 0 ? (
          <p className="text-caption text-muted-foreground">
            This agent holds no tools, so neither can a helper it writes.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {tools.map((name) => {
              const picked = value.tools.length === 0 || value.tools.includes(name);
              return (
                <button
                  key={name}
                  type="button"
                  aria-pressed={picked}
                  onClick={() => {
                    const current = value.tools.length === 0 ? tools : value.tools;
                    const next = picked
                      ? current.filter((n) => n !== name)
                      : [...current, name];
                    onChange({ ...value, tools: next.length === tools.length ? [] : next });
                  }}
                  className={
                    "rounded-full border px-2.5 py-1 font-technical text-caption transition-colors " +
                    (picked ? "border-primary bg-primary/10" : "border-border text-muted-foreground")
                  }
                >
                  {name}
                </button>
              );
            })}
          </div>
        )}
        <p className="text-micro text-muted-foreground">
          All of them by default. A helper is narrowed against this and against what this agent
          actually holds, so nothing here can widen anything.
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="spawn-models">Models a helper may run on</Label>
        <Input
          id="spawn-models"
          value={value.models.join(", ")}
          spellCheck={false}
          placeholder={models[0] ? `${models[0]}, ...` : "Comma separated; empty means this agent's own"}
          onChange={(e) =>
            onChange({
              ...value,
              models: e.target.value
                .split(",")
                .map((m) => m.trim())
                .filter(Boolean),
            })
          }
        />
        <p className="text-micro text-muted-foreground">
          Empty means this agent&apos;s own model and no other. A model named here that you never
          priced is a model you never priced.
        </p>
      </div>
      <div className="flex items-center justify-between gap-3 rounded-lg bg-surface/60 px-3 py-2">
        <div className="flex min-w-0 flex-col">
          <span className="text-body font-medium">May message a running helper</span>
          <span className="text-caption text-muted-foreground">
            Off means it can only start, check and wait.
          </span>
        </div>
        <Switch
          checked={value.may_message}
          onCheckedChange={(may_message) => onChange({ ...value, may_message })}
        />
      </div>
    </div>
  );
}
