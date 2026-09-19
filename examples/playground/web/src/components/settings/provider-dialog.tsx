"use client";

import { useMemo, useRef, useState } from "react";
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
import { LabelWithHelp } from "@/components/ui/help";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { FieldError, issuesByPath } from "@/components/settings/validation";
import type { ProviderIn, ProviderOut } from "@/components/settings/types";
import { ModelField } from "@/components/agents/model-field";
import type { CatalogueModel, ModelQuote } from "@/lib/types";

/**
 * A catalogue entry this dialog is opened against: the address and model are
 * already known, so all a person has to add is the key.
 *
 * Deliberately not a fake `ProviderOut`. `provider` being non-null is what
 * makes this dialog an edit -- the title, the key placeholder and the re-seed
 * guard all key on it -- and a catalogue offer is a first add, not an edit.
 */
export interface ProviderPrefill {
  id: string;
  label: string;
  base_url: string;
  model: string;
  key_url: string | null;
  /** Values the address needs, by name. Each one fills a `{name}` placeholder
   *  in the address. */
  requires: string[];
  /** Runs on the person's own machine, so it needs no key. */
  local: boolean;
  /** What the vendor serves, with list prices, from the catalogue. */
  models?: CatalogueModel[];
}

/** The catalogue's models as the combobox wants them: ids, and a quote for
 *  each one the vendor prices. */
function quotesFor(models: CatalogueModel[] | undefined): {
  ids: string[];
  prices: Record<string, ModelQuote>;
} {
  const ids: string[] = [];
  const prices: Record<string, ModelQuote> = {};
  for (const model of models ?? []) {
    ids.push(model.id);
    if (model.input !== null && model.output !== null) {
      prices[model.id] = { input: model.input, output: model.output, note: model.note };
    } else if (model.note) {
      prices[model.id] = { note: model.note };
    }
  }
  return { ids, prices };
}

interface ProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider: ProviderOut | null;
  prefill?: ProviderPrefill | null;
  /** Where a key for the provider being edited comes from, when the
   *  catalogue knows. An offer carries its own in `prefill`. */
  keyUrl?: string | null;
  /** The catalogue's models for the provider being edited, when it is one
   *  the catalogue knows. An offer carries its own in `prefill`. */
  catalogueModels?: CatalogueModel[];
  onSave: (edited: ProviderIn) => Promise<void>;
}

/** Every `{name}` the address still needs a value for, each once. */
function placeholdersIn(baseUrl: string): string[] {
  const names = new Set<string>();
  for (const match of baseUrl.matchAll(/\{([a-z0-9_]+)\}/gi)) names.add(match[1]);
  return [...names];
}

/** The address with each `{name}` replaced by what was typed for it. */
function fillPlaceholders(baseUrl: string, values: Record<string, string>): string {
  return Object.entries(values).reduce(
    (url, [name, value]) => url.split(`{${name}}`).join(value.trim()),
    baseUrl,
  );
}

export function ProviderDialog({
  open,
  onOpenChange,
  provider,
  prefill = null,
  keyUrl = null,
  catalogueModels,
  onSave,
}: ProviderDialogProps) {
  const offered = quotesFor(prefill?.models ?? catalogueModels);
  const isEdit = provider !== null;
  const addingKey = provider !== null && !provider.has_api_key;
  const getKeyUrl = prefill?.key_url ?? keyUrl;
  const [label, setLabel] = useState(provider?.label ?? prefill?.label ?? "");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? prefill?.base_url ?? "");
  const [model, setModel] = useState(provider?.model ?? prefill?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [extras, setExtras] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // Re-seed local state whenever the dialog is opened for a (possibly
  // different) provider, rather than on every render.
  const seedKey = provider?.id ?? (prefill !== null ? `catalogue:${prefill.id}` : null);
  const [seededFor, setSeededFor] = useState(seedKey);
  if (open && seededFor !== seedKey) {
    setSeededFor(seedKey);
    setLabel(provider?.label ?? prefill?.label ?? "");
    setBaseUrl(provider?.base_url ?? prefill?.base_url ?? "");
    setModel(provider?.model ?? prefill?.model ?? "");
    setApiKey("");
    setExtras({});
    setFormError(null);
    setFieldErrors({});
  }

  // Read from the address itself rather than from who opened the dialog: a
  // seeded provider carries its `{account_id}` into an edit exactly as a
  // catalogue offer does, and either way the person is asked for it here
  // rather than left to edit a URL by hand.
  const requires = useMemo(() => placeholdersIn(baseUrl), [baseUrl]);
  const missingExtra = requires.some((name) => (extras[name] ?? "").trim() === "");

  // Focus goes to the first thing left to type. Without this the dialog
  // lands on the first "?" and opens its tooltip over the form.
  const focusRef = useRef<HTMLInputElement>(null);
  const focusKey = (provider?.label ?? prefill?.label ?? "") !== "";

  async function handleSubmit() {
    setSaving(true);
    setFormError(null);
    setFieldErrors({});
    try {
      await onSave({
        id: provider?.id ?? prefill?.id,
        label: label.trim(),
        base_url: fillPlaceholders(baseUrl.trim(), extras),
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
      <DialogContent
        className="sm:max-w-md"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          focusRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>
            {addingKey
              ? `Add your ${provider.label} key`
              : isEdit
                ? "Edit provider"
                : prefill !== null
                  ? `Add ${prefill.label}`
                  : "Add a provider"}
          </DialogTitle>
          <DialogDescription>
            If nothing in use has a key yet, this one takes over as soon as it is saved.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          {formError && (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}

          <div>
            <LabelWithHelp
              htmlFor="provider-label"
              label="Name"
              help={<p>Yours, to tell this provider from the others in the list.</p>}
            />
            <Input
              id="provider-label"
              ref={focusKey ? undefined : focusRef}
              className="mt-1.5"
              placeholder="Cloudflare Workers AI"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              aria-invalid={fieldErrors.label !== undefined}
            />
            <FieldError message={fieldErrors.label} />
          </div>

          <div>
            <LabelWithHelp
              htmlFor="provider-base-url"
              label="Address"
              help={
                <p>
                  The OpenAI-compatible base URL this playground calls, usually ending in{" "}
                  <code>/v1</code>.
                </p>
              }
            />
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
            {/* The old copy said this was "the one new runs dispatch to",
                which is not true and sent people here to change what a
                published agent runs. An agent pins its own model at
                publication and keeps it. */}
            <LabelWithHelp
              htmlFor="provider-model"
              label="Default model"
              help={
                <p>
                  The model offered first when you build an agent. An agent that is already
                  published keeps running the model it was built with.
                </p>
              }
            />
            <div className="mt-1.5">
              <ModelField
                id="provider-model"
                value={model}
                onChange={setModel}
                models={offered.ids}
                prices={offered.prices}
                detail={
                  Object.values(offered.prices).some((quote) => quote.input != null)
                    ? "List prices per million tokens. Any other id can be typed."
                    : offered.ids.length > 0
                      ? "Any other id can be typed."
                      : null
                }
                placeholder="@cf/meta/llama-3.3-70b-instruct-fp8-fast"
                invalid={fieldErrors.model !== undefined}
              />
            </div>
            <FieldError message={fieldErrors.model} />
          </div>

          {requires.map((name) => (
            <div key={name}>
              <LabelWithHelp
                htmlFor={`provider-extra-${name}`}
                label={name.replace(/_/g, " ")}
                help={
                  <p>
                    This provider&apos;s address is per-account, so it goes into the address in
                    place of <code>{`{${name}}`}</code>.
                  </p>
                }
              />
              <Input
                id={`provider-extra-${name}`}
                className="mt-1.5 font-technical"
                autoComplete="off"
                value={extras[name] ?? ""}
                onChange={(e) => setExtras((current) => ({ ...current, [name]: e.target.value }))}
              />
            </div>
          ))}

          <div>
            {/* An edit that leaves this blank must send no `api_key` at all.
                Sending an empty string clears the stored key, which is how an
                unrelated edit used to lock everyone out of the provider. */}
            <LabelWithHelp
              htmlFor="provider-api-key"
              label="Key"
              help={
                isEdit && provider?.has_api_key ? (
                  <p>
                    A key is already stored. It is never sent back to this page, so type one only
                    to replace it.
                  </p>
                ) : (
                  <p>
                    Once saved, this page can only ever say whether a key is stored, never what it
                    is.
                  </p>
                )
              }
            />
            <Input
              id="provider-api-key"
              ref={focusKey ? focusRef : undefined}
              type="password"
              autoComplete="off"
              className="mt-1.5 font-technical"
              placeholder={provider?.has_api_key ? "Leave blank to keep the stored key" : "sk-..."}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              aria-invalid={fieldErrors.api_key !== undefined}
            />
            <FieldError message={fieldErrors.api_key} />
            {getKeyUrl && (
              <a
                href={getKeyUrl}
                target="_blank"
                rel="noreferrer"
                className="mt-1.5 inline-block text-caption font-medium text-primary underline underline-offset-2"
              >
                Get a key
              </a>
            )}
            {prefill?.local === true && (
              <p className="mt-1.5 text-caption text-muted-foreground">
                This one runs on your own machine and needs no key.
              </p>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={
              saving ||
              label.trim() === "" ||
              baseUrl.trim() === "" ||
              model.trim() === "" ||
              missingExtra
            }
          >
            {saving && <Loader2Icon className="animate-spin" />}
            {addingKey ? "Save key" : isEdit ? "Save changes" : "Add provider"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
