"use client";

import { useState } from "react";
import { CheckIcon, PlugIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/page";
import { Skeleton } from "@/components/ui/skeleton";
import { CheckList } from "@/components/agents/check-list";
import { describedAs } from "@/components/connections/connection-state";
import { formatDayLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { McpServerPreset } from "@/lib/types";

/**
 * One chosen connection, and how much of it this agent may use.
 *
 * `allow` empty means everything the connection offers, which is what
 * `McpServer.allow` means on the wire too.
 */
export interface ConnectionChoice {
  name: string;
  allow: string[];
}

interface ConnectionsFieldProps {
  connections: McpServerPreset[];
  value: ConnectionChoice[];
  onChange: (next: ConnectionChoice[]) => void;
  loading: boolean;
}

/** The tool names the last successful connection reported, or null when
 *  nobody has connected to it yet. An allow-list written against a guess is
 *  worse than no allow-list, so the picker only offers one when there is a
 *  real list to pick from. */
function knownTools(connection: McpServerPreset): string[] | null {
  const record = connection.last_connection;
  if (!record || !record.ok || record.tools.length === 0) return null;
  return record.tools;
}

function statusLine(connection: McpServerPreset): string {
  const record = connection.last_connection;
  if (!record) return "Not tried yet.";
  if (!record.ok) return `Last try failed: ${record.detail}`;
  const count = record.tools.length;
  const tools = count === 1 ? "1 tool" : `${count} tools`;
  return `${tools}, last checked ${formatDayLabel(record.checked_at)}.`;
}

export function ConnectionsField({
  connections,
  value,
  onChange,
  loading,
}: ConnectionsFieldProps) {
  const chosen = new Map(value.map((choice) => [choice.name, choice]));
  // Which of the two allow-list modes each connection is showing. Kept here
  // rather than derived from `allow.length`, because an empty allow-list is
  // "everything it offers" on the wire and also what you have the moment you
  // switch to picking and have not picked yet. Deriving the mode from it
  // bounced the picker shut on the first click.
  const [modes, setModes] = useState<Record<string, "all" | "pick">>({});

  function toggle(connection: McpServerPreset) {
    if (chosen.has(connection.name)) {
      onChange(value.filter((choice) => choice.name !== connection.name));
      return;
    }
    // Starts from whatever allow-list the connection itself carries. A
    // connection someone already narrowed in Connections should not silently
    // widen the moment an agent picks it up.
    onChange([...value, { name: connection.name, allow: [...connection.allow] }]);
  }

  function setAllow(name: string, allow: string[]) {
    onChange(value.map((choice) => (choice.name === name ? { ...choice, allow } : choice)));
  }

  if (loading) {
    // Shaped like the rows it becomes, so the card does not jump a hundred
    // pixels the moment the settings arrive.
    return (
      <div className="flex flex-col gap-3">
        <Skeleton className="h-16 w-full rounded-xl" />
        <Skeleton className="h-16 w-full rounded-xl" />
      </div>
    );
  }

  if (connections.length === 0) {
    return (
      <EmptyState
        icon={PlugIcon}
        title="No connections set up yet"
        description="Connections are the outside systems an agent can reach: a ticket system, a calendar, an internal search. Add one and it becomes available to every agent you build."
        action={
          <Button asChild size="sm" variant="outline">
            <Link href="/connections">Set up a connection</Link>
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {connections.map((connection) => {
        const choice = chosen.get(connection.name);
        const selected = choice !== undefined;
        const tools = knownTools(connection);
        const mode = modes[connection.name] ?? (choice && choice.allow.length > 0 ? "pick" : "all");
        const everything = mode === "all";

        return (
          <div
            key={connection.name}
            className={cn(
              "rounded-xl border transition-colors",
              selected ? "border-primary/50 bg-primary/5" : "border-border"
            )}
          >
            <button
              type="button"
              aria-pressed={selected}
              onClick={() => toggle(connection)}
              className="flex w-full items-start gap-3 px-3 py-2.5 text-left"
            >
              <span
                className={cn(
                  "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[4px] border",
                  selected ? "border-primary bg-primary text-primary-foreground" : "border-input"
                )}
                aria-hidden
              >
                {selected && <CheckIcon className="size-3" />}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-body font-medium">{connection.name}</span>
                {/* The same sentence the agent is told at run time. Picking
                    between three names is no easier for the person building
                    the agent than it is for the model running it. */}
                {describedAs(connection) !== "" && (
                  <span className="text-caption text-surface-foreground">
                    {describedAs(connection)}
                  </span>
                )}
                <span className="text-caption text-muted-foreground">{statusLine(connection)}</span>
              </span>
            </button>

            {selected && (
              <div className="flex flex-col gap-3 border-t border-border/60 px-3 py-3">
                {tools === null ? (
                  <p className="text-caption text-muted-foreground">
                    Nobody has connected to this one yet, so its list of tools is not known here.
                    The agent gets whatever it offers. Connect to it once and you can narrow that
                    down.
                  </p>
                ) : (
                  <>
                    <div className="flex flex-wrap gap-1.5">
                      <Button
                        type="button"
                        size="xs"
                        variant={everything ? "default" : "outline"}
                        onClick={() => {
                          setModes({ ...modes, [connection.name]: "all" });
                          setAllow(connection.name, []);
                        }}
                      >
                        Everything it offers
                      </Button>
                      <Button
                        type="button"
                        size="xs"
                        variant={everything ? "outline" : "default"}
                        onClick={() => setModes({ ...modes, [connection.name]: "pick" })}
                      >
                        Only what I pick
                      </Button>
                    </div>
                    {everything ? (
                      <p className="text-caption text-muted-foreground">
                        All {tools.length} of this connection&apos;s tools are available to the
                        agent.
                      </p>
                    ) : (
                      <CheckList
                        options={tools.map((tool) => ({ value: tool, label: tool, mono: true }))}
                        selected={choice.allow}
                        onChange={(next) => setAllow(connection.name, next)}
                        searchPlaceholder={`Search ${connection.name} tools`}
                        emptyText="No tool matches that search."
                        countLabel={(total) =>
                          total === 1 ? "1 tool offered" : `${total} tools offered`
                        }
                      />
                    )}
                    {!everything && choice.allow.length === 0 && (
                      <p className="text-caption text-muted-foreground">
                        Nothing picked yet, so the agent would get everything. Pick at least one
                        tool to narrow it.
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
