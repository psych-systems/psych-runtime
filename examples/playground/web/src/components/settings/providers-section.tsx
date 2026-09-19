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
import { ProviderDialog } from "@/components/settings/provider-dialog";
import { SettingsSectionBlock } from "@/components/settings/section-nav";
import type { ProviderIn, ProviderOut, ProviderTestResult } from "@/components/settings/types";

interface ProvidersSectionProps {
  providers: ProviderOut[];
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

  function openAdd() {
    setEditing(null);
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
        providers.length > 0 ? (
          <Button size="sm" onClick={openAdd}>
            <PlusIcon /> Add provider
          </Button>
        ) : null
      }
    >
      {providers.length === 0 ? (
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
                return (
                  <TableRow key={provider.id} className="align-top">
                    <TableCell className="font-medium">
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
                        <span className="text-muted-foreground">None</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <span className="flex min-w-0 flex-col gap-1">
                        <span className="inline-flex items-center gap-1.5 text-caption whitespace-nowrap">
                          <span
                            className={cn(
                              "size-2 shrink-0 rounded-full",
                              isActive ? "bg-status-done" : "bg-muted-foreground/50",
                            )}
                            aria-hidden
                          />
                          {isActive ? "In use" : "Standing by"}
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
                        {!isActive && (
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
            </TableBody>
          </Table>
        </div>
      )}

      <ProviderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        provider={editing}
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
