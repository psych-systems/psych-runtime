"use client";

import { useState } from "react";
import { Loader2Icon } from "lucide-react";

import { ApiError } from "@/lib/api";
import { describeApiError } from "@/lib/errors";
import { Alert, AlertDescription } from "@/components/ui/alert";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { FieldError, issuesByPath } from "@/components/settings/validation";
import { CredentialNameNote, SecretSelect } from "@/components/connections/secret-select";
import {
  DESCRIPTION_LIMIT,
  PRELOAD_OPTIONS,
  describedAs,
  preloadChoice,
  preloadValue,
  type ConnectionPreset,
  type PreloadChoice,
} from "@/components/connections/connection-state";
import type { McpGrant, McpServerPreset, McpTransport } from "@/components/settings/types";

interface ConnectionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The connection being edited, or null to add one. */
  connection: McpServerPreset | null;
  onSave: (edited: McpServerPreset, previousName: string | null) => Promise<void>;
  /** Every connection's name, so this form can refuse a duplicate. */
  existingNames: string[];
  /** Names of the secrets this backend holds, offered wherever a credential
   *  is named. */
  secretNames: string[];
}

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

export function ConnectionDialog({
  open,
  onOpenChange,
  connection,
  onSave,
  existingNames,
  secretNames,
}: ConnectionDialogProps) {
  const isEdit = connection !== null;

  const [name, setName] = useState(connection?.name ?? "");
  const [url, setUrl] = useState(connection?.url ?? "");
  const [description, setDescription] = useState(connection ? describedAs(connection) : "");
  const [transport, setTransport] = useState<McpTransport>(connection?.transport ?? "http");
  const [credential, setCredential] = useState(connection?.credential ?? "");
  const [allow, setAllow] = useState((connection?.allow ?? []).join(", "));
  const [optional, setOptional] = useState(connection?.optional ?? false);
  const [preload, setPreload] = useState<PreloadChoice>(preloadChoice(connection?.preload));
  const [useOAuth, setUseOAuth] = useState(connection?.oauth != null);
  const [grant, setGrant] = useState<McpGrant>(connection?.oauth?.grant ?? "client_credentials");
  const [clientId, setClientId] = useState(connection?.oauth?.preregistered_client_id ?? "");
  const [clientSecretCredential, setClientSecretCredential] = useState(
    connection?.oauth?.client_secret_credential ?? ""
  );

  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // Re-seed when the dialog opens for a different connection, rather than on
  // every render.
  const [seededFor, setSeededFor] = useState(connection?.name ?? null);
  if (open && seededFor !== (connection?.name ?? null)) {
    setSeededFor(connection?.name ?? null);
    setName(connection?.name ?? "");
    setUrl(connection?.url ?? "");
    setDescription(connection ? describedAs(connection) : "");
    setTransport(connection?.transport ?? "http");
    setCredential(connection?.credential ?? "");
    setAllow((connection?.allow ?? []).join(", "));
    setOptional(connection?.optional ?? false);
    setPreload(preloadChoice(connection?.preload));
    setUseOAuth(connection?.oauth != null);
    setGrant(connection?.oauth?.grant ?? "client_credentials");
    setClientId(connection?.oauth?.preregistered_client_id ?? "");
    setClientSecretCredential(connection?.oauth?.client_secret_credential ?? "");
    setFormError(null);
    setFieldErrors({});
  }

  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NAME_PATTERN.test(trimmedName);
  // The stored list is keyed by name, and it used to accept two connections
  // under the same one: deleting either then removed both, and a test ran
  // against whichever the backend happened to reach first.
  const nameTaken =
    trimmedName !== "" &&
    trimmedName !== connection?.name &&
    existingNames.includes(trimmedName);

  async function handleSubmit() {
    setSaving(true);
    setFormError(null);
    setFieldErrors({});
    try {
      const edited: ConnectionPreset = {
        name: trimmedName,
        url: url.trim(),
        description: description.trim(),
        transport,
        credential: credential.trim() === "" ? null : credential.trim(),
        allow: allow
          .split(",")
          .map((entry) => entry.trim())
          .filter((entry) => entry.length > 0),
        optional,
        preload: preloadValue(preload),
        oauth: useOAuth
          ? {
              grant,
              preregistered_client_id: clientId.trim() === "" ? null : clientId.trim(),
              client_secret_credential:
                clientSecretCredential.trim() === "" ? null : clientSecretCredential.trim(),
            }
          : null,
        // Carried through untouched: the record of the last real connection is
        // the backend's to write, and an edit to the allow-list is not a
        // reason to forget that this server once worked.
        last_connection: connection?.last_connection ?? null,
      };
      await onSave(edited, connection?.name ?? null);
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
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit connection" : "Add a connection"}</DialogTitle>
          <DialogDescription>
            A system your agents can reach. Saving it stores the details, it does not connect.
            Use Test connection for that.
          </DialogDescription>
        </DialogHeader>

        <div className="flex max-h-[65vh] flex-col gap-3 overflow-y-auto pr-1">
          {formError && (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="connection-name">Name</Label>
              <Input
                id="connection-name"
                className="mt-1.5 font-technical"
                placeholder="crm"
                value={name}
                onChange={(event) => setName(event.target.value)}
                aria-invalid={fieldErrors.name !== undefined || !nameValid || nameTaken}
              />
              {nameTaken ? (
                <FieldError message="Another connection already uses this name." />
              ) : (
                !nameValid && (
                  <FieldError message="Letters, digits, dots, dashes and underscores, starting with a letter." />
                )
              )}
              <FieldError message={fieldErrors.name} />
            </div>
            <div>
              <Label htmlFor="connection-transport">How it talks</Label>
              <Select value={transport} onValueChange={(next) => setTransport(next as McpTransport)}>
                <SelectTrigger id="connection-transport" className="mt-1.5 w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="http">Streaming HTTP</SelectItem>
                  <SelectItem value="sse">Server-sent events (older servers)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div>
            <Label htmlFor="connection-url">Address</Label>
            <Input
              id="connection-url"
              className="mt-1.5 font-technical"
              placeholder="https://mcp.example.com"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              aria-invalid={fieldErrors.url !== undefined}
            />
            <FieldError message={fieldErrors.url} />
          </div>

          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor="connection-description">What this system is for</Label>
              <span className="rounded-full bg-surface px-1.5 py-0.5 text-micro font-medium text-surface-foreground">
                Recommended
              </span>
            </div>
            {/* The bug this field exists to fix: an agent wired to three
                connections was shown three names, and a name is not an answer
                to "which of these should I use". This sentence reaches the
                agent when it runs. */}
            <Textarea
              id="connection-description"
              className="mt-1.5 min-h-20"
              placeholder="Our customer records. Reach for it to look up an account, its plan and its open tickets."
              maxLength={DESCRIPTION_LIMIT}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              aria-describedby="connection-description-help"
            />
            <div className="mt-1 flex flex-wrap items-start justify-between gap-2">
              <p id="connection-description-help" className="text-caption text-muted-foreground">
                Told to the agent so it knows what this system is and when to reach for it. A
                sentence or two, up to {DESCRIPTION_LIMIT} characters. Leave it empty and the
                agent hears whatever the server says about itself, which is often nothing.
              </p>
              <span className="tabular shrink-0 text-caption text-muted-foreground">
                {description.length} / {DESCRIPTION_LIMIT}
              </span>
            </div>
            <p className="mt-1 text-caption text-muted-foreground">
              Rewording this is free. It is not part of a published agent, so improving it changes
              what every agent hears from the next message on, and republishes nothing.
            </p>
          </div>

          <div>
            <Label htmlFor="connection-allow">Tools to allow</Label>
            <Input
              id="connection-allow"
              className="mt-1.5 font-technical"
              placeholder="search_tickets, create_ticket"
              value={allow}
              onChange={(event) => setAllow(event.target.value)}
            />
            <p className="mt-1 text-caption text-muted-foreground">
              Separate names with commas. Leave it empty to allow everything this connection
              offers.
            </p>
          </div>

          <div>
            <Label htmlFor="connection-preload">Tools in the prompt</Label>
            <Select value={preload} onValueChange={(next) => setPreload(next as PreloadChoice)}>
              <SelectTrigger id="connection-preload" className="mt-1.5 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PRELOAD_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="mt-1 text-caption text-muted-foreground">
              {PRELOAD_OPTIONS.find((option) => option.value === preload)?.hint}
            </p>
          </div>

          <div>
            <Label htmlFor="connection-credential">Credential</Label>
            <SecretSelect
              id="connection-credential"
              value={credential}
              onChange={setCredential}
              secretNames={secretNames}
            />
            <CredentialNameNote />
          </div>

          <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-2">
            <div>
              <p className="text-body font-medium">Keep going without it</p>
              <p className="text-caption text-muted-foreground">
                If it is unreachable when a message arrives, the agent carries on without its
                tools instead of failing.
              </p>
            </div>
            <Switch checked={optional} onCheckedChange={setOptional} />
          </div>

          <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-2">
            <div>
              <p className="text-body font-medium">Sign in with OAuth</p>
              <p className="text-caption text-muted-foreground">
                Turn this on when the server issues its own tokens.
              </p>
            </div>
            <Switch checked={useOAuth} onCheckedChange={setUseOAuth} />
          </div>

          {useOAuth && (
            <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
              <div>
                <Label htmlFor="connection-grant">Sign-in style</Label>
                <Select value={grant} onValueChange={(next) => setGrant(next as McpGrant)}>
                  <SelectTrigger id="connection-grant" className="mt-1.5 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="client_credentials">
                      This app signs in on its own
                    </SelectItem>
                    <SelectItem value="authorization_code">
                      A person signs in through a browser
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label htmlFor="connection-client-id">Client id</Label>
                <Input
                  id="connection-client-id"
                  className="mt-1.5 font-technical"
                  placeholder="Public, not a secret"
                  value={clientId}
                  onChange={(event) => setClientId(event.target.value)}
                />
                <p className="mt-1 text-caption text-muted-foreground">
                  Leave it empty if the server registers this app for itself.
                </p>
              </div>
              <div>
                <Label htmlFor="connection-client-secret">Client secret</Label>
                <SecretSelect
                  id="connection-client-secret"
                  value={clientSecretCredential}
                  onChange={setClientSecretCredential}
                  secretNames={secretNames}
                />
                <CredentialNameNote />
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={
              saving || trimmedName === "" || url.trim() === "" || !nameValid || nameTaken
            }
          >
            {saving && <Loader2Icon className="animate-spin" />}
            {isEdit ? "Save changes" : "Add connection"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
