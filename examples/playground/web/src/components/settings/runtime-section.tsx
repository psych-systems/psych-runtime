"use client";

import { useState } from "react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import type { CostPolicy, RuntimeSettings, RuntimeSettingsIn } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SaveRow } from "@/components/settings/save-row";
import { SettingsSectionBlock } from "@/components/settings/section-nav";

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
 * How this account's runs execute: rows, controls, and a "?" for the why.
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
  // Held as text rather than as the array: splitting on every keystroke means
  // a comma can never be typed.
  const [egressText, setEgressText] = useState(runtime.egress_allow.join(", "));
  const [deniedText, setDeniedText] = useState(runtime.denied_tools.join(", "));

  if (saved !== prevSaved) {
    setPrevSaved(saved);
    const next = JSON.parse(saved) as RuntimeSettingsIn;
    setDraft(next);
    setEgressText(next.egress_allow.join(", "));
    setDeniedText(next.denied_tools.join(", "));
  }

  const dirty =
    JSON.stringify(draft) !== saved ||
    egressText !== runtime.egress_allow.join(", ") ||
    deniedText !== runtime.denied_tools.join(", ");

  async function save() {
    setSaving(true);
    const egress_allow = egressText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const denied_tools = deniedText
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      await onSave({ ...draft, egress_allow, denied_tools });
      // Written back normalised, as the server now holds them. Typing a
      // trailing comma would otherwise leave the section permanently dirty:
      // the save changes nothing on the server, so the re-seed above never
      // fires and the text keeps the comma the server dropped.
      setEgressText(egress_allow.join(", "));
      setDeniedText(denied_tools.join(", "));
      toast.success("Runtime settings saved. They apply to the next message.");
    } catch (err) {
      toast.error("Could not save runtime settings", { description: describeApiError(err) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsSectionBlock
      id="runtime"
      title="Runtime"
      description="Execution, not identity: nothing here is part of any agent's published spec."
    >
      <FeatureTable>
        <FeatureRow
          htmlFor="rt-cost-policy"
          label="Cost source"
          detail={COST_POLICIES.find((p) => p.value === draft.cost_policy)?.help}
          help={
            <p>
              Which number a conversation records when the provider reports a cost and Psych also
              works one out from your rates. They often disagree.
            </p>
          }
          control={
            <Select
              value={draft.cost_policy}
              onValueChange={(cost_policy) =>
                setDraft({ ...draft, cost_policy: cost_policy as CostPolicy })
              }
            >
              <SelectTrigger id="rt-cost-policy" className="w-56 max-w-[60vw]">
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
          }
        />

        <FeatureRow
          htmlFor="rt-offload"
          label="Large result cutoff"
          detail="bytes"
          help={
            <p>
              A tool result bigger than this is stored on disk and read back with{" "}
              <code>read_tool_output</code> rather than sitting in the log record whole.
              Independent of an agent&apos;s own cutoff, which decides what the model sees.
            </p>
          }
          control={
            <Input
              id="rt-offload"
              type="number"
              className="tabular w-36"
              min={1}
              value={draft.blob_offload_bytes}
              onChange={(e) =>
                setDraft({ ...draft, blob_offload_bytes: Number(e.target.value) || 1 })
              }
            />
          }
        />

        <FeatureRow
          htmlFor="rt-catalogue"
          label="Catalogue budget"
          detail="characters"
          help={
            <p>
              How much of one connected server&apos;s tool list may sit in the prompt before it is
              discovered on demand instead.
            </p>
          }
          control={
            <Input
              id="rt-catalogue"
              type="number"
              className="tabular w-36"
              min={1}
              value={draft.catalogue_budget_chars}
              onChange={(e) =>
                setDraft({ ...draft, catalogue_budget_chars: Number(e.target.value) || 1 })
              }
            />
          }
        />

        <FeatureRow
          htmlFor="rt-egress"
          label="Egress allowlist"
          help={
            <p>
              Hostnames or wildcard patterns, separated by commas. Empty allows everything. It
              applies to every outbound call your agents make: the model, MCP servers, HTTP tools
              and A2A peers alike, since they share one seam.
            </p>
          }
        >
          <Input
            id="rt-egress"
            value={egressText}
            spellCheck={false}
            className="font-technical"
            placeholder="Empty allows everything. e.g. api.example.com, *.internal.example.com"
            onChange={(e) => setEgressText(e.target.value)}
          />
        </FeatureRow>

        <FeatureRow
          htmlFor="rt-denied"
          label="Always-off tools"
          help={
            <p>
              Tool names, separated by commas, switched off here whatever an agent grants. A call
              refused reaches the model as an explanation it can act on, not a crash.
            </p>
          }
        >
          <Input
            id="rt-denied"
            value={deniedText}
            spellCheck={false}
            className="font-technical"
            placeholder="Empty means none. e.g. delete_record"
            onChange={(e) => setDeniedText(e.target.value)}
          />
        </FeatureRow>
      </FeatureTable>

      <SaveRow dirty={dirty} saving={saving} onSave={() => void save()} />
    </SettingsSectionBlock>
  );
}
