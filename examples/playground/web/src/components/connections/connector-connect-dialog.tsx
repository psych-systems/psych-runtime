"use client";

import { useState } from "react";
import { Loader2Icon } from "lucide-react";

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
import { describeApiError } from "@/lib/errors";
import { credentialName, tokenAllowed } from "@/components/connections/connector-state";
import type { CatalogueConnector } from "@/lib/types";

/** What the dialog collected, for the section to act on. */
export type ConnectChoice =
  | { kind: "client_id"; clientId: string }
  | { kind: "token"; secretName: string; secretValue: string };

interface ConnectorConnectDialogProps {
  connector: CatalogueConnector | null;
  onOpenChange: (open: boolean) => void;
  onConnect: (choice: ConnectChoice) => Promise<void>;
}

/**
 * What a connector needs before anything can be attempted.
 *
 * Two shapes, because there are two reasons a connect cannot just start. A
 * connector that takes an API key needs the key. A connector that signs people
 * in but will not register this console on demand needs either a client id
 * somebody registered with it, or a token used instead of signing in -- both
 * paths in one dialog, because from here they are the same decision: how this
 * console is allowed to prove who it is.
 */
export function ConnectorConnectDialog({
  connector,
  onOpenChange,
  onConnect,
}: ConnectorConnectDialogProps) {
  const [path, setPath] = useState<"sign_in" | "token">("sign_in");
  const [clientId, setClientId] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Re-seed when the dialog opens for a different connector, rather than on
  // every render.
  const [seededFor, setSeededFor] = useState<string | null>(null);
  if (connector !== null && seededFor !== connector.name) {
    setSeededFor(connector.name);
    setPath(connector.auth.kind === "oauth" ? "sign_in" : "token");
    setClientId("");
    setSecretValue("");
    setFormError(null);
  }

  if (connector === null) return null;

  const isOAuth = connector.auth.kind === "oauth";
  const offersBoth = isOAuth && tokenAllowed(connector);
  const secretName = credentialName(connector);
  const hint = connector.auth.credential_hint ?? null;

  const ready =
    path === "sign_in" ? clientId.trim().length > 0 : secretValue.trim().length > 0;

  async function submit() {
    if (connector === null) return;
    setBusy(true);
    setFormError(null);
    try {
      await onConnect(
        path === "sign_in"
          ? { kind: "client_id", clientId: clientId.trim() }
          : { kind: "token", secretName, secretValue },
      );
      onOpenChange(false);
    } catch (err) {
      setFormError(describeApiError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onOpenChange(false)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Connect {connector.label}</DialogTitle>
          <DialogDescription>
            {isOAuth
              ? "This one does not hand out its own credentials, so it needs one of these before it can sign you in."
              : "Its key is stored under a name and looked up when the connection opens."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          {formError && (
            <Alert variant="destructive">
              <AlertDescription>{formError}</AlertDescription>
            </Alert>
          )}

          {offersBoth && (
            <div
              role="radiogroup"
              aria-label="How to connect"
              className="flex flex-wrap gap-2"
            >
              {(
                [
                  ["sign_in", "Sign in"],
                  ["token", "Use a token"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={path === value}
                  onClick={() => setPath(value)}
                  className={
                    path === value
                      ? "rounded-full border border-primary bg-primary/10 px-3 py-1 text-caption font-medium focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                      : "rounded-full border border-border px-3 py-1 text-caption text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                  }
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          {path === "sign_in" ? (
            <div>
              <LabelWithHelp
                htmlFor="connector-client-id"
                label="Client id"
                help={
                  <>
                    <p>
                      The identifier this system gave you when you registered this console with
                      it. It is not a secret and it is not your account name.
                    </p>
                    {connector.docs_url && (
                      <p>
                        Its own instructions are at <code>{connector.docs_url}</code>.
                      </p>
                    )}
                  </>
                }
              />
              <Input
                id="connector-client-id"
                className="mt-1.5 font-technical"
                autoComplete="off"
                value={clientId}
                onChange={(event) => setClientId(event.target.value)}
              />
              {connector.docs_url && (
                <a
                  href={connector.docs_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1.5 inline-block text-caption font-medium text-primary underline underline-offset-2"
                >
                  How to register one
                </a>
              )}
            </div>
          ) : (
            <div>
              <LabelWithHelp
                htmlFor="connector-secret"
                label={isOAuth ? "Token" : "Key"}
                help={
                  <>
                    {hint !== null && hint.trim() !== "" && <p>{hint}</p>}
                  <p>
                    Stored as a secret named <code>{secretName}</code> and looked up when the
                    connection opens. This page can only ever say whether it is stored, never
                    what it is.
                  </p>
                  </>
                }
              />
              <Input
                id="connector-secret"
                type="password"
                autoComplete="off"
                className="mt-1.5 font-technical"
                value={secretValue}
                onChange={(event) => setSecretValue(event.target.value)}
              />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={busy || !ready}>
            {busy && <Loader2Icon className="animate-spin" />}
            Connect
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
