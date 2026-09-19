"use client";

import {
  allBindingNames,
  STATUS_COPY,
  type BindingGroup,
} from "@/components/agents/bindable-tools";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { LabelWithHelp } from "@/components/ui/help";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { FieldError } from "@/components/settings/validation";
import { FieldGrid, NumberField } from "@/components/agents/field-bits";
import {
  ISOLATION_COPY,
  PRESERVE_COPY,
  isolationExceedsProfile,
  profileIsolation,
  type CodeExecutionFormState,
} from "@/components/agents/code-execution";
import { formatBytes } from "@/lib/format";
import type { IsolationLevel, RuntimeSettings } from "@/lib/types";

interface CodeExecutionFieldsProps {
  value: CodeExecutionFormState;
  onChange: (next: CodeExecutionFormState) => void;
  /** Keyed `code_execution.<field>`. */
  fieldErrors: Record<string, string>;
  /** What a program may call, grouped by where each tool comes from.
   *  Not a flat list of the agent's own tools: a program reaches every tool
   *  the agent is authorized to call, and an MCP tool is discovered rather
   *  than declared, so each entry carries its own status. */
  toolGroups: BindingGroup[];
  /** Profile names this account offers, from Settings. */
  profiles: string[];
  runtime: RuntimeSettings | null;
}

/**
 * The terms an agent asks for when it runs a program, as labelled controls.
 *
 * Every number here is a *request*: the profile carries the ceiling and the
 * effective cap is the smaller of the two, so a larger number buys nothing.
 * That, and everything else this used to explain in paragraphs, is now one
 * hover away behind the "?" beside the field it belongs to.
 */
export function CodeExecutionFields({
  value,
  onChange,
  fieldErrors,
  toolGroups,
  profiles,
  runtime,
}: CodeExecutionFieldsProps) {
  const known = profiles.length > 0 ? profiles : ["default"];
  const profileKnown = known.includes(value.profile);
  const provides = profileIsolation(runtime, value.profile);
  const mismatch = isolationExceedsProfile(runtime, value.profile, value.isolation);
  const ceiling =
    runtime && value.profile === "default"
      ? `Up to ${runtime.sandbox_limits.cpu_seconds}s CPU, ${runtime.sandbox_limits.wall_seconds}s wall clock, ${formatBytes(
          runtime.sandbox_limits.address_space_bytes,
        )} memory and ${runtime.sandbox_limits.process_count} processes.`
      : null;

  return (
    <div className="flex flex-col gap-4">
      <FieldGrid columns={2}>
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="code-profile"
            label="Sandbox profile"
            help="A name this installation resolves, never a machine or a credential. Profiles are set up in Settings, and an agent naming one that is not offered is refused at publish."
          />
          {known.length > 1 ? (
            <Select
              value={profileKnown ? value.profile : known[0]}
              onValueChange={(profile) => onChange({ ...value, profile })}
            >
              <SelectTrigger id="code-profile" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {known.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Input
              id="code-profile"
              value={value.profile}
              spellCheck={false}
              onChange={(event) => onChange({ ...value, profile: event.target.value })}
            />
          )}
          <FieldError message={fieldErrors["code_execution.profile"]} />
        </div>

        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="code-isolation"
            label="Isolation it requires"
            help={
              <>
                <p>
                  <strong>{ISOLATION_COPY.isolated.label}:</strong>{" "}
                  {ISOLATION_COPY.isolated.description}
                </p>
                <p>
                  <strong>{ISOLATION_COPY.process.label}:</strong>{" "}
                  {ISOLATION_COPY.process.description}
                </p>
              </>
            }
          />
          <Select
            value={value.isolation}
            onValueChange={(isolation) =>
              onChange({ ...value, isolation: isolation as IsolationLevel })
            }
          >
            <SelectTrigger id="code-isolation" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(ISOLATION_COPY) as IsolationLevel[]).map((level) => (
                <SelectItem key={level} value={level}>
                  {ISOLATION_COPY[level].label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {/* One line, not a banner. Asking for more than a profile provides
              does not run the program weaker; it refuses it. */}
          <p
            className={
              mismatch ? "text-caption text-status-failed" : "text-caption text-muted-foreground"
            }
          >
            {mismatch
              ? `This profile reaches ${provides} isolation only, so every program would be refused.`
              : provides !== null
                ? `This profile provides ${provides} isolation.`
                : "What this profile provides is not reported here."}
          </p>
        </div>

        <div className="flex min-w-0 items-center justify-between gap-3">
          <LabelWithHelp
            htmlFor="code-network"
            label="Raw network access"
            help="Off, the program has no route out and fetches through a host tool that passes the egress policy. On, it may open its own sockets, which bypasses that policy for the program; the profile must allow it too."
          />
          <Switch
            id="code-network"
            checked={value.network === "unrestricted"}
            onCheckedChange={(on) =>
              onChange({ ...value, network: on ? "unrestricted" : "denied" })
            }
          />
        </div>

        <div className="flex min-w-0 items-center justify-between gap-3">
          <LabelWithHelp
            htmlFor="code-bindings"
            label="May call every tool it holds"
            help="Host functions available inside the program, each going through the same policy, approvals and record as a direct call. Leaving this on keeps a program in step with the agent: a tool added to a connection becomes callable in the same turn it does for the model. No credential ever enters the sandbox, and a tool needing an approval or an answer is refused before it runs."
          />
          <Switch
            id="code-bindings"
            checked={value.bindings === null}
            disabled={toolGroups.length === 0}
            onCheckedChange={(on) =>
              onChange({ ...value, bindings: on ? null : allBindingNames(toolGroups) })
            }
          />
        </div>
      </FieldGrid>

      {value.bindings !== null && toolGroups.length > 0 && (
        <BindingPicker value={value} onChange={onChange} toolGroups={toolGroups} />
      )}

      <FieldGrid columns={3}>
        <NumberField
          id="code-cpu_seconds"
          label="CPU"
          suffix="seconds"
          help={`A request against the profile's ceiling. Empty takes the ceiling; a larger number is clamped to it, never granted.${ceiling ? ` ${ceiling}` : ""}`}
          value={value.cpu_seconds}
          placeholder="ceiling"
          error={fieldErrors["code_execution.cpu_seconds"]}
          onChange={(next) => onChange({ ...value, cpu_seconds: next })}
        />
        <NumberField
          id="code-wall_seconds"
          label="Wall clock"
          suffix="seconds"
          help="How long one program may run in real time. Empty takes the profile's ceiling."
          value={value.wall_seconds}
          placeholder="ceiling"
          error={fieldErrors["code_execution.wall_seconds"]}
          onChange={(next) => onChange({ ...value, wall_seconds: next })}
        />
        <NumberField
          id="code-memory_mb"
          label="Memory"
          suffix="MB"
          help="How much memory one program may address. Empty takes the profile's ceiling."
          value={value.memory_mb}
          placeholder="ceiling"
          error={fieldErrors["code_execution.memory_mb"]}
          onChange={(next) => onChange({ ...value, memory_mb: next })}
        />
        <NumberField
          id="code-process_count"
          label="Processes"
          help="How many processes the program's tree may hold at once. Empty takes the profile's ceiling."
          value={value.process_count}
          placeholder="ceiling"
          error={fieldErrors["code_execution.process_count"]}
          onChange={(next) => onChange({ ...value, process_count: next })}
        />
        <NumberField
          id="code-preview"
          label="Preview per stream"
          suffix="bytes"
          help="The model is shown a head-and-tail preview of each stream inside this budget. Everything beyond it stays out of the prompt and, when kept, is readable by handle."
          min={256}
          max={65536}
          step={256}
          value={value.preview_bytes}
          error={fieldErrors["code_execution.preview_bytes"]}
          onChange={(next) =>
            onChange({ ...value, preview_bytes: Number(next) || value.preview_bytes })
          }
        />
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="code-preserve"
            label="Output beyond the preview"
            help={
              <>
                {(
                  Object.keys(PRESERVE_COPY) as CodeExecutionFormState["preserve_output"][]
                ).map((key) => (
                  <p key={key}>
                    <strong>{PRESERVE_COPY[key].label}:</strong> {PRESERVE_COPY[key].description}
                  </p>
                ))}
              </>
            }
          />
          <Select
            value={value.preserve_output}
            onValueChange={(preserve_output) =>
              onChange({
                ...value,
                preserve_output: preserve_output as CodeExecutionFormState["preserve_output"],
              })
            }
          >
            <SelectTrigger id="code-preserve" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(Object.keys(PRESERVE_COPY) as CodeExecutionFormState["preserve_output"][]).map(
                (key) => (
                  <SelectItem key={key} value={key}>
                    {PRESERVE_COPY[key].label}
                  </SelectItem>
                ),
              )}
            </SelectContent>
          </Select>
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <LabelWithHelp
            htmlFor="code-artifacts"
            label="Files it writes"
            help="Files written to the working directory come back by relative path, up to this many. Links are never followed. Off, they are discarded when the program ends."
          />
          <div className="flex items-center gap-3">
            <Switch
              id="code-artifacts"
              checked={value.collect_artifacts}
              onCheckedChange={(collect_artifacts) => onChange({ ...value, collect_artifacts })}
            />
            <Input
              aria-label="Most files collected"
              type="number"
              className="tabular"
              min={0}
              max={256}
              disabled={!value.collect_artifacts}
              value={value.max_artifacts}
              onChange={(event) => {
                const parsed = event.target.valueAsNumber;
                onChange({
                  ...value,
                  max_artifacts: Number.isFinite(parsed) ? parsed : value.max_artifacts,
                });
              }}
            />
          </div>
          <FieldError message={fieldErrors["code_execution.max_artifacts"]} />
        </div>
      </FieldGrid>
    </div>
  );
}

/** Which of the agent's tools a program may call, shown only once somebody
 *  has turned off "every tool it holds". */
function BindingPicker({
  value,
  onChange,
  toolGroups,
}: {
  value: CodeExecutionFormState;
  onChange: (next: CodeExecutionFormState) => void;
  toolGroups: BindingGroup[];
}) {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
      {toolGroups.map((group) => (
        <div key={`${group.origin}:${group.label}`} className="flex flex-col gap-1.5">
          <div className="flex items-baseline gap-2">
            <span className="text-caption font-medium">{group.label}</span>
            <span className="text-micro text-muted-foreground">
              {group.origin === "mcp" ? "connected system" : `${group.origin} tools`}
            </span>
          </div>
          {group.note && <p className="text-micro text-muted-foreground">{group.note}</p>}
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {group.options.map((option) => {
              const on = value.bindings?.includes(option.name) ?? false;
              const status = STATUS_COPY[option.status];
              return (
                <label
                  key={option.name}
                  className="flex items-start gap-2 text-caption"
                  title={option.note}
                >
                  <Checkbox
                    checked={on}
                    disabled={option.status === "incompatible"}
                    onCheckedChange={(checked) => {
                      const current = value.bindings ?? [];
                      onChange({
                        ...value,
                        bindings: checked
                          ? [...current, option.name].filter((t, i, a) => a.indexOf(t) === i)
                          : current.filter((t) => t !== option.name),
                      });
                    }}
                  />
                  <span className="flex flex-col">
                    <span className="font-technical">{option.name}</span>
                    {option.status !== "available" && (
                      <span className={status.tone}>{status.label}</span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
