"use client";

import { useState } from "react";
import { KeyRoundIcon, Loader2Icon, PlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

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
import { EmptyState, Section } from "@/components/ui/page";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { FieldError } from "@/components/settings/validation";

interface SecretsSectionProps {
  secrets: string[];
  onAdd: (name: string, value: string) => Promise<void>;
  onRemove: (name: string) => Promise<void>;
}

const NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_.-]*$/;

/**
 * The named values a connection refers to.
 *
 * Names only, on the page and on the wire: the backend answers with the list
 * of names and has no endpoint that returns a value, so there is nothing here
 * to mask and no masked placeholder pretending there is.
 */
export function SecretsSection({ secrets, onAdd, onRemove }: SecretsSectionProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const trimmed = name.trim();
  const nameTaken = secrets.includes(trimmed);
  const nameValid = trimmed === "" || NAME_PATTERN.test(trimmed);

  function openAdd() {
    setName("");
    setValue("");
    setFormError(null);
    setAddOpen(true);
  }

  async function handleAdd() {
    setSaving(true);
    setFormError(null);
    try {
      await onAdd(trimmed, value);
      setAddOpen(false);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : describeApiError(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (pendingDelete === null) return;
    setDeleting(true);
    try {
      await onRemove(pendingDelete);
      setPendingDelete(null);
    } catch (err) {
      toast.error("Could not delete the secret", { description: describeApiError(err) });
    } finally {
      setDeleting(false);
    }
  }

  return (
    <Section
      title="Secrets"
      description="Passwords and tokens, kept under a name. A connection refers to the name, so the value itself never travels with an agent. This is a local tool, so the values sit in plain text on this machine."
      actions={
        secrets.length > 0 ? (
          <Button size="sm" onClick={openAdd}>
            <PlusIcon /> Add secret
          </Button>
        ) : null
      }
    >
      {secrets.length === 0 ? (
        <EmptyState
          icon={KeyRoundIcon}
          title="No secrets stored"
          description="Add one here, then point a connection at it by name."
          action={
            <Button onClick={openAdd}>
              <PlusIcon /> Add a secret
            </Button>
          }
        />
      ) : (
        <ul className="flex flex-col gap-1.5">
          {secrets.map((secretName) => (
            <li
              key={secretName}
              className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card px-3 py-2"
            >
              <span className="inline-flex items-center gap-2 font-technical text-body">
                <KeyRoundIcon className="size-3.5 text-muted-foreground" />
                {secretName}
              </span>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={`Delete ${secretName}`}
                onClick={() => setPendingDelete(secretName)}
              >
                <Trash2Icon />
              </Button>
            </li>
          ))}
        </ul>
      )}

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Add a secret</DialogTitle>
            <DialogDescription>
              Give it the name a connection will refer to. The value is stored here and is never
              shown again.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            {formError && (
              <Alert variant="destructive">
                <AlertDescription>{formError}</AlertDescription>
              </Alert>
            )}
            <div>
              <Label htmlFor="secret-name">Name</Label>
              <Input
                id="secret-name"
                className="mt-1.5 font-technical"
                placeholder="crm-client-secret"
                value={name}
                onChange={(event) => setName(event.target.value)}
                aria-invalid={!nameValid || nameTaken}
              />
              {nameTaken && <FieldError message="This name exists. Saving replaces its value." />}
              {!nameValid && !nameTaken && (
                <FieldError message="Letters, digits, dots, dashes and underscores, starting with a letter." />
              )}
            </div>
            <div>
              <Label htmlFor="secret-value">Value</Label>
              <Input
                id="secret-value"
                type="password"
                autoComplete="off"
                className="mt-1.5 font-technical"
                placeholder="The token or password itself"
                value={value}
                onChange={(event) => setValue(event.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button
              onClick={() => void handleAdd()}
              disabled={saving || trimmed === "" || value === "" || !nameValid}
            >
              {saving && <Loader2Icon className="animate-spin" />}
              Save secret
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pendingDelete !== null} onOpenChange={(open) => !open && setPendingDelete(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete this secret?</DialogTitle>
            <DialogDescription>
              Any connection that refers to{" "}
              <span className="font-technical font-medium text-foreground">{pendingDelete}</span>{" "}
              will stop working the next time it is used.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPendingDelete(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={() => void handleDelete()} disabled={deleting}>
              {deleting && <Loader2Icon className="animate-spin" />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Section>
  );
}
