"use client";

import { InfoIcon } from "lucide-react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const NO_CREDENTIAL = "__none__";

/**
 * Picks a stored secret by name instead of accepting free text.
 *
 * A typo here does not fail at save. It fails much later, when a run tries to
 * connect and the resolver finds nothing under that name. Offering only the
 * names that exist turns that into an impossible mistake rather than a delayed
 * one. A name that is stored in this connection but no longer in the secret
 * list, deleted after the connection was written, is still offered and marked,
 * so editing an unrelated field cannot silently drop it.
 */
export function SecretSelect({
  id,
  value,
  onChange,
  secretNames,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  secretNames: string[];
}) {
  const missing = value !== "" && !secretNames.includes(value);

  return (
    <>
      <Select
        value={value === "" ? NO_CREDENTIAL : value}
        onValueChange={(next) => onChange(next === NO_CREDENTIAL ? "" : next)}
      >
        <SelectTrigger id={id} className="mt-1.5 w-full font-technical">
          <SelectValue placeholder="None" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_CREDENTIAL}>None</SelectItem>
          {secretNames.map((name) => (
            <SelectItem key={name} value={name} className="font-technical">
              {name}
            </SelectItem>
          ))}
          {missing && (
            <SelectItem value={value} className="font-technical">
              {value} (not stored)
            </SelectItem>
          )}
        </SelectContent>
      </Select>
      {secretNames.length === 0 && (
        <p className="mt-1 text-caption text-muted-foreground">
          No secrets are stored yet. Add one under Settings, then pick it here.
        </p>
      )}
    </>
  );
}

/**
 * The one invariant this form exists to make unmistakable: a credential field
 * holds a NAME that gets looked up when the connection opens, never a secret
 * pasted in here. Getting it wrong is how a literal token ends up inside a
 * published agent.
 */
export function CredentialNameNote() {
  return (
    <p className="mt-1 flex items-start gap-1 text-caption text-muted-foreground">
      <InfoIcon className="mt-0.5 size-3 shrink-0" />
      <span>
        A <span className="font-medium text-foreground">name</span> that gets looked up when the
        connection opens, not the secret itself. Values live under Settings.
      </span>
    </p>
  );
}
