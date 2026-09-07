/**
 * What a connection's stored record means, in the words the page says out
 * loud.
 *
 * The bug this exists to kill: the connect result used to live in the
 * component's own state, inside a Radix tab that unmounted, so a server you
 * had just connected to offered you a "Connect" button again after a refresh
 * with no memory that it had ever worked. The backend now persists
 * `last_connection`, so the answer to "is this working" comes from the record
 * rather than from whether you happen to have clicked something this minute.
 *
 * `live` is a different question and is treated as one: it is the pooled
 * connection this backend process is holding right now, which is worth
 * showing to an operator and means nothing to anyone else.
 */

import type { McpServerPreset } from "@/components/settings/types";

export type ConnectionHealth = "connected" | "failed" | "untested";

export interface ConnectionState {
  health: ConnectionHealth;
  /** The one line the card leads with. */
  headline: string;
  /** The server's own words, if it said any. Never a generic "failed": a bad
   *  credential has to read differently from a URL that answers with a login
   *  page. */
  detail: string | null;
  toolCount: number | null;
  checkedAt: string | null;
}

/** The failure in plain language. `error_type` is the exception's class name,
 *  which is exact and unreadable, so it picks the sentence and the record's
 *  own `detail` carries the specifics underneath. */
function failureHeadline(errorType: string | null | undefined): string {
  switch (errorType) {
    case "CredentialNotFound":
      return "No credential stored under that name";
    case "McpServerUnreachable":
      return "The server did not answer";
    case "McpProtocolError":
      return "The server answered, but not as a connection";
    default:
      return "The last attempt failed";
  }
}

export function describeConnection(preset: McpServerPreset): ConnectionState {
  const record = preset.last_connection ?? null;

  if (record === null) {
    return {
      health: "untested",
      headline: "Never connected",
      detail: null,
      toolCount: null,
      checkedAt: null,
    };
  }

  if (record.ok) {
    // The live pool is the fresher number when this process holds the
    // connection; the stored record is what survives a restart.
    const count = preset.live?.tool_count ?? record.tools.length;
    return {
      health: "connected",
      headline: count === 1 ? "Connected, 1 tool" : `Connected, ${count} tools`,
      detail: record.detail,
      toolCount: count,
      checkedAt: record.checked_at,
    };
  }

  return {
    health: "failed",
    headline: failureHeadline(record.error_type),
    detail: record.detail,
    toolCount: null,
    checkedAt: record.checked_at,
  };
}

export type PreloadChoice = "auto" | "list" | "discover";

export function preloadChoice(preload: boolean | null | undefined): PreloadChoice {
  if (preload === true) return "list";
  if (preload === false) return "discover";
  return "auto";
}

export function preloadValue(choice: PreloadChoice): boolean | null {
  if (choice === "list") return true;
  if (choice === "discover") return false;
  return null;
}

export const PRELOAD_OPTIONS: ReadonlyArray<{
  value: PreloadChoice;
  label: string;
  hint: string;
}> = [
  {
    value: "auto",
    label: "Decide automatically",
    hint: "Lists a small catalogue, lets the model ask about a large one.",
  },
  {
    value: "list",
    label: "List its tools to the model",
    hint: "Every tool travels with every message, so a large catalogue costs a lot of prompt.",
  },
  {
    value: "discover",
    label: "Let the model discover them",
    hint: "The model asks for a tool when it needs one. Cheaper, one step slower.",
  },
];

export function preloadSummary(preload: boolean | null | undefined): string {
  return PRELOAD_OPTIONS.find((option) => option.value === preloadChoice(preload))!.label;
}

/**
 * The longest description the backend keeps. Anything past this is cut when
 * it is stored, so the form stops you there rather than accepting a
 * paragraph and silently keeping half of it.
 */
export const DESCRIPTION_LIMIT = 400;

/**
 * A connection preset plus the sentence that says what the system is for.
 *
 * The description lives here rather than on `McpServerPreset` because that
 * type mirrors the wire contract and is owned elsewhere. The field is real on
 * the wire in both directions; this is the local view of it, so reading and
 * writing it goes through one place instead of a cast at each call site.
 */
export interface ConnectionPreset extends McpServerPreset {
  description?: string;
}

/** The description as written, or the empty string when nobody wrote one. */
export function describedAs(preset: McpServerPreset): string {
  return (preset as ConnectionPreset).description?.trim() ?? "";
}
