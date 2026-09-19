"use client";

import { useState } from "react";
import { AlertTriangleIcon, PlugIcon, PlusIcon, ShieldCheckIcon } from "lucide-react";
import { toast } from "sonner";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState, Page, PageHeader, Section } from "@/components/ui/page";
import { HelpTip } from "@/components/ui/help";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { describeApiError } from "@/lib/errors";
import { useSettings } from "@/components/settings/use-settings";
import { ConnectionRow } from "@/components/connections/connection-row";
import { ConnectionDialog } from "@/components/connections/connection-dialog";
import { A2APeersSection } from "@/components/connections/a2a-peers-section";
import { useConnectionTests } from "@/components/connections/use-connection-tests";
import { ConnectorsSection } from "@/components/connections/connectors-section";
import { useCatalogue } from "@/hooks/use-catalogue";
import type { McpServerPreset } from "@/components/settings/types";

/**
 * Everything an agent can reach, and whether it currently works.
 *
 * This used to be a tab inside Settings, which was wrong twice over. It is not
 * configuration a person sets once, it is a live thing they come back to when
 * something stops working. And as a Radix tab it unmounted, taking every
 * connect result with it, so a server that had just answered with 351 tools
 * showed a bare "Connect" button again the moment you looked at anything else.
 * The status here comes from the backend's stored record, so it is the same
 * after a reload as it was before one.
 *
 * Two tables rather than two stacks of cards: an MCP server is a system with
 * tools, an A2A peer is another agent with skills, and both answer the same
 * question from opposite sides.
 */
export function ConnectionsView() {
  const {
    settings,
    loading,
    error,
    refresh,
    savePreset,
    removePreset,
    saveSecret,
    savePeers,
    addOwnAgentAsPeer,
  } = useSettings();
  const catalogue = useCatalogue();
  // A connect writes the backend's stored record, and both this page's tables
  // read it, so both are re-read when one settles.
  const tests = useConnectionTests(async () => {
    await refresh();
    await catalogue.refresh();
  });

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<McpServerPreset | null>(null);
  const [pendingDelete, setPendingDelete] = useState<McpServerPreset | null>(null);
  const [deleting, setDeleting] = useState(false);

  function openAdd() {
    setEditing(null);
    setDialogOpen(true);
  }

  function openEdit(connection: McpServerPreset) {
    setEditing(connection);
    setDialogOpen(true);
  }

  async function confirmDelete() {
    if (pendingDelete === null) return;
    setDeleting(true);
    try {
      await removePreset(pendingDelete.name);
      tests.forget(pendingDelete.name);
      setPendingDelete(null);
    } catch (err) {
      toast.error("Could not remove the connection", { description: describeApiError(err) });
    } finally {
      setDeleting(false);
    }
  }

  const allConnections = settings?.mcp_servers ?? [];
  const connectors = catalogue.catalogue?.connectors ?? [];
  const catalogueNames = new Set(connectors.map((connector) => connector.name));
  // The table below is for connections somebody added by hand. A catalogue
  // connector is the same preset underneath, but it has a row of its own
  // above, and printing it twice would offer two Connect buttons for one
  // system.
  const connections = allConnections.filter((preset) => !catalogueNames.has(preset.name));

  return (
    <Page>
      <PageHeader
        title="Connections"
        description={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            Systems your agents can reach, and whether they work right now.
            <HelpTip title="Connections" short="Attaching copies; it does not link.">
              <p>
                An agent gets its own copy of whatever it was built with, so changing a connection
                here changes what the next agent can reach, not what a published one already does.
              </p>
              <p>
                The one exception is the description: agents read it as they run, so rewording it
                takes effect everywhere from the next message on.
              </p>
            </HelpTip>
          </span>
        }
        actions={
          settings !== null ? (
            <Button variant="outline" onClick={openAdd}>
              <PlusIcon /> Add your own
            </Button>
          ) : null
        }
      />

      {tests.authUrl !== null && (
        // Shown while a connection waits on consent. Deliberately not opened
        // for you: a popup fired from a poll rather than a click is what
        // browsers block, and a blocked popup looks exactly like a connection
        // that has hung.
        <div className="flex items-start gap-2.5 rounded-xl border border-primary/40 bg-primary/8 px-3.5 py-3">
          <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-primary" />
          <div className="flex min-w-0 flex-col gap-1">
            <span className="text-body font-medium">This connection wants you to sign in</span>
            <span className="text-caption text-muted-foreground">
              It is waiting for you. Sign in on the page that opens, then come back. It finishes on
              its own.
            </span>
            <a
              href={tests.authUrl}
              target="_blank"
              rel="noreferrer"
              className="w-fit text-caption font-medium text-primary underline underline-offset-2"
            >
              Open the sign-in page
            </a>
          </div>
        </div>
      )}

      {loading && settings === null && (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-44 w-full rounded-xl" />
          <Skeleton className="h-44 w-full rounded-xl" />
        </div>
      )}

      {error && settings === null && !loading && (
        <Alert variant="destructive">
          <AlertTriangleIcon />
          <AlertTitle>Couldn&apos;t reach the backend</AlertTitle>
          <AlertDescription>
            <p>{error}</p>
            <Button size="sm" variant="outline" className="mt-2" onClick={() => void refresh()}>
              <PlugIcon /> Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {settings !== null && connectors.length > 0 && (
        <ConnectorsSection
          connectors={connectors}
          catalogueAgents={catalogue.catalogue?.agents ?? []}
          presets={allConnections}
          testing={tests.testing}
          onConnect={(name, options) => void tests.run(name, options)}
          onDisconnect={(name) => void tests.disconnect(name)}
          savePreset={savePreset}
          saveSecret={saveSecret}
          onSeed={async () => {
            await catalogue.seed(["connectors"]);
            await refresh();
          }}
        />
      )}

      {settings !== null && connections.length === 0 && connectors.length === 0 && (
        <EmptyState
          icon={PlugIcon}
          title="No connections yet"
          description="A connection gives your agents access to a system you already run, like a helpdesk or a document store."
          action={
            <Button onClick={openAdd}>
              <PlusIcon /> Add your first connection
            </Button>
          }
        />
      )}

      {settings !== null && connections.length > 0 && (
        <Section>
          <h2 className="text-base font-semibold">Your own connections</h2>
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Name</TableHead>
                  <TableHead className="hidden sm:table-cell">Kind</TableHead>
                  <TableHead className="hidden md:table-cell">Address</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="hidden lg:table-cell">
                    <span className="inline-flex items-center gap-1.5">
                      Tools
                      <HelpTip title="Tools" short="What the last connection found.">
                        <p>
                          The number of tools the server offered when it last answered. A dash
                          means nothing has connected to it yet.
                        </p>
                      </HelpTip>
                    </span>
                  </TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {connections.map((connection) => (
                  <ConnectionRow
                    key={connection.name}
                    connection={connection}
                    attempt={tests.results[connection.name]}
                    testing={tests.testing === connection.name}
                    onTest={() => void tests.run(connection.name)}
                    // Forced: the point of reconnecting is not to reuse what
                    // is already open.
                    onReconnect={() => void tests.run(connection.name, { force: true })}
                    onDisconnect={() => void tests.disconnect(connection.name)}
                    onEdit={() => openEdit(connection)}
                    onDelete={() => setPendingDelete(connection)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        </Section>
      )}

      {settings !== null && (
        <A2APeersSection
          peers={settings.a2a_peers}
          onSave={savePeers}
          onAddOwnAgent={(agentId) => addOwnAgentAsPeer({ agent_id: agentId })}
        />
      )}

      <ConnectionDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        connection={editing}
        onSave={savePreset}
        existingNames={connections.map((connection) => connection.name)}
        secretNames={settings?.secrets ?? []}
      />

      <Dialog open={pendingDelete !== null} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Remove this connection?</DialogTitle>
            <DialogDescription>
              <span className="font-medium">{pendingDelete?.name}</span> stops being offered when
              you build an agent. Agents already published with it keep working, because each one
              carries its own copy of what it was built with.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingDelete(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void confirmDelete()} disabled={deleting}>
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}
