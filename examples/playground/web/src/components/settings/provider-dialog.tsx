"use client";

import { useState } from "react";
import { Loader2Icon } from "lucide-react";

import { ApiError } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { FieldError, issuesByPath } from "@/components/settings/validation";
import type { ProviderIn, ProviderOut } from "@/components/settings/types";

interface ProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider: ProviderOut | null;
  onSave: (edited: ProviderIn) => Promise<void>;
}

export function ProviderDialog({ open, onOpenChange, provider, onSave }: ProviderDialogProps) {
  const isEdit = provider !== null;
  const [label, setLabel] = useState(provider?.label ?? "");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? "");
  const [model, setModel] = useState(provider?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // Re-seed local state whenever the dialog is opened for a (possibly
  // different) provider, rather than on every render.
  const [seededFor, setSeededFor] = useState(provider?.id ?? null);
  if (open && seededFor !== (provider?.id ?? null)) {
    setSeededFor(provider?.id ?? null);
    setLabel(provider?.label ?? "");
    setBaseUrl(provider?.base_url ?? "");
    setModel(provider?.model ?? "");
    setApiKey("");
    setFormError(null);
    setFieldErrors({});
  }

  async function handleSubmit() {
    setSaving(true);
    setFormError(null);
    setFieldErrors({});
    try {
      await onSave({
        id: provider?.id,
        label: label.trim(),
        base_url: baseUrl.trim(),
        model: model.trim(),
        ...(apiKey.length > 0 ? { api_key: apiKey } : {}),
      });
      onOpenChange(false);
    } catch (err) {
      if (err instanceof ApiError && err.issues.length > 0) {
        setFieldErrors(issuesByPath(err.issues));
        setFormError(err.message);
      } else {
        setFormError(describeApiError(err));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit provider" : "Add a provider"}</DialogTitle>
          <DialogDescription>
            One service that answers your agents. Saving it does not switch to it, that is a
            separate step.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          {formError && (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}

          <div>
            <Label htmlFor="provider-label">Name</Label>
            <Input
              id="provider-label"
              className="mt-1.5"
              placeholder="Cloudflare Workers AI"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              aria-invalid={fieldErrors.label !== undefined}
            />
            <FieldError message={fieldErrors.label} />
          </div>

          <div>
            <Label htmlFor="provider-base-url">Address</Label>
            <Input
              id="provider-base-url"
              className="mt-1.5 font-technical"
              placeholder="https://api.example.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              aria-invalid={fieldErrors.base_url !== undefined}
            />
            <FieldError message={fieldErrors.base_url} />
          </div>

          <div>
            <Label htmlFor="provider-model">Default model</Label>
            <Input
              id="provider-model"
              className="mt-1.5 font-technical"
              placeholder="@cf/meta/llama-3.3-70b-instruct-fp8-fast"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              aria-invalid={fieldErrors.model !== undefined}
            />
            <FieldError message={fieldErrors.model} />
            {/* The old copy said this was "the one new runs dispatch to",
                which is not true and sent people here to change what a
                published agent runs. An agent pins its own model at
                publication and keeps it. */}
            <p className="mt-1 text-caption text-muted-foreground">
              The model offered first when you build an agent. An agent that is already published
              keeps running the model it was built with.
            </p>
          </div>

          <div>
            <Label htmlFor="provider-api-key">Key</Label>
            <Input
              id="provider-api-key"
              type="password"
              autoComplete="off"
              className="mt-1.5 font-technical"
              placeholder={isEdit ? "Leave blank to keep the stored key" : "sk-..."}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              aria-invalid={fieldErrors.api_key !== undefined}
            />
            <FieldError message={fieldErrors.api_key} />
            {/* An edit that leaves this blank must send no `api_key` at all.
                Sending an empty string clears the stored key, which is how an
                unrelated edit used to lock everyone out of the provider. */}
            <p className="mt-1 text-caption text-muted-foreground">
              {isEdit && provider?.has_api_key
                ? "A key is already stored. It is never sent back to this page, so type one only to replace it."
                : "Once saved, this page can only ever say whether a key is stored, never what it is."}
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={saving || label.trim() === "" || baseUrl.trim() === "" || model.trim() === ""}
          >
            {saving && <Loader2Icon className="animate-spin" />}
            {isEdit ? "Save changes" : "Add provider"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
