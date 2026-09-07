"use client";

import { useState } from "react";
import { CoinsIcon, Loader2Icon, PlusIcon, Trash2Icon } from "lucide-react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import type { ModelPrice } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { EmptyState, Section } from "@/components/ui/page";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { FieldError } from "@/components/settings/validation";

/**
 * What each model costs, so a Run can report a number instead of a shrug.
 *
 * Psych ships rates for nine models and records `cost=None` for anything else.
 * That is deliberate and stays: a silent zero makes metering look correct and
 * be wrong, and nobody finds out until they reconcile against a bill. What was
 * missing is the seam on the other side. `PriceResolver` is a port so a
 * consumer can supply the rates they actually pay, and this console had
 * nowhere to put them, so an operator running a proxy that knows every rate
 * still saw "cost unknown" on every conversation.
 *
 * ## Per million, and four of them
 *
 * Providers bill cached reads and cache writes separately from fresh input,
 * often by an order of magnitude, and a cache-heavy workload priced on input
 * alone is wrong by more than a rounding error. The four fields here are the
 * four `ModelPrice` carries, named identically, so there is no mapping to get
 * wrong between what somebody types and what gets charged.
 *
 * ## Editing a rate does not rewrite history
 *
 * A cost is computed per model call and written into the Record at the time,
 * so an old conversation keeps the number it was actually charged. That is
 * worth saying on the page: somebody who fixes a typo in a rate and expects
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
    (row, i) => row.model.trim() !== "" && rows.findIndex((r) => r.model.trim() === row.model.trim()) !== i
  );

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
    <Section
      title="What models cost"
      description="Psych knows the rates for a handful of models and says so honestly when it does not. Add yours here and conversations start reporting a cost instead of an unknown."
      actions={
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            setDraft([
              ...rows,
              { model: "", input: "0", output: "0", cache_read: "0", cache_write: "0", currency: "USD" },
            ])
          }
        >
          <PlusIcon /> Add a model
        </Button>
      }
    >
      {rows.length === 0 ? (
        <EmptyState
          icon={CoinsIcon}
          title="No rates of your own"
          description="Conversations using a model Psych does not have a rate for report an unknown cost rather than guessing at zero. Add what your provider charges and the number becomes real."
        />
      ) : (
        <div className="flex flex-col gap-3">
          {rows.map((row, index) => (
            <div key={index} className="flex flex-col gap-3 rounded-lg border border-border p-3">
              <div className="flex items-end gap-2">
                <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <Label htmlFor={`price-model-${index}`}>Model</Label>
                  <Input
                    id={`price-model-${index}`}
                    className="font-technical"
                    list="price-model-options"
                    value={row.model}
                    onChange={(e) => edit(index, { model: e.target.value })}
                    placeholder="gpt-4o"
                    spellCheck={false}
                  />
                </div>
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  className="mb-1 shrink-0"
                  aria-label={`Remove the rate for ${row.model || "this model"}`}
                  onClick={() => setDraft(rows.filter((_, i) => i !== index))}
                >
                  <Trash2Icon />
                </Button>
              </div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Rate
                  id={`price-input-${index}`}
                  label="Input"
                  value={row.input}
                  onChange={(v) => edit(index, { input: v })}
                />
                <Rate
                  id={`price-output-${index}`}
                  label="Output"
                  value={row.output}
                  onChange={(v) => edit(index, { output: v })}
                />
                <Rate
                  id={`price-cache-read-${index}`}
                  label="Cache read"
                  value={row.cache_read}
                  onChange={(v) => edit(index, { cache_read: v })}
                />
                <Rate
                  id={`price-cache-write-${index}`}
                  label="Cache write"
                  value={row.cache_write}
                  onChange={(v) => edit(index, { cache_write: v })}
                />
              </div>
            </div>
          ))}
          <datalist id="price-model-options">
            {models.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </div>
      )}

      <p className="text-caption text-muted-foreground">
        {/* Said outright, because somebody who fixes a rate and watches
            yesterday's totals stay put will otherwise assume this is broken. */}
        Rates are per million tokens, in US dollars. They apply to conversations from here on: a
        cost is worked out and recorded when each call happens, so an old conversation keeps the
        number it was actually charged.
      </p>

      {duplicate && (
        <FieldError
          message={`Two rates are both for ${duplicate.model.trim()}. A model resolves to one rate, so one of them would silently win.`}
        />
      )}
      <FieldError message={error ?? undefined} />

      {dirty && (
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => void save()} disabled={saving || duplicate !== undefined}>
            {saving ? <Loader2Icon className="animate-spin" /> : null}
            Save rates
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setDraft(null)} disabled={saving}>
            Discard
          </Button>
        </div>
      )}
    </Section>
  );
}

function Rate({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id} className="text-caption">
        {label}
      </Label>
      <Input
        id={id}
        className="font-technical"
        // `inputMode` rather than `type="number"`: a number input in several
        // browsers silently drops a value it cannot parse mid-typing, and this
        // is money being entered a character at a time. The backend validates.
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="0"
      />
    </div>
  );
}
