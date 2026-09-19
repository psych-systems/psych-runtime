"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { PlusIcon, SearchIcon } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { FeatureGroup, FeatureRow, FeatureTable, ToggleRow } from "@/components/ui/feature-table";
import { FieldError } from "@/components/settings/validation";
import { CheckList } from "@/components/agents/check-list";
import { describedAs } from "@/components/connections/connection-state";
import { HttpToolsField } from "@/components/agents/http-tools-field";
import { SkillsField } from "@/components/agents/skills-field";
import type { AgentFormState } from "@/components/agents/use-agent-form";
import type { McpServerPreset } from "@/lib/types";
import { cn } from "@/lib/utils";

/** How many tool rows go in the DOM at once. One connection in this
 *  playground offers hundreds, and the search box is what makes the rest
 *  reachable. */
const RENDER_CAP = 40;

/**
 * What the agent may reach: its tools, the systems it connects to, and the
 * procedures it can load. All of it on by default, because an agent with
 * nothing to call is the least useful one this console can publish and a
 * person can uncheck faster than they can discover.
 */
export function CapabilitiesTable({ form }: { form: AgentFormState }) {
  return (
    <FeatureTable>
      <FeatureGroup
        title="Tools"
        help="Things this agent can do on its own, beyond writing a reply. Everything registered here is granted unless you take it away."
      >
        <ToolChecklist form={form} />
        <HttpToolsRow form={form} />
      </FeatureGroup>

      <FeatureGroup
        title="Connections"
        help="Outside systems it may reach. Each one is copied into the agent as it is set up today, so changing it later does not alter an agent you already published."
      >
        <ConnectionRows form={form} />
        <PeerRows form={form} />
      </FeatureGroup>

      <FeatureGroup
        title="Skills"
        help="Procedures the agent loads only when it needs one. The description sits in every prompt; the instructions are fetched on demand, so a long procedure costs nothing on conversations that never use it."
      >
        <SkillRows form={form} />
      </FeatureGroup>
    </FeatureTable>
  );
}

function ToolChecklist({ form }: { form: AgentFormState }) {
  const { tools, toolsError, selectedTools, setSelectedTools, fieldErrors } = form;
  const [query, setQuery] = useState("");

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = tools ?? [];
    if (needle === "") return all;
    return all.filter(
      (tool) =>
        tool.name.toLowerCase().includes(needle) ||
        tool.description.toLowerCase().includes(needle),
    );
  }, [tools, query]);

  const chosen = new Set(selectedTools);
  const total = tools?.length ?? 0;
  const detail =
    tools === null
      ? "Loading."
      : total === 0
        ? "This installation has no built-in tools. Connections are the other way to give it something to use."
        : selectedTools.length === total
          ? `All ${total} of them.`
          : `${selectedTools.length} of ${total}.`;

  return (
    <FeatureRow
      label="Built-in tools"
      help="Granting a tool does not make the agent use it; it makes it possible. Nothing granted means it answers from the conversation alone."
      detail={detail}
      control={
        total > 0 ? (
          <div className="flex items-center gap-1">
            <Button
              type="button"
              size="xs"
              variant="ghost"
              onClick={() => setSelectedTools((tools ?? []).map((tool) => tool.name))}
              disabled={selectedTools.length === total}
            >
              All
            </Button>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              onClick={() => setSelectedTools([])}
              disabled={selectedTools.length === 0}
            >
              None
            </Button>
          </div>
        ) : undefined
      }
    >
      {toolsError ? (
        <Alert variant="destructive">
          <AlertDescription>{toolsError}</AlertDescription>
        </Alert>
      ) : tools === null ? (
        <Skeleton className="h-40 w-full rounded-lg" />
      ) : total === 0 ? null : (
        <div className="flex flex-col gap-2">
          <div className="relative">
            <SearchIcon
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search tools"
              aria-label="Search tools"
              className="pl-8"
            />
          </div>
          <ul className="max-h-64 divide-y divide-border/60 overflow-y-auto rounded-lg border border-border">
            {matches.length === 0 && (
              <li className="px-3 py-6 text-center text-caption text-muted-foreground">
                No tool matches that search.
              </li>
            )}
            {matches.slice(0, RENDER_CAP).map((tool) => (
              <li key={tool.name}>
                <label className="flex cursor-pointer items-start gap-2.5 px-3 py-1.5 transition-colors hover:bg-surface/60">
                  <Checkbox
                    className="mt-0.5"
                    checked={chosen.has(tool.name)}
                    onCheckedChange={() =>
                      setSelectedTools(
                        chosen.has(tool.name)
                          ? selectedTools.filter((name) => name !== tool.name)
                          : [...selectedTools, tool.name],
                      )
                    }
                  />
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate font-technical text-caption leading-tight">
                      {tool.name}
                    </span>
                    <span className="line-clamp-1 text-micro text-muted-foreground">
                      {tool.description}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {matches.length > RENDER_CAP && (
            <p className="text-micro text-muted-foreground">
              {matches.length - RENDER_CAP} more match. Narrow the search to see them.
            </p>
          )}
        </div>
      )}
      <FieldError message={fieldErrors.tools} />
    </FeatureRow>
  );
}

/** The HTTP tool editor, unchanged, behind a dialog. It is the longest form
 *  on this page and it is empty for almost everybody. */
function HttpToolsRow({ form }: { form: AgentFormState }) {
  const [open, setOpen] = useState(false);
  const count = form.httpTools.length;
  const broken = Object.keys(form.fieldErrors).filter((key) =>
    key.startsWith("http_tools"),
  ).length;
  return (
    <FeatureRow
      label="HTTP tools"
      help="REST endpoints the agent may call directly, with a key it never sees. Nobody writes a function: the schema you give is what the model is shown."
      detail={
        broken > 0 ? (
          <span className="text-status-failed">
            {broken === 1 ? "One needs fixing." : `${broken} need fixing.`}
          </span>
        ) : count === 0 ? (
          "None."
        ) : count === 1 ? (
          "One endpoint."
        ) : (
          `${count} endpoints.`
        )
      }
      control={
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button type="button" size="sm" variant="outline">
              <PlusIcon /> {count === 0 ? "Add HTTP tool" : "Edit"}
            </Button>
          </DialogTrigger>
          <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle>HTTP tools</DialogTitle>
              <DialogDescription>
                An endpoint the model may call. The credential box takes the name of a secret, never
                the secret itself.
              </DialogDescription>
            </DialogHeader>
            <HttpToolsField
              value={form.httpTools}
              onChange={form.setHttpTools}
              fieldErrors={form.fieldErrors}
              secrets={form.settings?.secrets ?? []}
            />
            <DialogFooter>
              <Button type="button" onClick={() => setOpen(false)}>
                Done
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      }
    />
  );
}

function connectionDetail(preset: McpServerPreset): string {
  const record = preset.last_connection;
  if (!record) return "Not tried yet.";
  if (!record.ok) return `Last try failed: ${record.detail}`;
  return record.tools.length === 1 ? "1 tool offered." : `${record.tools.length} tools offered.`;
}

function ConnectionRows({ form }: { form: AgentFormState }) {
  const { presets, connections, setConnections, settingsLoading, settings, fieldErrors } = form;

  if (settingsLoading && settings === null) {
    return (
      <FeatureRow label="Connections" detail="Loading.">
        <Skeleton className="h-8 w-full rounded-lg" />
      </FeatureRow>
    );
  }

  if (presets.length === 0) {
    return (
      <FeatureRow
        label="MCP servers"
        help="A connection is an outside system an agent can reach: a ticket system, a calendar, an internal search."
        detail="None set up yet."
        control={
          <Button asChild size="sm" variant="outline">
            <Link href="/connections">Set one up</Link>
          </Button>
        }
      />
    );
  }

  const chosen = new Map(connections.map((choice) => [choice.name, choice]));

  return (
    <>
      {presets.map((preset) => {
        const choice = chosen.get(preset.name);
        const on = choice !== undefined;
        const offered = preset.last_connection?.ok ? preset.last_connection.tools : [];
        return (
          <ToggleRow
            key={preset.name}
            id={`mcp-${preset.name}`}
            label={preset.name}
            help={describedAs(preset) || `Connects to ${preset.url}.`}
            detail={
              on && choice.allow.length > 0
                ? `${choice.allow.length} of its tools.`
                : connectionDetail(preset)
            }
            checked={on}
            onCheckedChange={(next) =>
              setConnections(
                next
                  ? [...connections, { name: preset.name, allow: [...preset.allow] }]
                  : connections.filter((c) => c.name !== preset.name),
              )
            }
          >
            {offered.length > 0 && choice && (
              <div className="flex flex-col gap-2">
                <p className="text-caption text-muted-foreground">
                  Nothing picked means every tool it offers.
                </p>
                <CheckList
                  options={offered.map((tool) => ({ value: tool, label: tool, mono: true }))}
                  selected={choice.allow}
                  onChange={(allow) =>
                    setConnections(
                      connections.map((c) => (c.name === preset.name ? { ...c, allow } : c)),
                    )
                  }
                  searchPlaceholder={`Search ${preset.name} tools`}
                  emptyText="No tool matches that search."
                  countLabel={(total) => (total === 1 ? "1 tool offered" : `${total} tools offered`)}
                />
              </div>
            )}
          </ToggleRow>
        );
      })}
      <FieldError message={fieldErrors.connections} />
    </>
  );
}

function PeerRows({ form }: { form: AgentFormState }) {
  const { peerPresets, peers, setPeers } = form;
  if (peerPresets.length === 0) return null;
  return (
    <>
      {peerPresets.map((peer) => (
        <ToggleRow
          key={peer.name}
          id={`peer-${peer.name}`}
          label={peer.name}
          help="Another agent this one may hand work to over A2A. What it can do is read from its own card while a conversation runs, so it is not fixed here."
          detail={peer.description || peer.url}
          checked={peers.includes(peer.name)}
          onCheckedChange={(next) =>
            setPeers(next ? [...peers, peer.name] : peers.filter((n) => n !== peer.name))
          }
        />
      ))}
    </>
  );
}

function SkillRows({ form }: { form: AgentFormState }) {
  const [open, setOpen] = useState(false);
  const { skills, setSkills, settings, fieldErrors } = form;
  const library = settings?.skills ?? [];
  const attached = new Set(skills.map((skill) => skill.name.trim()));
  const broken = Object.keys(fieldErrors).some((key) => key.startsWith("skills."));

  return (
    <>
      {library.map((skill) => (
        <ToggleRow
          key={skill.name}
          id={`skill-${skill.name}`}
          label={skill.name}
          help={`${skill.description} Attaching copies it in; editing the library afterwards does not change this agent.`}
          detail={skill.description}
          checked={attached.has(skill.name.trim())}
          onCheckedChange={(next) =>
            setSkills(
              next
                ? [...skills, { ...skill }]
                : skills.filter((s) => s.name.trim() !== skill.name.trim()),
            )
          }
        />
      ))}
      <FeatureRow
        label="Skills on this agent"
        help="A procedure this agent alone carries. The description is in every prompt; the instructions load only when the model asks for them."
        detail={
          broken ? (
            <span className="text-status-failed">One needs fixing.</span>
          ) : skills.length === 0 ? (
            "None."
          ) : skills.length === 1 ? (
            "One skill."
          ) : (
            `${skills.length} skills.`
          )
        }
        control={
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild>
              <Button type="button" size="sm" variant="outline">
                <PlusIcon /> {skills.length === 0 ? "Write one" : "Edit"}
              </Button>
            </DialogTrigger>
            <DialogContent className={cn("max-h-[85vh] overflow-y-auto sm:max-w-2xl")}>
              <DialogHeader>
                <DialogTitle>Skills</DialogTitle>
                <DialogDescription>
                  One line the model always sees, and instructions it loads when it needs them.
                </DialogDescription>
              </DialogHeader>
              <SkillsField
                value={skills}
                onChange={setSkills}
                fieldErrors={fieldErrors}
                library={library}
              />
              <DialogFooter>
                <Button type="button" onClick={() => setOpen(false)}>
                  Done
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        }
      />
    </>
  );
}
