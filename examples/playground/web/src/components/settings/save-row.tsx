"use client";

import { Loader2Icon, SaveIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * One Save per section, in the same place in every section.
 *
 * Saving is per section rather than per page on purpose: each section writes
 * a different endpoint, and several of them (providers, secrets, memory) save
 * the moment you act, with no draft at all. A single page-wide bar would have
 * to pretend those were drafts too.
 */
export function SaveRow({
  dirty,
  saving,
  onSave,
  onDiscard,
  label = "Save",
  disabled = false,
  children,
}: {
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard?: () => void;
  label?: string;
  disabled?: boolean;
  /** Anything else that belongs on this line, such as an add button. */
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {children}
      <span className="ml-auto flex items-center gap-2">
        {dirty && !saving && (
          <span className="text-caption text-muted-foreground">Unsaved changes.</span>
        )}
        {dirty && onDiscard && (
          <Button size="sm" variant="ghost" onClick={onDiscard} disabled={saving}>
            Discard
          </Button>
        )}
        <Button size="sm" disabled={!dirty || saving || disabled} onClick={onSave}>
          {saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
          {saving ? "Saving" : label}
        </Button>
      </span>
    </div>
  );
}
