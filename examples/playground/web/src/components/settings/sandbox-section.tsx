"use client";

import { useState } from "react";
import {
  CheckCircle2Icon,
  CircleAlertIcon,
  Loader2Icon,
  PlusIcon,
  ShieldCheckIcon,
  ShieldIcon,
  StethoscopeIcon,
  Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";

import { describeApiError } from "@/lib/errors";
import { formatBytes } from "@/lib/format";
import type {
  RuntimeSettings,
  RuntimeSettingsIn,
  SandboxGuarantees,
  SandboxLimitsIn,
  SandboxProfileHealth,
  SandboxProfileIn,
} from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FeatureGroup, FeatureRow, FeatureTable } from "@/components/ui/feature-table";
import { LabelWithHelp } from "@/components/ui/help";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { SaveRow } from "@/components/settings/save-row";
import { SettingsSectionBlock } from "@/components/settings/section-nav";
import { cn } from "@/lib/utils";

const GUARANTEE_LABELS: Record<keyof SandboxGuarantees, string> = {
  filesystem: "Host files hidden",
  network: "Network denied",
  process_tree: "Process tree contained",
  identity: "Separate account",
  cpu: "CPU capped",
  memory: "Memory capped",
  file_size: "File size capped",
  process_count: "Process count capped",
  wall_clock: "Wall clock enforced",
  environment: "Environment scrubbed",
};

const PLATFORM_NAMES: Record<string, string> = {
  linux: "Linux",
  darwin: "macOS",
  win32: "Windows",
};

const DEFAULT_LIMITS: SandboxLimitsIn = {
  cpu_seconds: 10,
  address_space_bytes: 512 * 1024 * 1024,
  file_size_bytes: 10 * 1024 * 1024,
  process_count: 64,
  wall_seconds: 30,
};

function toIn(runtime: RuntimeSettings): RuntimeSettingsIn {
  return {
    cost_policy: runtime.cost_policy,
    blob_offload_bytes: runtime.blob_offload_bytes,
    catalogue_budget_chars: runtime.catalogue_budget_chars,
    sandbox_enabled: runtime.sandbox_enabled,
    sandbox_limits: runtime.sandbox_limits,
    sandbox_allow_network: runtime.sandbox_allow_network,
    sandbox_profiles: runtime.sandbox_profiles.map((p) => ({ ...p })),
    egress_allow: runtime.egress_allow,
    denied_tools: runtime.denied_tools,
  };
}

/**
 * Where an agent's programs run, and what that is worth on this host.
 *
 * Two kinds of thing on one list, and they are kept visibly apart. The
 * `default` profile is this machine's own backend: what it is, and the level
 * it reaches, come from the process's own detection and probe rather than
 * from anything written here, because the difference between "isolated" and
 * "a process on the same account" is the whole question a person is asking.
 * Further profiles are configuration: a container image, or a service by URL
 * with the name of the secret that authenticates to it. Each row carries a
 * check that asks the backend what it can actually do, without running any
 * agent's code.
 */
export function SandboxSection({
  runtime,
  secrets,
  onSave,
  onCheck,
}: {
  runtime: RuntimeSettings;
  secrets: string[];
  onSave: (runtime: RuntimeSettingsIn) => Promise<void>;
  onCheck: (name: string) => Promise<SandboxProfileHealth>;
}) {
  const saved = JSON.stringify(toIn(runtime));
  // Seeded once and never reset from the server. Runtime and Sandbox both PUT
  // the whole `RuntimeSettingsIn`, so each one's save refreshes the other's
  // source; re-seeding here would throw away half-written profiles the moment
  // somebody saved the Runtime section.
  const [draft, setDraft] = useState<RuntimeSettingsIn>(() => toIn(runtime));
  const [saving, setSaving] = useState(false);
  const [health, setHealth] = useState<Record<string, SandboxProfileHealth | "checking">>({});

  const dirty = JSON.stringify(draft) !== saved;
  const platform = PLATFORM_NAMES[runtime.sandbox_platform] ?? runtime.sandbox_platform;
  const local = runtime.sandbox_backends.find((b) => b.available) ?? null;

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      toast.success("Sandbox settings saved");
    } catch (err) {
      toast.error(describeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const check = async (name: string) => {
    setHealth((prev) => ({ ...prev, [name]: "checking" }));
    try {
      const report = await onCheck(name);
      setHealth((prev) => ({ ...prev, [name]: report }));
    } catch (err) {
      toast.error(describeApiError(err));
      setHealth((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const updateProfile = (index: number, patch: Partial<SandboxProfileIn>) => {
    setDraft({
      ...draft,
      sandbox_profiles: draft.sandbox_profiles.map((p, i) => (i === index ? { ...p, ...patch } : p)),
    });
  };

  const addProfile = (backend: "container" | "remote") => {
    const base = backend === "container" ? "pods" : "cluster";
    const taken = new Set(draft.sandbox_profiles.map((p) => p.name));
    let name = base;
    let n = 2;
    while (taken.has(name) || name === "default") name = `${base}-${n++}`;
    setDraft({
      ...draft,
      sandbox_profiles: [
        ...draft.sandbox_profiles,
        {
          name,
          backend,
          enabled: true,
          hard_limits: { ...DEFAULT_LIMITS },
          allow_network: false,
          image: backend === "container" ? "python:3.12-slim" : null,
          runtime: null,
          base_url: backend === "remote" ? "" : null,
          credential: null,
        },
      ],
    });
  };

  return (
    <SettingsSectionBlock
      id="sandbox"
      title="Sandbox"
      description="Profiles an agent can name when it wants to run a program."
      actions={
        <>
          <Button size="sm" variant="outline" onClick={() => addProfile("container")}>
            <PlusIcon /> Container
          </Button>
          <Button size="sm" variant="outline" onClick={() => addProfile("remote")}>
            <PlusIcon /> Remote
          </Button>
        </>
      }
    >
      <FeatureTable>
        <FeatureGroup
          title="This machine"
          help={
            <p>
              What this host can do on its own. Detected and probed by the running process, never
              declared here.
            </p>
          }
        >
        <FeatureRow
          label={
            <span className="inline-flex flex-wrap items-center gap-2">
              <StatusDot state={health.default} available={runtime.sandbox_available} />
              <span className="font-technical">default</span>
              <Badge variant="outline">this machine</Badge>
              {local && <LevelBadge level={local.isolation} />}
            </span>
          }
          detail={
            runtime.sandbox_available
              ? localDescription(local?.name ?? null, platform)
              : `Unavailable on this ${platform} host.`
          }
          help={
            <p>
              This host&apos;s own backend. What it is and how far it isolates come from the
              running process, not from anything written here.
            </p>
          }
          control={
            <>
              <CheckButton
                state={health.default}
                disabled={!runtime.sandbox_available}
                onClick={() => void check("default")}
              />
              <Switch
                checked={draft.sandbox_enabled}
                onCheckedChange={(sandbox_enabled) => setDraft({ ...draft, sandbox_enabled })}
                aria-label="Offer the default profile"
                disabled={!runtime.sandbox_available}
              />
            </>
          }
        >
          {!runtime.sandbox_available && (
            <Alert>
              <CircleAlertIcon />
              <AlertTitle>No local backend can be built here</AlertTitle>
              <AlertDescription>
                {runtime.sandbox_unavailable_reason ?? "This host cannot run programs locally."}{" "}
                Add a container or remote profile, or fix the host.
              </AlertDescription>
            </Alert>
          )}

          {local && local.isolation === "process" && (
            <p className="flex items-start gap-2 rounded-lg bg-surface/60 px-3 py-2 text-caption text-muted-foreground">
              <ShieldIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                Process-level only: a fresh process with resource limits, a scrubbed environment
                and a temporary working directory, sharing this account&apos;s view of the
                filesystem, with the network not denied. Agents that require{" "}
                <strong>isolated</strong> are refused here, with the reason returned to them.{" "}
                {runtime.sandbox_backends
                  .filter((b) => !b.available && b.isolation === "isolated")
                  .map((b) => b.reason)
                  .filter(Boolean)
                  .join(" ")}
              </span>
            </p>
          )}

          {typeof health.default === "object" && <HealthReport report={health.default} />}

          {draft.sandbox_enabled && runtime.sandbox_available && (
            <LimitsGrid
              id="default"
              value={draft.sandbox_limits}
              allowNetwork={draft.sandbox_allow_network}
              onChange={(sandbox_limits) => setDraft({ ...draft, sandbox_limits })}
              onAllowNetwork={(sandbox_allow_network) =>
                setDraft({ ...draft, sandbox_allow_network })
              }
            />
          )}
        </FeatureRow>
        </FeatureGroup>

        <FeatureGroup
          title="Profiles you added"
          help={
            <p>
              A container image, or a service that speaks the sandbox protocol. These are
              configuration: what they can guarantee still comes from a check.
            </p>
          }
        >
        {draft.sandbox_profiles.length === 0 && (
          <FeatureRow
            label={<span className="font-normal text-muted-foreground">None yet</span>}
            detail="Add a container or remote profile above."
          />
        )}
        {draft.sandbox_profiles.map((profile, index) => (
          <FeatureRow
            key={index}
            label={
              <span className="inline-flex flex-wrap items-center gap-2">
                <StatusDot state={health[profile.name]} available />
                <span className="font-technical">{profile.name || "unnamed"}</span>
                <Badge variant="outline">
                  {profile.backend === "container" ? "container" : "remote service"}
                </Badge>
                <LevelBadge level="isolated" hint="as the backend reports it" />
              </span>
            }
            detail={
              profile.backend === "container"
                ? profile.image || "No image set"
                : profile.base_url || "No address set"
            }
            control={
              <>
                <CheckButton
                  state={health[profile.name]}
                  disabled={dirty}
                  title={dirty ? "Save first, then check" : undefined}
                  onClick={() => void check(profile.name)}
                />
                <Switch
                  checked={profile.enabled}
                  onCheckedChange={(enabled) => updateProfile(index, { enabled })}
                  aria-label={`Offer ${profile.name}`}
                />
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`Remove ${profile.name}`}
                  onClick={() =>
                    setDraft({
                      ...draft,
                      sandbox_profiles: draft.sandbox_profiles.filter((_, i) => i !== index),
                    })
                  }
                >
                  <Trash2Icon />
                </Button>
              </>
            }
          >
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <LabelWithHelp
                  htmlFor={`sb-name-${index}`}
                  label="Name"
                  help={<p>What an agent asks for when it names this profile.</p>}
                />
                <Input
                  id={`sb-name-${index}`}
                  value={profile.name}
                  spellCheck={false}
                  className="font-technical"
                  onChange={(e) => updateProfile(index, { name: e.target.value })}
                />
              </div>
              {profile.backend === "container" ? (
                <>
                  <div className="flex flex-col gap-1">
                    <LabelWithHelp
                      htmlFor={`sb-image-${index}`}
                      label="Image"
                      help={
                        <p>
                          Pulled or built ahead of time; nothing pulls it for you. Prefer a digest
                          for a reproducible deployment.
                        </p>
                      }
                    />
                    <Input
                      id={`sb-image-${index}`}
                      value={profile.image ?? ""}
                      spellCheck={false}
                      className="font-technical"
                      placeholder="python:3.12-slim"
                      onChange={(e) => updateProfile(index, { image: e.target.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Label htmlFor={`sb-runtime-${index}`}>Runtime</Label>
                    <Select
                      value={profile.runtime ?? "auto"}
                      onValueChange={(v) =>
                        updateProfile(index, {
                          runtime: v === "auto" ? null : (v as "docker" | "podman"),
                        })
                      }
                    >
                      <SelectTrigger id={`sb-runtime-${index}`} className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="auto">Detect (docker, then podman)</SelectItem>
                        <SelectItem value="docker">docker</SelectItem>
                        <SelectItem value="podman">podman</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </>
              ) : (
                <>
                  <div className="flex flex-col gap-1">
                    <LabelWithHelp
                      htmlFor={`sb-url-${index}`}
                      label="Service URL"
                      help={
                        <p>
                          A service speaking the sandbox protocol, yours or a provider&apos;s.
                          Every call to it passes the egress allowlist under Runtime.
                        </p>
                      }
                    />
                    <Input
                      id={`sb-url-${index}`}
                      value={profile.base_url ?? ""}
                      spellCheck={false}
                      className="font-technical"
                      placeholder="https://sandboxes.internal.example.com"
                      onChange={(e) => updateProfile(index, { base_url: e.target.value })}
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <LabelWithHelp
                      htmlFor={`sb-cred-${index}`}
                      label="Credential"
                      help={
                        <p>
                          The name of a secret, sent as a bearer token. The value never appears
                          here, in an agent, or in a record.
                        </p>
                      }
                    />
                    <Select
                      value={profile.credential ?? "none"}
                      onValueChange={(v) =>
                        updateProfile(index, { credential: v === "none" ? null : v })
                      }
                    >
                      <SelectTrigger id={`sb-cred-${index}`} className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">No credential</SelectItem>
                        {secrets.map((name) => (
                          <SelectItem key={name} value={name}>
                            {name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )}
            </div>

            {typeof health[profile.name] === "object" && (
              <HealthReport report={health[profile.name] as SandboxProfileHealth} />
            )}

            <LimitsGrid
              id={`p${index}`}
              value={profile.hard_limits}
              allowNetwork={profile.allow_network}
              onChange={(hard_limits) => updateProfile(index, { hard_limits })}
              onAllowNetwork={(allow_network) => updateProfile(index, { allow_network })}
            />
          </FeatureRow>
        ))}
        </FeatureGroup>
      </FeatureTable>

      <SaveRow dirty={dirty} saving={saving} onSave={() => void save()} label="Save sandbox" />
    </SettingsSectionBlock>
  );
}

/** Green once a check said ready, red once one said otherwise, grey until
 *  anybody asks. Never a guess: an unchecked profile reads as unchecked. */
function StatusDot({
  state,
  available,
}: {
  state: SandboxProfileHealth | "checking" | undefined;
  available: boolean;
}) {
  const tone = !available
    ? "bg-muted-foreground/40"
    : state === "checking"
      ? "animate-pulse bg-status-running"
      : typeof state === "object"
        ? state.ready
          ? "bg-status-done"
          : "bg-status-failed"
        : "bg-muted-foreground/50";
  return <span className={cn("size-2 shrink-0 rounded-full", tone)} aria-hidden />;
}

function localDescription(backend: string | null, platform: string): string {
  switch (backend) {
    case "bubblewrap":
      return `Linux namespaces through bubblewrap: a private root, no network, kernel-enforced limits.`;
    case "windows-job":
      return `A job object on ${platform}: the process tree is contained and capped, on this account, with the host's files visible.`;
    case "subprocess":
      return `A fresh process on ${platform} with resource limits and a scrubbed environment, on this account, with the host's files visible.`;
    default:
      return `This host's own backend.`;
  }
}

function LevelBadge({ level, hint }: { level: "isolated" | "process"; hint?: string }) {
  const Icon = level === "isolated" ? ShieldCheckIcon : ShieldIcon;
  return (
    <Badge
      variant="outline"
      className={cn("gap-1", level === "isolated" ? "text-status-done" : "text-status-waiting")}
      title={hint}
    >
      <Icon className="size-3" aria-hidden />
      {level === "isolated" ? "isolated" : "process only"}
    </Badge>
  );
}

function CheckButton({
  state,
  disabled,
  title,
  onClick,
}: {
  state: SandboxProfileHealth | "checking" | undefined;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
}) {
  const checking = state === "checking";
  return (
    <Button
      size="xs"
      variant="outline"
      disabled={disabled || checking}
      title={title}
      onClick={onClick}
      aria-live="polite"
    >
      {checking ? (
        <Loader2Icon className="animate-spin" />
      ) : typeof state === "object" ? (
        state.ready ? (
          <CheckCircle2Icon className="text-status-done" />
        ) : (
          <CircleAlertIcon className="text-status-failed" />
        )
      ) : (
        <StethoscopeIcon />
      )}
      {checking ? "Checking" : "Check"}
    </Button>
  );
}

/** What the backend said about itself, laid out so a reader can tell a
 *  promise from an observation: the ten guarantees each carry the state the
 *  backend reported, not a tick invented here. */
function HealthReport({ report }: { report: SandboxProfileHealth }) {
  const platform = PLATFORM_NAMES[report.platform] ?? report.platform;
  return (
    <div
      role="status"
      className={cn(
        "flex flex-col gap-3 rounded-lg border px-3 py-2.5",
        report.ready
          ? "border-status-done/30 bg-status-done/5"
          : "border-status-failed/30 bg-status-failed/5"
      )}
    >
      <div className="flex flex-wrap items-center gap-2 text-caption">
        {report.ready ? (
          <CheckCircle2Icon className="size-4 text-status-done" aria-hidden />
        ) : (
          <CircleAlertIcon className="size-4 text-status-failed" aria-hidden />
        )}
        <span className="font-medium">
          {report.ready ? "Ready" : report.configured ? "Not ready" : "Not configured"}
        </span>
        {report.configured && (
          <>
            <span className="text-muted-foreground">
              {report.backend} on {platform}
            </span>
            {report.isolation && <LevelBadge level={report.isolation} />}
          </>
        )}
        <span className="ml-auto text-micro text-muted-foreground">
          checked in {report.checked_in_ms} ms
        </span>
      </div>
      {report.problems.length > 0 && (
        <ul className="list-disc pl-5 text-caption text-status-failed">
          {report.problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {report.configured && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-micro sm:grid-cols-2">
          {(Object.keys(GUARANTEE_LABELS) as (keyof SandboxGuarantees)[]).map((key) => (
            <div key={key} className="flex items-center justify-between gap-2">
              <dt className="text-muted-foreground">{GUARANTEE_LABELS[key]}</dt>
              <dd
                className={cn(
                  "font-technical",
                  report.guarantees[key] === "enforced" && "text-status-done",
                  report.guarantees[key] === "unavailable" && "text-muted-foreground",
                  report.guarantees[key] === "unverified" && "text-status-waiting"
                )}
              >
                {report.guarantees[key]}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {report.mechanisms.length > 0 && (
        <p className="text-micro text-muted-foreground">
          Mechanisms: {report.mechanisms.join(", ")}.
          {report.network_grant_supported
            ? " Network can be granted."
            : " Network cannot be granted."}
          {report.artifacts_supported ? " Files are collected." : " Files are not collected."}
        </p>
      )}
      {report.notes.map((note) => (
        <p key={note} className="text-micro text-muted-foreground">
          {note}
        </p>
      ))}
    </div>
  );
}

function LimitsGrid({
  id,
  value,
  allowNetwork,
  onChange,
  onAllowNetwork,
}: {
  id: string;
  value: SandboxLimitsIn;
  allowNetwork: boolean;
  onChange: (next: SandboxLimitsIn) => void;
  onAllowNetwork: (next: boolean) => void;
}) {
  const number = (key: keyof SandboxLimitsIn, label: string, step = 1) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`sb-${id}-${key}`} className="text-caption">
        {label}
      </Label>
      <Input
        id={`sb-${id}-${key}`}
        type="number"
        className="tabular"
        min={1}
        step={step}
        value={value[key]}
        onChange={(e) => {
          const parsed = e.target.valueAsNumber;
          onChange({ ...value, [key]: Number.isFinite(parsed) ? parsed : value[key] });
        }}
      />
    </div>
  );
  return (
    <div className="flex flex-col gap-3">
      <LabelWithHelp
        label="Ceilings"
        help={
          <p>
            The most any agent on this profile may take. An agent&apos;s own request can only
            lower them.
          </p>
        }
      />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {number("cpu_seconds", "CPU seconds")}
        {number("wall_seconds", "Wall clock seconds")}
        <div className="flex flex-col gap-1">
          <Label htmlFor={`sb-${id}-memory`} className="text-caption">
            Memory (MB)
          </Label>
          <Input
            id={`sb-${id}-memory`}
            type="number"
            className="tabular"
            min={1}
            value={Math.round(value.address_space_bytes / (1024 * 1024))}
            onChange={(e) => {
              const parsed = e.target.valueAsNumber;
              if (Number.isFinite(parsed) && parsed > 0) {
                onChange({ ...value, address_space_bytes: Math.round(parsed * 1024 * 1024) });
              }
            }}
          />
          <span className="text-micro text-muted-foreground">
            {formatBytes(value.address_space_bytes)}
          </span>
        </div>
        {number("process_count", "Processes")}
      </div>
      <label className="flex items-center justify-between gap-3 text-caption">
        <span className="inline-flex items-center gap-1.5">
          <span className="text-body font-medium">Raw network</span>
        </span>
        <Switch checked={allowNetwork} onCheckedChange={onAllowNetwork} />
      </label>
      <p className="-mt-2 text-micro text-muted-foreground">
        Off keeps every fetch behind a host tool and the egress allowlist.
      </p>
    </div>
  );
}
