"use client";

import { useState } from "react";
import Link from "next/link";
import {
  BotIcon,
  ChevronRightIcon,
  Loader2Icon,
  PlugIcon,
  PlugZapIcon,
  RefreshCwIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FeatureGroup, FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { HelpTip } from "@/components/ui/help";
import { Section } from "@/components/ui/page";
import { cn } from "@/lib/utils";
import { describeApiError } from "@/lib/errors";
import { ToolCatalogue } from "@/components/connections/tool-catalogue";
import {
  ConnectorConnectDialog,
  type ConnectChoice,
} from "@/components/connections/connector-connect-dialog";
import {
  CATEGORY_LABELS,
  authLabel,
  connectorStatus,
  groupByCategory,
  needsDialog,
} from "@/components/connections/connector-state";
import type { McpServerPreset, McpServerPresetIn } from "@/components/settings/types";
import type { CatalogueAgent, CatalogueConnector } from "@/lib/types";

interface ConnectorsSectionProps {
  connectors: CatalogueConnector[];
  /** The catalogue's agents, for the link to a connector's specialist. */
  catalogueAgents: CatalogueAgent[];
  /** The account's MCP presets. A connector joins one on `name`. */
  presets: McpServerPreset[];
  /** The connector currently being connected, from the page's test hook. */
  testing: string | null;
  onConnect: (name: string, options?: { force?: boolean }) => void;
  onDisconnect: (name: string) => void;
  savePreset: (edited: McpServerPresetIn, previousName: string | null) => Promise<void>;
  saveSecret: (name: string, value: string) => Promise<void>;
  /** Create the presets this account is missing. */
  onSeed: () => Promise<void>;
}

/** The writable part of a preset, with the computed fields dropped. */
function writable(preset: McpServerPreset): McpServerPresetIn {
  const { last_connection, live, ...rest } = preset;
  void last_connection;
  void live;
  return rest;
}

/**
 * Everything this console knows how to connect to, whether or not you have.
 *
 * The list is the same before and after: a connector is an offer until
 * somebody signs in, and then it is a system with tools. That is why one row
 * carries both -- the offer's description, and the catalogue it answered with
 * once it has answered.
 */
export function ConnectorsSection({
  connectors,
  catalogueAgents,
  presets,
  testing,
  onConnect,
  onDisconnect,
  savePreset,
  saveSecret,
  onSeed,
}: ConnectorsSectionProps) {
  const [openRow, setOpenRow] = useState<string | null>(null);
  const [dialogFor, setDialogFor] = useState<CatalogueConnector | null>(null);
  const [seeding, setSeeding] = useState(false);

  if (connectors.length === 0) return null;

  const missing = connectors.filter(
    (connector) => !presets.some((preset) => preset.name === connector.name),
  );

  function presetFor(connector: CatalogueConnector): McpServerPreset | null {
    return presets.find((preset) => preset.name === connector.name) ?? null;
  }

  function agentIdFor(connector: CatalogueConnector): string | null {
    if (connector.agent_id) return connector.agent_id;
    const wanted = connector.agent?.catalogue_id ?? null;
    const match = catalogueAgents.find((agent) =>
      wanted !== null ? agent.catalogue_id === wanted : agent.connector === connector.name,
    );
    return match?.agent_id ?? null;
  }

  async function addMissing() {
    setSeeding(true);
    try {
      await onSeed();
    } catch (err) {
      toast.error("Could not add the connectors", { description: describeApiError(err) });
    } finally {
      setSeeding(false);
    }
  }

  /** Start a connect, asking for whatever it needs first. */
  function startConnect(connector: CatalogueConnector) {
    if (presetFor(connector) === null) {
      void addMissing();
      return;
    }
    if (needsDialog(connector)) {
      setDialogFor(connector);
      return;
    }
    onConnect(connector.name);
  }

  /**
   * What the dialog collected, written down before the attempt.
   *
   * The order is not interchangeable: the secret has to exist under its name
   * before a preset points at it, or the first connect fails looking for a
   * credential nothing has stored yet.
   */
  async function applyChoice(connector: CatalogueConnector, choice: ConnectChoice) {
    const preset = presetFor(connector);
    if (preset === null) throw new Error("This connector is not in this account yet.");
    if (choice.kind === "token") {
      await saveSecret(choice.secretName, choice.secretValue);
      await savePreset({ ...writable(preset), credential: choice.secretName }, preset.name);
    } else {
      await savePreset(
        {
          ...writable(preset),
          oauth: {
            grant: "authorization_code",
            ...preset.oauth,
            preregistered_client_id: choice.clientId,
          },
        },
        preset.name,
      );
    }
    onConnect(connector.name, { force: true });
  }

  return (
    <Section>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-1.5 text-base font-semibold">
          Connectors
          <HelpTip title="Connectors" short="Ready to connect; they only need you to sign in.">
            <p>
              Systems this console already knows how to reach. Connecting one stores a sign-in for
              this account; building an agent with it copies what it can use at that moment.
            </p>
          </HelpTip>
        </h2>
        {missing.length > 0 && (
          <Button size="sm" variant="outline" onClick={() => void addMissing()} disabled={seeding}>
            {seeding && <Loader2Icon className="animate-spin" />}
            Add {missing.length === connectors.length ? "them" : "the missing ones"}
          </Button>
        )}
      </div>
      <p className="text-caption text-muted-foreground">
        Pick one and sign in. Nothing else is needed.
      </p>

      <FeatureTable>
        {groupByCategory(connectors).map((group) => (
          <FeatureGroup
            key={group.category}
            title={CATEGORY_LABELS[group.category] ?? group.category}
          >
            {group.connectors.map((connector) => {
              const preset = presetFor(connector);
              const status = connectorStatus(connector);
              const busy = testing === connector.name;
              const connected = connector.state === "connected" || connector.connected;
              const tools = preset?.last_connection?.tools ?? [];
              const open = openRow === connector.name;
              const agentId = agentIdFor(connector);
              return (
                <FeatureRow
                  key={connector.name}
                  label={
                    <span className="inline-flex flex-wrap items-center gap-1.5">
                      {connector.label}
                      <Badge variant="outline" className="text-muted-foreground">
                        {authLabel(connector)}
                      </Badge>
                    </span>
                  }
                  help={
                    <>
                      <p>{connector.description}</p>
                      <p>
                        Address <code>{connector.url}</code>.
                      </p>
                      {connector.docs_url && (
                        <p>
                          Its own instructions are at <code>{connector.docs_url}</code>.
                        </p>
                      )}
                    </>
                  }
                  detail={
                    <span className="flex flex-col gap-0.5">
                      <span>{connector.description}</span>
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          className={cn(
                            "size-2 shrink-0 rounded-full",
                            status.tone === "done"
                              ? "bg-status-done"
                              : status.tone === "failed"
                                ? "bg-status-failed"
                                : "bg-muted-foreground/50",
                            (busy || status.tone === "pending") && "animate-pulse",
                          )}
                          aria-hidden
                        />
                        <span
                          className={cn(
                            status.tone === "failed" && "text-status-failed",
                            status.tone === "done" && "text-foreground",
                          )}
                        >
                          {busy
                            ? "Connecting now"
                            : connected && tools.length > 0
                              ? `Connected, ${tools.length} tools`
                              : status.label}
                        </span>
                      </span>
                    </span>
                  }
                  control={
                    <span className="flex max-w-full flex-wrap items-center justify-end gap-1">
                      {/* Icon-only, with the count in the status line instead:
                          a connected row already carries Reconnect and
                          Disconnect, and four worded buttons do not fit
                          beside a label on a phone. */}
                      {connected && tools.length > 0 && (
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          aria-expanded={open}
                          aria-label={
                            open
                              ? `Hide the ${tools.length} tools ${connector.label} offers`
                              : `Show the ${tools.length} tools ${connector.label} offers`
                          }
                          onClick={() => setOpenRow(open ? null : connector.name)}
                        >
                          <ChevronRightIcon
                            className={cn("transition-transform", open && "rotate-90")}
                            aria-hidden
                          />
                        </Button>
                      )}
                      {agentId !== null && (
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          asChild
                          aria-label={`Open the agent for ${connector.label}`}
                        >
                          <Link href={`/agents/${encodeURIComponent(agentId)}`}>
                            <BotIcon />
                          </Link>
                        </Button>
                      )}
                      {connected ? (
                        <>
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={busy}
                            onClick={() => onConnect(connector.name, { force: true })}
                          >
                            {busy ? <Loader2Icon className="animate-spin" /> : <RefreshCwIcon />}
                            Reconnect
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => onDisconnect(connector.name)}
                          >
                            <PlugIcon /> Disconnect
                          </Button>
                        </>
                      ) : (
                        <Button
                          size="xs"
                          disabled={busy || seeding}
                          onClick={() => startConnect(connector)}
                        >
                          {busy ? <Loader2Icon className="animate-spin" /> : <PlugZapIcon />}
                          Connect
                        </Button>
                      )}
                    </span>
                  }
                >
                  {open && tools.length > 0 && <ToolCatalogue tools={tools} />}
                </FeatureRow>
              );
            })}
          </FeatureGroup>
        ))}
      </FeatureTable>

      <ConnectorConnectDialog
        connector={dialogFor}
        onOpenChange={(next) => !next && setDialogFor(null)}
        onConnect={async (choice) => {
          if (dialogFor === null) return;
          await applyChoice(dialogFor, choice);
        }}
      />
    </Section>
  );
}
