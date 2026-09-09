/**
 * Wire shapes for `/api/settings/*`, mirroring `app/schemas.py` field for
 * field.
 *
 * This is the one definition; `src/lib/types.ts` re-exports it so a caller
 * reaching for wire shapes finds them beside every other wire shape. It used
 * to type this contract as opaque JSON with a comment saying the contract was
 * still being written, so every settings call cast through `unknown` to reach
 * the very types defined here.
 */

import type { SkillIn } from "@/lib/types";

/** One saved A2A peer. `credential` is the NAME of a credential the secret
 *  resolver looks up at call time, never a secret value. */
export interface A2APeerPreset {
  name: string;
  url: string;
  description: string;
  credential?: string | null;
  scheme: string;
  tenant?: string | null;
  allow: string[];
  optional: boolean;
  extensions: string[];
}

export interface ProviderOut {
  id: string;
  label: string;
  base_url: string;
  model: string;
  /** Never the key itself -- the server asserts this endpoint cannot leak
   * one. Render "key set" from this, never a masked fake value. */
  has_api_key: boolean;
}

/**
 * One entry of `PUT /api/settings/providers`. `id` omitted (or naming a
 * provider that does not exist yet) creates one. `api_key` omitted keeps
 * whatever is already stored for that `id`; `""` clears it; anything else
 * replaces it -- so an edit that doesn't touch the key must never send
 * `api_key: ""`.
 */
export interface ProviderIn {
  id?: string;
  label: string;
  base_url: string;
  model: string;
  api_key?: string;
}

export interface ProviderTestResult {
  ok: boolean;
  /** A human-readable summary. On failure, the provider's own error text --
   * not a generic "connection failed" -- so a bad key reads differently
   * from a bad URL or a bad model id. */
  detail: string;
  models?: string[] | null;
}

export type McpTransport = "http" | "sse";
export type McpGrant = "authorization_code" | "client_credentials";

export interface McpOAuthPreset {
  grant: McpGrant;
  preregistered_client_id?: string | null;
  /** A credential NAME the secret resolver looks up at connect time --
   * never a literal secret. */
  client_secret_credential?: string | null;
  /** Authorization server that may receive registered client credentials. */
  issuer?: string | null;
  cimd_url?: string | null;
  allow_dynamic_registration?: boolean;
  application_type?: "native" | "web";
  client_name?: string;
}

/** What the last connection attempt to one server found, persisted so a
 *  connected server still looks connected after a refresh. */
export interface McpConnectionRecord {
  ok: boolean;
  detail: string;
  checked_at: string;
  /** The tool names, not just a count: an allow-list is written against
   *  them, and a builder that cannot show them makes a person guess. */
  tools: string[];
  /** The exception's class name when `ok` is false: "CredentialNotFound",
   *  "McpServerUnreachable", "McpProtocolError". */
  error_type?: string | null;
}

/** The pooled connection this backend process is holding right now. A read
 *  of what exists, not a second connection attempt. */
export interface McpLiveConnection {
  tenant: string;
  server: string;
  url: string;
  transport: string;
  era: string | null;
  tool_count: number;
  catalogue_age_seconds: number | null;
  catalogue_ttl_seconds: number;
}

export interface McpServerPreset {
  name: string;
  url: string;
  transport: McpTransport;
  /** A credential NAME for a static (non-OAuth) header, same rule as
   * `oauth.client_secret_credential`. */
  credential?: string | null;
  allow: string[];
  optional: boolean;
  oauth?: McpOAuthPreset | null;
  /** Whether this server's tools are listed in the model's prompt or
   *  discovered on demand. `null` decides from the catalogue: a server with
   *  more than forty tools is discovered, because listing one costs its whole
   *  schema set on every message. */
  preload?: boolean | null;
  last_connection?: McpConnectionRecord | null;
  live?: McpLiveConnection | null;
}

/** The writable part of a connection preset. Health and live-connection
 * fields are computed by the backend and never belong in a settings PUT. */
export type McpServerPresetIn = Omit<McpServerPreset, "last_connection" | "live">;

import type { ModelPrice, RuntimeSettings } from "@/lib/types";

export interface PlaygroundSettings {
  /** How this account's runs execute: cost policy, sandbox, egress, denied
   *  tools. See `RuntimeSettings`. */
  runtime: RuntimeSettings;
  providers: ProviderOut[];
  mcp_servers: McpServerPreset[];
  secrets: string[];
  active_provider_id: string | null;
  model_prices: ModelPrice[];
  /** Saved A2A peers: other agents this account can point an agent at.
   *
   *  Presets only, like the MCP list. Attaching one copies its URL, credential
   *  name and grants into the published spec, so editing here changes what the
   *  next publish gets and never what an already-published agent calls. */
  a2a_peers: A2APeerPreset[];
  /** Skills written once and attachable to any agent this account builds.
   *
   *  Read only to fill in an agent form. Attaching one copies it into the
   *  published spec, so it joins the version hash and editing this list later
   *  changes nothing about an agent already published. "Global" means
   *  available to every agent you build, not reaching into every agent you
   *  have built, and the page has to say so because the word invites the
   *  other reading. */
  skills: SkillIn[];
}

/** `providerOutToIn` converts a fetched provider back into the shape
 * `PUT /api/settings/providers` accepts, omitting `api_key` so the stored
 * key survives a write this dialog didn't touch. Use this to build the
 * full array the endpoint replaces the provider list with. */
export function providerOutToIn(provider: ProviderOut): ProviderIn {
  return {
    id: provider.id,
    label: provider.label,
    base_url: provider.base_url,
    model: provider.model,
  };
}
