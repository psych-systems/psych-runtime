"use client";

import { useState } from "react";
import { DownloadIcon, Loader2Icon } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpTip } from "@/components/ui/help";
import { describeApiError } from "@/lib/errors";
import type { CatalogueKind } from "@/lib/types";

/**
 * Marks something that came with the console rather than being written here.
 *
 * It is not a warning and it does not restrict anything: a catalogue agent is
 * an ordinary agent, editable and deletable like any other. The chip only
 * answers "where did this come from", which is the question somebody has on
 * first opening a list they never filled in.
 */
export function CatalogueChip() {
  return (
    <span className="inline-flex items-center gap-1">
      <Badge variant="outline" className="text-muted-foreground">
        Catalogue
      </Badge>
      <HelpTip title="Catalogue" short="Came with the console; yours to change.">
        <p>
          This came ready-made rather than being written here. It is an ordinary one underneath:
          edit it, duplicate it or stop offering it like any other.
        </p>
      </HelpTip>
    </span>
  );
}

/**
 * Creates whatever the catalogue offers and this account has not got.
 *
 * Shown only while something is missing, because a button that adds nothing is
 * a button that teaches people it does nothing.
 */
export function AddFromCatalogue({
  kinds,
  missing,
  onSeed,
  label = "Add from catalogue",
}: {
  kinds: CatalogueKind[];
  /** How many entries are missing. Nothing renders at zero. */
  missing: number;
  onSeed: (kinds: CatalogueKind[]) => Promise<unknown>;
  label?: string;
}) {
  const [busy, setBusy] = useState(false);

  if (missing === 0) return null;

  async function run() {
    setBusy(true);
    try {
      await onSeed(kinds);
      toast.success(missing === 1 ? "One was added" : `${missing} were added`);
    } catch (err) {
      toast.error("Could not add them", { description: describeApiError(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Button variant="outline" onClick={() => void run()} disabled={busy}>
      {busy ? <Loader2Icon className="animate-spin" /> : <DownloadIcon />}
      {label}
    </Button>
  );
}
