/**
 * How a catalogue connector reads on the page.
 *
 * The status comes from the catalogue's own `state`, not from whether somebody
 * clicked something this minute: a connector that finished its sign-in still
 * says so after a reload, and one waiting on consent says that rather than
 * nothing.
 */

import type { CatalogueConnector, ConnectorCategory, ConnectorState } from "@/lib/types";

export const CATEGORY_LABELS: Record<ConnectorCategory, string> = {
  code: "Code",
  work: "Work tracking",
  chat: "Chat",
  payments: "Payments",
  infra: "Infrastructure",
  data: "Data",
  design: "Design",
  automation: "Automation",
};

/** The order categories are shown in: the ones most people connect first. */
export const CATEGORY_ORDER: ConnectorCategory[] = [
  "code",
  "work",
  "chat",
  "data",
  "infra",
  "payments",
  "design",
  "automation",
];

export interface ConnectorStatus {
  label: string;
  tone: "done" | "failed" | "pending" | "idle";
}

export function connectorStatus(connector: CatalogueConnector): ConnectorStatus {
  const detail = connector.failure_reason ?? connector.detail ?? "";
  const state: ConnectorState = connector.state;
  switch (state) {
    case "connected":
      return { label: "Connected", tone: "done" };
    case "pending_authorization":
      return { label: "Waiting for you to sign in", tone: "pending" };
    case "needs_credential":
      return { label: "Needs a key", tone: "idle" };
    case "error":
    case "failed":
      return { label: detail === "" ? "Last attempt failed" : detail, tone: "failed" };
    default:
      return { label: "Not connected", tone: "idle" };
  }
}

/** "Sign in" or "API key", as the row's badge. */
export function authLabel(connector: CatalogueConnector): string {
  return connector.auth.kind === "oauth" ? "Sign in" : "API key";
}

/**
 * Whether connecting needs a dialog before anything is attempted.
 *
 * An OAuth connector that registers itself needs nothing: the attempt is the
 * sign-in. One that does not needs a client id somebody registered, or a token
 * instead, and an API-key connector always needs the key -- unless it connects
 * without one at all.
 */
export function needsDialog(connector: CatalogueConnector): boolean {
  if (connector.auth.optional === true) return false;
  // A key already stored is not a question worth asking again. Getting it
  // wrong is what Reconnect and the dialog behind it are for.
  if (connector.credential_set === true) return false;
  if (connector.auth.kind === "api_key") return true;
  return connector.auth.dynamic_registration !== true;
}

/** Whether a token may be used instead of signing in. */
export function tokenAllowed(connector: CatalogueConnector): boolean {
  return connector.auth.kind === "api_key" || connector.auth.token_alternative === true;
}

/** The name the connector's credential is stored under. */
export function credentialName(connector: CatalogueConnector): string {
  // The name the backend already chose, when it chose one.
  if (connector.credential) return connector.credential;
  const hint = connector.auth.credential_hint ?? "";
  // The hint is a sentence in the newer contract and a name in the older one.
  // A name is what a preset's `credential` field holds, so anything with a
  // space in it is prose and the connector's own name is used instead.
  if (hint !== "" && !/\s/.test(hint)) return hint;
  return `${connector.name}_token`;
}

export function groupByCategory(
  connectors: CatalogueConnector[],
): Array<{ category: ConnectorCategory; connectors: CatalogueConnector[] }> {
  const seen = new Map<ConnectorCategory, CatalogueConnector[]>();
  for (const connector of connectors) {
    const bucket = seen.get(connector.category);
    if (bucket) bucket.push(connector);
    else seen.set(connector.category, [connector]);
  }
  const ordered = [
    ...CATEGORY_ORDER.filter((category) => seen.has(category)),
    ...[...seen.keys()].filter((category) => !CATEGORY_ORDER.includes(category)),
  ];
  return ordered.map((category) => ({
    category,
    connectors: [...seen.get(category)!].sort((a, b) => a.label.localeCompare(b.label)),
  }));
}
