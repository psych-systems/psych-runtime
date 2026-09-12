"use client";

import { useState } from "react";
import { Loader2Icon, SaveIcon } from "lucide-react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import type { CostPolicy, RuntimeSettings, RuntimeSettingsIn } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Section } from "@/components/ui/page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Button } from "@/components/ui/button";

const COST_POLICIES: { value: CostPolicy; label: string; help: string }[] = [
  {
    value: "prefer_provider",
    label: "Provider's own number when it gives one",
    help: "Falls back to Psych's own arithmetic otherwise. The usual choice.",
  },
  {
    value: "computed",
    label: "Always Psych's own arithmetic",
    help: "Ignores whatever the provider reports, even when it does.",
  },
  {
    value: "provider_only",
    label: "Only the provider's own number",
    help: "A model call the provider does not price back reports cost as unknown.",
  },
];

function toIn(runtime: RuntimeSettings): RuntimeSettingsIn {
  return {
    cost_policy: runtime.cost_policy,
    blob_offload_bytes: runtime.blob_offload_bytes,
    catalogue_budget_chars: runtime.catalogue_budget_chars,
    sandbox_enabled: runtime.sandbox_enabled,
    sandbox_limits: runtime.sandbox_limits,
    sandbox_allow_network: runtime.sandbox_allow_network,
    sandbox_profiles: runtime.sandbox_profiles,
    egress_allow: runtime.egress_allow,
    denied_tools: runtime.denied_tools,
  };
}

/**
 * How this account's runs execute: what none of them is part of a Spec.
 *
 * Every field here is a `psych.Runtime` constructor argument or a port it
 * takes, not a field of any published agent. Changing one changes the next
 * Attempt of every agent you run and moves no version hash, which is the
 * whole distinction this section exists to draw: an agent's Spec decides
 * what it is, this decides how the installation runs it.
 */
export function RuntimeSection({
  runtime,
  onSave,
}: {
  runtime: RuntimeSettings;
  onSave: (next: RuntimeSettingsIn) => Promise<void>;
}) {
  const saved = JSON.stringify(toIn(runtime));
  const [draft, setDraft] = useState<RuntimeSettingsIn>(toIn(runtime));
  const [prevSaved, setPrevSaved] = useState(saved);
  const [saving, setSaving] = useState(false);
  const [egressText, setEgressText] = useState(runtime.egress_allow.join(", "));
  const [deniedText, setDeniedText] = useState(runtime.denied_tools.join(", "));

  if (saved !== prevSaved) {
    setPrevSaved(saved);
    const next = JSON.parse(saved) as RuntimeSettingsIn;
    setDraft(next);
    setEgressText(next.egress_allow.join(", "));
    setDeniedText(next.denied_tools.join(", "));
  }

  const dirty = JSON.stringify(draft) !== saved;

  async function save() {
    setSaving(true);
    try {
      await onSave({
        ...draft,
        egress_allow: egressText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        denied_tools: deniedText
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      toast.success("Runtime settings saved. They apply to the next message.");
    } catch (err) {
      toast.error("Could not save runtime settings", { description: describeApiError(err) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section
      title="How agents run"
      description="Execution, not identity. Nothing here is part of any agent's published spec, so changing it moves no version hash."
    >
      <div className="flex flex-col gap-5 rounded-xl border border-border p-4">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rt-cost-policy">Cost, when the provider and Psych disagree</Label>
          <Select
            value={draft.cost_policy}
            onValueChange={(cost_policy) => setDraft({ ...draft, cost_policy: cost_policy as CostPolicy })}
          >
            <SelectTrigger id="rt-cost-policy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {COST_POLICIES.map((policy) => (
                <SelectItem key={policy.value} value={policy.value}>
                  {policy.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-micro text-muted-foreground">
            {COST_POLICIES.find((p) => p.value === draft.cost_policy)?.help}
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rt-offload">Large result cutoff for storage (bytes)</Label>
            <Input
              id="rt-offload"
              type="number"
              className="tabular"
              min={1}
              value={draft.blob_offload_bytes}
              onChange={(e) =>
                setDraft({ ...draft, blob_offload_bytes: Number(e.target.value) || 1 })
              }
            />
            <p className="text-micro text-muted-foreground">
              A tool result bigger than this is stored on disk and read back with{" "}
              <code className="font-technical">read_tool_output</code>, rather than sitting in the
              log record whole. Independent of an agent&apos;s own large-result cutoff, which
              decides what the model sees.
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rt-catalogue">MCP catalogue budget (characters)</Label>
            <Input
              id="rt-catalogue"
              type="number"
              className="tabular"
              min={1}
              value={draft.catalogue_budget_chars}
              onChange={(e) =>
                setDraft({ ...draft, catalogue_budget_chars: Number(e.target.value) || 1 })
              }
            />
            <p className="text-micro text-muted-foreground">
              How much of one connected server&apos;s tool list may sit in the prompt before it is
              discovered on demand instead.
            </p>
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rt-egress">Where outbound calls may go</Label>
          <Input
            id="rt-egress"
            value={egressText}
            spellCheck={false}
            placeholder="Empty allows everything. e.g. api.example.com, *.internal.example.com"
            onChange={(e) => setEgressText(e.target.value)}
          />
          <p className="text-micro text-muted-foreground">
            Hostnames or wildcard patterns. Applies to every outbound call your agents make: the
            model, MCP servers, HTTP tools and A2A peers alike, since they all share one seam.
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="rt-denied">Tools switched off here, whatever an agent grants</Label>
          <Input
            id="rt-denied"
            value={deniedText}
            spellCheck={false}
            placeholder="Empty means none. e.g. issue_refund"
            onChange={(e) => setDeniedText(e.target.value)}
          />
          <p className="text-micro text-muted-foreground">
            A call refused here reaches the model as an explanation it can act on, not a crash.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
            {saving ? "Saving" : "Save runtime settings"}
          </Button>
          {dirty && !saving && (
            <span className="text-caption text-muted-foreground">Unsaved changes.</span>
          )}
        </div>
      </div>
    </Section>
  );
}
