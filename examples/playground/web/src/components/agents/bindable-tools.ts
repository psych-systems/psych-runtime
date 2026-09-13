import type { McpServerPreset } from "@/components/settings/types";
import type { HttpToolIn, ToolInfo } from "@/lib/types";

/**
 * What a program this agent writes may call, grouped the way a person thinks
 * about it: by where the tool comes from.
 *
 * The editor used to show one flat list of the agent's own tool names, because
 * that was all a program could reach. It can now reach every tool the agent is
 * authorized to call, which means MCP tools -- and MCP tools are *discovered*,
 * not declared. A picker that pretended otherwise would either hide them or
 * invent a list, and inventing one is worse: an allow-list written against a
 * guess refuses work the agent could have done.
 *
 * So each entry carries its own status, and the status is the honest answer
 * about what is known right now rather than a promise about run time:
 *
 * - `available` -- the agent holds it and it is callable.
 * - `discovered` -- an MCP server offers it; this is what it offered when
 *    somebody last connected, and the set is re-resolved at every turn.
 * - `unavailable` -- an optional server that did not answer. Its tools are
 *    not callable until it does, and the Run carries on without them.
 * - `approval` -- gated on a human. A program is refused it before it runs and
 *    told to call it directly, because nothing can ask a person while a
 *    subprocess waits.
 * - `incompatible` -- it cannot answer a program at all, and says why.
 */
export type BindingStatus =
  | "available"
  | "discovered"
  | "unavailable"
  | "approval"
  | "incompatible";

export interface BindingOption {
  /** The model-facing name, which is what a program passes to `call_tool`. */
  name: string;
  status: BindingStatus;
  /** One short sentence, shown only when the status needs explaining. */
  note?: string;
}

export interface BindingGroup {
  /** "Python", "HTTP", or the connection's own name. */
  label: string;
  origin: "code" | "http" | "mcp";
  options: BindingOption[];
  /** Said once per group rather than once per tool. */
  note?: string;
}

export const STATUS_COPY: Record<BindingStatus, { label: string; tone: string }> = {
  available: { label: "available", tone: "text-muted-foreground" },
  discovered: { label: "discovered at run time", tone: "text-muted-foreground" },
  unavailable: { label: "server unavailable", tone: "text-warning" },
  approval: { label: "needs approval", tone: "text-warning" },
  incompatible: { label: "not callable from a program", tone: "text-muted-foreground" },
};

/** The separator Psych puts between a server's name and a tool's own. */
export const MCP_SEPARATOR = "__";

/**
 * The model-facing name for one MCP tool, matching
 * `psych_runtime.core.tool_names.mcp_tool_name` for every name a person will
 * ever see here.
 *
 * The library also hashes a name that would exceed a provider's length limit.
 * That is deliberately *not* reproduced: a hash in a checkbox label tells a
 * person nothing, and the picker's job is to offer names, not to be a second
 * implementation of the naming rule. A name long enough to be hashed is left
 * as the server gave it, and the runtime resolves it either way.
 */
export function mcpToolName(server: string, tool: string): string {
  return `${server}${MCP_SEPARATOR}${tool}`.replace(/[^A-Za-z0-9_-]/g, "_");
}

/** Whether the approval selectors would gate this tool, from its annotations. */
function gatedByApproval(annotations: string[], selectors: string[]): boolean {
  if (selectors.length === 0) return false;
  const wanted = new Set(selectors.map((s) => s.replace(/^@/, "")));
  return annotations.some((annotation) => wanted.has(annotation));
}

export interface BindableToolsInput {
  /** The code tools this agent grants, by name. */
  selectedTools: string[];
  /** Everything the registry knows, for annotations. */
  catalogue: ToolInfo[];
  httpTools: HttpToolIn[];
  /** The connections this agent selected, and the allow rules it set. */
  connections: { name: string; allow: string[] }[];
  /** Every saved preset, for what each server last offered. */
  presets: McpServerPreset[];
  /** The deployment's approval selectors, so the editor can say which tools a
   *  program will be refused rather than leaving it to be discovered. */
  approvalSelectors: string[];
}

export function buildBindingGroups(input: BindableToolsInput): BindingGroup[] {
  const { selectedTools, catalogue, httpTools, connections, presets, approvalSelectors } = input;
  const byName = new Map(catalogue.map((tool) => [tool.name, tool]));
  const groups: BindingGroup[] = [];

  if (selectedTools.length > 0) {
    groups.push({
      label: "Python",
      origin: "code",
      options: selectedTools.map((name) => {
        const annotations = byName.get(name)?.annotations ?? [];
        return gatedByApproval(annotations, approvalSelectors)
          ? {
              name,
              status: "approval" as const,
              note: "A program is refused this and told to call it directly.",
            }
          : { name, status: "available" as const };
      }),
    });
  }

  const http = httpTools.map((tool) => tool.name).filter(Boolean);
  if (http.length > 0) {
    groups.push({
      label: "HTTP",
      origin: "http",
      options: http.map((name) => {
        const gated = gatedByApproval(["write"], approvalSelectors);
        return gated
          ? {
              name,
              status: "approval" as const,
              note: "A program is refused this and told to call it directly.",
            }
          : { name, status: "available" as const };
      }),
    });
  }

  const presetsByName = new Map(presets.map((preset) => [preset.name, preset]));
  for (const connection of connections) {
    const preset = presetsByName.get(connection.name);
    const reported = preset?.last_connection?.tools ?? null;
    const reachable = preset?.last_connection?.ok ?? null;
    const allowed =
      connection.allow.length === 0
        ? reported
        : (reported ?? []).filter((tool) => connection.allow.includes(tool));

    if (reachable === false) {
      groups.push({
        label: connection.name,
        origin: "mcp",
        options: [],
        note: preset?.optional
          ? "This server did not answer when it was last tested. It is optional, so a program runs without its tools and the model is told they are unavailable."
          : "This server did not answer when it was last tested. It is required, so a Run fails rather than quietly losing its tools.",
      });
      continue;
    }
    groups.push({
      label: connection.name,
      origin: "mcp",
      options: (allowed ?? []).map((tool) => ({
        name: mcpToolName(connection.name, tool),
        status: "discovered" as const,
      })),
      note:
        allowed === null
          ? "Nobody has connected to this server yet, so nothing can be listed. Its tools are still callable from a program: the set is resolved fresh at every turn."
          : "What this server offered when it was last tested. The set is resolved again at every turn, so a tool it removes stops being callable in the same turn it stops being callable directly.",
    });
  }

  return groups;
}

/** Every name across every group, for the "all of them" checkbox. */
export function allBindingNames(groups: BindingGroup[]): string[] {
  return groups.flatMap((group) => group.options.map((option) => option.name));
}
