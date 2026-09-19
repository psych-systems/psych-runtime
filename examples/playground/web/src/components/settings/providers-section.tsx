"use client";

import { useState } from "react";
import {
  CheckCircle2Icon,
  Loader2Icon,
  PencilIcon,
  PlugZapIcon,
  PlusIcon,
  ServerIcon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/page";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { HelpTip } from "@/components/ui/help";
import { describeApiError } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { ProviderDialog, type ProviderPrefill } from "@/components/settings/provider-dialog";
import { SettingsSectionBlock } from "@/components/settings/section-nav";
import type { ProviderIn, ProviderOut, ProviderTestResult } from "@/components/settings/types";
import type { CatalogueProvider } from "@/lib/types";

interface ProvidersSectionProps {
  providers: ProviderOut[];
  /** Providers this console knows the address of, listed before anybody has a
   *  key for one. */
  catalogue?: CatalogueProvider[];
  activeProviderId: string | null;
  onSave: (edited: ProviderIn) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onActivate: (id: string) => Promise<void>;
  onTest: (id: string) => Promise<ProviderTestResult>;
}

/**
 * Where the models come from, as a table.
 *
 * Test results stay put once they arrive: they used to live in a dialog on a
 * tab that unmounted, so the answer to "did that key work" disappeared the
 * moment anyone looked away. Nothing here unmounts.
 */
export function ProvidersSection({
  providers,
  catalogue = [],
  activeProviderId,
  onSave,
  onDelete,
  onActivate,
  onTest,
}: ProvidersSectionProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ProviderOut | null>(null);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ProviderTestResult>>({});
  const [pendingDelete, setPendingDelete] = useState<ProviderOut | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [prefill, setPrefill] = useState<ProviderPrefill | null>(null);

  // Offers only: an entry whose key is already stored is a provider, and it is
  // already a row above with everything a row can do.
  const offers = catalogue.filter(
    (entry) => !entry.configured && !providers.some((provider) => provider.id === entry.id),
  );
  // What the catalogue knows about a provider that already exists here: a
  // seeded one keeps its "get a key" page and whether it runs locally.
  const entries = new Map(catalogue.map((entry) => [entry.id, entry] as const));

  function openAdd() {
    setEditing(null);
    setPrefill(null);
    setDialogOpen(true);
  }

  function openOffer(entry: CatalogueProvider) {
    setEditing(null);
    setPrefill({
      id: entry.id,
      label: entry.label,
      base_url: entry.base_url,
      model: entry.default_model,
      key_url: entry.key_url,
      // An address the backend already filled in asks for nothing more.
      requires: entry.resolved === true ? [] : entry.requires,
      local: entry.local === true,
      models: entry.models,
    });
    setDialogOpen(true);
  }

  async function handleActivate(provider: ProviderOut) {
    setActivatingId(provider.id);
    try {
      await onActivate(provider.id);
    } catch (err) {
      toast.error("Could not switch to this provider", { description: describeApiError(err) });
    } finally {
      setActivatingId(null);
    }
  }

  async function handleTest(provider: ProviderOut) {
    setTestingId(provider.id);
    try {
      const result = await onTest(provider.id);
      setResults((current) => ({ ...current, [provider.id]: result }));
    } catch (err) {
      setResults((current) => ({
        ...current,
        [provider.id]: { ok: false, detail: describeApiError(err) },
      }));
    } finally {
      setTestingId(null);
    }
  }

  async function confirmDelete() {
    if (pendingDelete === null) return;
    setDeleting(true);
    try {
      await onDelete(pendingDelete.id);
      setResults((current) => {
        const next = { ...current };
        delete next[pendingDelete.id];
        return next;
      });
      setPendingDelete(null);
    } catch (err) {
      toast.error("Could not remove the provider", { description: describeApiError(err) });
    } finally {
      setDeleting(false);
    }
  }

  return (
    <SettingsSectionBlock
      id="providers"
      title="Providers"
      description="Where this playground gets its models. One is in use at a time."
      actions={
        providers.length > 0 || offers.length > 0 ? (
          <Button size="sm" onClick={openAdd}>
            <PlusIcon /> Add provider
          </Button>
        ) : null
      }
    >
      {providers.length === 0 && offers.length === 0 ? (
        <EmptyState
          icon={ServerIcon}
          title="No model provider yet"
          description="Add the endpoint and key for the service that answers your agents. Nothing can run without one."
          action={
            <Button onClick={openAdd}>
              <PlusIcon /> Add a provider
            </Button>
          }
        />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead className="hidden md:table-cell">Address</TableHead>
                <TableHead className="hidden lg:table-cell">
                  <span className="inline-flex items-center gap-1.5">
                    Default model
                    <HelpTip title="Default model" short="Offered first when you build an agent.">
                      <p>
                        The model offered first when you build an agent. One already published
                        keeps running the model it was built with.
                      </p>
                    </HelpTip>
                  </span>
                </TableHead>
                <TableHead>
                  <span className="inline-flex items-center gap-1.5">
                    Key
                    <HelpTip title="Key" short="Whether a key is stored, never what it is.">
                      <p>
                        This page can only ever say whether a key is stored. The backend has no
                        endpoint that hands one back.
                      </p>
                    </HelpTip>
                  </span>
                </TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {providers.map((provider) => {
                const isActive = provider.id === activeProviderId;
                const result = results[provider.id];
                const entry = entries.get(provider.id);
                // No key stored and not one that runs locally: the row asks
                // for the key first and offers nothing that cannot work
                // without it. This is every seeded provider on a new account.
                const needsKey = !provider.has_api_key && entry?.local !== true;
                return (
                  <TableRow key={provider.id} className="align-top">
                    <TableCell className="max-w-48 font-medium md:max-w-none">
                      <span className="flex min-w-0 flex-col gap-0.5">
                        <span className="inline-flex items-center gap-1.5">
                          {provider.label}
                          {/* Not hidden: an operator needs the identifier and
                              guessing is worse. One click away, as everywhere
                              else in this console. */}
                          <HelpTip title={provider.label} short={`Identifier: ${provider.id}`}>
                            <p>
                              Identifier <code>{provider.id}</code>, default model{" "}
                              <code>{provider.model}</code>, at <code>{provider.base_url}</code>.
                            </p>
                          </HelpTip>
                        </span>
                        <span className="truncate font-technical text-micro text-muted-foreground md:hidden">
                          {provider.base_url}
                        </span>
                      </span>
                    </TableCell>
                    <TableCell className="hidden max-w-56 truncate font-technical text-caption text-muted-foreground md:table-cell">
                      {provider.base_url}
                    </TableCell>
                    <TableCell className="hidden max-w-56 truncate font-technical text-caption text-muted-foreground lg:table-cell">
                      {provider.model}
                    </TableCell>
                    <TableCell className="text-caption">
                      {provider.has_api_key ? (
                        "Stored"
                      ) : (
                        <span className="text-muted-foreground">
                          {entry?.local === true ? "Not needed" : "None"}
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <span className="flex min-w-0 flex-col gap-1">
                        <span className="inline-flex items-center gap-1.5 text-caption whitespace-nowrap">
                          <span
                            className={cn(
                              "size-2 shrink-0 rounded-full",
                              needsKey
                                ? "bg-status-waiting"
                                : isActive
                                  ? "bg-status-done"
                                  : "bg-muted-foreground/50",
                            )}
                            aria-hidden
                          />
                          {needsKey
                            ? isActive
                              ? "In use, needs a key"
                              : "Needs a key"
                            : isActive
                              ? "In use"
                              : "Standing by"}
                        </span>
                        {result && (
                          <span
                            className={cn(
                              "inline-flex max-w-56 items-start gap-1 text-micro break-words",
                              result.ok ? "text-status-done" : "text-status-failed",
                            )}
                          >
                            {result.ok ? (
                              <CheckCircle2Icon className="mt-0.5 size-3 shrink-0" aria-hidden />
                            ) : (
                              <XCircleIcon className="mt-0.5 size-3 shrink-0" aria-hidden />
                            )}
                            <span className="min-w-0">{result.detail}</span>
                          </span>
                        )}
                        {result?.models && result.models.length > 0 && (
                          <span className="inline-flex items-center gap-1 text-micro text-muted-foreground">
                            {result.models.length} models offered
                            <HelpTip title="Models it reported" short="What the provider listed.">
                              <p className="font-technical break-words">
                                {result.models.join(", ")}
                              </p>
                            </HelpTip>
                          </span>
                        )}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className="flex flex-wrap items-center justify-end gap-1">
                        {needsKey && (
                          <Button
                            size="xs"
                            onClick={() => {
                              setPrefill(null);
                              setEditing(provider);
                              setDialogOpen(true);
                            }}
                          >
                            <PlusIcon /> Add key
                          </Button>
                        )}
                        {needsKey && entry?.key_url && (
                          <Button size="xs" variant="ghost" asChild>
                            <a href={entry.key_url} target="_blank" rel="noreferrer">
                              Get a key
                            </a>
                          </Button>
                        )}
                        {!isActive && !needsKey && (
                          <Button
                            size="xs"
                            variant="outline"
                            onClick={() => void handleActivate(provider)}
                            disabled={activatingId === provider.id}
                          >
                            {activatingId === provider.id && (
                              <Loader2Icon className="animate-spin" />
                            )}
                            Use
                          </Button>
                        )}
                        <Button
                          size="xs"
                          variant="ghost"
                          onClick={() => void handleTest(provider)}
                          disabled={testingId === provider.id}
                        >
                          {testingId === provider.id ? (
                            <Loader2Icon className="animate-spin" />
                          ) : (
                            <PlugZapIcon />
                          )}
                          Test
                        </Button>
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          aria-label={`Edit ${provider.label}`}
                          onClick={() => {
                            setEditing(provider);
                            setDialogOpen(true);
                          }}
                        >
                          <PencilIcon />
                        </Button>
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          aria-label={`Remove ${provider.label}`}
                          onClick={() => setPendingDelete(provider)}
                        >
                          <Trash2Icon />
                        </Button>
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}

              {/* Offers: an address and a default model this console already
                  knows, waiting on a key. The row is the same shape as a real
                  provider so the table reads as one list rather than two. */}
              {offers.map((entry) => (
                <TableRow key={`catalogue:${entry.id}`} className="align-top">
                  <TableCell className="max-w-48 font-medium md:max-w-none">
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="inline-flex items-center gap-1.5">
                        {entry.label}
                        <HelpTip title={entry.label} short={`Identifier: ${entry.id}`}>
                          <p>
                            Identifier <code>{entry.id}</code>, default model{" "}
                            <code>{entry.default_model}</code>, at <code>{entry.base_url}</code>.
                          </p>
                          {entry.local === true && <p>It runs on your own machine.</p>}
                        </HelpTip>
                      </span>
                      <span className="truncate font-technical text-micro text-muted-foreground md:hidden">
                        {entry.base_url}
                      </span>
                    </span>
                  </TableCell>
                  <TableCell className="hidden max-w-56 truncate font-technical text-caption text-muted-foreground md:table-cell">
                    {entry.base_url}
                  </TableCell>
                  <TableCell className="hidden max-w-56 truncate font-technical text-caption text-muted-foreground lg:table-cell">
                    {entry.default_model}
                  </TableCell>
                  <TableCell className="text-caption text-muted-foreground">
                    {entry.local === true ? "Not needed" : "None"}
                  </TableCell>
                  <TableCell>
                    <span className="inline-flex items-center gap-1.5 text-caption whitespace-nowrap text-muted-foreground">
                      <span className="size-2 shrink-0 rounded-full bg-muted-foreground/50" aria-hidden />
                      Not set up
                    </span>
                  </TableCell>
                  <TableCell>
                    <span className="flex flex-wrap items-center justify-end gap-1">
                      <Button size="xs" onClick={() => openOffer(entry)}>
                        <PlusIcon /> {entry.local === true ? "Add it" : "Add key"}
                      </Button>
                      {entry.key_url && (
                        <Button size="xs" variant="ghost" asChild>
                          <a href={entry.key_url} target="_blank" rel="noreferrer">
                            Get a key
                          </a>
                        </Button>
                      )}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <ProviderDialog
        open={dialogOpen}
        onOpenChange={(next) => {
          setDialogOpen(next);
          // Cleared on close so reopening the same offer starts empty rather
          // than with half a key somebody decided against.
          if (!next) setPrefill(null);
        }}
        provider={editing}
        prefill={prefill}
        keyUrl={editing !== null ? (entries.get(editing.id)?.key_url ?? null) : null}
        catalogueModels={editing !== null ? entries.get(editing.id)?.models : undefined}
        onSave={onSave}
      />

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Remove this provider?</DialogTitle>
            <DialogDescription>
              <span className="font-medium">{pendingDelete?.label}</span> and its stored key are
              forgotten. If it is the one in use, pick another before sending a message.
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
    </SettingsSectionBlock>
  );
}
