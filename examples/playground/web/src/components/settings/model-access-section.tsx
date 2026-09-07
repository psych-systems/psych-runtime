"use client";

import { useState } from "react";
import {
  CheckCircle2Icon,
  KeyIcon,
  Loader2Icon,
  PencilIcon,
  PlugZapIcon,
  PlusIcon,
  ServerIcon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DetailRow, EmptyState, Section, TechnicalDetails } from "@/components/ui/page";
import { describeApiError } from "@/lib/errors";
import { cn } from "@/lib/utils";
import { ProviderDialog } from "@/components/settings/provider-dialog";
import type { ProviderIn, ProviderOut, ProviderTestResult } from "@/components/settings/types";

interface ModelAccessSectionProps {
  providers: ProviderOut[];
  activeProviderId: string | null;
  onSave: (edited: ProviderIn) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onActivate: (id: string) => Promise<void>;
  onTest: (id: string) => Promise<ProviderTestResult>;
}

/**
 * Where the models come from.
 *
 * Test results are kept right here, next to the thing tested, and they stay
 * put: they used to live in a dialog on a tab that unmounted, so the answer to
 * "did that key work" disappeared the moment anyone looked away.
 */
export function ModelAccessSection({
  providers,
  activeProviderId,
  onSave,
  onDelete,
  onActivate,
  onTest,
}: ModelAccessSectionProps) {
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
    <Section
      title="Model access"
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
        <div className="flex flex-col gap-2">
          {providers.map((provider) => {
            const isActive = provider.id === activeProviderId;
            const result = results[provider.id];
            return (
              <div
                key={provider.id}
                className="flex flex-col gap-3 rounded-xl border border-border bg-card px-4 py-3.5"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-col gap-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{provider.label}</span>
                      {isActive && (
                        <Badge className="bg-status-done/15 text-status-done" variant="outline">
                          <CheckCircle2Icon /> In use
                        </Badge>
                      )}
                      {provider.has_api_key ? (
                        <Badge variant="secondary">
                          <KeyIcon /> Key stored
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="text-muted-foreground">
                          No key
                        </Badge>
                      )}
                    </div>
                    <span className="truncate text-caption text-muted-foreground">
                      {provider.base_url}
                    </span>
                    <span className="text-caption text-muted-foreground">
                      Offers <span className="text-foreground">{provider.model}</span> by default
                      when you build an agent.
                    </span>
                  </div>

                  <div className="flex shrink-0 items-center gap-1">
                    {!isActive && (
                      <Button
                        size="xs"
                        variant="outline"
                        onClick={() => void handleActivate(provider)}
                        disabled={activatingId === provider.id}
                      >
                        {activatingId === provider.id && <Loader2Icon className="animate-spin" />}
                        Use this one
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
                      size="xs"
                      variant="ghost"
                      onClick={() => {
                        setEditing(provider);
                        setDialogOpen(true);
                      }}
                    >
                      <PencilIcon /> Edit
                    </Button>
                    <Button
                      size="icon-xs"
                      variant="ghost"
                      aria-label={`Remove ${provider.label}`}
                      onClick={() => setPendingDelete(provider)}
                    >
                      <Trash2Icon />
                    </Button>
                  </div>
                </div>

                {result && (
                  <div
                    className={cn(
                      "flex items-start gap-2 rounded-md border px-3 py-2 text-caption",
                      result.ok
                        ? "border-status-done/30 bg-status-done/8 text-status-done"
                        : "border-status-failed/30 bg-status-failed/8 text-status-failed"
                    )}
                  >
                    {result.ok ? (
                      <CheckCircle2Icon className="mt-0.5 size-3.5 shrink-0" />
                    ) : (
                      <XCircleIcon className="mt-0.5 size-3.5 shrink-0" />
                    )}
                    <span className="min-w-0 break-words">{result.detail}</span>
                  </div>
                )}

                <TechnicalDetails>
                  <DetailRow label="Base URL">
                    <span className="font-technical">{provider.base_url}</span>
                  </DetailRow>
                  <DetailRow label="Default model">
                    <span className="font-technical">{provider.model}</span>
                  </DetailRow>
                  <DetailRow label="Identifier">
                    <span className="font-technical">{provider.id}</span>
                  </DetailRow>
                  {result?.models && result.models.length > 0 && (
                    <DetailRow label="Models it reported">
                      <span className="font-technical">{result.models.join(", ")}</span>
                    </DetailRow>
                  )}
                </TechnicalDetails>
              </div>
            );
          })}
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
    </Section>
  );
}
