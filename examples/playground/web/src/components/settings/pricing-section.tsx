"use client";

import { useState } from "react";
import { CoinsIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import type { ModelPrice } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/page";
import { HelpTip } from "@/components/ui/help";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { FieldError } from "@/components/settings/validation";
import { SaveRow } from "@/components/settings/save-row";
import { SettingsSectionBlock } from "@/components/settings/section-nav";

/**
 * What each model costs, so a Run can report a number instead of a shrug.
 *
 * Psych ships a broad, dated rate snapshot and records `cost=None` for
 * anything else. That is deliberate and stays: a silent zero makes metering
 * look correct and be wrong, and nobody finds out until they reconcile against
 * a bill. `PriceResolver` is a port so a consumer can supply the rates they
 * actually pay, and this console had nowhere to put them.
 *
 * ## Per million, and four of them
 *
 * Providers bill cached reads and cache writes separately from fresh input,
 * often by an order of magnitude, and a cache-heavy workload priced on input
 * alone is wrong by more than a rounding error. The four columns here are the
 * four `ModelPrice` carries, named identically, so there is no mapping to get
 * wrong between what somebody types and what gets charged.
 *
 * ## Editing a rate does not rewrite history
 *
 * A cost is computed per model call and written into the Record at the time,
 * so an old conversation keeps the number it was actually charged. The help
 * behind the heading says so: somebody who fixes a typo in a rate and expects
 * yesterday's totals to move would otherwise think this is broken.
 */
export function PricingSection({
  prices,
  models,
  onSave,
}: {
  prices: ModelPrice[];
  /** Names the active provider offers, for the picker. Empty when it will not
   *  say, which is ordinary for a proxy and is why the field stays typeable. */
  models: string[];
  onSave: (next: ModelPrice[]) => Promise<void>;
}) {
  const [draft, setDraft] = useState<ModelPrice[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const rows = draft ?? prices;
  const dirty = draft !== null;

  function edit(index: number, patch: Partial<ModelPrice>) {
    setDraft(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }

  const duplicate = rows.find(
    (row, i) =>
      row.model.trim() !== "" && rows.findIndex((r) => r.model.trim() === row.model.trim()) !== i
  );

  function addRow() {
    setDraft([
      ...rows,
      { model: "", input: "0", output: "0", cache_read: "0", cache_write: "0", currency: "USD" },
    ]);
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await onSave(rows.filter((row) => row.model.trim() !== ""));
      setDraft(null);
      toast.success("Rates saved. They apply to the next conversation.");
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsSectionBlock
      id="prices"
      title="Prices"
      description={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          Per million tokens, in US dollars.
          <HelpTip title="Prices" short="Your rates replace the shipped snapshot.">
            <p>
              Psych includes a broad rate snapshot and says so honestly when a model is absent,
              rather than guessing at zero. Adding your own rates replaces the estimate.
            </p>
            <p>
              Rates apply from here on. A cost is worked out and recorded when each call happens,
              so an old conversation keeps the number it was actually charged.
            </p>
          </HelpTip>
        </span>
      }
      actions={
        rows.length > 0 ? (
          <Button size="sm" variant="outline" onClick={addRow}>
            <PlusIcon /> Add a model
          </Button>
        ) : null
      }
    >
      {rows.length === 0 ? (
        <EmptyState
          icon={CoinsIcon}
          title="No rates of your own"
          description="A conversation using a model Psych has no rate for reports an unknown cost rather than guessing at zero."
          action={
            <Button onClick={addRow}>
              <PlusIcon /> Add a model
            </Button>
          }
        />
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="min-w-40">Model</TableHead>
                <TableHead>Input</TableHead>
                <TableHead>Output</TableHead>
                <TableHead>
                  <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                    Cache read
                    <HelpTip title="Cache read" short="Often an order of magnitude cheaper.">
                      <p>
                        What a provider charges for tokens served from its prompt cache. A
                        cache-heavy workload priced on input alone is wrong by far more than a
                        rounding error.
                      </p>
                    </HelpTip>
                  </span>
                </TableHead>
                <TableHead>
                  <span className="whitespace-nowrap">Cache write</span>
                </TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row, index) => (
                <TableRow key={index}>
                  <TableCell>
                    <Input
                      aria-label="Model"
                      className="font-technical h-8 min-w-36"
                      list="price-model-options"
                      value={row.model}
                      onChange={(e) => edit(index, { model: e.target.value })}
                      placeholder="gpt-4o"
                      spellCheck={false}
                    />
                  </TableCell>
                  <Rate
                    label={`Input rate for ${row.model || "this model"}`}
                    value={row.input}
                    onChange={(v) => edit(index, { input: v })}
                  />
                  <Rate
                    label={`Output rate for ${row.model || "this model"}`}
                    value={row.output}
                    onChange={(v) => edit(index, { output: v })}
                  />
                  <Rate
                    label={`Cache read rate for ${row.model || "this model"}`}
                    value={row.cache_read}
                    onChange={(v) => edit(index, { cache_read: v })}
                  />
                  <Rate
                    label={`Cache write rate for ${row.model || "this model"}`}
                    value={row.cache_write}
                    onChange={(v) => edit(index, { cache_write: v })}
                  />
                  <TableCell>
                    <Button
                      type="button"
                      size="icon-xs"
                      variant="ghost"
                      aria-label={`Remove the rate for ${row.model || "this model"}`}
                      onClick={() => setDraft(rows.filter((_, i) => i !== index))}
                    >
                      <Trash2Icon />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <datalist id="price-model-options">
            {models.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </div>
      )}

      {duplicate && (
        <FieldError
          message={`Two rates are both for ${duplicate.model.trim()}. A model resolves to one rate, so one of them would silently win.`}
        />
      )}
      <FieldError message={error ?? undefined} />

      <SaveRow
        dirty={dirty}
        saving={saving}
        disabled={duplicate !== undefined}
        onSave={() => void save()}
        onDiscard={() => setDraft(null)}
        label="Save rates"
      />
    </SettingsSectionBlock>
  );
}

function Rate({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <TableCell>
      <Input
        aria-label={label}
        className="font-technical h-8 w-20"
        // `inputMode` rather than `type="number"`: a number input in several
        // browsers silently drops a value it cannot parse mid-typing, and this
        // is money being entered a character at a time. The backend validates.
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0"
      />
    </TableCell>
  );
}
