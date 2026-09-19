"use client";

import { useState } from "react";
import { ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FeatureGroup, FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { Switch } from "@/components/ui/switch";
import { FieldGrid, NumberField } from "@/components/agents/field-bits";
import { LimitsFields } from "@/components/agents/limits-fields";
import { SuspensionFields } from "@/components/agents/suspension-fields";
import { ModelOptionsFields } from "@/components/agents/model-options-fields";
import type { AgentFormState } from "@/components/agents/use-agent-form";
import { cn } from "@/lib/utils";

/**
 * The numbers almost nobody changes: spending limits, how long each kind of
 * wait may last, and the rest of the model reference. Collapsed, because a
 * safe default is filled into every one of them, and forced open the moment
 * a rejected publish names one, because a number nobody can see is a form
 * that refuses to publish without saying why.
 */
export function AdvancedTable({ form }: { form: AgentFormState }) {
  const [opened, setOpened] = useState(false);
  // Derived rather than set from an effect: a rejected number inside here
  // forces the section open, and it stays open once somebody opens it.
  const open = opened || form.advancedHasIssue;

  return (
    <FeatureTable>
      <FeatureRow
        label="Limits, waiting and model options"
        detail="Every one already has a safe default."
        control={
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-expanded={open}
            onClick={() => setOpened(!open)}
          >
            {open ? "Hide" : "Show advanced"}
            <ChevronDownIcon className={cn("transition-transform", open && "rotate-180")} />
          </Button>
        }
      />
      {open && (
        <>
          <FeatureGroup
            title="Limits"
            help="What one answer may spend before it is stopped. These are money: they decide how large a bill one runaway conversation may run up."
          >
            <div className="px-4 py-3">
              <LimitsFields
                value={form.limits}
                onChange={form.setLimits}
                fieldErrors={form.fieldErrors}
              />
            </div>
          </FeatureGroup>

          <FeatureGroup
            title="How long it may wait"
            help="A conversation that stops for a person, a webhook or a helper holds no worker and costs nothing while it waits. These say when waiting becomes giving up."
          >
            <div className="px-4 py-3">
              <SuspensionFields value={form.suspension} onChange={form.setSuspension} />
            </div>
          </FeatureGroup>

          <FeatureGroup
            title="Model options"
            help="The rest of what a model reference carries. Leave them unset to use the provider's own."
          >
            <FeatureRow
              label="Set how varied its wording is"
              help="Temperature. Lower is more predictable, higher is more varied. Off uses the model's own."
              detail={form.temperatureEnabled ? `Temperature ${form.temperature}.` : "The model's own."}
              control={
                <Switch
                  checked={form.temperatureEnabled}
                  onCheckedChange={form.setTemperatureEnabled}
                  aria-label="Set how varied its wording is"
                />
              }
            >
              {form.temperatureEnabled && (
                <FieldGrid columns={3}>
                  <NumberField
                    id="agent-temperature"
                    label="Temperature"
                    help="Between 0 and 2."
                    min={0}
                    max={2}
                    step={0.1}
                    value={form.temperature}
                    error={form.fieldErrors.temperature}
                    onChange={(next) => {
                      const parsed = Number(next);
                      if (Number.isFinite(parsed) && next !== "") form.setTemperature(parsed);
                    }}
                  />
                </FieldGrid>
              )}
            </FeatureRow>
            <div className="px-4 py-3">
              <ModelOptionsFields
                value={form.modelOptions}
                onChange={form.setModelOptions}
                models={form.models}
              />
            </div>
          </FeatureGroup>
        </>
      )}
    </FeatureTable>
  );
}
