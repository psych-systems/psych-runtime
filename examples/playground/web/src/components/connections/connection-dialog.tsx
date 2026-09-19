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
import { LabelWithHelp } from "@/components/ui/help";
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
import type {
  McpGrant,
  McpServerPreset,
  McpServerPresetIn,
  McpTransport,
} from "@/components/settings/types";

interface ConnectionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The connection being edited, or null to add one. */
  connection: McpServerPreset | null;
  onSave: (edited: McpServerPresetIn, previousName: string | null) => Promise<void>;
  /** Every connection's name, so this form can refuse a duplicate. */
  existingNames: string[];
  /** Names of the secrets this backend holds, offered wherever a credential
   *  is named. */
  secretNames: string[];
}

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;
type Authentication = "none" | "bearer" | "oauth";

function initialAuthentication(connection: McpServerPreset | null): Authentication {
  if (connection?.oauth != null) return "oauth";
  if (connection?.credential) return "bearer";
  return "none";
}

/**
 * Fields and a "?" per field. Every paragraph this form used to carry is now
 * behind the help beside the label it explained, so the form is a form.
 */
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
  const [authentication, setAuthentication] = useState<Authentication>(
    initialAuthentication(connection)
  );
  const [grant, setGrant] = useState<McpGrant>(connection?.oauth?.grant ?? "client_credentials");
  const [clientId, setClientId] = useState(connection?.oauth?.preregistered_client_id ?? "");
  const [clientSecretCredential, setClientSecretCredential] = useState(
    connection?.oauth?.client_secret_credential ?? ""
  );
  const [issuer, setIssuer] = useState(connection?.oauth?.issuer ?? "");

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
    setAuthentication(initialAuthentication(connection));
    setGrant(connection?.oauth?.grant ?? "client_credentials");
    setClientId(connection?.oauth?.preregistered_client_id ?? "");
    setClientSecretCredential(connection?.oauth?.client_secret_credential ?? "");
    setIssuer(connection?.oauth?.issuer ?? "");
    setFormError(null);
    setFieldErrors({});
  }

  const trimmedName = name.trim();
  const nameValid = trimmedName === "" || NAME_PATTERN.test(trimmedName);
  // The stored list is keyed by name, and it used to accept two connections
  // under the same one: deleting either then removed both, and a test ran
  // against whichever the backend happened to reach first.
  const nameTaken =
    trimmedName !== "" && trimmedName !== connection?.name && existingNames.includes(trimmedName);

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
        credential:
          authentication === "bearer" && credential.trim() !== "" ? credential.trim() : null,
        allow: allow
          .split(",")
          .map((entry) => entry.trim())
          .filter((entry) => entry.length > 0),
        optional,
        preload: preloadValue(preload),
        oauth:
          authentication === "oauth"
            ? {
                grant,
                preregistered_client_id: clientId.trim() === "" ? null : clientId.trim(),
                client_secret_credential:
                  clientSecretCredential.trim() === "" ? null : clientSecretCredential.trim(),
                issuer: issuer.trim() === "" ? null : issuer.trim(),
              }
            : null,
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
      <DialogContent className="flex max-h-[min(90vh,760px)] flex-col sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit connection" : "Add a connection"}</DialogTitle>
          <DialogDescription>
            Saving stores the details. Testing is what connects.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-0.5 py-1 pr-2">
          {formError && (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <LabelWithHelp
                htmlFor="connection-name"
                label="Name"
                help={
                  <p>
                    Yours, and unique. Two connections under one name would collide: deleting
                    either would remove both.
                  </p>
                }
              />
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
              <LabelWithHelp
                htmlFor="connection-transport"
                label="How it talks"
                help={
                  <p>
                    Streaming HTTP is the current MCP transport. Server-sent events is what older
                    servers speak.
                  </p>
                }
              />
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
            <LabelWithHelp
              htmlFor="connection-url"
              label="Address"
              help={<p>The MCP endpoint this playground connects to.</p>}
            />
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
              {/* The bug this field exists to fix: an agent wired to three
                  connections was shown three names, and a name is not an
                  answer to "which of these should I use". This sentence
                  reaches the agent when it runs. */}
              <LabelWithHelp
                htmlFor="connection-description"
                label="What this system is for"
                help={
                  <>
                    <p>
                      Told to the agent so it knows what this system is and when to reach for it.
                      Leave it empty and the agent hears whatever the server says about itself,
                      which is often nothing.
                    </p>
                    <p>
                      Rewording is free. It is not part of a published agent, so improving it
                      changes what every agent hears from the next message on.
                    </p>
                  </>
                }
              />
              <span className="rounded-full bg-surface px-1.5 py-0.5 text-micro font-medium text-surface-foreground">
                Recommended
              </span>
            </div>
            <Textarea
              id="connection-description"
              className="mt-1.5 min-h-20"
              placeholder="Our customer records. Reach for it to look up an account, its plan and its open tickets."
              maxLength={DESCRIPTION_LIMIT}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
            <p className="tabular mt-1 text-right text-caption text-muted-foreground">
              {description.length} / {DESCRIPTION_LIMIT}
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <LabelWithHelp
                htmlFor="connection-allow"
                label="Tools to allow"
                help={
                  <p>
                    Tool names separated by commas. Empty allows everything this connection offers.
                  </p>
                }
              />
              <Input
                id="connection-allow"
                className="mt-1.5 font-technical"
                placeholder="search_tickets, create_ticket"
                value={allow}
                onChange={(event) => setAllow(event.target.value)}
              />
            </div>

            <div>
              <LabelWithHelp
                htmlFor="connection-preload"
                label="Tools in the prompt"
                help={
                  <>
                    {PRELOAD_OPTIONS.map((option) => (
                      <p key={option.value}>
                        <span className="font-medium text-foreground">{option.label}:</span>{" "}
                        {option.hint}
                      </p>
                    ))}
                  </>
                }
              />
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
            </div>
          </div>

          <div>
            <LabelWithHelp
              htmlFor="connection-authentication"
              label="Authentication"
              help={<p>How this server expects the playground to identify itself.</p>}
            />
            <Select
              value={authentication}
              onValueChange={(next) => setAuthentication(next as Authentication)}
            >
              <SelectTrigger id="connection-authentication" className="mt-1.5 w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">No authentication</SelectItem>
                <SelectItem value="bearer">Access token</SelectItem>
                <SelectItem value="oauth">OAuth</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {authentication === "bearer" && (
            <div className="rounded-xl border border-border bg-surface/35 p-4">
              <LabelWithHelp
                htmlFor="connection-credential"
                label="Access token credential"
                help={
                  <p>
                    A name that gets looked up when the connection opens, not the secret itself.
                  </p>
                }
              />
              <SecretSelect
                id="connection-credential"
                value={credential}
                onChange={setCredential}
                secretNames={secretNames}
              />
              <CredentialNameNote />
            </div>
          )}

          {authentication === "oauth" && (
            <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface/35 p-4">
              <div>
                <LabelWithHelp
                  htmlFor="connection-grant"
                  label="Sign-in style"
                  help={
                    <p>
                      A browser sign-in asks a person once and waits for them; an app sign-in uses
                      a client id and secret with nobody present.
                    </p>
                  }
                />
                <Select value={grant} onValueChange={(next) => setGrant(next as McpGrant)}>
                  <SelectTrigger id="connection-grant" className="mt-1.5 w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="client_credentials">This app signs in on its own</SelectItem>
                    <SelectItem value="authorization_code">
                      A person signs in through a browser
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <LabelWithHelp
                  htmlFor="connection-client-id"
                  label="Client id"
                  help={
                    <p>
                      Public, not a secret. Leave it empty if the server registers this app for
                      itself.
                    </p>
                  }
                />
                <Input
                  id="connection-client-id"
                  className="mt-1.5 font-technical"
                  placeholder="Public, not a secret"
                  value={clientId}
                  onChange={(event) => setClientId(event.target.value)}
                />
              </div>
              <div>
                <LabelWithHelp
                  htmlFor="connection-client-secret"
                  label="Client secret"
                  help={<p>The name of a stored secret, looked up when the connection opens.</p>}
                />
                <SecretSelect
                  id="connection-client-secret"
                  value={clientSecretCredential}
                  onChange={setClientSecretCredential}
                  secretNames={secretNames}
                />
                <CredentialNameNote />
              </div>
              {grant === "client_credentials" && (
                <div>
                  <LabelWithHelp
                    htmlFor="connection-oauth-issuer"
                    label="Authorization server"
                    help={
                      <p>
                        The issuer that registered this client. Credentials are sent only to this
                        authorization server.
                      </p>
                    }
                  />
                  <Input
                    id="connection-oauth-issuer"
                    className="mt-1.5 font-technical"
                    placeholder="https://auth.example.com"
                    value={issuer}
                    onChange={(event) => setIssuer(event.target.value)}
                  />
                </div>
              )}
            </div>
          )}

          <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-3 py-2">
            <LabelWithHelp
              htmlFor="connection-optional"
              label="Keep going without it"
              help={
                <p>
                  If it is unreachable when a message arrives, the agent carries on without its
                  tools instead of failing.
                </p>
              }
            />
            <Switch id="connection-optional" checked={optional} onCheckedChange={setOptional} />
          </div>
        </div>

        <DialogFooter className="border-t border-border pt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            disabled={saving || trimmedName === "" || url.trim() === "" || !nameValid || nameTaken}
          >
            {saving && <Loader2Icon className="animate-spin" />}
            {isEdit ? "Save changes" : "Add connection"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
