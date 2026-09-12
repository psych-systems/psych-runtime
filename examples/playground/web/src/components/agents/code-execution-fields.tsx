"use client";

import { ShieldCheckIcon, ShieldIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DetailRow } from "@/components/ui/page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { FieldError } from "@/components/settings/validation";
import {
  ISOLATION_COPY,
  PRESERVE_COPY,
  type CodeExecutionFormState,
} from "@/components/agents/code-execution";
import { formatBytes } from "@/lib/format";
import type { CodeExecutionIn, IsolationLevel, RuntimeSettings } from "@/lib/types";

interface CodeExecutionFieldsProps {
  value: CodeExecutionFormState;
  onChange: (next: CodeExecutionFormState) => void;
  /** Keyed `code_execution.<field>`. */
  fieldErrors: Record<string, string>;
  /** The tools the agent currently grants, which bound what a program may call. */
  tools: string[];
  /** Profile names this account offers, from Settings. */
  profiles: string[];
  runtime: RuntimeSettings | null;
}

/**
 * The terms an agent asks for when it runs a program.
 *
 * Grouped as three questions: where it runs and how far it is contained,
 * what it may call and spend, and what happens to what it produces. Every
 * limit is labelled as a request against the profile's ceiling, because a
 * person typing a larger number here is not buying a larger budget and the
 * form must not let them think so.
 */
export function CodeExecutionFields({
  value,
  onChange,
  fieldErrors,
  tools,
  profiles,
  runtime,
}: CodeExecutionFieldsProps) {
  const known = profiles.length > 0 ? profiles : ["default"];
  const profileKnown = known.includes(value.profile);
  const localLevel = runtime?.sandbox_backends.find((b) => b.available)?.isolation ?? null;
  const mismatch =
    value.profile === "default" && value.isolation === "isolated" && localLevel === "process";

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="code-profile">Sandbox profile</Label>
          {known.length > 1 ? (
            <Select
              value={profileKnown ? value.profile : known[0]}
              onValueChange={(profile) => onChange({ ...value, profile })}
            >
              <SelectTrigger id="code-profile">
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
          <p className="text-caption text-muted-foreground">
            A name resolved by this installation, never a machine or a credential. Profiles are
            configured in Settings; an agent naming one that is not offered is refused at publish.
          </p>
          <FieldError message={fieldErrors["code_execution.profile"]} />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="code-isolation">Isolation it requires</Label>
          <Select
            value={value.isolation}
            onValueChange={(isolation) =>
              onChange({ ...value, isolation: isolation as IsolationLevel })
            }
          >
            <SelectTrigger id="code-isolation">
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
          <p className="text-caption text-muted-foreground">
            {ISOLATION_COPY[value.isolation].description}
          </p>
        </div>
      </div>

      {mismatch && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-status-waiting/40 bg-status-waiting/10 px-3 py-2 text-caption"
        >
          <ShieldIcon className="mt-0.5 size-4 shrink-0 text-status-waiting" aria-hidden />
          <span>
            This host&apos;s local backend reaches <strong>process</strong> isolation only, so an
            agent requiring <strong>isolated</strong> on the default profile will have every program
            refused, with the reason returned to it. Add a container or remote profile in Settings,
            or choose process-level isolation for code you trust.
          </span>
        </div>
      )}

      <div className="flex items-start justify-between gap-4 rounded-lg border border-border px-3 py-2.5">
        <div className="flex flex-col gap-0.5">
          <p className="text-body font-medium">Raw network access</p>
          <p className="text-caption text-muted-foreground">
            Off, the program has no route out and fetches through a host tool that passes the
            egress policy. On, it may open its own sockets, which bypasses that policy for the
            program; the profile must allow it too.
          </p>
        </div>
        <Switch
          checked={value.network === "unrestricted"}
          onCheckedChange={(on) => onChange({ ...value, network: on ? "unrestricted" : "denied" })}
          aria-label="Raw network access"
        />
      </div>

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-0.5">
          <h3 className="text-body font-medium">Budgets it asks for</h3>
          <p className="text-caption text-muted-foreground">
            Requests against the profile&apos;s ceiling. Empty takes the ceiling; a larger number
            is clamped to it, never granted.
            {runtime && value.profile === "default" && (
              <>
                {" "}
                The default profile allows up to {runtime.sandbox_limits.cpu_seconds}s CPU,{" "}
                {runtime.sandbox_limits.wall_seconds}s wall clock,{" "}
                {formatBytes(runtime.sandbox_limits.address_space_bytes)} memory and{" "}
                {runtime.sandbox_limits.process_count} processes.
              </>
            )}
          </p>
        </div>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {(
            [
              ["cpu_seconds", "CPU seconds"],
              ["wall_seconds", "Wall clock seconds"],
              ["memory_mb", "Memory (MB)"],
              ["process_count", "Processes"],
            ] as const
          ).map(([key, label]) => (
            <div key={key} className="flex flex-col gap-1">
              <Label htmlFor={`code-${key}`}>{label}</Label>
              <Input
                id={`code-${key}`}
                inputMode="decimal"
                className="tabular"
                placeholder="ceiling"
                value={value[key]}
                aria-invalid={fieldErrors[`code_execution.${key}`] !== undefined}
                onChange={(event) => onChange({ ...value, [key]: event.target.value })}
              />
              <FieldError message={fieldErrors[`code_execution.${key}`]} />
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-0.5">
          <h3 className="text-body font-medium">Tools a program may call</h3>
          <p className="text-caption text-muted-foreground">
            Host functions available inside the program, each going through the same policy,
            approvals and record as a direct call. Only tools this agent already holds can be
            offered; narrowing here never widens anything.
          </p>
        </div>
        {tools.length === 0 ? (
          <p className="text-caption text-muted-foreground italic">
            This agent holds no tools yet, so a program can call none.
          </p>
        ) : (
          <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
            <label className="flex items-center gap-2 text-body">
              <Checkbox
                checked={value.bindings === null}
                onCheckedChange={(checked) =>
                  onChange({ ...value, bindings: checked ? null : [...tools] })
                }
              />
              Every tool the agent holds
            </label>
            {value.bindings !== null && (
              <div className="grid grid-cols-1 gap-1.5 pl-6 sm:grid-cols-2">
                {tools.map((tool) => {
                  const on = value.bindings?.includes(tool) ?? false;
                  return (
                    <label key={tool} className="flex items-center gap-2 text-caption">
                      <Checkbox
                        checked={on}
                        onCheckedChange={(checked) => {
                          const current = value.bindings ?? [];
                          onChange({
                            ...value,
                            bindings: checked
                              ? [...current, tool].filter((t, i, a) => a.indexOf(t) === i)
                              : current.filter((t) => t !== tool),
                          });
                        }}
                      />
                      <span className="font-technical">{tool}</span>
                    </label>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-0.5">
          <h3 className="text-body font-medium">What it produces</h3>
          <p className="text-caption text-muted-foreground">
            The model is shown a head-and-tail preview of each stream inside this budget. Everything
            beyond it stays out of the prompt and, when kept, is readable in windows by handle.
          </p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="code-preview">Preview per stream (bytes)</Label>
            <Input
              id="code-preview"
              type="number"
              className="tabular"
              min={256}
              max={65536}
              step={256}
              value={value.preview_bytes}
              aria-invalid={fieldErrors["code_execution.preview_bytes"] !== undefined}
              onChange={(event) => {
                const parsed = event.target.valueAsNumber;
                onChange({
                  ...value,
                  preview_bytes: Number.isFinite(parsed) ? parsed : value.preview_bytes,
                });
              }}
            />
            <FieldError message={fieldErrors["code_execution.preview_bytes"]} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="code-preserve">Output beyond the preview</Label>
            <Select
              value={value.preserve_output}
              onValueChange={(preserve_output) =>
                onChange({
                  ...value,
                  preserve_output: preserve_output as CodeExecutionFormState["preserve_output"],
                })
              }
            >
              <SelectTrigger id="code-preserve">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(PRESERVE_COPY) as CodeExecutionFormState["preserve_output"][]).map(
                  (key) => (
                    <SelectItem key={key} value={key}>
                      {PRESERVE_COPY[key].label}
                    </SelectItem>
                  )
                )}
              </SelectContent>
            </Select>
            <p className="text-caption text-muted-foreground">
              {PRESERVE_COPY[value.preserve_output].description}
            </p>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="code-artifacts">Files it writes</Label>
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
            <p className="text-caption text-muted-foreground">
              Files written to the working directory come back by relative path, up to this many.
              Links are never followed.
            </p>
            <FieldError message={fieldErrors["code_execution.max_artifacts"]} />
          </div>
        </div>
      </div>
    </div>
  );
}

/** What a published version asks for, on the agent page. */
export function CodeExecutionSummary({
  terms,
  runtime,
}: {
  terms: CodeExecutionIn | null;
  runtime: RuntimeSettings | null;
}) {
  if (terms === null || !terms.enabled) {
    return (
      <p className="text-body text-muted-foreground">
        This version does not run programs. It is never shown the{" "}
        <code className="font-technical">run_code</code> tool.
      </p>
    );
  }
  const offered = runtime?.sandbox_profile_names.includes(terms.profile) ?? true;
  const Icon = terms.isolation === "isolated" ? ShieldCheckIcon : ShieldIcon;
  const requested = [
    terms.limits.cpu_seconds != null ? `${terms.limits.cpu_seconds}s CPU` : null,
    terms.limits.wall_seconds != null ? `${terms.limits.wall_seconds}s wall clock` : null,
    terms.limits.memory_bytes != null ? `${formatBytes(terms.limits.memory_bytes)} memory` : null,
    terms.limits.process_count != null ? `${terms.limits.process_count} processes` : null,
  ].filter((s): s is string => s !== null);
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="gap-1">
          <Icon className="size-3" aria-hidden />
          {ISOLATION_COPY[terms.isolation].label}
        </Badge>
        <Badge variant="outline" className="font-technical">
          profile: {terms.profile}
        </Badge>
        {!offered && <Badge variant="destructive">profile not offered here</Badge>}
        <Badge variant="outline">
          {terms.network === "denied" ? "no network" : "raw network"}
        </Badge>
      </div>
      <DetailRow label="Budgets requested">
        {requested.length > 0 ? requested.join(", ") : "the profile's ceiling"}
      </DetailRow>
      <DetailRow label="Tools a program may call">
        {terms.bindings === null ? "every tool it holds" : terms.bindings.join(", ") || "none"}
      </DetailRow>
      <DetailRow label="Preview per stream">{terms.preview_bytes.toLocaleString()} bytes</DetailRow>
      <DetailRow label="Output beyond the preview">
        {PRESERVE_COPY[terms.preserve_output].label}
      </DetailRow>
      <DetailRow label="Files it writes">
        {terms.collect_artifacts ? `collected, up to ${terms.max_artifacts}` : "discarded"}
      </DetailRow>
    </div>
  );
}
